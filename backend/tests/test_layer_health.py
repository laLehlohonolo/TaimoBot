import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.auth import create_session, register_user
from app.database import init_db
from app.layer_health import LAYER_IDS, build_layer_health_snapshot
from app.main import app


class LayerHealthContractTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "layer-health.db"
        init_db()
        self.original_admin_emails = os.environ.get("TAIMOBOT_ADMIN_EMAILS")
        os.environ["TAIMOBOT_ADMIN_EMAILS"] = "layer-admin@taimobot.local"
        admin = register_user("layer-admin@taimobot.local", "AdminPassphrase-2026-Secure!")
        participant = register_user("layer-participant@taimobot.local", "ParticipantPassphrase-2026!")
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET role = 'ADMIN', email_verified = 1 WHERE user_id = ?",
                (admin["user_id"],),
            )
        self.admin_client = TestClient(app)
        self.participant_client = TestClient(app)
        self.admin_client.headers.update({
            "Authorization": "Bearer " + create_session(admin["user_id"])["access_token"]
        })
        self.participant_client.headers.update({
            "Authorization": "Bearer " + create_session(participant["user_id"])["access_token"]
        })

    def tearDown(self):
        self.admin_client.close()
        self.participant_client.close()
        if self.original_admin_emails is None:
            os.environ.pop("TAIMOBOT_ADMIN_EMAILS", None)
        else:
            os.environ["TAIMOBOT_ADMIN_EMAILS"] = self.original_admin_emails
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_snapshot_lists_all_seven_layers_without_fake_heartbeats(self):
        snapshot = build_layer_health_snapshot()
        self.assertEqual([item["layer_id"] for item in snapshot["layers"]], list(LAYER_IDS))
        self.assertEqual(snapshot["overall_status"], "NOT_IMPLEMENTED")
        self.assertFalse(snapshot["telemetry_registered"])
        for layer in snapshot["layers"]:
            self.assertEqual(layer["status"], "NOT_IMPLEMENTED")
            self.assertIsNone(layer["observed_at"])
            self.assertIsNone(layer["last_success_at"])
            self.assertIsNone(layer["evidence_ref"])

    def test_endpoint_requires_authentication(self):
        response = TestClient(app).get("/admin/brain/layer-health")
        self.assertEqual(response.status_code, 401)

    def test_participant_cannot_read_administrator_layer_health(self):
        response = self.participant_client.get("/admin/brain/layer-health")
        self.assertEqual(response.status_code, 403)

    def test_verified_administrator_can_read_layer_health_contract(self):
        response = self.admin_client.get("/admin/brain/layer-health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["contract_version"], "layer-health-v1")
        self.assertEqual(len(body["layers"]), 7)
        self.assertEqual(body["overall_status"], "NOT_IMPLEMENTED")
        self.assertTrue(all(item["status"] == "NOT_IMPLEMENTED" for item in body["layers"]))
        market = next(item for item in body["layers"] if item["layer_id"] == "MARKET")
        self.assertEqual(market["limitations"], ["No Market provider is registered."])
        self.assertFalse(market["execution_eligible"])
        self.assertFalse(market["signals_generated"])
        self.assertFalse(market["orders_submitted"])


if __name__ == "__main__":
    unittest.main()
