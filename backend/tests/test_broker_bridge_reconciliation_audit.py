import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_provisioning_store import load_or_create, update
from app.broker_bridge_reconciliation_audit import (
    ReconciliationAuditError,
    append_event,\n    list_events,
)
from app.database import init_db


class BrokerBridgeReconciliationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "reconciliation-audit.db"
        init_db()
        participant = register_user(
            "audit-participant@taimobot.local",
            "AuditParticipantPassphrase-2026!",
        )
        admin = register_user(
            "audit-admin@taimobot.local",
            "AuditAdminPassphrase-2026!",
        )
        self.participant_id = participant["user_id"]
        self.admin_id = admin["user_id"]
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET role='ADMIN', email_verified=1 WHERE user_id = ?",
                (self.admin_id,),
            )
            connection.execute(
                """
                INSERT INTO participant_broker_connections (
                    connection_id, participant_user_id, broker_id, platform,
                    state, created_at, updated_at
                ) VALUES (?, ?, 'exness', 'MT5', 'REQUESTED', ?, ?)
                """,
                (
                    "audit-test-connection", self.participant_id,
                    "2026-11-27T10:00:00+00:00",
                    "2026-11-27T10:00:00+00:00",
                ),
            )
            self.operation_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        load_or_create(
            self.operation_id, "audit-test-connection", self.admin_id
        )
        update(
            self.operation_id, "SECRET_CREATED",
            secret_ref="vault://test/audit-secret",
            error_code="TEST_ONLY",
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_append_records_operator_decision_and_does_not_mutate_operation(self):
        event = append_event(
            actor_id=self.admin_id,
            operation_id=self.operation_id,
            reason="Reviewed stale provisioning record before escalation",
            action="OPERATOR_REVIEW",
            classification="EXPIRED_ACTIVATION_LEASE_REQUIRES_REVIEW",
        )
        self.assertEqual(event["outcome"], "RECORDED")
        self.assertFalse(event["secret_material_exposed"])
        with database.get_connection() as connection:
            operation = connection.execute(
                "SELECT status, secret_ref FROM broker_bridge_provisioning_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM broker_bridge_reconciliation_audit WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchall()
        self.assertEqual(operation["status"], "SECRET_CREATED")
        self.assertEqual(operation["secret_ref"], "vault://test/audit-secret")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor_user_id"], self.admin_id)

    def test_application_database_bootstrap_creates_provisioning_and_audit_tables(self):
        with database.get_connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertIn("broker_bridge_provisioning_operations", tables)
        self.assertIn("broker_bridge_reconciliation_audit", tables)

    def test_sensitive_reason_fragments_are_redacted_before_persistence(self):
        event = append_event(
            actor_id=self.admin_id,
            operation_id=self.operation_id,
            reason="Reviewed vault://private/secret Bearer abcdef0123456789",
            action="DRY_RUN",
            classification="REQUIRES_OPERATOR_REVIEW",
        )
        self.assertNotIn("vault://private/secret", event["reason"])
        self.assertNotIn("Bearer abcdef0123456789", event["reason"])
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT reason FROM broker_bridge_reconciliation_audit WHERE event_id = ?",
                (event["event_id"],),
            ).fetchone()
        self.assertNotIn("vault://private/secret", row["reason"])
        self.assertNotIn("abcdef0123456789", row["reason"])

    def test_non_admin_and_unverified_admin_are_rejected(self):
        with self.assertRaises(ReconciliationAuditError):
            append_event(
                actor_id=self.participant_id,
                operation_id=self.operation_id,
                reason="Review",
                action="INSPECT",
                classification="REQUIRES_OPERATOR_REVIEW",
            )
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET email_verified=0 WHERE user_id = ?",
                (self.admin_id,),
            )
        with self.assertRaises(ReconciliationAuditError):
            append_event(
                actor_id=self.admin_id,
                operation_id=self.operation_id,
                reason="Review",
                action="INSPECT",
                classification="REQUIRES_OPERATOR_REVIEW",
            )

    def test_audit_rows_cannot_be_updated_or_deleted(self):
        event = append_event(
            actor_id=self.admin_id,
            operation_id=self.operation_id,
            reason="Immutable ledger test",
            action="INSPECT",
            classification="REQUIRES_OPERATOR_REVIEW",
        )
        with database.get_connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE broker_bridge_reconciliation_audit SET reason='changed' WHERE event_id = ?",
                    (event["event_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM broker_bridge_reconciliation_audit WHERE event_id = ?",
                    (event["event_id"],),
                )

    def test_invalid_action_reason_and_unknown_operation_fail_closed(self):
        invalid_cases = (
            {"action": "DELETE_SECRET", "reason": "Review", "operation_id": self.operation_id},
            {"action": "INSPECT", "reason": "", "operation_id": self.operation_id},
            {"action": "INSPECT", "reason": "x" * 301, "operation_id": self.operation_id},
            {"action": "INSPECT", "reason": "Review", "operation_id": "missing-operation"},
        )
        for case in invalid_cases:
            with self.subTest(case=case), self.assertRaises(ReconciliationAuditError):
                append_event(
                    actor_id=self.admin_id,
                    classification="REQUIRES_OPERATOR_REVIEW",
                    **case,
                )



    def test_history_is_admin_only_bounded_and_excludes_secret_reference_fields(self):
        for index in range(3):
            append_event(
                actor_id=self.admin_id,
                operation_id=self.operation_id,
                reason=f"Reviewed event {index}",
                action="INSPECT",
                classification="REQUIRES_OPERATOR_REVIEW",
            )
        page = list_events(actor_id=self.admin_id, operation_id=self.operation_id, limit=2)
        self.assertEqual(len(page["events"]), 2)
        self.assertIsNotNone(page["next_cursor"])
        self.assertNotIn("secret_ref", page["events"][0])
        self.assertNotIn("secret_material", page["events"][0])
        with self.assertRaises(ReconciliationAuditError):
            list_events(actor_id=self.participant_id, limit=2)
        with self.assertRaises(ReconciliationAuditError):
            list_events(actor_id=self.admin_id, limit=101)

    def test_history_pagination_is_stable_and_empty_filter_returns_no_rows(self):
        for index in range(3):
            append_event(
                actor_id=self.admin_id,
                operation_id=self.operation_id,
                reason=f"Page event {index}",
                action="INSPECT",
                classification="REQUIRES_OPERATOR_REVIEW",
            )
        first = list_events(actor_id=self.admin_id, limit=2)
        cursor = first["next_cursor"]
        second = list_events(
            actor_id=self.admin_id,
            limit=2,
            before_created_at=cursor["before_created_at"],
            before_event_id=cursor["before_event_id"],
        )
        first_ids = {event["event_id"] for event in first["events"]}
        second_ids = {event["event_id"] for event in second["events"]}
        self.assertTrue(first_ids.isdisjoint(second_ids))
        empty = list_events(
            actor_id=self.admin_id,
            operation_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        )
        self.assertEqual(empty["events"], [])
        self.assertIsNone(empty["next_cursor"])
        with self.assertRaises(ReconciliationAuditError):
            list_events(
                actor_id=self.admin_id,
                before_created_at="2026-11-28 10:00:00",
            )

if __name__ == "__main__":
    unittest.main()
