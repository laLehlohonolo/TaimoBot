import hashlib
import hmac
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from .database import get_connection

PBKDF2_ITERATIONS = 600_000
SESSION_HOURS = 12
PASSWORD_MIN_LENGTH = 12
LOGIN_FAILURE_LIMIT = 8
LOGIN_WINDOW_MINUTES = 15
LOGIN_LOCK_MINUTES = 15
DUMMY_PASSWORD_HASH = "pbkdf2_sha256$600000$" + ("00" * 16) + "$" + ("00" * 32)


class AuthenticationError(ValueError):
    """Raised when credentials or an authentication session are invalid."""


class AuthenticationRateLimitError(AuthenticationError):
    """Raised when repeated login failures temporarily lock an email."""


def _utc_now():
    return datetime.now(timezone.utc)


def _hash_password(password):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise AuthenticationError(
            "Password must contain at least " + str(PASSWORD_MIN_LENGTH) + " characters."
        )
    if len(password) > 1024:
        raise AuthenticationError("Password is too long.")

    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256$" + str(PBKDF2_ITERATIONS) + "$" + salt.hex() + "$" + digest.hex()


def _verify_password(password, encoded):
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (AttributeError, TypeError, ValueError):
        return False


def _normalize_email(email):
    value = str(email).strip().lower()
    if len(value) > 254 or value.count("@") != 1:
        raise AuthenticationError("A valid email address is required.")
    local, domain = value.split("@")
    if not local or not domain or "." not in domain or " " in value:
        raise AuthenticationError("A valid email address is required.")
    return value


def register_user(email, password):
    normalized_email = _normalize_email(email)
    password_hash = _hash_password(password)
    user_id = str(uuid.uuid4())

    try:
        with get_connection() as connection:
            connection.execute(
                "INSERT INTO users (user_id, email, password_hash) VALUES (?, ?, ?)",
                (user_id, normalized_email, password_hash)
            )
            connection.execute(
                "INSERT INTO participant_milestones (user_id) VALUES (?)",
                (user_id,)
            )
    except sqlite3.IntegrityError as error:
        raise AuthenticationError(
            "An account with this email already exists."
        ) from error

    return {"user_id": user_id, "email": normalized_email}


def _check_login_allowed(email):
    now = _utc_now()
    with get_connection() as connection:
        row = connection.execute(
            "SELECT attempts, window_started_at, locked_until "
            "FROM auth_login_attempts WHERE email = ?",
            (email,)
        ).fetchone()

        if row is None or not row["locked_until"]:
            return

        locked_until = datetime.fromisoformat(row["locked_until"])
        if locked_until > now:
            raise AuthenticationRateLimitError(
                "Too many failed login attempts. Try again later."
            )

        connection.execute(
            "DELETE FROM auth_login_attempts WHERE email = ?", (email,)
        )


def _record_login_failure(email):
    now = _utc_now()
    window_start = now - timedelta(minutes=LOGIN_WINDOW_MINUTES)

    with get_connection() as connection:
        row = connection.execute(
            "SELECT attempts, window_started_at FROM auth_login_attempts "
            "WHERE email = ?",
            (email,)
        ).fetchone()

        if row is None or datetime.fromisoformat(row["window_started_at"]) < window_start:
            attempts = 1
            started_at = now.isoformat()
        else:
            attempts = int(row["attempts"]) + 1
            started_at = row["window_started_at"]

        locked_until = (
            (now + timedelta(minutes=LOGIN_LOCK_MINUTES)).isoformat()
            if attempts >= LOGIN_FAILURE_LIMIT else None
        )

        connection.execute(
            """
            INSERT INTO auth_login_attempts (
                email, attempts, window_started_at, locked_until
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                attempts = excluded.attempts,
                window_started_at = excluded.window_started_at,
                locked_until = excluded.locked_until
            """,
            (email, attempts, started_at, locked_until)
        )


def _clear_login_failures(email):
    with get_connection() as connection:
        connection.execute(
            "DELETE FROM auth_login_attempts WHERE email = ?", (email,)
        )


def authenticate_user(email, password):
    try:
        normalized_email = _normalize_email(email)
    except AuthenticationError:
        normalized_email = ""

    _check_login_allowed(normalized_email)

    with get_connection() as connection:
        user = connection.execute(
            "SELECT user_id, email, password_hash, role, email_verified FROM users WHERE email = ?",
            (normalized_email,)
        ).fetchone()

    encoded_hash = user["password_hash"] if user else DUMMY_PASSWORD_HASH
    valid_password = _verify_password(password, encoded_hash)

    if user is None or not valid_password:
        _record_login_failure(normalized_email)
        raise AuthenticationError("Invalid email or password.")

    _clear_login_failures(normalized_email)
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "role": user["role"],
        "email_verified": bool(user["email_verified"]),
    }


def create_session(user_id):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (_utc_now() + timedelta(hours=SESSION_HOURS)).isoformat()

    with get_connection() as connection:
        user = connection.execute(
            "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if user is None:
            raise AuthenticationError("User account not found.")
        connection.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (token_hash, user_id, expires_at)
        )

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at
    }


def get_session_user(token):
    if not isinstance(token, str) or not token:
        raise AuthenticationError("Authentication required.")

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = _utc_now().isoformat()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT users.user_id, users.email, users.role, users.email_verified
            FROM auth_sessions
            JOIN users ON users.user_id = auth_sessions.user_id
            WHERE auth_sessions.token_hash = ?
              AND auth_sessions.expires_at > ?
            """,
            (token_hash, now)
        ).fetchone()

    if row is None:
        raise AuthenticationError("Invalid or expired session.")

    return {
        "user_id": row["user_id"],
        "email": row["email"],
        "role": row["role"],
        "email_verified": bool(row["email_verified"]),
    }



def change_password(user_id, current_password, new_password, current_token):
    """Change a user's password and revoke every other active session."""
    if not isinstance(current_token, str) or not current_token:
        raise AuthenticationError("An active session is required.")

    new_password_hash = _hash_password(new_password)
    current_token_hash = hashlib.sha256(
        current_token.encode("utf-8")
    ).hexdigest()
    now = _utc_now().isoformat()

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        account = connection.execute(
            "SELECT password_hash FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if account is None or not _verify_password(current_password, account["password_hash"]):
            raise AuthenticationError("Current password is incorrect.")

        active_session = connection.execute(
            """
            SELECT 1 FROM auth_sessions
            WHERE user_id = ? AND token_hash = ? AND expires_at > ?
            """,
            (user_id, current_token_hash, now),
        ).fetchone()
        if active_session is None:
            raise AuthenticationError("An active session is required.")

        connection.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (new_password_hash, user_id),
        )
        connection.execute(
            "DELETE FROM auth_sessions WHERE user_id = ? AND token_hash != ?",
            (user_id, current_token_hash),
        )

    return True

def revoke_session(token):
    if not isinstance(token, str) or not token:
        return False

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
        )
    return cursor.rowcount == 1
