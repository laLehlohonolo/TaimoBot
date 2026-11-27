import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_evidence import (
    BridgeEvidenceError,
    persist_authenticated_bridge_evidence,
)
from app.broker_bridge_security import (
    BridgeAuthenticationError,
    authenticate_bridge_request,
    sign_bridge_message,
)
from app.database import init_db


class BrokerBridgeIngestionPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "bridge-ingestion.db"
        init_db()

        first = register_user(
            "bridge-ingestion-owner-one@taimobot.local",
            "BridgeIngestionOwnerOnePassphrase-2026!",
        )
        second = register_user(
            "bridge-ingestion-owner-two@taimobot.local",
            "BridgeIngestionOwnerTwoPassphrase-2026!",
        )
        self.owner_one = first["user_id"]
        self.owner_two = second["user_id"]
        self.connection_one = "bridge-ingestion-connection-one"
        self.connection_two = "bridge-ingestion-connection-two"
        self.secret = b"test-only-bridge-secret-material-32-bytes-minimum"
        with database.get_connection() as connection:
            for connection_id, owner_id in (
                (self.connection_one, self.owner_one),
                (self.connection_two, self.owner_two),
            ):
                connection.execute(
                    """
                    INSERT INTO participant_broker_connections (
                        connection_id, participant_user_id, broker_id, platform,
                        state, created_at, updated_at
                    ) VALUES (?, ?, 'exness', 'MT5', 'REQUESTED', ?, ?)
                    """,
                    (
                        connection_id, owner_id,
                        "2026-11-27T10:00:00+00:00",
                        "2026-11-27T10:00:00+00:00",
                    ),
                )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _authenticate(self, connection_id, nonce, *, timestamp="1800000000", body=b'{"kind":"HEARTBEAT"}'):
        signature = sign_bridge_message(
            connection_id, timestamp, nonce, body, self.secret
        )
        return authenticate_bridge_request(
            connection_id, timestamp, nonce, signature, body, self.secret,
            now=1_800_000_000,
        )

    def test_valid_signature_is_accepted_without_order_capability(self):
        result = self._authenticate(self.connection_one, "nonce-valid-000001")
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["connection_id"], self.connection_one)
        self.assertEqual(result["replay_protection"], "PERSISTENT")
        self.assertFalse(result["live_order_routing"])

    def test_modified_body_and_wrong_secret_are_rejected(self):
        timestamp = "1800000000"
        nonce = "nonce-body-change-0001"
        body = b'{"kind":"HEARTBEAT"}'
        signature = sign_bridge_message(
            self.connection_one, timestamp, nonce, body, self.secret
        )
        with self.assertRaisesRegex(BridgeAuthenticationError, "signature is invalid"):
            authenticate_bridge_request(
                self.connection_one, timestamp, nonce, signature,
                b'{"kind":"QUOTE"}', self.secret, now=1_800_000_000,
            )
        with self.assertRaisesRegex(BridgeAuthenticationError, "signature is invalid"):
            authenticate_bridge_request(
                self.connection_one, timestamp, "nonce-wrong-secret-0001",
                sign_bridge_message(
                    self.connection_one, timestamp, "nonce-wrong-secret-0001",
                    body, self.secret,
                ),
                body, b"x" * 32, now=1_800_000_000,
            )

    def test_stale_timestamp_is_rejected_before_nonce_consumption(self):
        timestamp = "1799999800"
        nonce = "nonce-stale-time-000001"
        body = b'{"kind":"HEARTBEAT"}'
        signature = sign_bridge_message(
            self.connection_one, timestamp, nonce, body, self.secret
        )
        with self.assertRaisesRegex(BridgeAuthenticationError, "timestamp"):
            authenticate_bridge_request(
                self.connection_one, timestamp, nonce, signature, body,
                self.secret, now=1_800_000_000,
            )
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_nonces WHERE connection_id = ? AND nonce = ?",
                (self.connection_one, nonce),
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_nonce_replay_is_rejected_persistently(self):
        nonce = "nonce-replay-00000001"
        self._authenticate(self.connection_one, nonce)
        with self.assertRaisesRegex(BridgeAuthenticationError, "already been used"):
            self._authenticate(self.connection_one, nonce)

    def test_concurrent_replay_allows_exactly_one_request(self):
        nonce = "nonce-concurrent-000001"
        def attempt():
            try:
                self._authenticate(self.connection_one, nonce)
                return "accepted"
            except BridgeAuthenticationError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(outcomes.count("accepted"), 1)
        self.assertEqual(outcomes.count("rejected"), 1)

    def test_connection_identity_and_state_are_enforced(self):
        with self.assertRaisesRegex(BridgeAuthenticationError, "unknown or inactive"):
            self._authenticate("not-a-real-connection", "nonce-unknown-000001")
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE participant_broker_connections SET state='PAUSED' WHERE connection_id = ?",
                (self.connection_one,),
            )
        with self.assertRaisesRegex(BridgeAuthenticationError, "unknown or inactive"):
            self._authenticate(self.connection_one, "nonce-paused-000001")

    def test_evidence_ownership_is_derived_from_connection_record(self):
        evidence = {
            "evidence_id": "heartbeat-owner-one",
            "kind": "HEARTBEAT",
            "provider_timestamp": "2026-11-27T12:00:00+00:00",
        }
        result = persist_authenticated_bridge_evidence(
            self.connection_one, evidence,
            accepted_at="2026-11-27T12:00:01+00:00",
        )
        self.assertEqual(result["status"], "STORED")
        self.assertFalse(result["live_order_routing"])
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT participant_user_id, connection_id FROM broker_bridge_evidence WHERE evidence_id = ?",
                (evidence["evidence_id"],),
            ).fetchone()
        self.assertEqual(row["participant_user_id"], self.owner_one)
        self.assertEqual(row["connection_id"], self.connection_one)
        self.assertNotEqual(row["participant_user_id"], self.owner_two)

    def test_identical_evidence_is_idempotent_but_conflicting_reuse_fails(self):
        evidence = {
            "evidence_id": "quote-evidence-0001",
            "kind": "QUOTE",
            "provider_timestamp": "2026-11-27T12:00:00Z",
            "instrument": "XAUUSD",
            "server_symbol": "XAUUSD",
            "bid": 2650.0,
            "ask": 2650.5,
        }
        first = persist_authenticated_bridge_evidence(self.connection_one, evidence)
        duplicate = persist_authenticated_bridge_evidence(self.connection_one, evidence)
        self.assertEqual(first["status"], "STORED")
        self.assertEqual(duplicate["status"], "DUPLICATE_IGNORED")
        conflicting = {**evidence, "ask": 2651.0}
        with self.assertRaisesRegex(BridgeEvidenceError, "different content"):
            persist_authenticated_bridge_evidence(self.connection_one, conflicting)

    def test_invalid_quote_shapes_and_values_are_rejected(self):
        base = {
            "evidence_id": "quote-invalid-0001",
            "kind": "QUOTE",
            "provider_timestamp": "2026-11-27T12:00:00+00:00",
            "instrument": "XAUUSD",
            "server_symbol": "XAUUSD",
            "bid": 2650.0,
            "ask": 2650.5,
        }
        for changes in (
            {"ask": 2649.0},
            {"bid": float("nan")},
            {"ask": True},
            {"unsupported": "participant_user_id"},
            {"provider_timestamp": "2026-11-27T12:00:00"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(BridgeEvidenceError):
                    persist_authenticated_bridge_evidence(
                        self.connection_one, {**base, **changes}
                    )

    def test_paused_connection_cannot_persist_evidence(self):
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE participant_broker_connections SET state='REVOKED' WHERE connection_id = ?",
                (self.connection_two,),
            )
        evidence = {
            "evidence_id": "heartbeat-revoked",
            "kind": "HEARTBEAT",
            "provider_timestamp": "2026-11-27T12:00:00+00:00",
        }
        with self.assertRaisesRegex(BridgeEvidenceError, "unknown or inactive"):
            persist_authenticated_bridge_evidence(self.connection_two, evidence)


if __name__ == "__main__":
    unittest.main()
