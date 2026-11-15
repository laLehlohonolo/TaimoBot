
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app import database
from app.copy_trading import close_trade_with_copies, replicate_open_trade
from app.execution import open_paper_trade, update_copy_relationship_status
from app.safety import deactivate_emergency_stop


class CopyTradingDatabaseTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test_taimobot.db"
        database.init_db()
        deactivate_emergency_stop()
        # These tests verify copy-trading behavior, not the wall-clock
        # maintenance gate. The dedicated maintenance tests cover that gate.
        self.maintenance_window_patch = patch(
            "app.safety.is_maintenance_window", return_value=False
        )
        self.maintenance_window_patch.start()

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO paper_accounts
                    (account_id, starting_balance, balance, risk_profile)
                VALUES
                    ('SOURCE-001', 10000, 10000, 'STANDARD'),
                    ('FOLLOWER-001', 5000, 5000, 'STANDARD')
                """
            )

    def tearDown(self):
        self.maintenance_window_patch.stop()
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_copy_tables_are_created(self):
        with database.get_connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                    """
                ).fetchall()
            }

        self.assertIn("copy_relationships", tables)
        self.assertIn("copy_executions", tables)

    def test_create_copy_relationship(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_relationships (
                    relationship_id,
                    source_account_id,
                    follower_account_id
                )
                VALUES ('REL-001', 'SOURCE-001', 'FOLLOWER-001')
                """
            )

            relationship = connection.execute(
                """
                SELECT *
                FROM copy_relationships
                WHERE relationship_id = 'REL-001'
                """
            ).fetchone()

        self.assertIsNotNone(relationship)
        self.assertEqual(relationship["status"], "ACTIVE")

    def test_self_follow_is_rejected(self):
        with database.get_connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO copy_relationships (
                        relationship_id,
                        source_account_id,
                        follower_account_id
                    )
                    VALUES ('REL-SELF', 'SOURCE-001', 'SOURCE-001')
                    """
                )

    def test_duplicate_relationship_is_rejected(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_relationships (
                    relationship_id,
                    source_account_id,
                    follower_account_id
                )
                VALUES ('REL-001', 'SOURCE-001', 'FOLLOWER-001')
                """
            )

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO copy_relationships (
                        relationship_id,
                        source_account_id,
                        follower_account_id
                    )
                    VALUES ('REL-002', 'SOURCE-001', 'FOLLOWER-001')
                    """
                )

    def test_invalid_relationship_status_is_rejected(self):
        with database.get_connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO copy_relationships (
                        relationship_id,
                        source_account_id,
                        follower_account_id,
                        status
                    )
                    VALUES (
                        'REL-INVALID',
                        'SOURCE-001',
                        'FOLLOWER-001',
                        'INVALID'
                    )
                    """
                )

    def test_relationship_requires_existing_accounts(self):
        with database.get_connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO copy_relationships (
                        relationship_id,
                        source_account_id,
                        follower_account_id
                    )
                    VALUES (
                        'REL-UNKNOWN',
                        'UNKNOWN-SOURCE',
                        'FOLLOWER-001'
                    )
                    """
                )

    def test_copy_execution_requires_unique_source_trade_per_relationship(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_relationships (
                    relationship_id,
                    source_account_id,
                    follower_account_id
                )
                VALUES ('REL-001', 'SOURCE-001', 'FOLLOWER-001')
                """
            )

            connection.execute(
                """
                INSERT INTO trades (
                    trade_id,
                    account_id,
                    client_order_id,
                    symbol,
                    side,
                    quantity,
                    entry_price,
                    stop_loss,
                    status,
                    risk_amount
                )
                VALUES (
                    'TRADE-001',
                    'SOURCE-001',
                    'ORDER-001',
                    'EURUSD',
                    'BUY',
                    0.1,
                    1.1,
                    1.09,
                    'OPEN',
                    10
                )
                """
            )

            connection.execute(
                """
                INSERT INTO copy_executions (
                    copy_execution_id,
                    relationship_id,
                    source_trade_id,
                    status
                )
                VALUES (
                    'COPY-001',
                    'REL-001',
                    'TRADE-001',
                    'PENDING'
                )
                """
            )

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO copy_executions (
                        copy_execution_id,
                        relationship_id,
                        source_trade_id,
                        status
                    )
                    VALUES (
                        'COPY-002',
                        'REL-001',
                        'TRADE-001',
                        'PENDING'
                    )
                    """
                )

    def test_invalid_copy_execution_status_is_rejected(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_relationships (
                    relationship_id,
                    source_account_id,
                    follower_account_id
                )
                VALUES ('REL-001', 'SOURCE-001', 'FOLLOWER-001')
                """
            )

            connection.execute(
                """
                INSERT INTO trades (
                    trade_id,
                    account_id,
                    client_order_id,
                    symbol,
                    side,
                    quantity,
                    entry_price,
                    stop_loss,
                    status,
                    risk_amount
                )
                VALUES (
                    'TRADE-001',
                    'SOURCE-001',
                    'ORDER-001',
                    'EURUSD',
                    'BUY',
                    0.1,
                    1.1,
                    1.09,
                    'OPEN',
                    10
                )
                """
            )

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO copy_executions (
                        copy_execution_id,
                        relationship_id,
                        source_trade_id,
                        status
                    )
                    VALUES (
                        'COPY-INVALID',
                        'REL-001',
                        'TRADE-001',
                        'INVALID'
                    )
                    """
                )


    def create_relationship(self, relationship_id="REL-001"):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_relationships (
                    relationship_id,
                    source_account_id,
                    follower_account_id
                )
                VALUES (?, 'SOURCE-001', 'FOLLOWER-001')
                """,
                (relationship_id,)
            )
        return relationship_id

    def open_trade(self, account_id, order_id):
        return open_paper_trade(
            account_id=account_id,
            client_order_id=order_id,
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            symbol="XAUUSD"
        )

    def test_repeated_copy_attempt_does_not_create_duplicate_trade(self):
        self.create_relationship()
        source = self.open_trade("SOURCE-001", "SOURCE-ORDER-001")

        first = replicate_open_trade(source["trade_id"])
        second = replicate_open_trade(source["trade_id"])

        self.assertEqual(first["successful_copies"], 1)
        self.assertEqual(second["successful_copies"], 0)
        self.assertEqual(second["copies"][0]["status"], "DUPLICATE")
        self.assertTrue(second["copies"][0]["duplicate_prevented"])

        with database.get_connection() as connection:
            copies = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE account_id = 'FOLLOWER-001'
                  AND copied_from_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()["total"]
            executions = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()["total"]

        self.assertEqual(copies, 1)
        self.assertEqual(executions, 1)

    def test_follower_risk_is_checked_independently(self):
        self.create_relationship()
        self.open_trade("FOLLOWER-001", "FOLLOWER-ORDER-001")
        self.open_trade("FOLLOWER-001", "FOLLOWER-ORDER-002")
        source = self.open_trade("SOURCE-001", "SOURCE-ORDER-RISK")

        result = replicate_open_trade(source["trade_id"])

        self.assertEqual(result["successful_copies"], 0)
        self.assertEqual(result["failed_copies"], 1)
        self.assertEqual(result["copies"][0]["status"], "FAILED")
        self.assertIn("combined open risk", result["copies"][0]["failure_reason"])

        with database.get_connection() as connection:
            follower_copies = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE account_id = 'FOLLOWER-001'
                  AND copied_from_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()["total"]

        self.assertEqual(follower_copies, 0)

    def test_pause_resume_and_stop_control_new_copies(self):
        relationship_id = self.create_relationship()
        source = self.open_trade("SOURCE-001", "SOURCE-ORDER-STATUS")

        paused = update_copy_relationship_status(relationship_id, "PAUSED")
        self.assertEqual(paused["status"], "PAUSED")

        paused_result = replicate_open_trade(source["trade_id"])
        self.assertEqual(paused_result["eligible_followers"], 0)
        self.assertEqual(paused_result["successful_copies"], 0)

        resumed = update_copy_relationship_status(relationship_id, "ACTIVE")
        self.assertEqual(resumed["status"], "ACTIVE")

        resumed_result = replicate_open_trade(source["trade_id"])
        self.assertEqual(resumed_result["successful_copies"], 1)

        stopped = update_copy_relationship_status(relationship_id, "STOPPED")
        self.assertEqual(stopped["status"], "STOPPED")

        with self.assertRaises(ValueError):
            update_copy_relationship_status(relationship_id, "ACTIVE")

    def test_closing_source_updates_copy_execution_status(self):
        self.create_relationship()
        source = self.open_trade("SOURCE-001", "SOURCE-ORDER-CLOSE")
        copied = replicate_open_trade(source["trade_id"])
        self.assertEqual(copied["successful_copies"], 1)

        result = close_trade_with_copies(source["trade_id"], 4010)
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["closed_copies"], 1)

        with database.get_connection() as connection:
            execution = connection.execute(
                """
                SELECT status
                FROM copy_executions
                WHERE source_trade_id = ?
                """,
                (source["trade_id"],)
            ).fetchone()
            copied_trade = connection.execute(
                """
                SELECT status
                FROM trades
                WHERE trade_id = ?
                """,
                (copied["copies"][0]["copied_trade_id"],)
            ).fetchone()

        self.assertEqual(execution["status"], "CLOSED")
        self.assertEqual(copied_trade["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
 
