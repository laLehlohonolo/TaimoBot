"""Authentication primitives for a future read-only broker bridge.

Secrets are supplied by a vault adapter; this module never stores or retrieves
shared secrets and exposes no broker order capability.
"""
import hashlib
import hmac
import re
import sqlite3
import time
from datetime import datetime, timezone

from .database import get_connection


_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_HEX_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class BridgeAuthenticationError(ValueError):
    """Raised when bridge evidence fails authentication or replay checks."""


def canonical_bridge_message(connection_id, timestamp, nonce, raw_body):
    body_hash = hashlib.sha256(raw_body).hexdigest()
    return f"v1\n{connection_id}\n{timestamp}\n{nonce}\n{body_hash}".encode("utf-8")


def sign_bridge_message(connection_id, timestamp, nonce, raw_body, shared_secret):
    """Create the HMAC signature; intended for bridge clients and deterministic tests."""
    if not isinstance(shared_secret, bytes) or len(shared_secret) < 32:
        raise ValueError("Bridge secret must contain at least 32 bytes.")
    return hmac.new(
        shared_secret,
        canonical_bridge_message(connection_id, timestamp, nonce, raw_body),
        hashlib.sha256,
    ).hexdigest()


def ensure_bridge_security_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_bridge_nonces (
            connection_id TEXT NOT NULL,
            nonce TEXT NOT NULL,
            request_timestamp INTEGER NOT NULL,
            accepted_at TEXT NOT NULL,
            PRIMARY KEY (connection_id, nonce),
            FOREIGN KEY (connection_id)
                REFERENCES participant_broker_connections(connection_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_bridge_nonces_accepted_at
        ON broker_bridge_nonces(accepted_at)
        """
    )


def authenticate_bridge_request(
    connection_id,
    timestamp,
    nonce,
    signature,
    raw_body,
    shared_secret,
    *,
    now=None,
    max_clock_skew_seconds=60,
):
    """Authenticate one bridge request and atomically consume its nonce.

    The caller must resolve shared_secret from a managed vault using the
    server-owned connection_id. The payload cannot choose its participant.
    """
    if not isinstance(connection_id, str) or not connection_id:
        raise BridgeAuthenticationError("Invalid connection identity.")
    if not isinstance(timestamp, str) or not timestamp.isdigit():
        raise BridgeAuthenticationError("Invalid request timestamp.")
    request_time = int(timestamp)
    if not isinstance(nonce, str) or not _NONCE_PATTERN.fullmatch(nonce):
        raise BridgeAuthenticationError("Invalid request nonce.")
    if not isinstance(signature, str) or not _HEX_SIGNATURE_PATTERN.fullmatch(signature):
        raise BridgeAuthenticationError("Invalid request signature.")
    if not isinstance(raw_body, bytes):
        raise BridgeAuthenticationError("Raw request body must be bytes.")
    if not isinstance(shared_secret, bytes) or len(shared_secret) < 32:
        raise BridgeAuthenticationError("Bridge key is unavailable or invalid.")
    if not isinstance(max_clock_skew_seconds, int) or max_clock_skew_seconds < 1:
        raise ValueError("Clock-skew window must be a positive integer.")

    current_time = int(time.time() if now is None else now)
    if abs(current_time - request_time) > max_clock_skew_seconds:
        raise BridgeAuthenticationError("Bridge request timestamp is outside the allowed window.")

    expected = sign_bridge_message(
        connection_id, timestamp, nonce, raw_body, shared_secret
    )
    if not hmac.compare_digest(expected, signature):
        raise BridgeAuthenticationError("Bridge request signature is invalid.")

    accepted_at = datetime.fromtimestamp(current_time, timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                """
                SELECT state FROM participant_broker_connections
                WHERE connection_id = ?
                """,
                (connection_id,),
            ).fetchone()
            if owner is None or owner["state"] != "REQUESTED":
                raise BridgeAuthenticationError("Bridge connection is unknown or inactive.")
            connection.execute(
                """
                INSERT INTO broker_bridge_nonces (
                    connection_id, nonce, request_timestamp, accepted_at
                ) VALUES (?, ?, ?, ?)
                """,
                (connection_id, nonce, request_time, accepted_at),
            )
    except sqlite3.IntegrityError as error:
        raise BridgeAuthenticationError("Bridge nonce has already been used.") from error

    return {
        "authenticated": True,
        "connection_id": connection_id,
        "request_timestamp": request_time,
        "accepted_at": accepted_at,
        "replay_protection": "PERSISTENT",
        "live_order_routing": False,
    }
