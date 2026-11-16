import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.auth import register_user, create_session
from app.execution import create_paper_account, open_paper_trade
import app.copy_trading as copy_trading
from app.main import app
from app.copy_trading import replicate_open_trade, close_trade_with_copies
from app.safety import activate_emergency_stop, deactivate_emergency_stop


class CopyTradeReplicationTests(unittest.TestCase):

    def setUp(self):
        # Keep ordinary trading tests independent of the wall clock.
        # The maintenance-window block itself is tested with an explicit clock override.
        self.maintenance_clock = patch("app.safety.is_maintenance_window", return_value=False)
        self.maintenance_clock.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "copy.db"
        init_db()
        deactivate_emergency_stop()
        user = register_user("copy-replication-tests@taimobot.local", "TestPassphrase-2026-Secure!")
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
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_source_trade(self, order_id="SOURCE-ORDER-001", risk_percent=1):
        return open_paper_trade(
            account_id="SOURCE",
            client_order_id=order_id,
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=risk_percent,
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
        for number, symbol in enumerate(("EURUSD", "GBPUSD"), start=1):
            open_paper_trade(
                account_id="FOLLOWER",
                client_order_id=f"FOLLOWER-EXISTING-{number}",
                side="BUY",
                entry_price=4000,
                stop_loss=3990,
                risk_percent=1,
                symbol=symbol
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


    def test_emergency_stop_blocks_copy_execution_without_affecting_source(self):
        source = self.create_source_trade()
        activate_emergency_stop()

        result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn("emergency stop", result["copies"][0]["failure_reason"].lower())

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            follower_open_count = connection.execute(
                """
                SELECT COUNT(*) AS total FROM trades
                WHERE account_id = 'FOLLOWER' AND status = 'OPEN'
                """
            ).fetchone()["total"]
            copy_status = connection.execute(
                "SELECT status FROM copy_executions WHERE source_trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]

        self.assertEqual(source_status, "OPEN")
        self.assertEqual(follower_open_count, 0)
        self.assertEqual(copy_status, "FAILED")

    def test_maintenance_blocks_copy_execution_without_affecting_source(self):
        source = self.create_source_trade()

        with patch("app.safety.is_maintenance_window", return_value=True):
            result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn("maintenance window", result["copies"][0]["failure_reason"])

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            follower_open_count = connection.execute(
                """
                SELECT COUNT(*) AS total FROM trades
                WHERE account_id = 'FOLLOWER' AND status = 'OPEN'
                """
            ).fetchone()["total"]
            copy_status = connection.execute(
                "SELECT status FROM copy_executions WHERE source_trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]

        self.assertEqual(source_status, "OPEN")
        self.assertEqual(follower_open_count, 0)
        self.assertEqual(copy_status, "FAILED")


    def _create_pending_copy_execution(self, source_trade_id, updated_at_sql="CURRENT_TIMESTAMP"):
        with database.get_connection() as connection:
            relationship_id = connection.execute(
                "SELECT relationship_id FROM copy_relationships"
            ).fetchone()["relationship_id"]
            connection.execute(
                f"""
                INSERT INTO copy_executions (
                    copy_execution_id, relationship_id, source_trade_id,
                    status, updated_at
                )
                VALUES (?, ?, ?, 'PENDING', {updated_at_sql})
                """,
                ("PENDING-" + source_trade_id, relationship_id, source_trade_id)
            )
        return relationship_id

    def test_pending_copy_recovers_existing_committed_follower_trade(self):
        source = self.create_source_trade("SOURCE-RECOVERY-EXISTING")
        relationship_id = self._create_pending_copy_execution(source["trade_id"])
        order_id = f"COPY-{relationship_id}-{source['trade_id']}"

        existing = open_paper_trade(
            account_id="FOLLOWER",
            client_order_id=order_id,
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            symbol="XAUUSD"
        )

        result = replicate_open_trade(source["trade_id"])
        self.assertEqual(result["successful_copies"], 1)
        self.assertTrue(result["copies"][0]["recovered"])
        self.assertEqual(result["copies"][0]["copied_trade_id"], existing["trade_id"])

        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT COUNT(*) AS total FROM trades
                WHERE account_id = 'FOLLOWER' AND client_order_id = ?
                """,
                (order_id,)
            ).fetchone()
            execution = connection.execute(
                """
                SELECT status, copied_trade_id FROM copy_executions
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            ).fetchone()

        self.assertEqual(rows["total"], 1)
        self.assertEqual(execution["status"], "COPIED")
        self.assertEqual(execution["copied_trade_id"], existing["trade_id"])

    def test_recent_pending_copy_is_not_stolen_from_active_worker(self):
        source = self.create_source_trade("SOURCE-RECOVERY-ACTIVE")
        self._create_pending_copy_execution(source["trade_id"])

        result = replicate_open_trade(source["trade_id"])
        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["copies"][0]["status"], "PENDING")
        self.assertTrue(result["copies"][0]["recovery_deferred"])

        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM trades WHERE account_id = 'FOLLOWER'"
            ).fetchone()["total"]

        self.assertEqual(count, 0)

    def test_restart_reconciles_pending_copy_after_source_and_follower_closed(self):
        source = self.create_source_trade("SOURCE-CLOSED-PENDING-RECOVERY")
        original_open = copy_trading.open_paper_trade

        def commit_copy_close_source_then_simulate_crash(**kwargs):
            copied = original_open(**kwargs)
            close_trade_with_copies(source["trade_id"], 3990)
            # Simulate process termination after the source and follower
            # positions close, but before copy_executions is finalized.
            raise SystemExit("simulated worker crash")

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=commit_copy_close_source_then_simulate_crash
        ):
            with self.assertRaises(SystemExit):
                replicate_open_trade(source["trade_id"])

        with self.assertRaisesRegex(ValueError, "Only open source trades"):
            replicate_open_trade(source["trade_id"])

        with database.get_connection() as connection:
            source_row = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()
            follower_row = connection.execute(
                """
                SELECT trade_id, status FROM trades
                WHERE account_id = 'FOLLOWER'
                """
            ).fetchone()
            execution = connection.execute(
                """
                SELECT status, copied_trade_id, failure_reason
                FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()

        self.assertEqual(source_row["status"], "CLOSED")
        self.assertEqual(follower_row["status"], "CLOSED")
        self.assertEqual(execution["status"], "CLOSED")
        self.assertEqual(execution["copied_trade_id"], follower_row["trade_id"])
        self.assertIsNone(execution["failure_reason"])

    def test_source_close_during_copy_execution_keeps_follower_and_execution_closed(self):
        source = self.create_source_trade("SOURCE-CLOSE-DURING-COPY")
        original_open = copy_trading.open_paper_trade
        close_result = {}

        def open_copy_then_close_source(**kwargs):
            copied = original_open(**kwargs)
            # Simulate the source being closed after the follower trade is
            # committed but before copy_executions is finalized as COPIED.
            close_result.update(
                close_trade_with_copies(source["trade_id"], 3990)
            )
            return copied

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=open_copy_then_close_source
        ):
            result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["copies"][0]["status"], "CLOSED")
        self.assertEqual(close_result["closed_copies"], 1)

        with database.get_connection() as connection:
            source_row = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()
            follower_row = connection.execute(
                """
                SELECT trade_id, status FROM trades
                WHERE account_id = 'FOLLOWER'
                """
            ).fetchone()
            execution = connection.execute(
                """
                SELECT status, copied_trade_id FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()

        self.assertEqual(source_row["status"], "CLOSED")
        self.assertEqual(follower_row["status"], "CLOSED")
        self.assertEqual(execution["status"], "CLOSED")
        self.assertEqual(execution["copied_trade_id"], follower_row["trade_id"])

    def test_source_closed_before_atomic_copy_insert_prevents_follower_entry(self):
        source = self.create_source_trade("SOURCE-CLOSE-RACE")
        original_open = copy_trading.open_paper_trade

        def close_source_then_attempt_copy(**kwargs):
            close_trade_with_copies(source["trade_id"], 3990)
            return original_open(**kwargs)

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=close_source_then_attempt_copy
        ):
            result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn("source trade must remain open", result["copies"][0]["failure_reason"].lower())

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            follower_count = connection.execute(
                """
                SELECT COUNT(*) AS total FROM trades
                WHERE account_id = 'FOLLOWER'
                """
            ).fetchone()["total"]
            execution_status = connection.execute(
                "SELECT status FROM copy_executions WHERE source_trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]

        self.assertEqual(source_status, "CLOSED")
        self.assertEqual(follower_count, 0)
        self.assertEqual(execution_status, "FAILED")


    def test_paused_relationship_before_atomic_copy_insert_prevents_follower_entry(self):
        source = self.create_source_trade("SOURCE-PAUSE-RACE")
        with database.get_connection() as connection:
            relationship_id = connection.execute(
                "SELECT relationship_id FROM copy_relationships"
            ).fetchone()["relationship_id"]

        original_open = copy_trading.open_paper_trade

        def pause_relationship_then_attempt_copy(**kwargs):
            # Simulate the relationship being paused by another request
            # immediately before the atomic follower-entry transaction.
            with database.get_connection() as connection:
                connection.execute(
                    """
                    UPDATE copy_relationships
                    SET status = 'PAUSED', updated_at = CURRENT_TIMESTAMP
                    WHERE relationship_id = ?
                    """,
                    (relationship_id,)
                )
            return original_open(**kwargs)

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=pause_relationship_then_attempt_copy
        ):
            result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn("relationship must remain active", result["copies"][0]["failure_reason"].lower())

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            follower_count = connection.execute(
                """
                SELECT COUNT(*) AS total FROM trades
                WHERE account_id = 'FOLLOWER'
                """
            ).fetchone()["total"]
            execution_status = connection.execute(
                "SELECT status FROM copy_executions WHERE source_trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]

        self.assertEqual(source_status, "OPEN")
        self.assertEqual(follower_count, 0)
        self.assertEqual(execution_status, "FAILED")


    def test_stale_pending_copy_without_trade_is_retried(self):
        source = self.create_source_trade("SOURCE-RECOVERY-STALE")
        self._create_pending_copy_execution(
            source["trade_id"],
            "datetime('now', '-10 minutes')"
        )

        result = replicate_open_trade(source["trade_id"])
        self.assertEqual(result["successful_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "COPIED")

        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS total FROM trades WHERE account_id = 'FOLLOWER'"
            ).fetchone()["total"]

        self.assertEqual(count, 1)

    def test_concurrent_copy_entries_cannot_exceed_followers_combined_risk(self):
        # The follower has a 0.5% allocation per copy. One existing
        # position consumes half of its 1% combined-risk allowance.
        open_paper_trade(
            account_id="FOLLOWER",
            client_order_id="FOLLOWER-EXISTING-RISK",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="EURUSD"
        )

        first_source = self.create_source_trade(
            order_id="SOURCE-CONCURRENT-COPY-A",
            risk_percent=0.5
        )
        second_source = self.create_source_trade(
            order_id="SOURCE-CONCURRENT-COPY-B",
            risk_percent=0.5
        )
        start = Barrier(2)

        def replicate(source_trade_id):
            start.wait()
            return replicate_open_trade(source_trade_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                replicate,
                (first_source["trade_id"], second_source["trade_id"])
            ))

        successful = sum(result["successful_copies"] for result in results)
        failed = sum(result["failed_copies"] for result in results)
        self.assertEqual(successful, 1)
        self.assertEqual(failed, 1)

        with database.get_connection() as connection:
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS open_positions,
                    COALESCE(SUM(risk_amount), 0) AS total_open_risk
                FROM trades
                WHERE account_id = 'FOLLOWER'
                  AND status = 'OPEN'
                """
            ).fetchone()
            executions = connection.execute(
                """
                SELECT status
                FROM copy_executions
                WHERE source_trade_id IN (?, ?)
                ORDER BY source_trade_id
                """,
                (first_source["trade_id"], second_source["trade_id"])
            ).fetchall()

        self.assertEqual(totals["open_positions"], 2)
        self.assertEqual(totals["total_open_risk"], 50)
        self.assertLessEqual(totals["total_open_risk"], 50)
        self.assertEqual(
            sorted(row["status"] for row in executions),
            ["COPIED", "FAILED"]
        )


if __name__ == "__main__":
    unittest.main()
