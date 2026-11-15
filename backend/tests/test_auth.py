import hashlib
import tempfile
import unittest
from pathlib import Path

from app import database
from app.auth import (
    AuthenticationError,
    authenticate_user,
    create_session,
    get_session_user,
    register_user,
    revoke_session,
)
from app.database import init_db


class AuthenticationPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "auth-test.db"
        init_db()

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
