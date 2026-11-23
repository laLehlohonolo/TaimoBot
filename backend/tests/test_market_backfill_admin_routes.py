import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database, main
from app.database import init_db


class MarketBackfillAdminRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "market-backfill-admin.db"
        init_db()
        self.client = TestClient(main.app)
        self.instrument_patch = patch(
            "app.main.get_instrument",
            return_value={
                "symbol": "1HZ100V",
                "provider": "Deriv",
                "asset_class": "synthetic_index",
            },
        )
        self.instrument_patch.start()
        self.admin_env = patch.dict(
            "os.environ", {"TAIMOBOT_ADMIN_EMAILS": "admin@example.com"}, clear=True
        )
        self.admin_env.start()
        self.member_headers = self._register_and_login(
            "member@example.com", "MemberPassphrase-2026!"
        )
        self.admin_headers = self._register_and_login(
            "admin@example.com", "AdminPassphrase-2026!"
        )
        self._seed_gap_review_checkpoint()

    def tearDown(self):
        self.admin_env.stop()
        self.instrument_patch.stop()
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def _register_and_login(self, email, password):
        registered = self.client.post(
            "/auth/register", json={"email": email, "password": password}
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        login = self.client.post(
            "/auth/login", json={"email": email, "password": password}
        )
        self.assertEqual(login.status_code, 200, login.text)
        return {"Authorization": "Bearer " + login.json()["access_token"]}

    def _seed_gap_review_checkpoint(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_backfill_checkpoints
                    (symbol, timeframe, next_end_epoch, status, last_error)
                VALUES ('1HZ100V', 'M1', 3600, 'GAP_REVIEW', 'missing candle range')
                """
            )

    def _checkpoint(self):
        with database.get_connection() as connection:
            return connection.execute(
                """
                SELECT next_end_epoch, status, last_error
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()

    def test_non_admin_cannot_list_checkpoints_review_history_or_authorize_retry(self):
        self.assertEqual(
            self.client.get("/admin/market-backfills", headers=self.member_headers).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/admin/market-backfills/1HZ100V/M1/reviews",
                headers=self.member_headers,
            ).status_code,
            403,
        )
        response = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.member_headers,
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "Investigated provider gap",
            },
        )
        self.assertEqual(response.status_code, 403)
        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["status"], "GAP_REVIEW")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)

    def test_admin_review_records_identity_and_rationale_without_advancing_cursor(self):
        response = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "Provider gap investigated; retry exact range",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "RUNNING")
        self.assertEqual(body["next_end_epoch"], 3600)
        self.assertFalse(body["cursor_advanced"])
        self.assertFalse(body["execution_triggered"])

        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertIsNone(checkpoint["last_error"])

        history = self.client.get(
            "/admin/market-backfills/1HZ100V/M1/reviews",
            headers=self.admin_headers,
        )
        self.assertEqual(history.status_code, 200, history.text)
        audit = history.json()
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["reviews"][0]["decision"], "RETRY_SAME_RANGE")
        self.assertEqual(
            audit["reviews"][0]["rationale"],
            "Provider gap investigated; retry exact range",
        )
        with database.get_connection() as connection:
            admin_user_id = connection.execute(
                "SELECT user_id FROM users WHERE email = 'admin@example.com'"
            ).fetchone()["user_id"]
        self.assertEqual(audit["reviews"][0]["decided_by"], admin_user_id)

    def test_retry_requires_explicit_confirmation_and_non_whitespace_rationale(self):
        missing_confirmation = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": False,
                "rationale": "Investigated provider gap",
            },
        )
        self.assertEqual(missing_confirmation.status_code, 400)

        blank_rationale = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "          ",
            },
        )
        self.assertEqual(blank_rationale.status_code, 422)
        self.assertEqual(self._checkpoint()["status"], "GAP_REVIEW")
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM market_backfill_review_audit"
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_second_review_cannot_reauthorize_a_checkpoint_already_released(self):
        payload = {
            "decision": "RETRY_SAME_RANGE",
            "confirm_retry": True,
            "rationale": "Investigated provider gap",
        }
        first = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json=payload,
        )
        second = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json=payload,
        )
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 409)
        with database.get_connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM market_backfill_review_audit"
            ).fetchone()[0]
        self.assertEqual(count, 1)


    def test_approved_retry_recovers_exact_range_and_advances_only_after_validated_persist(self):
        from datetime import datetime, timezone

        class FakeDerivClient:
            async def collect_candle_range(
                self, symbol, timeframe, start_epoch, end_epoch, max_candles
            ):
                self.requested_range = (symbol, timeframe, start_epoch, end_epoch, max_candles)
                return [
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                        "open": 100,
                        "high": 101,
                        "low": 99,
                        "close": 100,
                        "volume": 0,
                        "source": "deriv",
                    }
                    for epoch in (3480, 3540, 3600)
                ]

        approval = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "Provider range inspected; exact retry approved",
            },
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        fake_client = FakeDerivClient()
        with patch("app.main.DerivPublicMarketDataClient", return_value=fake_client):
            response = self.client.post(
                "/admin/market-backfills/1HZ100V/M1/run",
                headers=self.admin_headers,
                json={"confirm_collection": True, "batch_size": 3},
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(fake_client.requested_range, ("1HZ100V", "M1", 3480, 3600, 3))
        self.assertEqual(body["inserted_count"], 3)
        self.assertEqual(body["next_end_epoch"], 3420)
        self.assertEqual(body["status"], "RUNNING")
        self.assertTrue(body["read_only"])
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])

        with database.get_connection() as connection:
            observations = connection.execute(
                """
                SELECT timestamp FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                ORDER BY timestamp
                """
            ).fetchall()
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM market_backfill_review_audit"
            ).fetchone()[0]
            checkpoint = connection.execute(
                """
                SELECT next_end_epoch, status, batches_completed
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()

        self.assertEqual(len(observations), 3)
        self.assertEqual(audit_count, 1)
        self.assertEqual(checkpoint["next_end_epoch"], 3420)
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["batches_completed"], 1)


if __name__ == "__main__":
    unittest.main()
