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


if __name__ == "__main__":
    unittest.main()
