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


if __name__ == "__main__":
    unittest.main()
