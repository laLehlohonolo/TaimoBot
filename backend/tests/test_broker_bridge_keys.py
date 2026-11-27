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
)
from app.database import init_db


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
        with database.get_connection() as connection:
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
            created_by=self.participant_id,
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
            "bridge-key-connection", "vault://taimobot/bridge/key-one"
        )
        second = activate_bridge_key(
            "bridge-key-connection", "vault://taimobot/bridge/key-two"
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
            "bridge-key-connection", "vault://taimobot/bridge/key-one"
        )
        self.assertFalse(revoke_bridge_key("another-connection", key["key_id"]))
        self.assertTrue(revoke_bridge_key("bridge-key-connection", key["key_id"]))
        self.assertFalse(revoke_bridge_key("bridge-key-connection", key["key_id"]))
        self.assertIsNone(get_active_bridge_key_metadata("bridge-key-connection"))

    def test_paused_or_unknown_connection_cannot_receive_a_key(self):
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE participant_broker_connections SET state='PAUSED' "
                "WHERE connection_id = ?",
                ("bridge-key-connection",),
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "active requested connection"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-one"
            )
        with self.assertRaisesRegex(BridgeKeyMetadataError, "active requested connection"):
            activate_bridge_key(
                "missing-connection", "vault://taimobot/bridge/key-two"
            )

    def test_invalid_secret_reference_and_naive_time_fail_closed(self):
        with self.assertRaisesRegex(BridgeKeyMetadataError, "opaque secret-manager reference"):
            activate_bridge_key("bridge-key-connection", "plaintext-secret-material")
        with self.assertRaisesRegex(BridgeKeyMetadataError, "timezone-aware"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-three",
                now="2026-11-27T12:00:00",
            )

    def test_unknown_provisioning_actor_is_rejected(self):
        with self.assertRaisesRegex(BridgeKeyMetadataError, "actor is unknown"):
            activate_bridge_key(
                "bridge-key-connection", "vault://taimobot/bridge/key-four",
                created_by="not-a-real-user",
            )


if __name__ == "__main__":
    unittest.main()
