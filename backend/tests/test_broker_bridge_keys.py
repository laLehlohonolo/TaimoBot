import tempfile
import unittest
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_keys import (
    BridgeKeyMetadataError,
    activate_bridge_key,
    get_active_bridge_key_metadata,
    revoke_bridge_key,
    resolve_bridge_key_auth_context,
)
from app.database import init_db
from app.broker_bridge_provisioning import (
    BridgeKeyProvisioningError,
    provision_bridge_key,
)


class FakeSecretManager:
    def __init__(self, *, fail_create=False, fail_delete=False, on_create=None):
        self.secrets = {}
        self.deleted = []
        self.fail_create = fail_create
        self.fail_delete = fail_delete
        self.on_create = on_create

    def create_secret(self, *, name, value):
        if self.fail_create:
            raise RuntimeError("vault unavailable")
        ref = "vault://taimobot/test/" + str(len(self.secrets) + 1)
        self.secrets[ref] = {"name": name, "value": value}
        if self.on_create is not None:
            self.on_create()
        return ref

    def delete_secret(self, secret_ref):
        if self.fail_delete:
            raise RuntimeError("vault delete unavailable")
        self.deleted.append(secret_ref)
        self.secrets.pop(secret_ref, None)


class BrokerBridgeKeyMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "bridge-keys.db"
        init_db()
        participant = register_user(
            "bridge-key-owner@taimobot.local",
            "BridgeKeyOwnerPassphrase-2026!",
        )
        self.participant_id = participant["user_id"]
        admin = register_user(
            "bridge-key-admin@taimobot.local",
            "BridgeKeyAdminPassphrase-2026!",
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
                    "bridge-key-connection", self.participant_id,
                    "2026-11-27T10:00:00+00:00",
                    "2026-11-27T10:00:00+00:00",
                ),
            )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_activation_returns_only_non_secret_metadata(self):
        result = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-one",
            created_by=self.admin_id,
            now="2026-11-27T12:00:00+00:00",
        )
        self.assertEqual(result["status"], "ACTIVE")
        self.assertFalse(result["secret_material_returned"])
        self.assertNotIn("secret_ref", result)
        self.assertNotIn("secret", result)
        self.assertEqual(result["rotation_sequence"], 1)
        metadata = get_active_bridge_key_metadata("bridge-key-connection")
        self.assertEqual(metadata["key_id"], result["key_id"])
        self.assertNotIn("secret_ref", metadata)

    def test_rotation_atomically_revokes_previous_key(self):
        first = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-one",
            created_by=self.admin_id,
        )
        second = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-two",
            created_by=self.admin_id,
        )
        self.assertNotEqual(first["key_id"], second["key_id"])
        self.assertEqual(second["rotation_sequence"], 2)
        self.assertEqual(
            get_active_bridge_key_metadata("bridge-key-connection")["key_id"],
            second["key_id"],
        )
        with database.get_connection() as connection:
            rows = connection.execute(
                "SELECT key_id, status FROM broker_bridge_keys "
                "WHERE connection_id = ? ORDER BY rotation_sequence",
                ("bridge-key-connection",),
            ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["REVOKED", "ACTIVE"])

    def test_revocation_is_connection_scoped_and_idempotent(self):
        key = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-one",
            created_by=self.admin_id,
        )
        self.assertFalse(revoke_bridge_key("another-connection", key["key_id"]))
        self.assertTrue(revoke_bridge_key("bridge-key-connection", key["key_id"]))
        self.assertFalse(revoke_bridge_key("bridge-key-connection", key["key_id"]))
        self.assertIsNone(get_active_bridge_key_metadata("bridge-key-connection"))

    def test_paused_or_unknown_connection_cannot_receive_a_key(self):
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE participant_broker_connections SET state='PAUSED' "
                "WHERE connection_id = ?", ("bridge-key-connection",)
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "active requested connection"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-one",
                created_by=self.admin_id,
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "active requested connection"):
            activate_bridge_key(
                "missing-connection", "vault://taimobot/bridge/key-two",
                created_by=self.admin_id,
            )

    def test_invalid_secret_reference_and_naive_time_fail_closed(self):
        with self.assertRaisesRegex(BridgeKeyMetadataError, "opaque secret-manager reference"):
            activate_bridge_key(
                "bridge-key-connection", "plaintext-secret-material",
                created_by=self.admin_id,
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "timezone-aware"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-three",
                created_by=self.admin_id,
                now="2026-11-27T12:00:00",
            )

    def test_unknown_or_unverified_non_admin_provisioner_is_rejected(self):
        with self.assertRaisesRegex(BridgeKeyMetadataError, "verified administrator"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-four"
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "verified administrator"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-five",
                created_by="not-a-real-user",
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "verified administrator"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-six",
                created_by=self.participant_id,
            )
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET email_verified=0 WHERE user_id = ?",
                (self.admin_id,),
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "verified administrator"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-seven",
                created_by=self.admin_id,
            )

    def test_auth_context_is_server_resolved_and_never_returns_key_bytes(self):
        key = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-auth-context",
            created_by=self.admin_id,
        )
        context = resolve_bridge_key_auth_context(key["key_id"])
        self.assertEqual(context["connection_id"], "bridge-key-connection")
        self.assertEqual(context["participant_user_id"], self.participant_id)
        self.assertEqual(
            context["secret_ref"], "vault://taimobot/bridge/key-auth-context"
        )
        self.assertNotIn("secret", context)
        self.assertNotIn("shared_secret", context)

    def test_auth_context_rejects_unknown_revoked_and_paused_keys(self):
        key = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-auth-context-two",
            created_by=self.admin_id,
        )
        self.assertIsNone(resolve_bridge_key_auth_context("unknown-key-id"))
        self.assertTrue(revoke_bridge_key("bridge-key-connection", key["key_id"]))
        self.assertIsNone(resolve_bridge_key_auth_context(key["key_id"]))
        active = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-auth-context-three",
            created_by=self.admin_id,
        )
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE participant_broker_connections SET state='PAUSED' "
                "WHERE connection_id = ?", ("bridge-key-connection",)
            )
        self.assertIsNone(resolve_bridge_key_auth_context(active["key_id"]))


    def test_provisioning_generates_256_bit_secret_without_returning_it(self):
        manager = FakeSecretManager()
        result = provision_bridge_key(
            "bridge-key-connection", self.admin_id, manager,
            now="2026-11-27T12:00:00+00:00",
        )
        self.assertEqual(result["status"], "ACTIVE")
        self.assertNotIn("secret_ref", result)
        self.assertNotIn("secret_material", result)
        self.assertEqual(len(manager.secrets), 1)
        stored = next(iter(manager.secrets.values()))
        self.assertEqual(len(stored["value"]), 32)
        self.assertTrue(stored["name"].startswith(
            "taimobot/broker-bridge/bridge-key-connection/"
        ))

    def test_provisioning_compensates_if_connection_is_paused_during_vault_write(self):
        def pause_connection():
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE participant_broker_connections SET state='PAUSED' "
                    "WHERE connection_id = ?", ("bridge-key-connection",)
                )

        manager = FakeSecretManager(on_create=pause_connection)
        with self.assertRaisesRegex(BridgeKeyProvisioningError, "no key was activated"):
            provision_bridge_key(
                "bridge-key-connection", self.admin_id, manager
            )
        self.assertEqual(manager.secrets, {})
        self.assertEqual(len(manager.deleted), 1)

    def test_provisioning_rejects_participant_before_vault_write(self):
        manager = FakeSecretManager()
        with self.assertRaisesRegex(BridgeKeyProvisioningError, "verified administrator"):
            provision_bridge_key(
                "bridge-key-connection", self.participant_id, manager
            )
        self.assertEqual(manager.secrets, {})
        self.assertEqual(manager.deleted, [])

    def test_provisioning_rejects_unknown_connection_before_vault_write(self):
        manager = FakeSecretManager()
        with self.assertRaisesRegex(BridgeKeyProvisioningError, "active broker connection"):
            provision_bridge_key(
                "missing-connection", self.admin_id, manager
            )
        self.assertEqual(manager.secrets, {})
        self.assertEqual(manager.deleted, [])

    def test_provisioning_reports_failed_vault_compensation_without_leaking_ref(self):
        def pause_connection():
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE participant_broker_connections SET state='PAUSED' "
                    "WHERE connection_id = ?", ("bridge-key-connection",)
                )

        manager = FakeSecretManager(fail_delete=True, on_create=pause_connection)
        with self.assertRaisesRegex(BridgeKeyProvisioningError, "operator cleanup is required") as caught:
            provision_bridge_key(
                "bridge-key-connection", self.admin_id, manager
            )
        self.assertNotIn("vault://", str(caught.exception))
        self.assertEqual(len(manager.secrets), 1)

    def test_vault_creation_failure_fails_closed(self):
        manager = FakeSecretManager(fail_create=True)
        with self.assertRaisesRegex(BridgeKeyProvisioningError, "no key was activated"):
            provision_bridge_key(
                "bridge-key-connection", self.admin_id, manager
            )
        self.assertEqual(manager.secrets, {})


if __name__ == "__main__":
    unittest.main()
