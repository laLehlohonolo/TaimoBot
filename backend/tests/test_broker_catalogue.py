import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.auth import create_session, register_user
from app.broker_catalogue import BROKER_CATALOGUE_VERSION, list_broker_catalogue
from app.database import init_db
from app.main import app


class BrokerCatalogueTests(unittest.TestCase):
    def test_catalogue_contains_multiple_independent_broker_candidates(self):
        result = list_broker_catalogue()
        ids = {item["broker_id"] for item in result["brokers"]}
        self.assertEqual(
            ids,
            {"exness", "avatrade", "vantage", "fp_markets", "tickmill", "hfm"},
        )
        self.assertEqual(result["catalogue_version"], BROKER_CATALOGUE_VERSION)

    def test_candidates_are_not_misrepresented_as_connected_or_supported(self):
        result = list_broker_catalogue()
        for broker in result["brokers"]:
            self.assertEqual(broker["integration_status"], "CANDIDATE_ONLY")
            self.assertEqual(broker["connection_status"], "NOT_AVAILABLE")
            self.assertEqual(broker["regional_entity_review"], "REQUIRED")
            self.assertFalse(broker["execution_enabled"])
            self.assertEqual(broker["supported_instruments"], [])

    def test_catalogue_is_paper_only_and_read_only(self):
        result = list_broker_catalogue()
        self.assertEqual(result["mode"], "paper_trading")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["live_order_routing"])

    def test_catalogue_returns_detached_records(self):
        first = list_broker_catalogue()
        first["brokers"][0]["platform_candidates"].append("INJECTED")
        second = list_broker_catalogue()
        self.assertNotIn("INJECTED", second["brokers"][0]["platform_candidates"])


class BrokerCatalogueRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "broker-catalogue.db"
        init_db()
        participant = register_user(
            "broker-catalogue-participant@taimobot.local",
            "BrokerCataloguePassphrase-2026!",
        )
        self.client = TestClient(app)
        self.client.headers.update({
            "Authorization": "Bearer " + create_session(participant["user_id"])["access_token"]
        })

    def tearDown(self):
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_catalogue_route_requires_authentication(self):
        with TestClient(app) as unauthenticated:
            response = unauthenticated.get("/brokers")
        self.assertEqual(response.status_code, 401)

    def test_authenticated_participant_receives_candidate_only_catalogue(self):
        response = self.client.get("/brokers")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["catalogue_version"], BROKER_CATALOGUE_VERSION)
        self.assertEqual(body["mode"], "paper_trading")
        self.assertTrue(body["read_only"])
        self.assertFalse(body["live_order_routing"])
        self.assertEqual(len(body["brokers"]), 6)
        for broker in body["brokers"]:
            self.assertEqual(broker["integration_status"], "CANDIDATE_ONLY")
            self.assertEqual(broker["connection_status"], "NOT_AVAILABLE")
            self.assertFalse(broker["execution_enabled"])
            self.assertNotIn("password", broker)
            self.assertNotIn("token", broker)



class BrokerConnectionIntentRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "broker-connections.db"
        init_db()
        first = register_user(
            "broker-owner-one@taimobot.local",
            "BrokerOwnerOnePassphrase-2026!",
        )
        second = register_user(
            "broker-owner-two@taimobot.local",
            "BrokerOwnerTwoPassphrase-2026!",
        )
        self.owner_id = first["user_id"]
        self.other_id = second["user_id"]
        self.owner_client = TestClient(app)
        self.other_client = TestClient(app)
        self.owner_client.headers.update({
            "Authorization": "Bearer " + create_session(self.owner_id)["access_token"]
        })
        self.other_client.headers.update({
            "Authorization": "Bearer " + create_session(self.other_id)["access_token"]
        })

    def tearDown(self):
        self.owner_client.close()
        self.other_client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_connection_intent_is_participant_scoped_and_not_connected(self):
        created = self.owner_client.post(
            "/broker-connections",
            json={"broker_id": "exness", "platform": "MT5"},
        )
        self.assertEqual(created.status_code, 201)
        body = created.json()
        self.assertEqual(body["state"], "REQUESTED")
        self.assertEqual(body["connection_status"], "NOT_CONNECTED")
        self.assertEqual(body["integration_status"], "CANDIDATE_ONLY")
        self.assertFalse(body["credentials_required_by_api"])
        self.assertFalse(body["live_order_routing"])
        self.assertNotIn("secret", body)
        self.assertNotIn("password", body)

        owner_list = self.owner_client.get("/broker-connections").json()
        other_list = self.other_client.get("/broker-connections").json()
        self.assertEqual(len(owner_list["connections"]), 1)
        self.assertEqual(other_list["connections"], [])
        self.assertFalse(owner_list["live_order_routing"])

    def test_connection_intent_rejects_credentials_and_unknown_platforms(self):
        credential_payload = self.owner_client.post(
            "/broker-connections",
            json={"broker_id": "exness", "platform": "MT5", "password": "do-not-send"},
        )
        self.assertEqual(credential_payload.status_code, 422)
        unknown_broker = self.owner_client.post(
            "/broker-connections",
            json={"broker_id": "unknown", "platform": "MT5"},
        )
        self.assertEqual(unknown_broker.status_code, 422)
        unknown_platform = self.owner_client.post(
            "/broker-connections",
            json={"broker_id": "exness", "platform": "cTrader"},
        )
        self.assertEqual(unknown_platform.status_code, 422)

    def test_connection_state_transitions_and_owner_isolation(self):
        created = self.owner_client.post(
            "/broker-connections",
            json={"broker_id": "avatrade", "platform": "MT5"},
        ).json()
        connection_id = created["connection_id"]

        cross_owner = self.other_client.post(
            f"/broker-connections/{connection_id}/pause"
        )
        self.assertEqual(cross_owner.status_code, 404)

        paused = self.owner_client.post(
            f"/broker-connections/{connection_id}/pause"
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["state"], "PAUSED")

        resumed = self.owner_client.post(
            f"/broker-connections/{connection_id}/resume"
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["state"], "REQUESTED")

        revoked = self.owner_client.post(
            f"/broker-connections/{connection_id}/revoke"
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["state"], "REVOKED")
        self.assertIsNotNone(revoked.json()["revoked_at"])

        cannot_resume = self.owner_client.post(
            f"/broker-connections/{connection_id}/resume"
        )
        self.assertEqual(cannot_resume.status_code, 409)

    def test_connection_routes_require_authentication(self):
        with TestClient(app) as unauthenticated:
            response = unauthenticated.get("/broker-connections")
        self.assertEqual(response.status_code, 401)



class BrokerBridgeSecurityTests(unittest.TestCase):
    def setUp(self):
        import secrets
        from app.broker_bridge_security import (
            ensure_bridge_security_schema,
            authenticate_bridge_request,
            sign_bridge_message,
        )
        self.authenticate = authenticate_bridge_request
        self.sign = sign_bridge_message
        self.secret = secrets.token_bytes(32)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "bridge-security.db"
        init_db()
        participant = register_user(
            "bridge-security-owner@taimobot.local",
            "BridgeSecurityPassphrase-2026!",
        )
        self.participant_id = participant["user_id"]
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO participant_broker_connections (
                    connection_id, participant_user_id, broker_id, platform,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'REQUESTED', ?, ?)
                """,
                (
                    "bridge-test-connection", self.participant_id, "exness",
                    "MT5", "2026-11-27T10:00:00+00:00",
                    "2026-11-27T10:00:00+00:00",
                ),
            )

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _request(self, nonce="nonce-0123456789abcdef", timestamp="1795773600",
                 body=b'{"kind":"heartbeat","schema_version":1}'):
        signature = self.sign(
            "bridge-test-connection", timestamp, nonce, body, self.secret
        )
        return self.authenticate(
            "bridge-test-connection", timestamp, nonce, signature, body,
            self.secret, now=int(timestamp), max_clock_skew_seconds=60,
        )

    def test_valid_request_consumes_nonce_persistently(self):
        result = self._request()
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["replay_protection"], "PERSISTENT")
        with self.assertRaisesRegex(ValueError, "already been used"):
            self._request()

    def test_wrong_signature_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            self.authenticate(
                "bridge-test-connection", "1795773600",
                "nonce-1123456789abcdef", "0" * 64, b"{}", self.secret,
                now=1795773600,
            )

    def test_stale_timestamp_is_rejected(self):
        signature = self.sign(
            "bridge-test-connection", "1795770000",
            "nonce-2123456789abcdef", b"{}", self.secret
        )
        with self.assertRaisesRegex(ValueError, "outside the allowed window"):
            self.authenticate(
                "bridge-test-connection", "1795770000",
                "nonce-2123456789abcdef", signature, b"{}", self.secret,
                now=1795773600,
            )

    def test_unknown_or_inactive_connection_is_rejected(self):
        signature = self.sign(
            "unknown-connection", "1795773600",
            "nonce-3123456789abcdef", b"{}", self.secret
        )
        with self.assertRaisesRegex(ValueError, "unknown or inactive"):
            self.authenticate(
                "unknown-connection", "1795773600",
                "nonce-3123456789abcdef", signature, b"{}", self.secret,
                now=1795773600,
            )

        with database.get_connection() as connection:
            connection.execute(
                "UPDATE participant_broker_connections SET state='PAUSED' "
                "WHERE connection_id='bridge-test-connection'"
            )
        with self.assertRaisesRegex(ValueError, "unknown or inactive"):
            self._request(nonce="nonce-4123456789abcdef")

    def test_signature_is_bound_to_raw_body_and_connection(self):
        signature = self.sign(
            "bridge-test-connection", "1795773600",
            "nonce-5123456789abcdef", b'{"kind":"heartbeat"}', self.secret
        )
        with self.assertRaisesRegex(ValueError, "signature is invalid"):
            self.authenticate(
                "bridge-test-connection", "1795773600",
                "nonce-5123456789abcdef", signature, b'{"kind":"quote"}',
                self.secret, now=1795773600,
            )

    def test_invalid_nonce_and_weak_key_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "nonce"):
            self.authenticate(
                "bridge-test-connection", "1795773600", "short", "0" * 64,
                b"{}", self.secret, now=1795773600,
            )
        with self.assertRaisesRegex(ValueError, "unavailable or invalid"):
            self.authenticate(
                "bridge-test-connection", "1795773600",
                "nonce-6123456789abcdef", "0" * 64, b"{}", b"weak",
                now=1795773600,
            )


if __name__ == "__main__":
    unittest.main()
