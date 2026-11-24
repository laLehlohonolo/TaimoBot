import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from app.history import get_account_history, get_trade_history


class TradeHistoryProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE paper_accounts (
                account_id TEXT PRIMARY KEY,
                starting_balance REAL NOT NULL,
                balance REAL NOT NULL,
                settlement_currency TEXT NOT NULL,
                created_at TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                client_order_id TEXT,
                symbol TEXT,
                side TEXT,
                quantity REAL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                status TEXT NOT NULL,
                risk_amount REAL,
                position_group_id TEXT,
                copied_from_trade_id TEXT,
                realized_pnl REAL NOT NULL DEFAULT 0,
                gross_realized_pnl REAL,
                spread_cost REAL NOT NULL DEFAULT 0,
                commission_cost REAL NOT NULL DEFAULT 0,
                swap_cost REAL NOT NULL DEFAULT 0,
                other_costs REAL NOT NULL DEFAULT 0,
                cost_provenance_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                opened_at TEXT,
                closed_at TEXT,
                created_at TEXT
            )
            """
        )
        self.connection.execute(
            "INSERT INTO paper_accounts VALUES (?, ?, ?, ?, ?)",
            ("paper-1", 10000, 10125, "ZAR", "2026-01-01"),
        )

    def tearDown(self):
        self.connection.close()

    def insert_trade(self, trade_id, status, pnl, gross, provenance, costs=(0, 0, 0, 0)):
        self.connection.execute(
            """
            INSERT INTO trades (
                trade_id, account_id, status, realized_pnl, gross_realized_pnl,
                cost_provenance_status, spread_cost, commission_cost, swap_cost,
                other_costs, created_at
            ) VALUES (?, 'paper-1', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (trade_id, status, pnl, gross, provenance, *costs, trade_id),
        )
        self.connection.commit()

    def history_connection(self):
        @contextmanager
        def connection_context():
            yield self.connection
        return connection_context()

    def test_unknown_and_manual_costs_are_never_reported_as_verified_net(self):
        self.insert_trade("unknown", "CLOSED", 100, 100, "UNKNOWN")
        self.insert_trade(
            "manual", "CLOSED", 25, 50, "MANUAL_UNVERIFIED",
            costs=(5, 10, 5, 5),
        )
        with patch("app.history.get_connection", side_effect=self.history_connection):
            account = get_account_history("paper-1")
            trades = get_trade_history("paper-1")["trades"]

        self.assertEqual(account["gross_realized_pnl"], 150)
        self.assertEqual(account["recorded_costs_total"], 25)
        self.assertEqual(account["net_pnl_status"], "UNVERIFIED_COSTS_PRESENT")
        self.assertIsNone(account["net_realized_pnl"])
        self.assertEqual(account["cost_provenance_counts"]["UNKNOWN"], 1)
        self.assertEqual(account["cost_provenance_counts"]["MANUAL_UNVERIFIED"], 1)
        by_id = {trade["trade_id"]: trade for trade in trades}
        self.assertIsNone(by_id["unknown"]["net_realized_pnl"])
        self.assertEqual(by_id["unknown"]["net_pnl_status"], "UNVERIFIED_COSTS")
        self.assertIsNone(by_id["manual"]["net_realized_pnl"])
        self.assertEqual(by_id["manual"]["cost_provenance_status"], "MANUAL_UNVERIFIED")

    def test_net_pnl_is_exposed_only_when_all_closed_costs_are_actual(self):
        self.insert_trade(
            "actual", "CLOSED", 42, 50, "ACTUAL", costs=(2, 3, 1, 2)
        )
        with patch("app.history.get_connection", side_effect=self.history_connection):
            account = get_account_history("paper-1")
            trade = get_trade_history("paper-1")["trades"][0]

        self.assertEqual(account["net_pnl_status"], "VERIFIED_ACTUAL_COSTS")
        self.assertEqual(account["net_realized_pnl"], 42)
        self.assertEqual(trade["net_realized_pnl"], 42)
        self.assertEqual(trade["net_pnl_status"], "VERIFIED_ACTUAL_COSTS")

    def test_open_trade_does_not_count_as_closed_or_verified_net_pnl(self):
        self.insert_trade("open", "OPEN", 0, None, "UNKNOWN")
        with patch("app.history.get_connection", side_effect=self.history_connection):
            account = get_account_history("paper-1")
            trade = get_trade_history("paper-1")["trades"][0]

        self.assertEqual(account["closed_trades"], 0)
        self.assertEqual(account["net_pnl_status"], "NO_CLOSED_TRADES")
        self.assertIsNone(account["net_realized_pnl"])
        self.assertEqual(trade["net_pnl_status"], "NOT_CLOSED")


if __name__ == "__main__":
    unittest.main()
