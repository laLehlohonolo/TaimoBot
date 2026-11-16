
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.auth import register_user, create_session
from app.main import app
from app.execution import (
    create_paper_account,
    open_paper_trade,
    close_paper_trade,
    validate_symbol
)
from app.risk import (
    calculate_position_size,
    get_risk_profile,
    RiskError
)
from app.safety import (
    activate_emergency_stop,
    deactivate_emergency_stop
)


class PaperTradingTests(unittest.TestCase):

    def setUp(self):
        # Keep ordinary trading tests independent of the wall clock.
        # The maintenance-window block itself is tested with an explicit clock override.
        self.maintenance_clock = patch("app.safety.is_maintenance_window", return_value=False)
        self.maintenance_clock.start()
        self.admin_env = patch.dict(
            "os.environ",
            {"TAIMOBOT_ADMIN_EMAILS": "paper-tests@taimobot.local"}
        )
        self.admin_env.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"

        init_db()
        deactivate_emergency_stop()
        user = register_user("paper-tests@taimobot.local", "TestPassphrase-2026-Secure!")
        self.user_id = user["user_id"]
        session = create_session(self.user_id)
        create_paper_account("TEST-001", 10000, owner_user_id=self.user_id)

        self.client = TestClient(app)
        self.client.headers.update({"Authorization": "Bearer " + session["access_token"]})

    def tearDown(self):
        self.maintenance_clock.stop()
        self.client.close()
        self.admin_env.stop()
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def open_trade(self, order_id, risk_percent=0.5, symbol="XAUUSD"):
        return open_paper_trade(
            account_id="TEST-001",
            client_order_id=order_id,
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=risk_percent,
            symbol=symbol
        )

    def test_one_percent_risk_calculation(self):
        result = calculate_position_size(
            account_balance=10000,
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1,
            side="BUY"
        )

        self.assertEqual(result["risk_amount"], 100)
        self.assertEqual(result["position_size"], 10)
        self.assertEqual(
            result["projected_total_open_risk"], 100
        )

    def test_risk_above_one_percent_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=3990,
                risk_percent=1.1,
                side="BUY"
            )

    def test_funding_profile_boundaries(self):
        cases = [
            (4999.99, "STARTER", 0.25),
            (5000, "STANDARD", 0.50),
            (24999.99, "STANDARD", 0.50),
            (25000, "PREMIUM", 1.00)
        ]

        for funding, expected_profile, expected_risk in cases:
            with self.subTest(funding=funding):
                result = get_risk_profile(funding)
                self.assertEqual(
                    result["profile"], expected_profile
                )
                self.assertEqual(
                    result["risk_percent"], expected_risk
                )

    def test_standard_profile_uses_half_percent(self):
        result = self.open_trade("ORDER-STANDARD")
        self.assertEqual(result["risk_profile"], "STANDARD")
        self.assertEqual(result["risk_percent"], 0.50)
        self.assertEqual(result["risk_amount"], 50)

    def test_submitted_risk_cannot_increase_standard_profile(self):
        result = self.open_trade(
            "ORDER-OVERRIDE-HIGH",
            risk_percent=1.0
        )

        self.assertEqual(result["risk_percent"], 0.50)
        self.assertEqual(result["risk_amount"], 50)

    def test_submitted_risk_cannot_reduce_standard_profile(self):
        result = self.open_trade(
            "ORDER-OVERRIDE-LOW",
            risk_percent=0.01
        )

        self.assertEqual(result["risk_percent"], 0.50)
        self.assertEqual(result["risk_amount"], 50)

    def test_starter_profile_uses_quarter_percent(self):
        create_paper_account("STARTER-001", 4000)

        result = open_paper_trade(
            account_id="STARTER-001",
            client_order_id="STARTER-ORDER-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1.0,
            symbol="EURUSD"
        )

        self.assertEqual(result["risk_profile"], "STARTER")
        self.assertEqual(result["risk_percent"], 0.25)
        self.assertEqual(result["risk_amount"], 10)

    def test_premium_profile_uses_one_percent(self):
        create_paper_account("PREMIUM-001", 30000)

        result = open_paper_trade(
            account_id="PREMIUM-001",
            client_order_id="PREMIUM-ORDER-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.25,
            symbol="XAUUSD"
        )

        self.assertEqual(result["risk_profile"], "PREMIUM")
        self.assertEqual(result["risk_percent"], 1.0)
        self.assertEqual(result["risk_amount"], 300)

    def test_two_positions_can_share_one_percent_budget(self):
        first = self.open_trade("ORDER-001", 0.5)
        second = self.open_trade("ORDER-002", 0.5)

        self.assertEqual(first["risk_amount"], 50)
        self.assertEqual(second["risk_amount"], 50)
        self.assertEqual(second["total_open_risk"], 100)
        self.assertEqual(second["remaining_risk"], 0)

    def test_third_position_is_rejected_when_budget_is_full(self):
        self.open_trade("ORDER-001", 0.5)
        self.open_trade("ORDER-002", 0.5)

        with self.assertRaises(RiskError):
            self.open_trade("ORDER-003", 0.1)

    def test_starter_can_open_four_positions_within_limit(self):
        create_paper_account("STARTER-002", 4000)

        results = []

        for number in range(1, 5):
            results.append(
                open_paper_trade(
                    account_id="STARTER-002",
                    client_order_id=f"STARTER-{number}",
                    side="BUY",
                    entry_price=4000,
                    stop_loss=3990,
                    risk_percent=1.0,
                    symbol="XAUUSD"
                )
            )

        self.assertEqual(
            results[-1]["total_open_risk"], 40
        )
        self.assertEqual(
            results[-1]["maximum_risk_percent"], 1.0
        )

    def test_duplicate_order_is_rejected(self):
        self.open_trade("ORDER-DUPLICATE")

        with self.assertRaises(ValueError):
            self.open_trade("ORDER-DUPLICATE")

    def test_emergency_stop_blocks_new_orders(self):
        activate_emergency_stop()

        with self.assertRaises(RuntimeError):
            self.open_trade("ORDER-STOPPED")

    def test_invalid_buy_stop_loss_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=4010,
                risk_percent=0.5,
                side="BUY"
            )

    def test_closing_trade_releases_risk_budget(self):
        first = self.open_trade("ORDER-001")
        self.open_trade("ORDER-002")

        close_result = close_paper_trade(
            trade_id=first["trade_id"],
            exit_price=4000
        )

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(close_result["realized_pnl"], 0)
        self.assertEqual(close_result["remaining_open_risk"], 50)

        third = self.open_trade("ORDER-003")
        self.assertEqual(third["total_open_risk"], 100)

    def test_trade_closing_updates_account_balance(self):
        trade = self.open_trade("ORDER-CLOSE", 1.0)

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["realized_pnl"], 50)
        self.assertEqual(result["new_balance"], 10050)
        self.assertEqual(result["remaining_open_risk"], 0)

    def test_open_risk_is_account_specific(self):
        self.open_trade("ORDER-001")
        create_paper_account("TEST-002", 10000)

        second_account_trade = open_paper_trade(
            account_id="TEST-002",
            client_order_id="ORDER-ACCOUNT-2",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1.0,
            symbol="EURUSD"
        )

        self.assertEqual(
            second_account_trade["total_open_risk"], 50
        )

    def test_account_risk_endpoint_reports_open_risk(self):
        self.open_trade("ORDER-API-001")
        self.open_trade("ORDER-API-002")

        response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["account_id"], "TEST-001")
        self.assertEqual(result["account_balance"], 10000)
        self.assertEqual(result["open_positions"], 2)
        self.assertEqual(result["current_open_risk"], 100)
        self.assertEqual(result["maximum_risk_percent"], 1.0)
        self.assertEqual(result["maximum_risk_amount"], 100)
        self.assertEqual(result["remaining_risk_budget"], 0)
        self.assertTrue(result["risk_limit_reached"])

    def test_account_risk_endpoint_reports_unused_budget(self):
        self.open_trade("ORDER-API-003")

        response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["open_positions"], 1)
        self.assertEqual(result["current_open_risk"], 50)
        self.assertEqual(result["remaining_risk_budget"], 50)
        self.assertFalse(result["risk_limit_reached"])

    def test_account_risk_endpoint_rejects_unknown_account(self):
        response = self.client.get("/accounts/UNKNOWN/risk")
        self.assertEqual(response.status_code, 404)

    def test_symbol_is_normalized_to_uppercase(self):
        self.assertEqual(validate_symbol("eurusd"), "EURUSD")

    def test_symbol_with_supported_separator_is_valid(self):
        self.assertEqual(validate_symbol("EUR/USD"), "EUR/USD")

    def test_empty_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_symbol("")

    def test_invalid_symbol_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_symbol("EUR USD!")

    def test_symbol_length_is_limited(self):
        with self.assertRaises(ValueError):
            validate_symbol("A" * 31)

    def test_different_instruments_can_open_separate_positions(self):
        eur = self.open_trade("ORDER-EUR", 0.5, "EURUSD")
        gold = self.open_trade("ORDER-GOLD", 0.5, "XAUUSD")

        self.assertEqual(eur["symbol"], "EURUSD")
        self.assertEqual(gold["symbol"], "XAUUSD")
        self.assertNotEqual(eur["trade_id"], gold["trade_id"])
        self.assertEqual(gold["total_open_risk"], 100)

    def test_different_instruments_share_one_risk_limit(self):
        self.open_trade("ORDER-EUR", 0.5, "EURUSD")
        self.open_trade("ORDER-GOLD", 0.5, "XAUUSD")

        with self.assertRaises(RiskError):
            self.open_trade("ORDER-BTC", 0.1, "BTCUSD")

    def test_trade_api_returns_consistent_paper_order_contract(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-CONTRACT-001",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["trade_id"])
        self.assertEqual(result["client_order_id"], "API-CONTRACT-001")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["mode"], "paper_trading")
        self.assertEqual(result["risk_amount"], 50)
        self.assertEqual(result["copy_summary"]["eligible_followers"], 0)
        self.assertEqual(result["copy_summary"]["successful_copies"], 0)
        self.assertEqual(result["copy_summary"]["failed_copies"], 0)

    def test_trade_api_duplicate_order_returns_client_error_without_second_trade(self):
        payload = {
            "account_id": "TEST-001",
            "client_order_id": "API-DUPLICATE-001",
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_price": 4000,
            "stop_loss": 3990,
            "risk_percent": 0.5
        }

        first = self.client.post("/trades", json=payload)
        second = self.client.post("/trades", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertIn("Duplicate order ID", second.json()["detail"])

        with database.get_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'API-DUPLICATE-001'
                """
            ).fetchone()["total"]

        self.assertEqual(count, 1)

    def test_emergency_stop_api_blocks_new_orders_but_allows_existing_close(self):
        existing = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-BEFORE-STOP",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )
        self.assertEqual(existing.status_code, 200)

        stop_response = self.client.post("/safety/emergency-stop")
        self.assertEqual(stop_response.status_code, 200)
        self.assertTrue(self.client.get("/safety/status").json()["emergency_stop"])

        rejected = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-DURING-STOP",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )
        self.assertEqual(rejected.status_code, 400)

        close_response = self.client.post(
            f"/trades/{existing.json()['trade_id']}/close",
            json={"exit_price": 4010}
        )
        self.assertEqual(close_response.status_code, 200)
        self.assertEqual(close_response.json()["status"], "CLOSED")

        resumed = self.client.post("/safety/resume")
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(self.client.get("/safety/status").json()["emergency_stop"])

    def test_api_accepts_a_valid_instrument_symbol(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-EUR-001",
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["symbol"], "EURUSD")
        self.assertEqual(response.json()["risk_percent"], 0.5)
        self.assertEqual(response.json()["risk_amount"], 50)

    def test_api_cannot_override_assigned_risk_profile(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-RISK-OVERRIDE",
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 1.0
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_percent"], 0.5)
        self.assertEqual(response.json()["risk_amount"], 50)

    def test_api_rejects_invalid_symbol_format(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-BAD-001",
                "symbol": "EUR USD!",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )

        self.assertEqual(response.status_code, 422)

    def test_closing_trade_returns_its_instrument_symbol(self):
        trade = self.open_trade(
            "ORDER-CLOSE-SYMBOL", 0.5, "GBPUSD"
        )

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4000
        )

        self.assertEqual(result["symbol"], "GBPUSD")
        self.assertEqual(result["status"], "CLOSED")

    def test_legacy_database_migration_preserves_existing_trades(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        database.DB_PATH = legacy_path

        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    starting_balance REAL NOT NULL,
                    balance REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE trades (
                    trade_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    risk_amount REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    opened_at TEXT,
                    closed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id)
                        REFERENCES paper_accounts(account_id)
                );

                INSERT INTO paper_accounts (
                    account_id, starting_balance, balance
                ) VALUES ('LEGACY-001', 10000, 10000);

                INSERT INTO trades (
                    trade_id, account_id, client_order_id,
                    symbol, side, quantity, entry_price,
                    stop_loss, status, risk_amount
                ) VALUES (
                    'OLD-TRADE-001', 'LEGACY-001', 'OLD-ORDER-001',
                    'XAUUSD', 'BUY', 10, 4000,
                    3990, 'OPEN', 100
                );
                """
            )

        init_db()
        init_db()

        with database.get_connection() as connection:
            trade = connection.execute(
                """
                SELECT trade_id, account_id, symbol, status,
                       position_group_id, copied_from_trade_id,
                       copy_group_id
                FROM trades
                WHERE trade_id = ?
                """,
                ("OLD-TRADE-001",)
            ).fetchone()

            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(trades)"
                ).fetchall()
            }

            group_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'position_groups'
                """
            ).fetchone()

            migrated_account = connection.execute(
                """
                SELECT risk_profile
                FROM paper_accounts
                WHERE account_id = 'LEGACY-001'
                """
            ).fetchone()

        self.assertIsNotNone(trade)
        self.assertEqual(trade["account_id"], "LEGACY-001")
        self.assertEqual(trade["symbol"], "XAUUSD")
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNone(trade["position_group_id"])
        self.assertIsNone(trade["copied_from_trade_id"])
        self.assertIsNone(trade["copy_group_id"])

        self.assertIn("position_group_id", columns)
        self.assertIn("copied_from_trade_id", columns)
        self.assertIn("copy_group_id", columns)
        self.assertIsNotNone(group_table)
        self.assertEqual(migrated_account["risk_profile"], "STANDARD")


    def test_invalid_sell_stop_loss_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=3990,
                risk_percent=0.5,
                side="SELL"
            )

    def test_stop_loss_equal_to_entry_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=4000,
                risk_percent=0.5,
                side="BUY"
            )

    def test_non_finite_prices_are_rejected(self):
        for bad_value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad_value):
                with self.assertRaises(RiskError):
                    calculate_position_size(
                        account_balance=10000,
                        entry_price=bad_value,
                        stop_loss=3990,
                        risk_percent=0.5,
                        side="BUY"
                    )

    def test_rejected_stop_loss_does_not_create_trade(self):
        with self.assertRaises(RiskError):
            open_paper_trade(
                account_id="TEST-001",
                client_order_id="ORDER-INVALID-STOP",
                side="BUY",
                entry_price=4000,
                stop_loss=4000,
                risk_percent=0.5,
                symbol="XAUUSD"
            )

        with database.get_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'ORDER-INVALID-STOP'
                """
            ).fetchone()["total"]

        self.assertEqual(count, 0)


    def test_maintenance_blocks_service_and_api_entries_but_allows_close(self):
        existing_trade = self.open_trade("ORDER-BEFORE-MAINTENANCE")

        with patch("app.safety.is_maintenance_window", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "maintenance window"):
                self.open_trade("ORDER-DURING-MAINTENANCE")

            response = self.client.post(
                "/trades",
                json={
                    "account_id": "TEST-001",
                    "client_order_id": "API-DURING-MAINTENANCE",
                    "symbol": "XAUUSD",
                    "side": "BUY",
                    "entry_price": 4000,
                    "stop_loss": 3990,
                    "risk_percent": 0.5
                }
            )
            self.assertEqual(response.status_code, 400)

            # Maintenance blocks entries, never the ability to reduce risk.
            close_result = close_paper_trade(
                trade_id=existing_trade["trade_id"],
                exit_price=4010
            )
            self.assertEqual(close_result["status"], "CLOSED")

        with database.get_connection() as connection:
            rejected_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id IN (
                    'ORDER-DURING-MAINTENANCE',
                    'API-DURING-MAINTENANCE'
                )
                """
            ).fetchone()["total"]

        self.assertEqual(rejected_count, 0)

    def test_emergency_stop_blocks_new_orders_but_allows_safe_close(self):
        existing_trade = self.open_trade("ORDER-BEFORE-EMERGENCY-STOP")

        activate_emergency_stop()

        with self.assertRaises(RuntimeError):
            self.open_trade("ORDER-DURING-EMERGENCY-STOP")

        close_result = close_paper_trade(
            trade_id=existing_trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(close_result["realized_pnl"], 50)
        self.assertEqual(close_result["new_balance"], 10050)

        with database.get_connection() as connection:
            account = connection.execute(
                """
                SELECT starting_balance, balance
                FROM paper_accounts
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()
            rejected_order_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'ORDER-DURING-EMERGENCY-STOP'
                """
            ).fetchone()["total"]

        self.assertEqual(account["balance"], 10050)
        self.assertEqual(rejected_order_count, 0)

    def test_account_balance_reconciles_with_all_realized_pnl(self):
        first = self.open_trade("ORDER-RECONCILE-001")
        second = self.open_trade("ORDER-RECONCILE-002")

        close_paper_trade(first["trade_id"], 3990)
        close_paper_trade(second["trade_id"], 4010)

        with database.get_connection() as connection:
            account = connection.execute(
                """
                SELECT starting_balance, balance
                FROM paper_accounts
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()
            totals = connection.execute(
                """
                SELECT
                    COALESCE(SUM(realized_pnl), 0) AS realized_total,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)
                        AS open_positions
                FROM trades
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()

        expected_balance = (
            account["starting_balance"] + totals["realized_total"]
        )
        self.assertEqual(account["balance"], expected_balance)
        self.assertEqual(totals["realized_total"], 0)
        self.assertEqual(totals["open_positions"], 0)


    def test_open_trade_and_account_persist_after_database_reinitialization(self):
        trade = self.open_trade("ORDER-PERSIST-OPEN")

        # Simulate application startup against the same persistent database.
        init_db()

        account_response = self.client.get("/accounts/TEST-001")
        trades_response = self.client.get("/accounts/TEST-001/trades")
        risk_response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(trades_response.status_code, 200)
        self.assertEqual(risk_response.status_code, 200)

        account = account_response.json()
        trades = trades_response.json()["trades"]

        self.assertEqual(account["current_balance"], 10000)
        self.assertEqual(account["open_trades"], 1)
        self.assertEqual(account["realized_pnl"], 0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["trade_id"], trade["trade_id"])
        self.assertEqual(trades[0]["status"], "OPEN")
        self.assertEqual(risk_response.json()["current_open_risk"], 50)

    def test_closed_trade_and_realized_balance_persist_after_reinitialization(self):
        trade = self.open_trade("ORDER-PERSIST-CLOSED")
        close_result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(close_result["new_balance"], 10050)

        # Re-running initialization must not reset or duplicate financial data.
        init_db()
        init_db()

        account_response = self.client.get("/accounts/TEST-001")
        trades_response = self.client.get("/accounts/TEST-001/trades")

        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(trades_response.status_code, 200)

        account = account_response.json()
        trades = trades_response.json()["trades"]

        self.assertEqual(account["starting_balance"], 10000)
        self.assertEqual(account["current_balance"], 10050)
        self.assertEqual(account["closed_trades"], 1)
        self.assertEqual(account["realized_pnl"], 50)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "CLOSED")
        self.assertEqual(trades[0]["realized_pnl"], 50)


    def test_concurrent_duplicate_order_requests_create_only_one_trade(self):
        start = Barrier(2)

        def submit_duplicate():
            start.wait()
            try:
                result = self.open_trade("ORDER-CONCURRENT-DUPLICATE")
                return ("created", result["trade_id"])
            except ValueError:
                return ("rejected", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(
                lambda _: submit_duplicate(), range(2)
            ))

        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "created"), 1
        )
        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "rejected"), 1
        )

        with database.get_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'ORDER-CONCURRENT-DUPLICATE'
                """
            ).fetchone()["total"]

        self.assertEqual(count, 1)

    def test_concurrent_orders_cannot_exceed_combined_one_percent_risk(self):
        # A PREMIUM account risks 1% per order, so two simultaneous
        # requests must not both consume the same 1% account allowance.
        create_paper_account("CONCURRENT-PREMIUM", 30000)
        start = Barrier(2)

        def submit_order(order_id):
            start.wait()
            try:
                result = open_paper_trade(
                    account_id="CONCURRENT-PREMIUM",
                    client_order_id=order_id,
                    side="BUY",
                    entry_price=4000,
                    stop_loss=3990,
                    risk_percent=0.5,
                    symbol="XAUUSD"
                )
                return ("created", result["risk_amount"])
            except RiskError:
                return ("risk_rejected", None)

        order_ids = (
            "ORDER-CONCURRENT-RISK-A",
            "ORDER-CONCURRENT-RISK-B"
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit_order, order_ids))

        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "created"), 1
        )
        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "risk_rejected"), 1
        )

        with database.get_connection() as connection:
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS open_positions,
                    COALESCE(SUM(risk_amount), 0) AS total_open_risk
                FROM trades
                WHERE account_id = 'CONCURRENT-PREMIUM'
                  AND status = 'OPEN'
                """
            ).fetchone()

        self.assertEqual(totals["open_positions"], 1)
        self.assertEqual(totals["total_open_risk"], 300)
        self.assertLessEqual(totals["total_open_risk"], 300)


    def test_profit_preview_api_is_read_only_and_accounts_for_costs(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW")

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={
                "current_price": 4001,
                "spread_cost": 0.2,
                "commission_cost": 0.1,
                "swap_cost": 0.1,
                "other_costs": 0.1,
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertEqual(preview["trade_id"], trade["trade_id"])
        self.assertEqual(preview["symbol"], "XAUUSD")
        self.assertAlmostEqual(preview["gross_pnl"], trade["quantity"])
        self.assertAlmostEqual(preview["total_costs"], 0.5)
        self.assertAlmostEqual(
            preview["net_pnl"], trade["quantity"] - 0.5
        )
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["execution_triggered"])

        with database.get_connection() as connection:
            stored_trade = connection.execute(
                """
                SELECT status, realized_pnl
                FROM trades
                WHERE trade_id = ?
                """,
                (trade["trade_id"],),
            ).fetchone()
            account = connection.execute(
                """
                SELECT balance
                FROM paper_accounts
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()

        self.assertEqual(stored_trade["status"], "OPEN")
        self.assertEqual(stored_trade["realized_pnl"], 0)
        self.assertEqual(account["balance"], 10000)

    def test_profit_preview_rejects_closed_trade(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-CLOSED")
        close_paper_trade(trade["trade_id"], 4010)

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={"current_price": 4011},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("only for open trades", response.json()["detail"])

    def test_profit_preview_validates_positive_current_price(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-INVALID")

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={"current_price": 0},
        )

        self.assertEqual(response.status_code, 422)

    def test_profit_preview_does_not_mark_small_gross_profit_as_net_profit(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-COSTS")

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={
                "current_price": 4000.01,
                "spread_cost": 1,
                "commission_cost": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertLess(preview["net_pnl"], 0)
        self.assertFalse(preview["should_take_profit"])


    def test_group_profit_preview_aggregates_positions_and_group_costs_once(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        self.assertEqual(group_response.status_code, 200)
        group_id = group_response.json()["group_id"]

        first = open_paper_trade(
            account_id="TEST-001",
            client_order_id="GROUP-PREVIEW-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
            position_group_id=group_id,
        )
        second = open_paper_trade(
            account_id="TEST-001",
            client_order_id="GROUP-PREVIEW-002",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
            position_group_id=group_id,
        )

        response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={
                "current_price": 4001,
                "spread_cost": 0.2,
                "commission_cost": 0.1,
                "swap_cost": 0.1,
                "other_costs": 0.1,
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        expected_gross = first["quantity"] + second["quantity"]
        self.assertEqual(preview["group_id"], group_id)
        self.assertEqual(
            set(preview["trade_ids"]),
            {first["trade_id"], second["trade_id"]},
        )
        self.assertEqual(preview["position_count"], 2)
        self.assertAlmostEqual(preview["gross_pnl"], expected_gross)
        self.assertAlmostEqual(preview["total_costs"], 0.5)
        self.assertAlmostEqual(preview["net_pnl"], expected_gross - 0.5)
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["execution_triggered"])
        self.assertEqual(
            preview["cost_allocation"],
            "proportional_to_position_quantity",
        )

        with database.get_connection() as connection:
            states = connection.execute(
                """
                SELECT status, realized_pnl
                FROM trades
                WHERE position_group_id = ?
                """,
                (group_id,),
            ).fetchall()
            balance = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        self.assertEqual([row["status"] for row in states], ["OPEN", "OPEN"])
        self.assertEqual(sum(row["realized_pnl"] for row in states), 0)
        self.assertEqual(balance, 10000)

    def test_group_profit_preview_rejects_group_without_open_positions(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        group_id = group_response.json()["group_id"]

        response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={"current_price": 4001},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("open positions", response.json()["detail"])


    def test_group_profit_preview_rejects_closed_group_even_if_open_leg_remains(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        group_id = group_response.json()["group_id"]
        self.open_trade("GROUP-PREVIEW-CLOSED-LEG")

        # Simulate an inconsistent persisted state: a closed group with an
        # orphaned open leg. The preview must trust the group lifecycle state.
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE position_groups SET status = 'CLOSED' WHERE group_id = ?",
                (group_id,),
            )
            connection.execute(
                "UPDATE trades SET position_group_id = ? WHERE client_order_id = ?",
                (group_id, "GROUP-PREVIEW-CLOSED-LEG"),
            )

        response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={"current_price": 4001},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("open position groups", response.json()["detail"])

    def test_group_profit_preview_requires_authentication(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        group_id = group_response.json()["group_id"]
        unauthenticated = TestClient(app)

        try:
            response = unauthenticated.post(
                f"/groups/{group_id}/profit-preview",
                json={"current_price": 4001},
            )
        finally:
            unauthenticated.close()

        self.assertEqual(response.status_code, 401)

if __name__ == "__main__":
    unittest.main()
