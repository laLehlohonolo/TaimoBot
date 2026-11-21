import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.main import app


class ParticipantDisplayCurrencyApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "currency-preference-test.db"
        init_db()
        self.client = TestClient(app)
        registration = self.client.post(
            "/auth/register",
            json={"email": "currency@example.com", "password": "StrongPassphrase123!"},
        )
        self.assertEqual(registration.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": "currency@example.com", "password": "StrongPassphrase123!"},
        )
        self.assertEqual(login.status_code, 200)
        self.headers = {"Authorization": "Bearer " + login.json()["access_token"]}

    def tearDown(self):
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_read_requires_authentication(self):
        response = self.client.get("/participant/currency-preference")
        self.assertEqual(response.status_code, 401)

    def test_unselected_participant_has_no_automatic_default(self):
        response = self.client.get("/participant/currency-preference", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["currency"], None)
        self.assertFalse(response.json()["selected"])
        self.assertFalse(response.json()["locked"])

    def test_participant_can_select_each_supported_currency(self):
        for currency in ("ZAR", "USD", "EUR", "GBP", "KWD"):
            with self.subTest(currency=currency):
                registration = self.client.post(
                    "/auth/register",
                    json={"email": currency.lower() + "@example.com", "password": "StrongPassphrase123!"},
                )
                login = self.client.post(
                    "/auth/login",
                    json={"email": currency.lower() + "@example.com", "password": "StrongPassphrase123!"},
                )
                headers = {"Authorization": "Bearer " + login.json()["access_token"]}
                response = self.client.post(
                    "/participant/currency-preference",
                    json={"currency": currency, "confirmed": True},
                    headers=headers,
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["currency"], currency)
                self.assertTrue(response.json()["locked"])

    def test_selection_requires_explicit_confirmation(self):
        response = self.client.post(
            "/participant/currency-preference",
            json={"currency": "ZAR", "confirmed": False},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)

    def test_second_selection_is_rejected_even_when_same_currency(self):
        first = self.client.post(
            "/participant/currency-preference",
            json={"currency": "USD", "confirmed": True},
            headers=self.headers,
        )
        second = self.client.post(
            "/participant/currency-preference",
            json={"currency": "USD", "confirmed": True},
            headers=self.headers,
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)

    def test_change_of_currency_is_rejected_and_original_remains(self):
        first = self.client.post(
            "/participant/currency-preference",
            json={"currency": "EUR", "confirmed": True},
            headers=self.headers,
        )
        second = self.client.post(
            "/participant/currency-preference",
            json={"currency": "KWD", "confirmed": True},
            headers=self.headers,
        )
        current = self.client.get("/participant/currency-preference", headers=self.headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(current.json()["currency"], "EUR")

    def test_concurrent_first_selections_allow_exactly_one_winner(self):
        from concurrent.futures import ThreadPoolExecutor

        def submit(currency):
            with TestClient(app) as client:
                return client.post(
                    "/participant/currency-preference",
                    json={"currency": currency, "confirmed": True},
                    headers=self.headers,
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(submit, ("USD", "EUR")))

        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        current = self.client.get("/participant/currency-preference", headers=self.headers)
        self.assertIn(current.json()["currency"], ("USD", "EUR"))

    def test_unsupported_currency_is_rejected(self):
        response = self.client.post(
            "/participant/currency-preference",
            json={"currency": "JPY", "confirmed": True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)

    def test_selection_survives_database_reinitialisation_and_fresh_login(self):
        selected = self.client.post(
            "/participant/currency-preference",
            json={"currency": "KWD", "confirmed": True},
            headers=self.headers,
        )
        self.assertEqual(selected.status_code, 201)
        selected_at = selected.json()["selected_at"]

        # Re-running the idempotent schema initialisation must not erase the choice.
        init_db()

        # Authenticate again to ensure the preference belongs to the participant,
        # rather than relying on the original in-memory client/session.
        fresh_client = TestClient(app)
        try:
            login = fresh_client.post(
                "/auth/login",
                json={"email": "currency@example.com", "password": "StrongPassphrase123!"},
            )
            self.assertEqual(login.status_code, 200)
            fresh_headers = {
                "Authorization": "Bearer " + login.json()["access_token"]
            }
            restored = fresh_client.get(
                "/participant/currency-preference",
                headers=fresh_headers,
            )
        finally:
            fresh_client.close()

        self.assertEqual(restored.status_code, 200)
        self.assertTrue(restored.json()["locked"])
        self.assertEqual(restored.json()["currency"], "KWD")
        self.assertEqual(restored.json()["selected_at"], selected_at)

    def test_database_prevents_update_and_delete(self):
        self.client.post(
            "/participant/currency-preference",
            json={"currency": "GBP", "confirmed": True},
            headers=self.headers,
        )
        with database.get_connection() as connection:
            with self.assertRaisesRegex(Exception, "immutable"):
                connection.execute(
                    "UPDATE participant_display_currency_preferences SET currency = 'KWD' WHERE user_id = (SELECT user_id FROM users WHERE email = 'currency@example.com')"
                )
            with self.assertRaisesRegex(Exception, "cannot be deleted"):
                connection.execute(
                    "DELETE FROM participant_display_currency_preferences WHERE user_id = (SELECT user_id FROM users WHERE email = 'currency@example.com')"
                )


if __name__ == "__main__":
    unittest.main()
