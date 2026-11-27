import unittest
from unittest.mock import patch

from app.broker_bridge_vault import (
    BridgeSecretUnavailable,
    load_bridge_key_secret,
)


class FakeSecretReader:
    def __init__(self, value=b"x" * 32, error=None):
        self.value = value
        self.error = error
        self.references = []

    def get_secret(self, secret_ref):
        self.references.append(secret_ref)
        if self.error:
            raise self.error
        return self.value


class BrokerBridgeVaultBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.context = {
            "key_id": "key-123",
            "connection_id": "connection-123",
            "participant_user_id": "participant-123",
            "secret_ref": "vault://taimobot/bridge/key-123",
        }

    @patch("app.broker_bridge_vault.resolve_bridge_key_auth_context")
    def test_reads_only_server_resolved_reference_and_returns_no_cache(self, resolve):
        resolve.return_value = self.context
        reader = FakeSecretReader()

        result = load_bridge_key_secret("key-123", reader)

        self.assertEqual(resolve.call_count, 2)
        self.assertEqual(reader.references, [self.context["secret_ref"]])
        self.assertEqual(result["connection_id"], self.context["connection_id"])
        self.assertEqual(result["participant_user_id"], self.context["participant_user_id"])
        self.assertEqual(result["shared_secret"], b"x" * 32)
        self.assertFalse(result["secret_cached"])
        self.assertNotIn("secret_ref", result)

    @patch("app.broker_bridge_vault.resolve_bridge_key_auth_context")
    def test_unknown_or_revoked_key_fails_before_vault_read(self, resolve):
        resolve.return_value = None
        reader = FakeSecretReader()

        with self.assertRaises(BridgeSecretUnavailable):
            load_bridge_key_secret("unknown", reader)

        self.assertEqual(reader.references, [])

    @patch("app.broker_bridge_vault.resolve_bridge_key_auth_context")
    def test_vault_outage_fails_closed_without_leaking_adapter_error(self, resolve):
        resolve.return_value = self.context
        reader = FakeSecretReader(error=RuntimeError("sensitive vault diagnostic"))

        with self.assertRaises(BridgeSecretUnavailable) as raised:
            load_bridge_key_secret("key-123", reader)

        self.assertNotIn("sensitive vault diagnostic", str(raised.exception))

    @patch("app.broker_bridge_vault.resolve_bridge_key_auth_context")
    def test_short_or_non_bytes_secret_fails_closed(self, resolve):
        resolve.return_value = self.context
        for value in (b"short", "not-bytes", None):
            with self.subTest(value=value):
                with self.assertRaises(BridgeSecretUnavailable):
                    load_bridge_key_secret("key-123", FakeSecretReader(value=value))

    @patch("app.broker_bridge_vault.resolve_bridge_key_auth_context")
    def test_rotation_or_revocation_during_vault_read_fails_closed(self, resolve):
        changed = {**self.context, "key_id": "new-key"}
        resolve.side_effect = [self.context, changed]
        reader = FakeSecretReader()

        with self.assertRaises(BridgeSecretUnavailable):
            load_bridge_key_secret("key-123", reader)

        self.assertEqual(resolve.call_count, 2)

    @patch("app.broker_bridge_vault.resolve_bridge_key_auth_context")
    def test_untrusted_or_malformed_vault_reference_fails_before_read(self, resolve):
        resolve.return_value = {**self.context, "secret_ref": "https://attacker.example/secret"}
        reader = FakeSecretReader()

        with self.assertRaises(BridgeSecretUnavailable):
            load_bridge_key_secret("key-123", reader)

        self.assertEqual(reader.references, [])


if __name__ == "__main__":
    unittest.main()
