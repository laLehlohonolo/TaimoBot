
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app import database
from app.copy_trading import (
    close_trade_with_copies, close_position_group_with_copies,
    replicate_open_trade
)
from app.execution import (
    open_paper_trade, create_position_group, update_copy_relationship_status
)
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

    def test_closing_source_fails_uncommitted_pending_copy_and_releases_lease(self):
        relationship_id = self.create_relationship("REL-PENDING-CLOSE")
        source = self.open_trade("SOURCE-001", "SOURCE-ORDER-PENDING-CLOSE")

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO copy_executions (
                    copy_execution_id, relationship_id, source_trade_id,
                    status, lease_token, lease_expires_at
                )
                VALUES (
                    'COPY-PENDING-CLOSE', ?, ?, 'PENDING',
                    'stale-worker-token', datetime('now', '+5 minutes')
                )
                """,
                (relationship_id, source["trade_id"])
            )

        result = close_trade_with_copies(source["trade_id"], 4010)
        self.assertEqual(result["status"], "CLOSED")

        with database.get_connection() as connection:
            execution = connection.execute(
                """
                SELECT status, failure_reason, lease_token, lease_expires_at
                FROM copy_executions
                WHERE relationship_id = ?
                  AND source_trade_id = ?
                """,
                (relationship_id, source["trade_id"])
            ).fetchone()

        self.assertEqual(execution["status"], "FAILED")
        self.assertIn("Source trade closed", execution["failure_reason"])
        self.assertIsNone(execution["lease_token"])
        self.assertIsNone(execution["lease_expires_at"])

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


    def test_source_close_persists_costs_without_duplicating_them_to_copy(self):
        self.create_relationship()
        source = self.open_trade("SOURCE-001", "SOURCE-COST-ORDER")
        copied = replicate_open_trade(source["trade_id"])
        self.assertEqual(copied["successful_copies"], 1)

        result = close_trade_with_copies(
            source["trade_id"],
            4010,
            spread_cost=0.10,
            commission_cost=0.20,
            swap_cost=0.05,
            other_costs=0.01,
        )
        self.assertEqual(result["status"], "CLOSED")

        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, account_id, realized_pnl,
                       gross_realized_pnl, spread_cost,
                       commission_cost, swap_cost, other_costs
                FROM trades
                WHERE trade_id IN (?, ?)
                ORDER BY account_id
                """,
                (source["trade_id"], copied["copies"][0]["copied_trade_id"])
            ).fetchall()
            balances = {
                row["account_id"]: row["balance"]
                for row in connection.execute(
                    "SELECT account_id, balance FROM paper_accounts"
                ).fetchall()
            }

        source_row = next(row for row in rows if row["account_id"] == "SOURCE-001")
        copy_row = next(row for row in rows if row["account_id"] == "FOLLOWER-001")
        self.assertAlmostEqual(
            source_row["realized_pnl"],
            source_row["gross_realized_pnl"] - 0.36
        )
        self.assertAlmostEqual(source_row["spread_cost"], 0.10)
        self.assertAlmostEqual(source_row["commission_cost"], 0.20)
        self.assertAlmostEqual(source_row["swap_cost"], 0.05)
        self.assertAlmostEqual(source_row["other_costs"], 0.01)
        self.assertEqual(copy_row["realized_pnl"], copy_row["gross_realized_pnl"])
        self.assertEqual(copy_row["spread_cost"], 0)
        self.assertEqual(copy_row["commission_cost"], 0)
        self.assertEqual(copy_row["swap_cost"], 0)
        self.assertEqual(copy_row["other_costs"], 0)
        self.assertAlmostEqual(
            balances["SOURCE-001"],
            10000 + source_row["realized_pnl"]
        )
        self.assertAlmostEqual(
            balances["FOLLOWER-001"],
            5000 + copy_row["realized_pnl"]
        )


    def test_group_close_allocates_costs_by_quantity_and_not_to_copies(self):
        self.create_relationship("REL-GROUP-COSTS")
        group = create_position_group("SOURCE-001", "XAUUSD", "BUY")
        first = open_paper_trade(
            account_id="SOURCE-001",
            client_order_id="GROUP-COST-ORDER-1",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            symbol="XAUUSD",
            position_group_id=group["group_id"],
        )
        second = open_paper_trade(
            account_id="SOURCE-001",
            client_order_id="GROUP-COST-ORDER-2",
            side="BUY",
            entry_price=4000,
            stop_loss=3980,
            symbol="XAUUSD",
            position_group_id=group["group_id"],
        )
        first_copy = replicate_open_trade(first["trade_id"])
        second_copy = replicate_open_trade(second["trade_id"])
        self.assertEqual(first_copy["successful_copies"], 1)
        self.assertEqual(second_copy["successful_copies"], 1)

        close_position_group_with_copies(
            group["group_id"],
            4010,
            spread_cost=0.30,
            commission_cost=0.06,
            swap_cost=0.03,
            other_costs=0.01,
        )

        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, account_id, copied_from_trade_id,
                       quantity, gross_realized_pnl, realized_pnl,
                       spread_cost, commission_cost, swap_cost, other_costs
                FROM trades
                WHERE trade_id IN (?, ?, ?, ?)
                """,
                (
                    first["trade_id"],
                    second["trade_id"],
                    first_copy["copies"][0]["copied_trade_id"],
                    second_copy["copies"][0]["copied_trade_id"],
                )
            ).fetchall()
            balances = {
                row["account_id"]: row["balance"]
                for row in connection.execute(
                    "SELECT account_id, balance FROM paper_accounts"
                ).fetchall()
            }

        source_rows = [row for row in rows if row["copied_from_trade_id"] is None]
        copy_rows = [row for row in rows if row["copied_from_trade_id"] is not None]
        self.assertEqual(len(source_rows), 2)
        self.assertEqual(len(copy_rows), 2)

        total_quantity = sum(row["quantity"] for row in source_rows)
        self.assertAlmostEqual(sum(row["spread_cost"] for row in source_rows), 0.30)
        self.assertAlmostEqual(sum(row["commission_cost"] for row in source_rows), 0.06)
        self.assertAlmostEqual(sum(row["swap_cost"] for row in source_rows), 0.03)
        self.assertAlmostEqual(sum(row["other_costs"] for row in source_rows), 0.01)

        for row in source_rows:
            expected_spread = 0.30 * row["quantity"] / total_quantity
            self.assertAlmostEqual(row["spread_cost"], expected_spread)
            self.assertAlmostEqual(
                row["realized_pnl"],
                row["gross_realized_pnl"]
                - row["spread_cost"]
                - row["commission_cost"]
                - row["swap_cost"]
                - row["other_costs"],
            )

        for row in copy_rows:
            self.assertEqual(row["realized_pnl"], row["gross_realized_pnl"])
            self.assertEqual(row["spread_cost"], 0)
            self.assertEqual(row["commission_cost"], 0)
            self.assertEqual(row["swap_cost"], 0)
            self.assertEqual(row["other_costs"], 0)

        source_net = sum(row["realized_pnl"] for row in source_rows)
        copy_net = sum(row["realized_pnl"] for row in copy_rows)
        self.assertAlmostEqual(balances["SOURCE-001"], 10000 + source_net)
        self.assertAlmostEqual(balances["FOLLOWER-001"], 5000 + copy_net)


    def test_group_close_rolls_back_all_trades_balances_and_copy_records_on_midflight_failure(self):
        self.create_relationship("REL-GROUP-ROLLBACK")
        group = create_position_group("SOURCE-001", "XAUUSD", "BUY")
        first = open_paper_trade(
            account_id="SOURCE-001",
            client_order_id="GROUP-ROLLBACK-ORDER-1",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            symbol="XAUUSD",
            position_group_id=group["group_id"],
        )
        second = open_paper_trade(
            account_id="SOURCE-001",
            client_order_id="GROUP-ROLLBACK-ORDER-2",
            side="BUY",
            entry_price=4000,
            stop_loss=3980,
            symbol="XAUUSD",
            position_group_id=group["group_id"],
        )
        first_copy = replicate_open_trade(first["trade_id"])
        second_copy = replicate_open_trade(second["trade_id"])
        self.assertEqual(first_copy["successful_copies"], 1)
        self.assertEqual(second_copy["successful_copies"], 1)

        with database.get_connection() as connection:
            before_balances = {
                row["account_id"]: row["balance"]
                for row in connection.execute(
                    "SELECT account_id, balance FROM paper_accounts"
                ).fetchall()
            }
            before_executions = [
                tuple(row) for row in connection.execute(
                    """
                    SELECT source_trade_id, status, lease_token, lease_expires_at
                    FROM copy_executions
                    WHERE source_trade_id IN (?, ?)
                    ORDER BY source_trade_id
                    """,
                    (first["trade_id"], second["trade_id"])
                ).fetchall()
            ]
            connection.execute(
                """
                CREATE TRIGGER fail_second_group_trade_close
                BEFORE UPDATE OF status ON trades
                WHEN OLD.trade_id = ?
                  AND NEW.status = 'CLOSED'
                BEGIN
                    SELECT RAISE(ABORT, 'injected mid-flight close failure');
                END
                """,
                (second["trade_id"],)
            )

        with self.assertRaises(sqlite3.IntegrityError):
            close_position_group_with_copies(
                group["group_id"],
                4010,
                spread_cost=0.30,
                commission_cost=0.06,
            )

        with database.get_connection() as connection:
            trade_states = [
                tuple(row) for row in connection.execute(
                    """
                    SELECT trade_id, status, realized_pnl, gross_realized_pnl,
                           spread_cost, commission_cost
                    FROM trades
                    WHERE trade_id IN (?, ?, ?, ?)
                    ORDER BY trade_id
                    """,
                    (
                        first["trade_id"],
                        second["trade_id"],
                        first_copy["copies"][0]["copied_trade_id"],
                        second_copy["copies"][0]["copied_trade_id"],
                    )
                ).fetchall()
            ]
            after_balances = {
                row["account_id"]: row["balance"]
                for row in connection.execute(
                    "SELECT account_id, balance FROM paper_accounts"
                ).fetchall()
            }
            group_status = connection.execute(
                "SELECT status FROM position_groups WHERE group_id = ?",
                (group["group_id"],)
            ).fetchone()["status"]
            after_executions = [
                tuple(row) for row in connection.execute(
                    """
                    SELECT source_trade_id, status, lease_token, lease_expires_at
                    FROM copy_executions
                    WHERE source_trade_id IN (?, ?)
                    ORDER BY source_trade_id
                    """,
                    (first["trade_id"], second["trade_id"])
                ).fetchall()
            ]

        self.assertEqual(len(trade_states), 4)
        self.assertTrue(all(row[1] == "OPEN" for row in trade_states))
        self.assertTrue(all(row[2] == 0 for row in trade_states))
        self.assertTrue(all(row[3] is None for row in trade_states))
        self.assertTrue(all(row[4] == 0 for row in trade_states))
        self.assertTrue(all(row[5] == 0 for row in trade_states))
        self.assertEqual(after_balances, before_balances)
        self.assertEqual(group_status, "OPEN")
        self.assertEqual(after_executions, before_executions)


if __name__ == "__main__":
    unittest.main()
 
