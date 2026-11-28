import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database
from app.auth import register_user
from app.broker_bridge_ingestion_gateway import (
    append_bridge_ingestion_audit,
    consume_bridge_ingestion_rate_limit,
    ensure_bridge_ingestion_gateway_schema,
)
from app.database import init_db


class BrokerBridgeIngestionGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "bridge-gateway.db"
        init_db()
        self.owner = register_user(
            "gateway-owner@taimobot.local",
            "GatewayOwnerPassphrase-2026!",
        )["user_id"]
        self.other_owner = register_user(
            "gateway-other@taimobot.local",
            "GatewayOtherPassphrase-2026!",
        )["user_id"]
        self.connection_one = "gateway-connection-one"
        self.connection_two = "gateway-connection-two"
        with database.get_connection() as connection:
            for connection_id, owner_id in (
                (self.connection_one, self.owner),
                (self.connection_two, self.owner),
                ("gateway-other-owner", self.other_owner),
            ):
                connection.execute(
                    """INSERT INTO participant_broker_connections
                       (connection_id, participant_user_id, broker_id, platform,
                        state, created_at, updated_at)
                       VALUES (?, ?, 'exness', 'MT5', 'REQUESTED', ?, ?)""",
                    (connection_id, owner_id, "2026-11-28T00:00:00+00:00",
                     "2026-11-28T00:00:00+00:00"),
                )
            ensure_bridge_ingestion_gateway_schema(connection)

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _limit(self, connection_id, *, now=1000, participant_capacity=120,
               connection_capacity=30, window_seconds=60):
        return consume_bridge_ingestion_rate_limit(
            self.owner, connection_id, now=now,
            participant_capacity=participant_capacity,
            connection_capacity=connection_capacity,
            window_seconds=window_seconds,
        )

    def test_connection_bucket_blocks_bursts_and_refills(self):
        first = self._limit(
            self.connection_one, connection_capacity=2, participant_capacity=10,
        )
        second = self._limit(
            self.connection_one, connection_capacity=2, participant_capacity=10,
        )
        blocked = self._limit(
            self.connection_one, connection_capacity=2, participant_capacity=10,
        )
        refilled = self._limit(
            self.connection_one, now=1030, connection_capacity=2, participant_capacity=10,
        )
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["scope"], "CONNECTION")
        self.assertGreaterEqual(blocked["retry_after"], 1)
        self.assertTrue(refilled["allowed"])

    def test_participant_bucket_limits_aggregate_connections(self):
        kwargs = {"participant_capacity": 2, "connection_capacity": 10}
        first = self._limit(self.connection_one, **kwargs)
        second = self._limit(self.connection_two, **kwargs)
        blocked = self._limit(self.connection_one, **kwargs)
        self.assertTrue(first["allowed"])
        self.assertTrue(second["allowed"])
        self.assertFalse(blocked["allowed"])
        self.assertEqual(blocked["scope"], "PARTICIPANT")

    def test_rate_limit_buckets_are_isolated_between_participants(self):
        self._limit(self.connection_one, participant_capacity=1, connection_capacity=5)
        result = consume_bridge_ingestion_rate_limit(
            self.other_owner, "gateway-other-owner", now=1000,
            participant_capacity=1, connection_capacity=5,
        )
        self.assertTrue(result["allowed"])

    def test_audit_stores_only_redacted_allowlisted_metadata(self):
        append_bridge_ingestion_audit(
            request_id="request-safe-001", event_type="ATTEMPT",
            participant_user_id=self.owner, connection_id=self.connection_one,
            key_id="key-safe-001", http_status=0,
            reason_code="REQUEST_RECEIVED", body_bytes=0,
        )
        with database.get_connection() as connection:
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(broker_bridge_ingestion_audit)"
                ).fetchall()
            }
            row = connection.execute(
                "SELECT * FROM broker_bridge_ingestion_audit"
            ).fetchone()
        self.assertNotIn("raw_body", columns)
        self.assertNotIn("signature", columns)
        self.assertNotIn("nonce", columns)
        self.assertNotIn("secret", columns)
        self.assertEqual(row["reason_code"], "REQUEST_RECEIVED")
        self.assertEqual(row["connection_id"], self.connection_one)

    def test_audit_is_immutable_except_for_privacy_anonymisation(self):
        append_bridge_ingestion_audit(
            request_id="request-safe-002", event_type="RESULT",
            participant_user_id=self.owner, connection_id=self.connection_one,
            key_id="key-safe-002", http_status=200,
            reason_code="ACCEPTED", body_bytes=42,
        )
        with database.get_connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE broker_bridge_ingestion_audit SET reason_code='INTERNAL_FAILURE'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM broker_bridge_ingestion_audit")
            connection.execute(
                "UPDATE broker_bridge_ingestion_audit SET participant_user_id = NULL"
            )
            row = connection.execute(
                "SELECT participant_user_id, reason_code, body_bytes "
                "FROM broker_bridge_ingestion_audit"
            ).fetchone()
        self.assertIsNone(row["participant_user_id"])
        self.assertEqual(row["reason_code"], "ACCEPTED")
        self.assertEqual(row["body_bytes"], 42)

    def test_failed_audit_trigger_migration_preserves_existing_immutability(self):
        append_bridge_ingestion_audit(
            request_id="request-migration-rollback", event_type="RESULT",
            participant_user_id=self.owner, connection_id=self.connection_one,
            key_id="key-migration-rollback", http_status=200,
            reason_code="ACCEPTED", body_bytes=8,
        )
        with database.get_connection() as connection:
            def deny_trigger_creation(action, _arg1, _arg2, _database, _trigger):
                if action == sqlite3.SQLITE_CREATE_TRIGGER:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_trigger_creation)
            with self.assertRaises(sqlite3.DatabaseError):
                ensure_bridge_ingestion_gateway_schema(connection)
            connection.set_authorizer(None)

            # A failed replacement must roll back the DROP as well. The old
            # immutability trigger remains active for other connections too.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE broker_bridge_ingestion_audit "
                    "SET reason_code='INTERNAL_FAILURE' "
                    "WHERE request_id='request-migration-rollback'"
                )

    def test_account_deletion_anonymises_audit_without_erasing_event(self):
        disposable_user = register_user(
            "gateway-deletion@taimobot.local",
            "GatewayDeletionPassphrase-2026!",
        )["user_id"]
        append_bridge_ingestion_audit(
            request_id="request-delete-001", event_type="RESULT",
            participant_user_id=disposable_user, http_status=200,
            reason_code="ACCEPTED", body_bytes=12,
        )
        with database.get_connection() as connection:
            connection.execute("DELETE FROM users WHERE user_id = ?", (disposable_user,))
            row = connection.execute(
                "SELECT participant_user_id, request_id, reason_code, body_bytes "
                "FROM broker_bridge_ingestion_audit WHERE request_id = ?",
                ("request-delete-001",),
            ).fetchone()
        self.assertIsNone(row["participant_user_id"])
        self.assertEqual(row["request_id"], "request-delete-001")
        self.assertEqual(row["reason_code"], "ACCEPTED")
        self.assertEqual(row["body_bytes"], 12)

    def test_unknown_audit_classification_is_rejected(self):
        with self.assertRaises(ValueError):
            append_bridge_ingestion_audit(
                request_id="request-safe-003", event_type="RESULT",
                http_status=400, reason_code="RAW_EXCEPTION_TEXT",
            )


    def _http_client_and_key(self):
        import json
        import time
        from datetime import datetime, timezone
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app import broker_bridge_ingestion_gateway as gateway
        from app.broker_bridge_security import sign_bridge_message

        key_id = "gateway-http-key"
        secret_ref = "vault://gateway/http-test"
        secret = b"gateway-http-test-secret-material-32-bytes"
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO broker_bridge_keys
                   (key_id, connection_id, secret_ref, status,
                    rotation_sequence, created_at, activated_at)
                   VALUES (?, ?, ?, 'ACTIVE', 1, ?, ?)""",
                (key_id, self.connection_one, secret_ref,
                 "2026-11-28T00:00:00+00:00", "2026-11-28T00:00:00+00:00"),
            )

        class Reader:
            def get_secret(self, requested_ref):
                if requested_ref != secret_ref:
                    raise RuntimeError("Unexpected secret reference.")
                return secret

        app = FastAPI()
        app.include_router(gateway.router)
        app.state.broker_bridge_secret_reader = Reader()
        client = TestClient(app)

        timestamp = str(int(time.time()))
        nonce = "gateway-http-nonce-0001"
        evidence = {
            "evidence_id": "gateway-http-heartbeat-001",
            "kind": "HEARTBEAT",
            "provider_timestamp": datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(evidence, separators=(",", ":")).encode("utf-8")
        signature = sign_bridge_message(
            self.connection_one, timestamp, nonce, body, secret,
        )
        headers = {
            "content-type": "application/json",
            "x-taimo-key-id": key_id,
            "x-taimo-timestamp": timestamp,
            "x-taimo-nonce": nonce,
            "x-taimo-signature": signature,
        }
        return client, body, headers

    def test_http_gateway_stores_authenticated_evidence_and_audit(self):
        import os
        from unittest.mock import patch

        client, body, headers = self._http_client_and_key()
        with patch.dict(os.environ, {"TAIMOBOT_BROKER_INGESTION_HTTP_ENABLED": "true"}):
            response = client.post(
                f"/internal/broker-bridge/connections/{self.connection_one}/evidence",
                content=body, headers=headers,
            )
        client.close()
        self.assertEqual(response.status_code, 200, response.text)
        with database.get_connection() as connection:
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_evidence"
            ).fetchone()["count"]
            audit = connection.execute(
                "SELECT event_type, reason_code FROM broker_bridge_ingestion_audit ORDER BY audit_id"
            ).fetchall()
            bucket_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_ingestion_rate_limits"
            ).fetchone()["count"]
        self.assertEqual(evidence_count, 1)
        self.assertEqual([(row["event_type"], row["reason_code"]) for row in audit], [
            ("ATTEMPT", "REQUEST_RECEIVED"), ("RESULT", "ACCEPTED"),
        ])
        self.assertEqual(bucket_count, 2)

    def test_http_invalid_signature_creates_no_audit_or_rate_limit_debit(self):
        import os
        from unittest.mock import patch

        client, body, headers = self._http_client_and_key()
        headers["x-taimo-signature"] = "0" * 64
        with patch.dict(os.environ, {"TAIMOBOT_BROKER_INGESTION_HTTP_ENABLED": "true"}):
            response = client.post(
                f"/internal/broker-bridge/connections/{self.connection_one}/evidence",
                content=body, headers=headers,
            )
        client.close()
        self.assertEqual(response.status_code, 401)
        with database.get_connection() as connection:
            audit_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_ingestion_audit"
            ).fetchone()["count"]
            bucket_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_ingestion_rate_limits"
            ).fetchone()["count"]
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_evidence"
            ).fetchone()["count"]
        self.assertEqual(audit_count, 0)
        self.assertEqual(bucket_count, 0)
        self.assertEqual(evidence_count, 0)

    def test_http_gateway_disabled_returns_not_found(self):
        import os
        from unittest.mock import patch

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.broker_bridge_ingestion_gateway import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        with patch.dict(os.environ, {}, clear=True):
            response = client.post(
                f"/internal/broker-bridge/connections/{self.connection_one}/evidence",
                content=b"{}",
                headers={"content-type": "application/json"},
            )
        client.close()
        self.assertEqual(response.status_code, 404)
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_ingestion_audit"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_http_rate_limit_rejection_is_audited_without_evidence(self):
        import os
        import time
        from unittest.mock import patch

        client, body, headers = self._http_client_and_key()
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO broker_bridge_ingestion_rate_limits
                   (bucket_key, bucket_scope, participant_user_id, connection_id,
                    tokens, updated_at)
                   VALUES (?, 'PARTICIPANT', ?, NULL, 0, ?)""",
                (f"participant:{self.owner}", self.owner, int(time.time()) + 3600),
            )
        with patch.dict(os.environ, {"TAIMOBOT_BROKER_INGESTION_HTTP_ENABLED": "true"}):
            response = client.post(
                f"/internal/broker-bridge/connections/{self.connection_one}/evidence",
                content=body, headers=headers,
            )
        client.close()
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("retry-after", response.headers)
        with database.get_connection() as connection:
            audit = connection.execute(
                "SELECT event_type, reason_code FROM broker_bridge_ingestion_audit ORDER BY audit_id"
            ).fetchall()
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_evidence"
            ).fetchone()["count"]
        self.assertEqual([(row["event_type"], row["reason_code"]) for row in audit], [
            ("ATTEMPT", "REQUEST_RECEIVED"), ("RESULT", "RATE_LIMITED"),
        ])
        self.assertEqual(evidence_count, 0)


    def test_result_audit_failure_rolls_back_evidence_and_retry_stores_once(self):
        import os
        from unittest.mock import patch
        from app import broker_bridge_ingestion_gateway as gateway
        from app.broker_bridge_security import sign_bridge_message

        client, body, headers = self._http_client_and_key()
        original_append = gateway.append_bridge_ingestion_audit
        failed = {"value": False}

        def fail_first_result(**kwargs):
            if kwargs["event_type"] == "RESULT" and not failed["value"]:
                failed["value"] = True
                raise RuntimeError("simulated result-audit storage failure")
            return original_append(**kwargs)

        endpoint = (
            f"/internal/broker-bridge/connections/{self.connection_one}/evidence"
        )
        secret = b"gateway-http-test-secret-material-32-bytes"
        with patch.dict(os.environ, {"TAIMOBOT_BROKER_INGESTION_HTTP_ENABLED": "true"}):
            with patch.object(gateway, "append_bridge_ingestion_audit",
                              side_effect=fail_first_result):
                first = client.post(endpoint, content=body, headers=headers)

            self.assertEqual(first.status_code, 503)
            with database.get_connection() as connection:
                evidence_after_first = connection.execute(
                    "SELECT COUNT(*) AS count FROM broker_bridge_evidence"
                ).fetchone()["count"]
                audit_after_first = connection.execute(
                    "SELECT event_type, reason_code FROM broker_bridge_ingestion_audit "
                    "ORDER BY audit_id"
                ).fetchall()
            self.assertEqual(evidence_after_first, 0)
            self.assertEqual(
                [(row["event_type"], row["reason_code"]) for row in audit_after_first],
                [
                    ("ATTEMPT", "REQUEST_RECEIVED"),
                    ("RESULT", "INTERNAL_FAILURE"),
                ],
            )

            retry_headers = dict(headers)
            retry_headers["x-taimo-nonce"] = "gateway-http-nonce-0002"
            retry_headers["x-taimo-signature"] = sign_bridge_message(
                self.connection_one,
                retry_headers["x-taimo-timestamp"],
                retry_headers["x-taimo-nonce"],
                body,
                secret,
            )
            retry = client.post(endpoint, content=body, headers=retry_headers)

        client.close()
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["status"], "STORED")
        with database.get_connection() as connection:
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS count FROM broker_bridge_evidence"
            ).fetchone()["count"]
            audit = connection.execute(
                "SELECT event_type, reason_code FROM broker_bridge_ingestion_audit "
                "ORDER BY audit_id"
            ).fetchall()
        self.assertEqual(evidence_count, 1)
        self.assertEqual(
            [(row["event_type"], row["reason_code"]) for row in audit],
            [
                ("ATTEMPT", "REQUEST_RECEIVED"),
                ("RESULT", "INTERNAL_FAILURE"),
                ("ATTEMPT", "REQUEST_RECEIVED"),
                ("RESULT", "ACCEPTED"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
