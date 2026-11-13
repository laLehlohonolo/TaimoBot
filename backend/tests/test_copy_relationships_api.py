import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.execution import create_paper_account
from app.main import app


class CopyRelationshipApiTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "copy_relationships.db"
        init_db()
        create_paper_account("SOURCE-001", 10000)
        create_paper_account("FOLLOWER-001", 5000)
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_relationship(self):
        return self.client.post(
            "/accounts/FOLLOWER-001/copy-relationships",
            json={"source_account_id": "SOURCE-001"}
        )

    def test_create_copy_relationship(self):
        response = self.create_relationship()

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["source_account_id"], "SOURCE-001")
        self.assertEqual(result["follower_account_id"], "FOLLOWER-001")
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(result["mode"], "paper_trading")

    def test_list_copy_relationships(self):
        created = self.create_relationship()
        relationship_id = created.json()["relationship_id"]

        response = self.client.get(
            "/accounts/FOLLOWER-001/copy-relationships"
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["relationships"][0]["relationship_id"],
            relationship_id
        )

    def test_self_follow_is_rejected_by_api(self):
        response = self.client.post(
            "/accounts/SOURCE-001/copy-relationships",
            json={"source_account_id": "SOURCE-001"}
        )

        self.assertEqual(response.status_code, 400)

    def test_duplicate_relationship_is_rejected_by_api(self):
        first = self.create_relationship()
        second = self.create_relationship()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_unknown_account_is_rejected(self):
        response = self.client.post(
            "/accounts/UNKNOWN/copy-relationships",
            json={"source_account_id": "SOURCE-001"}
        )

        self.assertEqual(response.status_code, 404)

    def test_pause_and_resume_relationship(self):
        created = self.create_relationship()
        relationship_id = created.json()["relationship_id"]

        paused = self.client.post(
            f"/copy-relationships/{relationship_id}/pause"
        )
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(paused.json()["status"], "PAUSED")

        resumed = self.client.post(
            f"/copy-relationships/{relationship_id}/resume"
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.json()["status"], "ACTIVE")

    def test_stopped_relationship_cannot_be_resumed(self):
        created = self.create_relationship()
        relationship_id = created.json()["relationship_id"]

        stopped = self.client.post(
            f"/copy-relationships/{relationship_id}/stop"
        )
        self.assertEqual(stopped.status_code, 200)
        self.assertEqual(stopped.json()["status"], "STOPPED")

        resumed = self.client.post(
            f"/copy-relationships/{relationship_id}/resume"
        )
        self.assertEqual(resumed.status_code, 400)

    def test_unknown_relationship_returns_not_found(self):
        response = self.client.post(
            "/copy-relationships/UNKNOWN/pause"
        )

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
