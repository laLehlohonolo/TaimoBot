
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database


class CopyTradingDatabaseTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test_taimobot.db"
        database.init_db()

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


if __name__ == "__main__":
    unittest.main()
 
