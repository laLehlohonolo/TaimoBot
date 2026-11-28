import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_ingestion import (
    BridgeIngestionRequestError,
    MAX_EVIDENCE_BODY_BYTES,
    ingest_authenticated_bridge_evidence,
)
from app.broker_bridge_security import BridgeAuthenticationError, sign_bridge_message
from app.database import init_db


class FakeSecretReader:
    def __init__(self, secret):
        self.secret = secret
        self.calls = []

    def get_secret(self, secret_ref):
        self.calls.append(secret_ref)
        if secret_ref == "vault://test/bridge/one":
            return self.secret
        raise RuntimeError("private vault failure detail")


class BrokerBridgeIngestionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "bridge-service.db"
        init_db()
        self.owner = register_user(
            "bridge-service-owner@taimobot.local",
            "BridgeServiceOwnerPassphrase-2026!",
        )["user_id"]
        self.other_owner = register_user(
            "bridge-service-other@taimobot.local",
            "BridgeServiceOtherPassphrase-2026!",
        )["user_id"]
        self.connection_id = "bridge-service-connection"
        self.other_connection = "bridge-service-other-connection"
        self.key_id = "bridge-service-key"
        self.secret = b"test-only-bridge-secret-material-32-bytes-minimum"
        with database.get_connection() as connection:
            for connection_id, owner_id in (
                (self.connection_id, self.owner),
                (self.other_connection, self.other_owner),
            ):
                connection.execute(
                    """INSERT INTO participant_broker_connections
                       (connection_id, participant_user_id, broker_id, platform,
                        state, created_at, updated_at)
                       VALUES (?, ?, 'exness', 'MT5', 'REQUESTED', ?, ?)""",
                    (connection_id, owner_id, "2026-11-28T00:00:00+00:00",
                     "2026-11-28T00:00:00+00:00"),
                )
            connection.execute(
                """INSERT INTO broker_bridge_keys
                   (key_id, connection_id, secret_ref, status,
                    rotation_sequence, created_at, activated_at)
                   VALUES (?, ?, ?, 'ACTIVE', 1, ?, ?)""",
                (self.key_id, self.connection_id, "vault://test/bridge/one",
                 "2026-11-28T00:00:00+00:00", "2026-11-28T00:00:00+00:00"),
            )
        self.reader = FakeSecretReader(self.secret)
        self.now = 1_800_000_000
        self.provider_time = datetime.fromtimestamp(
            self.now - 30, timezone.utc
        ).isoformat()
        self.evidence = {
            "evidence_id": "heartbeat-service-0001",
            "kind": "HEARTBEAT",
            "provider_timestamp": self.provider_time,
        }

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _request(self, *, evidence=None, raw_body=None, connection_id=None,
                 key_id=None, nonce="nonce-service-000001",
                 content_type="application/json", reader=None, timestamp=None,
                 after_authenticate=None, signature_override=None):
        body = raw_body if raw_body is not None else json.dumps(
            self.evidence if evidence is None else evidence,
            separators=(",", ":"),
        ).encode("utf-8")
        request_time = str(self.now) if timestamp is None else timestamp
        signature = sign_bridge_message(
            self.connection_id if connection_id is None else connection_id,
            request_time,
            nonce,
            body,
            self.secret,
        )
        return ingest_authenticated_bridge_evidence(
            connection_id=self.connection_id if connection_id is None else connection_id,
            key_id=self.key_id if key_id is None else key_id,
            timestamp=request_time,
            nonce=nonce,
            signature=signature if signature_override is None else signature_override,
            raw_body=body,
            content_type=content_type,
            secret_reader=self.reader if reader is None else reader,
            now=self.now,
            after_authenticate=after_authenticate,
        )

    def test_valid_heartbeat_is_authenticated_and_persisted_read_only(self):
        result = self._request()
        self.assertEqual(result["status"], "STORED")
        self.assertEqual(result["connection_id"], self.connection_id)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["live_order_routing"])
        self.assertEqual(self.reader.calls, ["vault://test/bridge/one"])

    def test_participant_identity_cannot_be_supplied_in_payload(self):
        evidence = {**self.evidence, "participant_user_id": self.other_owner}
        with self.assertRaises(BridgeIngestionRequestError):
            self._request(evidence=evidence)
        self.assertEqual(self.reader.calls, [])

    def test_server_database_ownership_is_persisted(self):
        self._request()
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT participant_user_id FROM broker_bridge_evidence WHERE evidence_id=?",
                (self.evidence["evidence_id"],),
            ).fetchone()
        self.assertEqual(row["participant_user_id"], self.owner)
        self.assertNotEqual(row["participant_user_id"], self.other_owner)

    def test_oversized_body_is_rejected_before_vault_access(self):
        with self.assertRaisesRegex(BridgeIngestionRequestError, "size"):
            self._request(raw_body=b"x" * (MAX_EVIDENCE_BODY_BYTES + 1))
        self.assertEqual(self.reader.calls, [])

    def test_non_json_content_type_is_rejected_before_vault_access(self):
        with self.assertRaisesRegex(BridgeIngestionRequestError, "content type"):
            self._request(content_type="text/plain")
        self.assertEqual(self.reader.calls, [])

    def test_invalid_utf8_and_malformed_json_are_rejected(self):
        for body in (b"\xff", b'{"kind":'):
            with self.subTest(body=body):
                with self.assertRaises(BridgeIngestionRequestError):
                    self._request(raw_body=body)
        self.assertEqual(self.reader.calls, [])

    def test_duplicate_json_fields_are_rejected(self):
        body = (
            '{"evidence_id":"heartbeat-service-0001",'
            '"kind":"HEARTBEAT","kind":"QUOTE",'
            '"provider_timestamp":"' + self.provider_time + '"}'
        ).encode()
        with self.assertRaisesRegex(BridgeIngestionRequestError, "duplicate"):
            self._request(raw_body=body)
        self.assertEqual(self.reader.calls, [])

    def test_non_finite_json_numbers_are_rejected(self):
        body = (
            '{"evidence_id":"quote-service-0001","kind":"QUOTE",'
            f'"provider_timestamp":"{self.provider_time}",'
            '"instrument":"XAUUSD","server_symbol":"XAUUSD",'
            '"bid":NaN,"ask":2700.0}'
        ).encode()
        with self.assertRaisesRegex(BridgeIngestionRequestError, "non-finite"):
            self._request(raw_body=body)
        self.assertEqual(self.reader.calls, [])

    def test_stale_and_future_provider_timestamps_are_rejected(self):
        stale = {**self.evidence, "provider_timestamp":
                 datetime.fromtimestamp(self.now - 901, timezone.utc).isoformat()}
        future = {**self.evidence, "provider_timestamp":
                  datetime.fromtimestamp(self.now + 61, timezone.utc).isoformat()}
        for evidence in (stale, future):
            with self.subTest(timestamp=evidence["provider_timestamp"]):
                with self.assertRaises(BridgeIngestionRequestError):
                    self._request(evidence=evidence)
        self.assertEqual(self.reader.calls, [])

    def test_untrusted_connection_selector_cannot_reuse_another_key(self):
        with self.assertRaisesRegex(BridgeIngestionRequestError, "identity"):
            self._request(connection_id=self.other_connection)
        self.assertEqual(self.reader.calls, ["vault://test/bridge/one"])

    def test_vault_failure_is_redacted_and_fails_closed(self):
        class BrokenReader:
            def get_secret(self, _secret_ref):
                raise RuntimeError("sensitive cloud account response")

        with self.assertRaisesRegex(BridgeIngestionRequestError, "unavailable") as caught:
            self._request(reader=BrokenReader())
        self.assertNotIn("sensitive cloud", str(caught.exception))

    def test_replay_is_rejected_by_persistent_nonce_store(self):
        nonce = "nonce-service-replay-01"
        self._request(nonce=nonce)
        with self.assertRaises(BridgeAuthenticationError):
            self._request(nonce=nonce)


    def test_after_authenticate_hook_runs_before_evidence_persistence(self):
        calls = []
        result = self._request(
            after_authenticate=lambda authentication, context: calls.append(
                (authentication["authenticated"], context["connection_id"])
            ),
        )
        self.assertEqual(result["status"], "STORED")
        self.assertEqual(calls, [(True, self.connection_id)])

    def test_after_authenticate_hook_does_not_run_for_invalid_signature(self):
        calls = []
        with self.assertRaises(BridgeAuthenticationError):
            self._request(
                nonce="nonce-service-invalidsig1",
                signature_override="0" * 64,
                after_authenticate=lambda *_args: calls.append("called"),
            )
        self.assertEqual(calls, [])
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_evidence"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_identical_evidence_is_idempotent_across_fresh_requests(self):
        self._request(nonce="nonce-service-idempotent1")
        result = self._request(nonce="nonce-service-idempotent2")
        self.assertEqual(result["status"], "DUPLICATE_IGNORED")


if __name__ == "__main__":
    unittest.main()
