import tempfile
import unittest
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_provisioning_store import ensure_schema, load_or_create, update
from app.broker_bridge_reconciliation import (
    ReconciliationInspectionError,
    list_provisioning_operations,
    preflight_provisioning_operation,
)
from app.database import init_db


class BrokerBridgeReconciliationInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "reconciliation-inspection.db"
        init_db()
        participant = register_user(
            "reconciliation-participant@taimobot.local",
            "ReconcileParticipantPassphrase-2026!",
        )
        admin = register_user(
            "reconciliation-admin@taimobot.local",
            "ReconcileAdminPassphrase-2026!",
        )
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
                    "reconciliation-test-connection", participant["user_id"],
                    "2026-11-27T10:00:00+00:00",
                    "2026-11-27T10:00:00+00:00",
                ),
            )
        ensure_schema()
        self.operation_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        load_or_create(
            self.operation_id,
            "reconciliation-test-connection",
            self.admin_id,
        )
        update(
            self.operation_id,
            "SECRET_CREATED",
            secret_ref="vault://test/reconciliation-secret",
            error_code="TEST_ONLY",
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_inventory_is_bounded_and_never_exposes_secret_reference(self):
        rows = list_provisioning_operations(actor_id=self.admin_id, limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["operation_id"], self.operation_id)
        self.assertNotIn("secret_ref", rows[0])
        self.assertNotIn("key_id", rows[0])

    def test_preflight_is_dry_run_and_does_not_expose_secret_reference(self):
        result = preflight_provisioning_operation(
            actor_id=self.admin_id,
            operation_id=self.operation_id,
        )
        self.assertEqual(result["classification"], "REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(result["proposed_action"], "DRY_RUN_ONLY")
        self.assertFalse(result["mutated"])
        self.assertFalse(result["vault_contacted"])
        self.assertFalse(result["secret_reference_exposed"])
        self.assertFalse(result["secret_material_exposed"])
        self.assertNotIn("vault://test/reconciliation-secret", repr(result))
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status, secret_ref FROM broker_bridge_provisioning_operations WHERE operation_id = ?",
                (self.operation_id,),
            ).fetchone()
        self.assertEqual(row["status"], "SECRET_CREATED")
        self.assertEqual(row["secret_ref"], "vault://test/reconciliation-secret")

    def test_any_key_metadata_reference_blocks_deletion_even_if_revoked(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO broker_bridge_keys (
                    key_id, connection_id, secret_ref, status, rotation_sequence,
                    created_at, activated_at, revoked_at, created_by
                ) VALUES (?, ?, ?, 'REVOKED', 1, ?, ?, ?, ?)
                """,
                (
                    "reconciliation-key", "reconciliation-test-connection",
                    "vault://test/reconciliation-secret",
                    "2026-11-27T10:00:00+00:00",
                    "2026-11-27T10:00:00+00:00",
                    "2026-11-27T11:00:00+00:00", self.admin_id,
                ),
            )
        result = preflight_provisioning_operation(
            actor_id=self.admin_id,
            operation_id=self.operation_id,
        )
        self.assertEqual(result["classification"], "BLOCKED_KEY_METADATA_REFERENCE")
        self.assertEqual(result["proposed_action"], "DO_NOT_DELETE")

    def test_unverified_or_non_admin_actor_is_rejected(self):
        with database.get_connection() as connection:
            participant = connection.execute(
                "SELECT user_id FROM users WHERE email = ?",
                ("reconciliation-participant@taimobot.local",),
            ).fetchone()
        with self.assertRaises(ReconciliationInspectionError):
            list_provisioning_operations(actor_id=participant["user_id"])
        with self.assertRaises(ReconciliationInspectionError):
            preflight_provisioning_operation(
                actor_id="not-a-real-user",
                operation_id=self.operation_id,
            )

    def test_invalid_limit_and_unknown_operation_fail_closed(self):
        with self.assertRaises(ReconciliationInspectionError):
            list_provisioning_operations(actor_id=self.admin_id, limit=True)
        with self.assertRaises(ReconciliationInspectionError):
            preflight_provisioning_operation(
                actor_id=self.admin_id,
                operation_id="missing-operation",
            )


if __name__ == "__main__":
    unittest.main()
