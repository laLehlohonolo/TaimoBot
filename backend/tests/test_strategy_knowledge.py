import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.main import app


class StrategyKnowledgeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "strategies.db"
        init_db()
        self.client = TestClient(app)
        self.user_token = self._register_and_login(
            "strategy-owner@example.com", "StrategyOwnerPassphrase2026!"
        )
        self.other_token = self._register_and_login(
            "strategy-other@example.com", "StrategyOtherPassphrase2026!"
        )

    def tearDown(self):
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _register_and_login(self, email, password):
        registered = self.client.post(
            "/auth/register",
            json={"email": email, "password": password}
        )
        self.assertEqual(registered.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": email, "password": password}
        )
        self.assertEqual(login.status_code, 200)
        return login.json()["access_token"]

    @staticmethod
    def headers(token):
        return {"Authorization": "Bearer " + token}

    def test_strategy_library_requires_authentication(self):
        response = self.client.get("/strategies")
        self.assertEqual(response.status_code, 401)

    def test_builtin_techniques_are_seeded_as_unvalidated_references(self):
        response = self.client.get(
            "/strategies", headers=self.headers(self.user_token)
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreaterEqual(payload["count"], 10)
        self.assertTrue(
            any(item["name"] == "Scalping" for item in payload["strategies"])
        )
        self.assertTrue(
            all(item["evidence_status"] == "UNVALIDATED"
                for item in payload["strategies"])
        )
        self.assertTrue(
            all(item["provenance"] == "BUILTIN_REFERENCE"
                for item in payload["strategies"] if item["owner_user_id"] is None)
        )

    def test_user_can_add_strategy_knowledge_but_it_remains_unvalidated(self):
        response = self.client.post(
            "/strategies",
            headers=self.headers(self.user_token),
            json={
                "name": "London session pullback",
                "category": "SESSION",
                "description": "A proposed pullback technique for evaluation.",
                "entry_logic": "Wait for a defined session trend and a confirmed pullback.",
                "exit_logic": "Exit at the planned target or when the setup is invalidated.",
                "risk_notes": "Validate spread, slippage and stop distance before paper testing."
            }
        )
        self.assertEqual(response.status_code, 200)
        strategy = response.json()
        self.assertEqual(strategy["evidence_status"], "UNVALIDATED")
        self.assertEqual(strategy["provenance"], "USER_SUBMITTED")
        self.assertEqual(strategy["owner_user_id"], self.client.get(
            "/auth/me", headers=self.headers(self.user_token)
        ).json()["user_id"])

        visible = self.client.get(
            "/strategies", headers=self.headers(self.user_token)
        ).json()
        self.assertTrue(
            any(item["strategy_id"] == strategy["strategy_id"]
                for item in visible["strategies"])
        )

    def test_user_contributions_are_not_visible_to_another_user(self):
        created = self.client.post(
            "/strategies",
            headers=self.headers(self.user_token),
            json={
                "name": "Private research idea",
                "category": "MOMENTUM",
                "description": "A private technique note awaiting research.",
                "entry_logic": "Require a confirmed momentum shift and defined invalidation.",
                "exit_logic": "Close at the predefined target or thesis invalidation.",
                "risk_notes": "Keep total account risk within the hard one percent limit."
            }
        )
        self.assertEqual(created.status_code, 200)
        strategy_id = created.json()["strategy_id"]

        other_library = self.client.get(
            "/strategies", headers=self.headers(self.other_token)
        ).json()
        self.assertFalse(
            any(item["strategy_id"] == strategy_id
                for item in other_library["strategies"])
        )
        hidden = self.client.get(
            "/strategies/" + strategy_id,
            headers=self.headers(self.other_token)
        )
        self.assertEqual(hidden.status_code, 404)

    def test_invalid_strategy_payload_is_rejected(self):
        response = self.client.post(
            "/strategies",
            headers=self.headers(self.user_token),
            json={
                "name": "X",
                "category": "not valid",
                "description": "short",
                "entry_logic": "short",
                "exit_logic": "short",
                "risk_notes": "short"
            }
        )
        self.assertEqual(response.status_code, 422)

    def test_reference_seeding_is_idempotent(self):
        with database.get_connection() as connection:
            before = connection.execute(
                "SELECT COUNT(*) AS total FROM strategy_knowledge"
            ).fetchone()["total"]
        init_db()
        with database.get_connection() as connection:
            after = connection.execute(
                "SELECT COUNT(*) AS total FROM strategy_knowledge"
            ).fetchone()["total"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
