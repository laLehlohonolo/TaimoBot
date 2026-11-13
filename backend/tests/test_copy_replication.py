import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.execution import create_paper_account, open_paper_trade
from app.main import app
from app.copy_trading import replicate_open_trade
from app.safety import deactivate_emergency_stop


class CopyTradeReplicationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "copy.db"
        deactivate_emergency_stop()
        init_db()
        create_paper_account("SOURCE", 10000)
        create_paper_account("FOLLOWER", 5000)
        self.client = TestClient(app)

        self.client.post(
            "/accounts/FOLLOWER/copy-relationships",
            json={"source_account_id": "SOURCE"}
        )

    def tearDown(self):
        self.client.close()
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_source_trade(self):
        return open_paper_trade(
            account_id="SOURCE",
            client_order_id="SOURCE-ORDER-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1,
            symbol="XAUUSD"
        )

    def test_api_trade_creation_triggers_copy(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "SOURCE",
                "client_order_id": "API-SOURCE-001",
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 1.1,
                "stop_loss": 1.09,
                "risk_percent": 0.5
            }
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["copy_summary"]["successful_copies"], 1)

    def test_follower_position_size_is_calculated_from_own_funding(self):
        source = self.create_source_trade()
        result = replicate_open_trade(source["trade_id"])
        self.assertEqual(result["successful_copies"], 1)

        with database.get_connection() as connection:
            copied = connection.execute(
                """
                SELECT quantity, risk_amount, copied_from_trade_id
                FROM trades
                WHERE account_id = 'FOLLOWER'
                """
            ).fetchone()

        self.assertEqual(copied["quantity"], 2.5)
        self.assertEqual(copied["risk_amount"], 25)
        self.assertEqual(copied["copied_from_trade_id"], source["trade_id"])

    def test_repeated_replication_does_not_create_duplicate(self):
        source = self.create_source_trade()
        first = replicate_open_trade(source["trade_id"])
        second = replicate_open_trade(source["trade_id"])

        self.assertEqual(first["successful_copies"], 1)
        self.assertEqual(second["successful_copies"], 0)
        self.assertTrue(second["copies"][0]["duplicate_prevented"])

        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM trades WHERE account_id = 'FOLLOWER'"
            ).fetchone()["total"]
        self.assertEqual(count, 1)

    def test_paused_relationship_receives_no_copy(self):
        source = self.create_source_trade()
        with database.get_connection() as connection:
            relationship_id = connection.execute(
                "SELECT relationship_id FROM copy_relationships"
            ).fetchone()["relationship_id"]

        self.client.post(f"/copy-relationships/{relationship_id}/pause")
        result = replicate_open_trade(source["trade_id"])
        self.assertEqual(result["eligible_followers"], 0)

    def test_copied_trade_cannot_trigger_cascading_copy(self):
        source = self.create_source_trade()
        result = replicate_open_trade(source["trade_id"])
        copied_id = result["copies"][0]["copied_trade_id"]

        cascade = replicate_open_trade(copied_id)
        self.assertEqual(cascade["status"], "SKIPPED")
        self.assertEqual(cascade["copies"], [])

    def test_source_trade_must_be_open(self):
        source = self.create_source_trade()
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE trades SET status = 'CLOSED' WHERE trade_id = ?",
                (source["trade_id"],)
            )

        with self.assertRaises(ValueError):
            replicate_open_trade(source["trade_id"])

    def test_follower_risk_rejection_is_recorded_without_affecting_source(self):
        # Consume the follower's full combined risk budget first.
        open_paper_trade(
            account_id="FOLLOWER",
            client_order_id="FOLLOWER-EXISTING",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1,
            symbol="EURUSD"
        )
        source = self.create_source_trade()
        result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]

        self.assertEqual(source_status, "OPEN")


if __name__ == "__main__":
    unittest.main()
