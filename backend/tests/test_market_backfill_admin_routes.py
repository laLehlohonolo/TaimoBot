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
        if email == "admin@example.com":
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE users SET role = 'ADMIN', email_verified = 1 WHERE user_id = ?",
                    (registered.json()["user_id"],),
                )
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
                SELECT observed_at FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                ORDER BY observed_at
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


    def test_recovery_is_idempotent_preserves_existing_candle_and_advances_exact_cursor(self):
        from datetime import datetime, timezone

        existing_timestamp = datetime.fromtimestamp(3540, timezone.utc).isoformat()
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_observations
                    (symbol, timeframe, observed_at, source, open, high, low, close, volume)
                VALUES ('1HZ100V', 'M1', ?, 'deriv', 700, 780, 690, 777, 4)
                """,
                (existing_timestamp,),
            )

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
                "rationale": "Existing candle checked; exact range retry approved",
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
        self.assertEqual(body["received_count"], 3)
        self.assertEqual(body["inserted_count"], 2)
        self.assertEqual(body["duplicate_count"], 1)
        self.assertEqual(body["next_end_epoch"], 3420)
        self.assertEqual(body["status"], "RUNNING")

        with database.get_connection() as connection:
            observations = connection.execute(
                """
                SELECT observed_at, close FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1' AND source = 'deriv'
                ORDER BY observed_at
                """
            ).fetchall()
            checkpoint = connection.execute(
                """
                SELECT next_end_epoch, status, batches_completed
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()

        self.assertEqual(len(observations), 3)
        preserved = next(row for row in observations if row["observed_at"] == existing_timestamp)
        self.assertEqual(preserved["close"], 777)
        self.assertEqual(checkpoint["next_end_epoch"], 3420)
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["batches_completed"], 1)


    def test_approved_retry_with_incomplete_provider_batch_returns_to_gap_review_without_cursor_advance(self):
        from datetime import datetime, timezone

        class IncompleteDerivClient:
            async def collect_candle_range(
                self, symbol, timeframe, start_epoch, end_epoch, max_candles
            ):
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
                    for epoch in (3480, 3600)
                ]

        approval = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "Retry approved after reviewing incomplete provider history",
            },
        )
        self.assertEqual(approval.status_code, 200, approval.text)

        with patch("app.main.DerivPublicMarketDataClient", return_value=IncompleteDerivClient()):
            response = self.client.post(
                "/admin/market-backfills/1HZ100V/M1/run",
                headers=self.admin_headers,
                json={"confirm_collection": True, "batch_size": 3},
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "GAP_REVIEW")
        self.assertEqual(body["next_end_epoch"], 3600)
        self.assertEqual(body["inserted_count"], 2)
        self.assertFalse(body["execution_triggered"])
        self.assertFalse(body["signals_generated"])

        checkpoint = self._checkpoint()
        self.assertEqual(checkpoint["status"], "GAP_REVIEW")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertIn("1 expected candle(s) are missing", checkpoint["last_error"])

        with database.get_connection() as connection:
            stored = connection.execute(
                """
                SELECT COUNT(*) FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()[0]
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM market_backfill_review_audit"
            ).fetchone()[0]
        self.assertEqual(stored, 2)
        self.assertEqual(audit_count, 1)


    def test_second_approved_retry_completes_partial_gap_without_overwriting_saved_candles(self):
        from datetime import datetime, timezone

        class BatchClient:
            def __init__(self, epochs, price):
                self.epochs = epochs
                self.price = price

            async def collect_candle_range(
                self, symbol, timeframe, start_epoch, end_epoch, max_candles
            ):
                self.requested_range = (symbol, timeframe, start_epoch, end_epoch, max_candles)
                return [
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                        "open": self.price,
                        "high": self.price + 1,
                        "low": self.price - 1,
                        "close": self.price,
                        "volume": 0,
                        "source": "deriv",
                    }
                    for epoch in self.epochs
                ]

        approval_payload = {
            "decision": "RETRY_SAME_RANGE",
            "confirm_retry": True,
            "rationale": "Retry exact range after inspecting remaining provider gap",
        }

        first_approval = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json=approval_payload,
        )
        self.assertEqual(first_approval.status_code, 200, first_approval.text)

        partial_client = BatchClient((3480, 3600), 100)
        with patch("app.main.DerivPublicMarketDataClient", return_value=partial_client):
            partial_response = self.client.post(
                "/admin/market-backfills/1HZ100V/M1/run",
                headers=self.admin_headers,
                json={"confirm_collection": True, "batch_size": 3},
            )
        self.assertEqual(partial_response.status_code, 200, partial_response.text)
        self.assertEqual(partial_response.json()["status"], "GAP_REVIEW")
        self.assertEqual(partial_response.json()["next_end_epoch"], 3600)

        second_approval = self.client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            headers=self.admin_headers,
            json={
                **approval_payload,
                "rationale": "Previously stored candles checked; retry remaining gap",
            },
        )
        self.assertEqual(second_approval.status_code, 200, second_approval.text)

        complete_client = BatchClient((3480, 3540, 3600), 200)
        with patch("app.main.DerivPublicMarketDataClient", return_value=complete_client):
            complete_response = self.client.post(
                "/admin/market-backfills/1HZ100V/M1/run",
                headers=self.admin_headers,
                json={"confirm_collection": True, "batch_size": 3},
            )
        self.assertEqual(complete_response.status_code, 200, complete_response.text)
        body = complete_response.json()
        self.assertEqual(body["status"], "RUNNING")
        self.assertEqual(body["next_end_epoch"], 3420)
        self.assertEqual(body["received_count"], 3)
        self.assertEqual(body["inserted_count"], 1)
        self.assertEqual(body["duplicate_count"], 2)
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])

        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT observed_at, open, close FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1' AND source = 'deriv'
                ORDER BY observed_at
                """
            ).fetchall()
            checkpoint = connection.execute(
                """
                SELECT next_end_epoch, status, batches_completed
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()
            audit_count = connection.execute(
                "SELECT COUNT(*) FROM market_backfill_review_audit"
            ).fetchone()[0]

        self.assertEqual(len(rows), 3)
        by_epoch = {
            int(datetime.fromisoformat(row["observed_at"]).timestamp()): row
            for row in rows
        }
        self.assertEqual(by_epoch[3480]["close"], 100)
        self.assertEqual(by_epoch[3600]["close"], 100)
        self.assertEqual(by_epoch[3540]["close"], 200)
        self.assertEqual(checkpoint["next_end_epoch"], 3420)
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["batches_completed"], 2)
        self.assertEqual(audit_count, 2)


if __name__ == "__main__":
    unittest.main()
