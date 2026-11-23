import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
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
        # These copy-trading fixtures intentionally require two owned accounts.
        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE participant_subscription_entitlements
                SET tier = 'EXCLUSIVE'
                WHERE user_id = ?
                """,
                (self.user_id,),
            )
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

    def test_runtime_rejects_cross_currency_copy_even_for_legacy_relationship(self):
        create_paper_account(
            "SOURCE-USD",
            10000,
            owner_user_id=self.user_id,
            settlement_currency="USD",
        )
        source = open_paper_trade(
            account_id="SOURCE-USD",
            client_order_id="SOURCE-USD-ORDER-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
        )

        # Simulate an old or manually inserted relationship that bypassed
        # the relationship-creation guard. Runtime must still reject it.
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_relationships (
                    relationship_id, source_account_id, follower_account_id
                )
                VALUES ('REL-CROSS-CURRENCY', 'SOURCE-USD', 'FOLLOWER')
                """
            )

        result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn(
            "Cross-currency copy is disabled",
            result["copies"][0]["failure_reason"],
        )

        with database.get_connection() as connection:
            copied_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE account_id = 'FOLLOWER'
                  AND copied_from_trade_id = ?
                """,
                (source["trade_id"],),
            ).fetchone()["total"]

        self.assertEqual(copied_count, 0)

    def test_api_trade_creation_triggers_copy(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "SOURCE",
                "client_order_id": "API-SOURCE-001",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
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

    def test_concurrent_replication_of_same_source_creates_one_follower_trade(self):
        source = self.create_source_trade(order_id="SOURCE-SAME-TRADE-RACE")
        start = Barrier(2)

        def replicate():
            start.wait()
            return replicate_open_trade(source["trade_id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: replicate(), range(2)))

        self.assertEqual(
            sum(result["successful_copies"] for result in results),
            1
        )

        with database.get_connection() as connection:
            trades = connection.execute(
                """
                SELECT trade_id, copied_from_trade_id, status
                FROM trades
                WHERE account_id = 'FOLLOWER'
                  AND copied_from_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchall()
            executions = connection.execute(
                """
                SELECT status, copied_trade_id, lease_token, lease_expires_at
                FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchall()

        self.assertEqual(len(trades), 1)
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["status"], "COPIED")
        self.assertEqual(executions[0]["copied_trade_id"], trades[0]["trade_id"])
        self.assertIsNone(executions[0]["lease_token"])
        self.assertIsNone(executions[0]["lease_expires_at"])

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

    def test_unavailable_follower_account_records_failure_without_affecting_source(self):
        source = self.create_source_trade("SOURCE-MISSING-FOLLOWER")
        # Simulate a legacy/orphaned relationship whose paper account is no
        # longer available. Production foreign-key enforcement stays enabled.
        with database.get_connection() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "DELETE FROM paper_accounts WHERE account_id = 'FOLLOWER'"
            )

        result = replicate_open_trade(source["trade_id"])
        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn("account not found", result["copies"][0]["failure_reason"].lower())

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            execution_status = connection.execute(
                "SELECT status FROM copy_executions WHERE source_trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]

        self.assertEqual(source_status, "OPEN")
        self.assertEqual(execution_status, "FAILED")

    def test_follower_risk_rejection_is_recorded_without_affecting_source(self):
        # Consume the follower's full combined risk budget first.
        for number, symbol in enumerate(("XAUUSD", "XAUUSD"), start=1):
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


    def _create_pending_copy_execution(
        self,
        source_trade_id,
        updated_at_sql="CURRENT_TIMESTAMP",
        lease_expires_at_sql="datetime('now', '+5 minutes')"
    ):
        with database.get_connection() as connection:
            relationship_id = connection.execute(
                "SELECT relationship_id FROM copy_relationships"
            ).fetchone()["relationship_id"]
            connection.execute(
                f"""
                INSERT INTO copy_executions (
                    copy_execution_id, relationship_id, source_trade_id,
                    status, updated_at, lease_token, lease_expires_at
                )
                VALUES (?, ?, ?, 'PENDING', {updated_at_sql}, ?, {lease_expires_at_sql})
                """,
                (
                    "PENDING-" + source_trade_id,
                    relationship_id,
                    source_trade_id,
                    "TEST-LEASE-" + source_trade_id
                )
            )
        return relationship_id

    def test_worker_crash_after_follower_insert_is_recovered_without_duplicate(self):
        source = self.create_source_trade("SOURCE-CRASH-AFTER-INSERT")
        original_open = copy_trading.open_paper_trade

        class SimulatedWorkerCrash(BaseException):
            pass

        def insert_follower_then_crash(**kwargs):
            # The follower trade commits before open_paper_trade returns.
            # Simulate process death before copy execution finalization.
            copied = original_open(**kwargs)
            raise SimulatedWorkerCrash(copied["trade_id"])

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=insert_follower_then_crash
        ):
            with self.assertRaises(SimulatedWorkerCrash):
                replicate_open_trade(source["trade_id"])

        with database.get_connection() as connection:
            relationship_id = connection.execute(
                "SELECT relationship_id FROM copy_relationships"
            ).fetchone()["relationship_id"]
            order_id = f"COPY-{relationship_id}-{source['trade_id']}"
            pending = connection.execute(
                """
                SELECT status, lease_token, lease_expires_at
                FROM copy_executions
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            ).fetchone()
            inserted = connection.execute(
                """
                SELECT trade_id, copied_from_trade_id
                FROM trades
                WHERE account_id = 'FOLLOWER' AND client_order_id = ?
                """,
                (order_id,)
            ).fetchone()

        self.assertEqual(pending["status"], "PENDING")
        self.assertIsNotNone(pending["lease_token"])
        self.assertIsNotNone(pending["lease_expires_at"])
        self.assertIsNotNone(inserted)
        self.assertEqual(inserted["copied_from_trade_id"], source["trade_id"])

        # Simulate a crashed worker: its lease has expired, so recovery may
        # safely adopt the already-committed follower trade.
        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE copy_executions
                SET lease_token = NULL,
                    lease_expires_at = datetime('now', '-1 minute')
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            )

        recovered = replicate_open_trade(source["trade_id"])
        self.assertEqual(recovered["successful_copies"], 1)
        self.assertTrue(recovered["copies"][0]["recovered"])
        self.assertEqual(recovered["copies"][0]["copied_trade_id"], inserted["trade_id"])

        with database.get_connection() as connection:
            follower_rows = connection.execute(
                """
                SELECT trade_id FROM trades
                WHERE account_id = 'FOLLOWER' AND client_order_id = ?
                """,
                (order_id,)
            ).fetchall()
            execution = connection.execute(
                """
                SELECT status, copied_trade_id, lease_token, lease_expires_at
                FROM copy_executions
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            ).fetchone()

        self.assertEqual(len(follower_rows), 1)
        self.assertEqual(execution["status"], "COPIED")
        self.assertEqual(execution["copied_trade_id"], inserted["trade_id"])
        self.assertIsNone(execution["lease_token"])
        self.assertIsNone(execution["lease_expires_at"])

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
        # This fixture represents an abandoned worker after the trade commit.
        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE copy_executions
                SET lease_token = NULL,
                    lease_expires_at = datetime('now', '-1 minute')
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
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

    def test_expired_lease_reclaim_rejects_old_worker_token(self):
        source = self.create_source_trade("SOURCE-LEASE-OWNER-ROTATION")
        relationship_id = self._create_pending_copy_execution(
            source["trade_id"],
            "datetime('now', '-10 minutes')",
            "datetime('now', '-10 minutes')"
        )
        old_token = "TEST-LEASE-" + source["trade_id"]
        original_open = copy_trading.open_paper_trade
        old_worker_rejections = []

        def verify_old_worker_is_fenced(**kwargs):
            with self.assertRaisesRegex(ValueError, "lease token"):
                original_open(
                    account_id="FOLLOWER",
                    client_order_id="OLD-WORKER-" + source["trade_id"],
                    side="BUY",
                    entry_price=4000,
                    stop_loss=3990,
                    symbol="XAUUSD",
                    copy_source_trade_id=source["trade_id"],
                    copy_relationship_id=relationship_id,
                    copy_execution_token=old_token
                )
            old_worker_rejections.append(True)
            self.assertNotEqual(kwargs["copy_execution_token"], old_token)
            return original_open(**kwargs)

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=verify_old_worker_is_fenced
        ):
            result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "COPIED")
        self.assertEqual(old_worker_rejections, [True])

        with database.get_connection() as connection:
            executions = connection.execute(
                """
                SELECT status, lease_token, lease_expires_at
                FROM copy_executions
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            ).fetchone()
            follower_trades = connection.execute(
                "SELECT COUNT(*) AS total FROM trades WHERE account_id = 'FOLLOWER'"
            ).fetchone()["total"]

        self.assertEqual(executions["status"], "COPIED")
        self.assertIsNone(executions["lease_token"])
        self.assertIsNone(executions["lease_expires_at"])
        self.assertEqual(follower_trades, 1)

    def test_two_workers_racing_to_reclaim_expired_lease_have_one_owner(self):
        source = self.create_source_trade("SOURCE-EXPIRED-LEASE-RACE")
        relationship_id = self._create_pending_copy_execution(
            source["trade_id"],
            "datetime('now', '-10 minutes')",
            "datetime('now', '-10 minutes')"
        )
        original_open = copy_trading.open_paper_trade
        first_worker_at_open = Event()
        release_first_worker = Event()

        def pause_first_worker_after_claim(**kwargs):
            first_worker_at_open.set()
            if not release_first_worker.wait(timeout=10):
                raise TimeoutError("Test did not release the first copy worker.")
            return original_open(**kwargs)

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=pause_first_worker_after_claim
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(replicate_open_trade, source["trade_id"])
                self.assertTrue(first_worker_at_open.wait(timeout=5))

                # The first worker owns the newly claimed lease but has not
                # inserted its follower trade. The second worker must defer.
                second = executor.submit(replicate_open_trade, source["trade_id"])
                second_result = second.result(timeout=5)
                release_first_worker.set()
                first_result = first.result(timeout=10)

        self.assertEqual(first_result["successful_copies"], 1)
        self.assertEqual(second_result["successful_copies"], 0)
        self.assertEqual(second_result["copies"][0]["status"], "PENDING")
        self.assertTrue(second_result["copies"][0]["recovery_deferred"])

        with database.get_connection() as connection:
            trades = connection.execute(
                """
                SELECT trade_id, copied_from_trade_id
                FROM trades
                WHERE account_id = 'FOLLOWER'
                  AND copied_from_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchall()
            execution = connection.execute(
                """
                SELECT status, copied_trade_id, lease_token, lease_expires_at
                FROM copy_executions
                WHERE relationship_id = ? AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            ).fetchone()

        self.assertEqual(len(trades), 1)
        self.assertEqual(execution["status"], "COPIED")
        self.assertEqual(execution["copied_trade_id"], trades[0]["trade_id"])
        self.assertIsNone(execution["lease_token"])
        self.assertIsNone(execution["lease_expires_at"])

    def test_paused_or_stopped_relationship_finalizes_pending_without_opening_copy(self):
        for index, status in enumerate(("PAUSED", "STOPPED"), start=1):
            source = self.create_source_trade(f"SOURCE-INACTIVE-PENDING-{index}")
            relationship_id = self._create_pending_copy_execution(source["trade_id"])
            with database.get_connection() as connection:
                connection.execute(
                    """
                    UPDATE copy_relationships
                    SET status = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE relationship_id = ?
                    """,
                    (status, relationship_id)
                )

            result = replicate_open_trade(source["trade_id"])
            self.assertEqual(result["eligible_followers"], 1)
            self.assertEqual(result["failed_copies"], 1)
            self.assertEqual(result["copies"][0]["status"], "FAILED")
            self.assertIn(status.lower(), result["copies"][0]["failure_reason"].lower())

            with database.get_connection() as connection:
                execution = connection.execute(
                    """
                    SELECT status FROM copy_executions
                    WHERE relationship_id = ? AND source_trade_id = ?
                    """,
                    (relationship_id, source["trade_id"])
                ).fetchone()
                follower_count = connection.execute(
                    """
                    SELECT COUNT(*) AS total FROM trades
                    WHERE account_id = 'FOLLOWER'
                      AND client_order_id = ?
                    """,
                    (f"COPY-{relationship_id}-{source['trade_id']}",)
                ).fetchone()["total"]

            self.assertEqual(execution["status"], "FAILED")
            self.assertEqual(follower_count, 0)

            # Re-enable only to set up the next independent test case.
            if status == "PAUSED":
                with database.get_connection() as connection:
                    connection.execute(
                        """
                        UPDATE copy_relationships
                        SET status = 'ACTIVE', updated_at = CURRENT_TIMESTAMP
                        WHERE relationship_id = ?
                        """,
                        (relationship_id,)
                    )

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
            "datetime('now', '-10 minutes')",
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
            symbol="XAUUSD"
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


    def test_concurrent_source_close_wins_before_copy_insert(self):
        source = self.create_source_trade("SOURCE-CONCURRENT-CLOSE-WINS")
        original_open = copy_trading.open_paper_trade
        copy_worker_ready = Event()
        allow_copy_attempt = Event()

        def pause_before_atomic_copy_insert(**kwargs):
            copy_worker_ready.set()
            if not allow_copy_attempt.wait(timeout=10):
                raise TimeoutError("Test did not release the copy worker.")
            return original_open(**kwargs)

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=pause_before_atomic_copy_insert
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                copy_future = executor.submit(
                    replicate_open_trade, source["trade_id"]
                )
                self.assertTrue(copy_worker_ready.wait(timeout=5))

                # Close commits while the copy worker is waiting. The atomic
                # source-open check must reject the subsequent follower insert.
                close_result = close_trade_with_copies(
                    source["trade_id"], 3990
                )
                allow_copy_attempt.set()
                copy_result = copy_future.result(timeout=10)

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(copy_result["successful_copies"], 0)
        self.assertEqual(copy_result["failed_copies"], 1)
        self.assertEqual(copy_result["copies"][0]["status"], "FAILED")

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            follower_count = connection.execute(
                "SELECT COUNT(*) AS total FROM trades WHERE account_id = 'FOLLOWER'"
            ).fetchone()["total"]
            execution = connection.execute(
                """
                SELECT status, lease_token, lease_expires_at
                FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()

        self.assertEqual(source_status, "CLOSED")
        self.assertEqual(follower_count, 0)
        self.assertEqual(execution["status"], "FAILED")
        self.assertIsNone(execution["lease_token"])
        self.assertIsNone(execution["lease_expires_at"])

    def test_concurrent_source_close_after_copy_insert_closes_follower_before_finalization(self):
        source = self.create_source_trade("SOURCE-CONCURRENT-COPY-WINS")
        original_open = copy_trading.open_paper_trade
        follower_inserted = Event()
        allow_copy_finalization = Event()
        copied_ids = []

        def pause_after_atomic_copy_insert(**kwargs):
            copied = original_open(**kwargs)
            copied_ids.append(copied["trade_id"])
            follower_inserted.set()
            if not allow_copy_finalization.wait(timeout=10):
                raise TimeoutError("Test did not release copy finalization.")
            return copied

        with patch(
            "app.copy_trading.open_paper_trade",
            side_effect=pause_after_atomic_copy_insert
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                copy_future = executor.submit(
                    replicate_open_trade, source["trade_id"]
                )
                self.assertTrue(follower_inserted.wait(timeout=5))

                # The follower insert has committed, but the copy execution is
                # still PENDING. Source close must close both positions and
                # terminalize the execution before the worker can finalize.
                close_result = close_trade_with_copies(
                    source["trade_id"], 3990
                )
                allow_copy_finalization.set()
                copy_result = copy_future.result(timeout=10)

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(close_result["closed_copies"], 1)
        self.assertEqual(copy_result["successful_copies"], 0)
        self.assertEqual(copy_result["copies"][0]["status"], "CLOSED")
        self.assertEqual(
            copy_result["copies"][0]["copied_trade_id"], copied_ids[0]
        )

        with database.get_connection() as connection:
            source_status = connection.execute(
                "SELECT status FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()["status"]
            follower = connection.execute(
                "SELECT trade_id, status FROM trades WHERE trade_id = ?",
                (copied_ids[0],)
            ).fetchone()
            execution = connection.execute(
                """
                SELECT status, copied_trade_id, lease_token, lease_expires_at
                FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()

        self.assertEqual(source_status, "CLOSED")
        self.assertEqual(follower["status"], "CLOSED")
        self.assertEqual(execution["status"], "CLOSED")
        self.assertEqual(execution["copied_trade_id"], follower["trade_id"])
        self.assertIsNone(execution["lease_token"])
        self.assertIsNone(execution["lease_expires_at"])



    def account_balance_for(self, account_id):
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = ?",
                (account_id,)
            ).fetchone()
        return row["balance"]

    def create_three_follower_source(self, order_id):
        for account_id, balance in (("FOLLOWER_TWO", 7000), ("FOLLOWER_THREE", 9000)):
            create_paper_account(account_id, balance, owner_user_id=self.user_id)
            response = self.client.post(
                f"/accounts/{account_id}/copy-relationships",
                json={"source_account_id": "SOURCE"}
            )
            self.assertEqual(response.status_code, 200)

        source = self.create_source_trade(order_id)
        replication = replicate_open_trade(source["trade_id"])
        self.assertEqual(replication["successful_copies"], 3)
        with database.get_connection() as connection:
            copies = connection.execute(
                """
                SELECT trade_id, account_id FROM trades
                WHERE copied_from_trade_id = ?
                ORDER BY account_id
                """,
                (source["trade_id"],)
            ).fetchall()
        self.assertEqual(len(copies), 3)
        return source, {row["account_id"]: row["trade_id"] for row in copies}

    def assert_multi_follower_close_rolled_back(self, source_trade_id, copied_ids):
        with database.get_connection() as connection:
            source = connection.execute(
                "SELECT status, realized_pnl, gross_realized_pnl FROM trades WHERE trade_id = ?",
                (source_trade_id,)
            ).fetchone()
            copies = connection.execute(
                """
                SELECT trade_id, status, realized_pnl, gross_realized_pnl
                FROM trades WHERE trade_id IN (?, ?, ?)
                """,
                tuple(copied_ids.values())
            ).fetchall()
            executions = connection.execute(
                """
                SELECT status FROM copy_executions WHERE source_trade_id = ?
                """,
                (source_trade_id,)
            ).fetchall()
        self.assertEqual(source["status"], "OPEN")
        self.assertEqual(source["realized_pnl"], 0)
        self.assertIsNone(source["gross_realized_pnl"])
        self.assertEqual(len(copies), 3)
        self.assertTrue(all(row["status"] == "OPEN" for row in copies))
        self.assertTrue(all(row["realized_pnl"] == 0 for row in copies))
        self.assertTrue(all(row["gross_realized_pnl"] is None for row in copies))
        self.assertEqual(len(executions), 3)
        self.assertTrue(all(row["status"] == "COPIED" for row in executions))

    def create_close_failure_trigger(self, trigger_name, table, update_column, where):
        with database.get_connection() as connection:
            connection.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE UPDATE OF {update_column} ON {table}
                WHEN {where}
                BEGIN
                    SELECT RAISE(ABORT, 'simulated multi-follower close write failure');
                END
                """
            )


    def test_multi_follower_close_rolls_back_if_first_follower_balance_write_fails(self):
        source, copies = self.create_three_follower_source("MULTI-ROLLBACK-FIRST-BALANCE")
        before = {
            "SOURCE": 10000, "FOLLOWER": 5000,
            "FOLLOWER_TWO": 7000, "FOLLOWER_THREE": 9000
        }
        self.create_close_failure_trigger(
            "fail_multi_first_follower_balance", "paper_accounts", "balance",
            "OLD.account_id = 'FOLLOWER'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(source["trade_id"], 4010)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        with database.get_connection() as connection:
            balances = dict(connection.execute(
                "SELECT account_id, balance FROM paper_accounts"
            ).fetchall())
        for account_id, balance in before.items():
            self.assertEqual(balances[account_id], balance)

    def test_multi_follower_close_rolls_back_if_last_follower_balance_write_fails(self):
        source, copies = self.create_three_follower_source("MULTI-ROLLBACK-LAST-BALANCE")
        before = {"SOURCE": 10000, "FOLLOWER": 5000, "FOLLOWER_TWO": 7000, "FOLLOWER_THREE": 9000}
        self.create_close_failure_trigger(
            "fail_multi_last_follower_balance", "paper_accounts", "balance",
            "OLD.account_id = 'FOLLOWER_TWO'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(source["trade_id"], 4010)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        with database.get_connection() as connection:
            balances = dict(connection.execute(
                "SELECT account_id, balance FROM paper_accounts"
            ).fetchall())
        for account_id, balance in before.items():
            self.assertEqual(balances[account_id], balance)

    def test_multi_follower_close_rolls_back_if_a_copy_trade_write_fails(self):
        source, copies = self.create_three_follower_source("MULTI-ROLLBACK-COPY-TRADE")
        target_id = copies["FOLLOWER_TWO"]
        self.create_close_failure_trigger(
            "fail_multi_copy_trade_close", "trades", "status",
            f"OLD.trade_id = '{target_id}'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(source["trade_id"], 4010)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        self.assertEqual(self.account_balance_for("SOURCE"), 10000)
        self.assertEqual(self.account_balance_for("FOLLOWER"), 5000)
        self.assertEqual(self.account_balance_for("FOLLOWER_TWO"), 7000)
        self.assertEqual(self.account_balance_for("FOLLOWER_THREE"), 9000)

    def test_multi_follower_close_rolls_back_if_copy_execution_finalization_fails(self):
        source, copies = self.create_three_follower_source("MULTI-ROLLBACK-EXECUTION")
        target_id = copies["FOLLOWER_TWO"]
        self.create_close_failure_trigger(
            "fail_multi_execution_finalize", "copy_executions", "status",
            f"OLD.copied_trade_id = '{target_id}'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(source["trade_id"], 4010)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        for account_id, balance in (("SOURCE", 10000), ("FOLLOWER", 5000), ("FOLLOWER_TWO", 7000), ("FOLLOWER_THREE", 9000)):
            self.assertEqual(self.account_balance_for(account_id), balance)

    def test_multi_follower_close_rolls_back_if_source_trade_write_fails(self):
        source, copies = self.create_three_follower_source("MULTI-ROLLBACK-SOURCE-TRADE")
        self.create_close_failure_trigger(
            "fail_multi_source_trade_close", "trades", "status",
            f"OLD.trade_id = '{source['trade_id']}'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(source["trade_id"], 4010)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)

    def test_multi_follower_close_retry_succeeds_after_balance_failure_is_removed(self):
        source, copies = self.create_three_follower_source("MULTI-RETRY-AFTER-ROLLBACK")
        self.create_close_failure_trigger(
            "fail_multi_retry_balance", "paper_accounts", "balance",
            "OLD.account_id = 'FOLLOWER_TWO'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(source["trade_id"], 4010)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        with database.get_connection() as connection:
            connection.execute("DROP TRIGGER fail_multi_retry_balance")
        result = close_trade_with_copies(source["trade_id"], 4010)
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["closed_copies"], 3)
        self.assertEqual(len(result["copy_closures"]), 3)

    def test_multi_follower_close_rejects_duplicate_retry_without_double_credit(self):
        source, copies = self.create_three_follower_source("MULTI-DUPLICATE-CLOSE")
        result = close_trade_with_copies(source["trade_id"], 4010)
        self.assertEqual(result["closed_copies"], 3)
        balances_after_close = {
            account_id: self.account_balance_for(account_id)
            for account_id in ("SOURCE", "FOLLOWER", "FOLLOWER_TWO", "FOLLOWER_THREE")
        }
        with self.assertRaisesRegex(ValueError, "Only open trades"):
            close_trade_with_copies(source["trade_id"], 4010)
        for account_id, balance in balances_after_close.items():
            self.assertEqual(self.account_balance_for(account_id), balance)

    def test_multi_follower_close_still_closes_existing_copies_when_relationships_paused(self):
        source, copies = self.create_three_follower_source("MULTI-CLOSE-PAUSED-RELATIONSHIPS")
        with database.get_connection() as connection:
            relationship_ids = [
                row["relationship_id"]
                for row in connection.execute(
                    "SELECT relationship_id FROM copy_relationships WHERE source_account_id = 'SOURCE'"
                ).fetchall()
            ]
        for relationship_id in relationship_ids:
            response = self.client.post(f"/copy-relationships/{relationship_id}/pause")
            self.assertEqual(response.status_code, 200)
        result = close_trade_with_copies(source["trade_id"], 4010)
        self.assertEqual(result["closed_copies"], 3)
        with database.get_connection() as connection:
            statuses = connection.execute(
                "SELECT status FROM trades WHERE copied_from_trade_id = ?",
                (source["trade_id"],)
            ).fetchall()
        self.assertEqual(len(statuses), 3)
        self.assertTrue(all(row["status"] == "CLOSED" for row in statuses))

    def test_multi_follower_close_rejects_negative_follower_balance_without_partial_close(self):
        source, copies = self.create_three_follower_source("MULTI-NEGATIVE-FOLLOWER-BALANCE")
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE paper_accounts SET balance = 1 WHERE account_id = 'FOLLOWER_TWO'"
            )
        with self.assertRaisesRegex(ValueError, "negative"):
            close_trade_with_copies(source["trade_id"], 3990)
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        self.assertEqual(self.account_balance_for("SOURCE"), 10000)
        self.assertEqual(self.account_balance_for("FOLLOWER"), 5000)
        self.assertEqual(self.account_balance_for("FOLLOWER_TWO"), 1)
        self.assertEqual(self.account_balance_for("FOLLOWER_THREE"), 9000)

    def test_multi_follower_close_preserves_source_costs_once_after_retry(self):
        source, copies = self.create_three_follower_source("MULTI-COST-RETRY")
        self.create_close_failure_trigger(
            "fail_multi_cost_retry_balance", "paper_accounts", "balance",
            "OLD.account_id = 'FOLLOWER_THREE'"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            close_trade_with_copies(
                source["trade_id"], 4010, spread_cost=1.1,
                commission_cost=0.8, swap_cost=0.6, other_costs=0.5
            )
        self.assert_multi_follower_close_rolled_back(source["trade_id"], copies)
        with database.get_connection() as connection:
            connection.execute("DROP TRIGGER fail_multi_cost_retry_balance")
        result = close_trade_with_copies(
            source["trade_id"], 4010, spread_cost=1.1,
            commission_cost=0.8, swap_cost=0.6, other_costs=0.5
        )
        self.assertEqual(result["closed_copies"], 3)
        with database.get_connection() as connection:
            source_row = connection.execute(
                "SELECT * FROM trades WHERE trade_id = ?", (source["trade_id"],)
            ).fetchone()
            follower_rows = connection.execute(
                "SELECT * FROM trades WHERE copied_from_trade_id = ?",
                (source["trade_id"],)
            ).fetchall()
        self.assertAlmostEqual(
            source_row["realized_pnl"],
            source_row["gross_realized_pnl"] - 3.0,
            places=6
        )
        self.assertTrue(all(row["realized_pnl"] == row["gross_realized_pnl"] for row in follower_rows))
        self.assertTrue(all(
            (row["spread_cost"], row["commission_cost"], row["swap_cost"], row["other_costs"])
            == (0, 0, 0, 0)
            for row in follower_rows
        ))

    def test_source_close_reconciles_multiple_followers_independently(self):
        create_paper_account("FOLLOWER_TWO", 7000, owner_user_id=self.user_id)
        response = self.client.post(
            "/accounts/FOLLOWER_TWO/copy-relationships",
            json={"source_account_id": "SOURCE"}
        )
        self.assertEqual(response.status_code, 200)

        source = self.create_source_trade("MULTI-FOLLOWER-SOURCE")
        copied = replicate_open_trade(source["trade_id"])
        self.assertEqual(copied["successful_copies"], 2)

        with database.get_connection() as connection:
            follower_trades = connection.execute(
                """
                SELECT trade_id, account_id, quantity, entry_price
                FROM trades WHERE copied_from_trade_id = ?
                ORDER BY account_id
                """,
                (source["trade_id"],)
            ).fetchall()
        self.assertEqual(len(follower_trades), 2)

        result = close_trade_with_copies(
            source["trade_id"], 4010,
            spread_cost=1.25, commission_cost=0.75,
            swap_cost=0.5, other_costs=0.25
        )
        self.assertEqual(result["closed_copies"], 2)
        self.assertEqual(len(result["copy_closures"]), 2)

        with database.get_connection() as connection:
            source_row = connection.execute(
                "SELECT * FROM trades WHERE trade_id = ?",
                (source["trade_id"],)
            ).fetchone()
            followers = connection.execute(
                """
                SELECT * FROM trades WHERE copied_from_trade_id = ?
                ORDER BY account_id
                """,
                (source["trade_id"],)
            ).fetchall()
            executions = connection.execute(
                """
                SELECT status FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchall()
            balances = {
                row["account_id"]: row["balance"]
                for row in connection.execute(
                    """
                    SELECT account_id, balance FROM paper_accounts
                    WHERE account_id IN ('SOURCE', 'FOLLOWER', 'FOLLOWER_TWO')
                    """
                ).fetchall()
            }

        self.assertEqual(source_row["status"], "CLOSED")
        self.assertAlmostEqual(
            source_row["realized_pnl"],
            source_row["gross_realized_pnl"] - 2.75,
            places=6
        )
        self.assertEqual(len(followers), 2)
        self.assertTrue(all(row["status"] == "CLOSED" for row in followers))
        self.assertTrue(all(row["spread_cost"] == 0 for row in followers))
        self.assertTrue(all(row["commission_cost"] == 0 for row in followers))
        self.assertTrue(all(row["swap_cost"] == 0 for row in followers))
        self.assertTrue(all(row["other_costs"] == 0 for row in followers))
        self.assertTrue(all(row["realized_pnl"] == row["gross_realized_pnl"] for row in followers))
        self.assertEqual(len(executions), 2)
        self.assertTrue(all(row["status"] == "CLOSED" for row in executions))
        self.assertAlmostEqual(balances["SOURCE"], 10000 + source_row["realized_pnl"], places=6)
        for row in followers:
            self.assertAlmostEqual(
                balances[row["account_id"]],
                (5000 if row["account_id"] == "FOLLOWER" else 7000)
                + row["realized_pnl"],
                places=6
            )

    def test_individual_follower_close_does_not_close_source_or_other_followers(self):
        create_paper_account("FOLLOWER_TWO", 7000, owner_user_id=self.user_id)
        response = self.client.post(
            "/accounts/FOLLOWER_TWO/copy-relationships",
            json={"source_account_id": "SOURCE"}
        )
        self.assertEqual(response.status_code, 200)

        source = self.create_source_trade("FOLLOWER-ONLY-CLOSE")
        copied = replicate_open_trade(source["trade_id"])
        self.assertEqual(copied["successful_copies"], 2)

        with database.get_connection() as connection:
            target = connection.execute(
                """
                SELECT trade_id FROM trades
                WHERE copied_from_trade_id = ? AND account_id = 'FOLLOWER'
                """,
                (source["trade_id"],)
            ).fetchone()["trade_id"]
            other = connection.execute(
                """
                SELECT trade_id FROM trades
                WHERE copied_from_trade_id = ? AND account_id = 'FOLLOWER_TWO'
                """,
                (source["trade_id"],)
            ).fetchone()["trade_id"]

        result = close_trade_with_copies(
            target, 4010, commission_cost=0.5
        )
        self.assertEqual(result["close_scope"], "FOLLOWER_ONLY")
        self.assertEqual(result["closed_copies"], 0)

        with database.get_connection() as connection:
            statuses = {
                row["trade_id"]: row["status"]
                for row in connection.execute(
                    """
                    SELECT trade_id, status FROM trades
                    WHERE trade_id IN (?, ?, ?)
                    """,
                    (source["trade_id"], target, other)
                ).fetchall()
            }
            execution_statuses = {
                row["copied_trade_id"]: row["status"]
                for row in connection.execute(
                    """
                    SELECT copied_trade_id, status FROM copy_executions
                    WHERE source_trade_id = ?
                    """,
                    (source["trade_id"],)
                ).fetchall()
            }

        self.assertEqual(statuses[source["trade_id"]], "OPEN")
        self.assertEqual(statuses[target], "CLOSED")
        self.assertEqual(statuses[other], "OPEN")
        self.assertEqual(execution_statuses[target], "CLOSED")
        self.assertEqual(execution_statuses[other], "COPIED")


if __name__ == "__main__":
    unittest.main()
