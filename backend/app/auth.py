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


class AuthenticationError(ValueError):
    """Raised when credentials or an authentication session are invalid."""


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
    except sqlite3.IntegrityError as error:
        raise AuthenticationError(
            "An account with this email already exists."
        ) from error

    return {"user_id": user_id, "email": normalized_email}


def authenticate_user(email, password):
    try:
        normalized_email = _normalize_email(email)
    except AuthenticationError:
        normalized_email = ""

    with get_connection() as connection:
        user = connection.execute(
            "SELECT user_id, email, password_hash FROM users WHERE email = ?",
            (normalized_email,)
        ).fetchone()

    if user is None or not _verify_password(password, user["password_hash"]):
        raise AuthenticationError("Invalid email or password.")

    return {"user_id": user["user_id"], "email": user["email"]}


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
            SELECT users.user_id, users.email
            FROM auth_sessions
            JOIN users ON users.user_id = auth_sessions.user_id
            WHERE auth_sessions.token_hash = ?
              AND auth_sessions.expires_at > ?
            """,
            (token_hash, now)
        ).fetchone()

    if row is None:
        raise AuthenticationError("Invalid or expired session.")

    return {"user_id": row["user_id"], "email": row["email"]}


def revoke_session(token):
    if not isinstance(token, str) or not token:
        return False

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with get_connection() as connection:
        cursor = connection.execute(
            "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
        )
    return cursor.rowcount == 1
