import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.auth import register_user, create_session
from app.execution import create_paper_account, open_paper_trade
from app.main import app
from app.copy_trading import replicate_open_trade


class CopyTradeClosingTests(unittest.TestCase):

    def setUp(self):
        # Keep ordinary trading tests independent of the wall clock.
        # The maintenance-window block itself is tested with an explicit clock override.
        self.maintenance_clock = patch("app.safety.is_maintenance_window", return_value=False)
        self.maintenance_clock.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "copy_closing.db"
        init_db()
        user = register_user("copy-closing-tests@taimobot.local", "TestPassphrase-2026-Secure!")
        self.user_id = user["user_id"]
        session = create_session(self.user_id)
        create_paper_account("SOURCE", 10000, owner_user_id=self.user_id)
        create_paper_account("FOLLOWER", 5000, owner_user_id=self.user_id)
        self.client = TestClient(app)
        self.client.headers.update({"Authorization": "Bearer " + session["access_token"]})
        self.client.post(
            "/accounts/FOLLOWER/copy-relationships",
            json={"source_account_id": "SOURCE"}
        )

    def tearDown(self):
        self.maintenance_clock.stop()
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_copied_trade(self):
        source = open_paper_trade(
            account_id="SOURCE",
            client_order_id="SOURCE-CLOSE-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD"
        )
        summary = replicate_open_trade(source["trade_id"])
        self.assertEqual(summary["successful_copies"], 1)
        copied_id = summary["copies"][0]["copied_trade_id"]
        return source, copied_id

    def account_balance(self, account_id):
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = ?",
                (account_id,)
            ).fetchone()
        return float(row["balance"])

    def trade_status(self, trade_id):
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (trade_id,)
            ).fetchone()
        return row["status"]

    def test_closing_source_closes_copies_and_updates_each_balance(self):
        source, copied_id = self.create_copied_trade()

        response = self.client.post(
            f"/trades/{source['trade_id']}/close",
            json={"exit_price": 4010}
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["closed_copies"], 1)
        self.assertEqual(self.trade_status(source["trade_id"]), "CLOSED")
        self.assertEqual(self.trade_status(copied_id), "CLOSED")
        self.assertEqual(self.account_balance("SOURCE"), 10050)
        self.assertEqual(self.account_balance("FOLLOWER"), 5025)

        with database.get_connection() as connection:
            execution = connection.execute(
                "SELECT status FROM copy_executions WHERE copied_trade_id = ?",
                (copied_id,)
            ).fetchone()
        self.assertEqual(execution["status"], "CLOSED")

    def test_source_close_assigns_costs_once_and_never_duplicates_them_to_copy(self):
        source, copied_id = self.create_copied_trade()

        response = self.client.post(
            f"/trades/{source['trade_id']}/close",
            json={
                "exit_price": 4010,
                "spread_cost": 0.12,
                "commission_cost": 0.23,
                "swap_cost": 0.34,
                "other_costs": 0.45,
            },
        )

        self.assertEqual(response.status_code, 200)
        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, realized_pnl, gross_realized_pnl,
                       spread_cost, commission_cost, swap_cost, other_costs
                FROM trades
                WHERE trade_id IN (?, ?)
                ORDER BY trade_id
                """,
                (source["trade_id"], copied_id),
            ).fetchall()
        by_id = {row["trade_id"]: row for row in rows}
        source_row = by_id[source["trade_id"]]
        copy_row = by_id[copied_id]

        self.assertAlmostEqual(source_row["spread_cost"], 0.12)
        self.assertAlmostEqual(source_row["commission_cost"], 0.23)
        self.assertAlmostEqual(source_row["swap_cost"], 0.34)
        self.assertAlmostEqual(source_row["other_costs"], 0.45)
        self.assertEqual(
            (
                copy_row["spread_cost"],
                copy_row["commission_cost"],
                copy_row["swap_cost"],
                copy_row["other_costs"],
            ),
            (0, 0, 0, 0),
        )
        self.assertAlmostEqual(
            source_row["realized_pnl"],
            source_row["gross_realized_pnl"] - 1.14,
        )
        self.assertAlmostEqual(
            copy_row["realized_pnl"],
            copy_row["gross_realized_pnl"],
        )
        self.assertAlmostEqual(
            self.account_balance("SOURCE"),
            10000 + source_row["realized_pnl"],
        )
        self.assertAlmostEqual(
            self.account_balance("FOLLOWER"),
            5000 + copy_row["realized_pnl"],
        )


    def test_manually_closing_copy_does_not_close_source(self):
        source, copied_id = self.create_copied_trade()

        response = self.client.post(
            f"/trades/{copied_id}/close",
            json={"exit_price": 4010}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.trade_status(source["trade_id"]), "OPEN")
        self.assertEqual(self.trade_status(copied_id), "CLOSED")
        self.assertEqual(self.account_balance("SOURCE"), 10000)
        self.assertEqual(self.account_balance("FOLLOWER"), 5025)

        with database.get_connection() as connection:
            execution = connection.execute(
                "SELECT status FROM copy_executions WHERE copied_trade_id = ?",
                (copied_id,)
            ).fetchone()
        self.assertEqual(execution["status"], "CLOSED")

    def test_repeated_source_close_cannot_apply_profit_twice(self):
        source, copied_id = self.create_copied_trade()

        first = self.client.post(
            f"/trades/{source['trade_id']}/close",
            json={"exit_price": 4010}
        )
        second = self.client.post(
            f"/trades/{source['trade_id']}/close",
            json={"exit_price": 4010}
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(self.account_balance("SOURCE"), 10050)
        self.assertEqual(self.account_balance("FOLLOWER"), 5025)

    def test_source_and_copies_remain_open_if_any_balance_would_go_negative(self):
        source, copied_id = self.create_copied_trade()

        with database.get_connection() as connection:
            connection.execute(
                "UPDATE paper_accounts SET balance = 1 WHERE account_id = 'FOLLOWER'"
            )

        response = self.client.post(
            f"/trades/{source['trade_id']}/close",
            json={"exit_price": 3000}
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.trade_status(source["trade_id"]), "OPEN")
        self.assertEqual(self.trade_status(copied_id), "OPEN")
        self.assertEqual(self.account_balance("SOURCE"), 10000)
        self.assertEqual(self.account_balance("FOLLOWER"), 1)


if __name__ == "__main__":
    unittest.main()
