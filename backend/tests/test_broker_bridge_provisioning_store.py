import tempfile
import unittest
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_provisioning_store import (
    claim_activation,
    ensure_schema,
    load_or_create,
    update,
)
from app.database import init_db


class ProvisioningActivationLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "provisioning-leases.db"
        init_db()
        participant = register_user(
            "lease-owner@taimobot.local",
            "LeaseOwnerPassphrase-2026!",
        )
        admin = register_user(
            "lease-admin@taimobot.local",
            "LeaseAdminPassphrase-2026!",
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
                    "lease-test-connection", participant["user_id"],
                    "2026-11-27T10:00:00+00:00",
                    "2026-11-27T10:00:00+00:00",
                ),
            )
        ensure_schema()
        load_or_create(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "lease-test-connection",
            self.admin_id,
        )
        update(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "SECRET_CREATED",
            secret_ref="vault://test/lease",
        )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_only_one_worker_can_claim_activation(self):
        operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.assertTrue(claim_activation(operation_id))
        self.assertFalse(claim_activation(operation_id))

    def test_expired_activation_lease_can_be_reclaimed(self):
        operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        self.assertTrue(claim_activation(operation_id))
        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE broker_bridge_provisioning_operations
                SET lease_expires_at = '2000-01-01T00:00:00+00:00'
                WHERE operation_id = ?
                """,
                (operation_id,),
            )
        self.assertTrue(claim_activation(operation_id))
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status, lease_expires_at FROM broker_bridge_provisioning_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        self.assertEqual(row["status"], "ACTIVATING")
        self.assertGreater(row["lease_expires_at"], "2000-01-01T00:00:00+00:00")

    def test_terminal_state_cannot_be_claimed(self):
        operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        update(operation_id, "FAILED", error_code="TEST_TERMINAL")
        self.assertFalse(claim_activation(operation_id))


if __name__ == "__main__":
    unittest.main()
