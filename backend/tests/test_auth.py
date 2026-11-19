import hashlib
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.auth import (
    AuthenticationError,
    AuthenticationRateLimitError,
    authenticate_user,
    create_session,
    get_session_user,
    register_user,
    revoke_session,
)
from app.database import init_db
from app.main import app
from app.safety import deactivate_emergency_stop


class AuthenticationPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "auth-test.db"
        init_db()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_registration_normalizes_email_and_stores_password_hash(self):
        user = register_user("  Test.User@Example.com ", "StrongPassphrase123!")
        self.assertEqual(user["email"], "test.user@example.com")

        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE user_id = ?",
                (user["user_id"],)
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertNotIn("StrongPassphrase123!", row["password_hash"])
        self.assertTrue(row["password_hash"].startswith("pbkdf2_sha256$"))

    def test_registration_creates_participant_joining_milestone(self):
        user = register_user("milestone@example.com", "StrongPassphrase123!")
        with database.get_connection() as connection:
            milestone = connection.execute(
                "SELECT joined_at, first_trade_at FROM participant_milestones WHERE user_id = ?",
                (user["user_id"],)
            ).fetchone()
        self.assertIsNotNone(milestone)
        self.assertTrue(milestone["joined_at"])
        self.assertIsNone(milestone["first_trade_at"])

    def test_participant_timeline_requires_authentication(self):
        response = self.client.get("/auth/participant/timeline")
        self.assertEqual(response.status_code, 401)

    def test_participant_timeline_returns_only_authenticated_users_milestones(self):
        registered = self.client.post(
            "/auth/register",
            json={"email": "timeline@example.com", "password": "StrongPassphrase123!"}
        )
        self.assertEqual(registered.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": "timeline@example.com", "password": "StrongPassphrase123!"}
        )
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}

        response = self.client.get(
            "/auth/participant/timeline",
            headers=headers
        )

        self.assertEqual(response.status_code, 200)
        timeline = response.json()
        self.assertTrue(timeline["joined_at"])
        self.assertIsNone(timeline["first_trade_at"])
        self.assertEqual(timeline["loyalty_milestone_years"], 10)
        self.assertEqual(timeline["loyalty_reward_status"], "TO_BE_DETERMINED")
        self.assertEqual(
            set(timeline),
            {"joined_at", "first_trade_at", "loyalty_milestone_years", "loyalty_reward_status"}
        )

    def test_duplicate_email_is_rejected_case_insensitively(self):
        register_user("same@example.com", "StrongPassphrase123!")
        with self.assertRaises(AuthenticationError):
            register_user("SAME@example.com", "AnotherPassphrase456!")

    def test_short_password_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            register_user("short@example.com", "tiny")

    def test_login_rejects_wrong_password(self):
        register_user("login@example.com", "StrongPassphrase123!")
        with self.assertRaises(AuthenticationError):
            authenticate_user("login@example.com", "WrongPassphrase123!")

    def test_session_token_is_hashed_and_can_be_revoked(self):
        user = register_user("session@example.com", "StrongPassphrase123!")
        session = create_session(user["user_id"])
        token = session["access_token"]
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        self.assertEqual(session["token_type"], "bearer")
        self.assertEqual(get_session_user(token)["user_id"], user["user_id"])

        with database.get_connection() as connection:
            stored = connection.execute(
                "SELECT token_hash FROM auth_sessions"
            ).fetchone()["token_hash"]

        self.assertEqual(stored, token_hash)
        self.assertNotEqual(stored, token)
        self.assertTrue(revoke_session(token))
        with self.assertRaises(AuthenticationError):
            get_session_user(token)

    def test_legacy_accounts_gain_nullable_owner_without_data_loss(self):
        legacy_path = Path(self.temp_dir.name) / "legacy-auth.db"
        with __import__("sqlite3").connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    starting_balance REAL NOT NULL,
                    balance REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO paper_accounts (
                    account_id, starting_balance, balance
                ) VALUES ('LEGACY-ACCOUNT', 10000, 9500);
                """
            )

        database.DB_PATH = legacy_path
        init_db()

        with database.get_connection() as connection:
            account = connection.execute(
                "SELECT account_id, starting_balance, balance, owner_user_id "
                "FROM paper_accounts WHERE account_id = 'LEGACY-ACCOUNT'"
            ).fetchone()

        self.assertIsNotNone(account)
        self.assertEqual(account["starting_balance"], 10000)
        self.assertEqual(account["balance"], 9500)
        self.assertIsNone(account["owner_user_id"])

    def test_registration_login_me_and_logout_api_flow(self):
        registered = self.client.post(
            "/auth/register",
            json={"email": "api@example.com", "password": "StrongPassphrase123!"}
        )
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(registered.json()["email"], "api@example.com")

        login = self.client.post(
            "/auth/login",
            json={"email": "API@example.com", "password": "StrongPassphrase123!"}
        )
        self.assertEqual(login.status_code, 200)
        token = login.json()["access_token"]
        self.assertEqual(login.json()["token_type"], "bearer")

        me = self.client.get(
            "/auth/me", headers={"Authorization": "Bearer " + token}
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "api@example.com")

        logout = self.client.post(
            "/auth/logout", headers={"Authorization": "Bearer " + token}
        )
        self.assertEqual(logout.status_code, 200)
        expired = self.client.get(
            "/auth/me", headers={"Authorization": "Bearer " + token}
        )
        self.assertEqual(expired.status_code, 401)

    def test_auth_me_rejects_missing_or_invalid_bearer_token(self):
        missing = self.client.get("/auth/me")
        invalid = self.client.get(
            "/auth/me", headers={"Authorization": "Bearer not-a-valid-token"}
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)

    def test_registration_api_rejects_duplicate_email(self):
        payload = {"email": "duplicate@example.com", "password": "StrongPassphrase123!"}
        first = self.client.post("/auth/register", json=payload)
        second = self.client.post("/auth/register", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_private_account_routes_reject_anonymous_requests(self):
        response = self.client.get("/accounts/PRIVATE-001")
        emergency = self.client.post("/safety/emergency-stop")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(emergency.status_code, 401)

    def test_user_cannot_access_another_users_paper_account_or_trade(self):
        owner = self.client.post(
            "/auth/register",
            json={"email": "owner@example.com", "password": "OwnerPassphrase-2026!"}
        )
        self.assertEqual(owner.status_code, 200)
        owner_login = self.client.post(
            "/auth/login",
            json={"email": "owner@example.com", "password": "OwnerPassphrase-2026!"}
        )
        owner_headers = {"Authorization": "Bearer " + owner_login.json()["access_token"]}
        created = self.client.post(
            "/accounts",
            json={"account_id": "OWNER-ACCOUNT", "starting_balance": 10000},
            headers=owner_headers
        )
        self.assertEqual(created.status_code, 200)

        stranger = self.client.post(
            "/auth/register",
            json={"email": "stranger@example.com", "password": "StrangerPassphrase-2026!"}
        )
        self.assertEqual(stranger.status_code, 200)
        stranger_login = self.client.post(
            "/auth/login",
            json={"email": "stranger@example.com", "password": "StrangerPassphrase-2026!"}
        )
        stranger_headers = {"Authorization": "Bearer " + stranger_login.json()["access_token"]}
        stranger_accounts = self.client.get("/accounts", headers=stranger_headers)
        owner_accounts = self.client.get("/accounts", headers=owner_headers)
        self.assertEqual(stranger_accounts.status_code, 200)
        self.assertEqual(stranger_accounts.json()["count"], 0)
        self.assertEqual(owner_accounts.json()["count"], 1)

        account = self.client.get("/accounts/OWNER-ACCOUNT", headers=stranger_headers)
        trade = self.client.post(
            "/trades",
            json={
                "account_id": "OWNER-ACCOUNT",
                "client_order_id": "STRANGER-ORDER",
                "symbol": "XAUUSD", "side": "BUY",
                "entry_price": 4000, "stop_loss": 3990,
                "risk_percent": 0.5
            },
            headers=stranger_headers
        )
        self.assertEqual(account.status_code, 404)
        self.assertEqual(trade.status_code, 404)

    def test_foreign_user_cannot_read_preview_or_close_another_users_trade(self):
        owner = self.client.post(
            "/auth/register",
            json={"email": "isolated-owner@example.com", "password": "OwnerPassphrase-2026!"}
        )
        self.assertEqual(owner.status_code, 200)
        owner_login = self.client.post(
            "/auth/login",
            json={"email": "isolated-owner@example.com", "password": "OwnerPassphrase-2026!"}
        )
        owner_headers = {
            "Authorization": "Bearer " + owner_login.json()["access_token"]
        }
        account = self.client.post(
            "/accounts",
            json={"account_id": "ISOLATED-OWNER", "starting_balance": 10000},
            headers=owner_headers,
        )
        self.assertEqual(account.status_code, 200)
        opened = self.client.post(
            "/trades",
            json={
                "account_id": "ISOLATED-OWNER",
                "client_order_id": "ISOLATION-OWNER-ORDER",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5,
            },
            headers=owner_headers,
        )
        self.assertEqual(opened.status_code, 200)
        trade_id = opened.json()["trade_id"]

        stranger = self.client.post(
            "/auth/register",
            json={"email": "isolated-stranger@example.com", "password": "StrangerPassphrase-2026!"}
        )
        self.assertEqual(stranger.status_code, 200)
        stranger_login = self.client.post(
            "/auth/login",
            json={"email": "isolated-stranger@example.com", "password": "StrangerPassphrase-2026!"}
        )
        stranger_headers = {
            "Authorization": "Bearer " + stranger_login.json()["access_token"]
        }

        attempts = (
            self.client.get("/accounts/ISOLATED-OWNER", headers=stranger_headers),
            self.client.get("/accounts/ISOLATED-OWNER/risk", headers=stranger_headers),
            self.client.get("/accounts/ISOLATED-OWNER/trades", headers=stranger_headers),
            self.client.post(
                f"/trades/{trade_id}/profit-preview",
                json={"current_price": 4001},
                headers=stranger_headers,
            ),
            self.client.post(
                f"/trades/{trade_id}/close",
                json={"exit_price": 4001},
                headers=stranger_headers,
            ),
        )
        self.assertEqual([response.status_code for response in attempts], [404] * 5)

        owner_history = self.client.get(
            "/accounts/ISOLATED-OWNER", headers=owner_headers
        )
        self.assertEqual(owner_history.status_code, 200)
        self.assertEqual(owner_history.json()["current_balance"], 10000)
        self.assertEqual(owner_history.json()["open_trades"], 1)
        self.assertEqual(owner_history.json()["closed_trades"], 0)

    def test_eight_failed_logins_trigger_temporary_lockout(self):
        email = "locked@example.com"
        password = "StrongPassphrase123!"
        register_user(email, password)
        for _ in range(8):
            with self.assertRaises(AuthenticationError):
                authenticate_user(email, "IncorrectPassphrase123!")
        with self.assertRaises(AuthenticationRateLimitError):
            authenticate_user(email, password)

    def test_login_api_returns_429_for_locked_email(self):
        email = "api-locked@example.com"
        user = register_user(email, "StrongPassphrase123!")
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO auth_login_attempts (email, attempts, window_started_at, locked_until) "
                "VALUES (?, 8, '2026-11-15T20:00:00+00:00', '2099-01-01T00:00:00+00:00')",
                (email,)
            )
        response = self.client.post(
            "/auth/login",
            json={"email": email, "password": "StrongPassphrase123!"}
        )
        self.assertEqual(response.status_code, 429)

    def test_expired_session_is_rejected(self):
        user = register_user("expired@example.com", "StrongPassphrase123!")
        session = create_session(user["user_id"])
        token_hash = hashlib.sha256(
            session["access_token"].encode("utf-8")
        ).hexdigest()

        with database.get_connection() as connection:
            connection.execute(
                "UPDATE auth_sessions SET expires_at = '2000-01-01T00:00:00+00:00' WHERE token_hash = ?",
                (token_hash,)
            )

        with self.assertRaises(AuthenticationError):
            get_session_user(session["access_token"])


    def test_regular_authenticated_user_cannot_control_global_emergency_stop(self):
        registered = self.client.post(
            "/auth/register",
            json={"email": "member@example.com", "password": "MemberPassphrase-2026!"}
        )
        self.assertEqual(registered.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": "member@example.com", "password": "MemberPassphrase-2026!"}
        )
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}

        with patch.dict(
            "os.environ",
            {"TAIMOBOT_ADMIN_EMAILS": "admin@example.com"},
            clear=True
        ):
            activate = self.client.post("/safety/emergency-stop", headers=headers)
            resume = self.client.post("/safety/resume", headers=headers)

        self.assertEqual(activate.status_code, 403)
        self.assertEqual(resume.status_code, 403)

    def test_configured_admin_can_activate_and_resume_emergency_stop(self):
        deactivate_emergency_stop()
        registered = self.client.post(
            "/auth/register",
            json={"email": "admin@example.com", "password": "AdminPassphrase-2026!"}
        )
        self.assertEqual(registered.status_code, 200)
        login = self.client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "AdminPassphrase-2026!"}
        )
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}

        with __import__("unittest").mock.patch.dict(
            "os.environ",
            {"TAIMOBOT_ADMIN_EMAILS": " admin@example.com,other@example.com "},
            clear=True
        ):
            activate = self.client.post("/safety/emergency-stop", headers=headers)
            self.assertEqual(activate.status_code, 200)
            self.assertTrue(activate.json()["emergency_stop"])
            resume = self.client.post("/safety/resume", headers=headers)
            self.assertEqual(resume.status_code, 200)
            self.assertFalse(resume.json()["emergency_stop"])


if __name__ == "__main__":
    unittest.main()
