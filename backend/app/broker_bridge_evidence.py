"""Participant-scoped persistence for authenticated, read-only broker evidence.

This module accepts only already-authenticated bridge evidence. It does not
resolve keys, open broker sessions, or expose trading operations.
"""
import json
import math
import sqlite3
from datetime import datetime, timezone

from .database import get_connection


class BridgeEvidenceError(ValueError):
    """Raised when bridge evidence is malformed or conflicts with stored data."""


def ensure_bridge_evidence_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_bridge_evidence (
            evidence_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id TEXT NOT NULL,
            participant_user_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('HEARTBEAT', 'QUOTE')),
            provider_timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (connection_id)
                REFERENCES participant_broker_connections(connection_id)
                ON DELETE CASCADE,
            FOREIGN KEY (participant_user_id)
                REFERENCES users(user_id)
                ON DELETE CASCADE,
            UNIQUE (connection_id, evidence_id)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_bridge_evidence_owner_time
        ON broker_bridge_evidence(participant_user_id, connection_id, received_at)
        """
    )


def _validate_timestamp(value):
    if not isinstance(value, str):
        raise BridgeEvidenceError("provider_timestamp must be an ISO-8601 string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BridgeEvidenceError("provider_timestamp is invalid.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BridgeEvidenceError("provider_timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat()


def _validate_evidence(evidence):
    if not isinstance(evidence, dict):
        raise BridgeEvidenceError("Evidence must be an object.")
    kind = evidence.get("kind")
    if kind == "HEARTBEAT":
        allowed = {"evidence_id", "kind", "provider_timestamp"}
    elif kind == "QUOTE":
        allowed = {
            "evidence_id", "kind", "provider_timestamp",
            "instrument", "server_symbol", "bid", "ask",
        }
    else:
        raise BridgeEvidenceError("Only HEARTBEAT and QUOTE evidence are accepted.")
    if set(evidence) != allowed:
        raise BridgeEvidenceError("Evidence contains missing or unsupported fields.")
    evidence_id = evidence.get("evidence_id")
    if not isinstance(evidence_id, str) or not 8 <= len(evidence_id) <= 160:
        raise BridgeEvidenceError("evidence_id must contain 8 to 160 characters.")
    provider_timestamp = _validate_timestamp(evidence.get("provider_timestamp"))
    normalized = dict(evidence)
    normalized["provider_timestamp"] = provider_timestamp

    if kind == "QUOTE":
        instrument = evidence.get("instrument")
        server_symbol = evidence.get("server_symbol")
        if not isinstance(instrument, str) or not 1 <= len(instrument) <= 30:
            raise BridgeEvidenceError("instrument is invalid.")
        if not isinstance(server_symbol, str) or not 1 <= len(server_symbol) <= 60:
            raise BridgeEvidenceError("server_symbol is invalid.")
        bid = evidence.get("bid")
        ask = evidence.get("ask")
        for field, value in (("bid", bid), ("ask", ask)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise BridgeEvidenceError(f"{field} must be numeric.")
            if not math.isfinite(value) or value <= 0:
                raise BridgeEvidenceError(f"{field} must be finite and positive.")
        if ask < bid:
            raise BridgeEvidenceError("ask cannot be below bid.")
    return kind, evidence_id, provider_timestamp, normalized


def persist_authenticated_bridge_evidence(connection_id, evidence, *, accepted_at=None):
    """Persist evidence using connection ownership from the database, never payload claims.

    The caller must invoke this only after authenticate_bridge_request succeeds.
    Duplicate identical evidence is idempotent; conflicting reuse of an evidence
    ID is rejected.
    """
    kind, evidence_id, provider_timestamp, normalized = _validate_evidence(evidence)
    received_at = accepted_at or datetime.now(timezone.utc).isoformat()
    try:
        parsed_received = datetime.fromisoformat(received_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise BridgeEvidenceError("accepted_at must be a valid timestamp.") from error
    if parsed_received.tzinfo is None or parsed_received.utcoffset() is None:
        raise BridgeEvidenceError("accepted_at must include a timezone.")
    received_at = parsed_received.astimezone(timezone.utc).isoformat()
    payload_json = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        owner = connection.execute(
            """
            SELECT participant_user_id, state
            FROM participant_broker_connections
            WHERE connection_id = ?
            """,
            (connection_id,),
        ).fetchone()
        if owner is None or owner["state"] != "REQUESTED":
            raise BridgeEvidenceError("Bridge connection is unknown or inactive.")
        existing = connection.execute(
            """
            SELECT payload_json FROM broker_bridge_evidence
            WHERE connection_id = ? AND evidence_id = ?
            """,
            (connection_id, evidence_id),
        ).fetchone()
        if existing is not None:
            if existing["payload_json"] != payload_json:
                raise BridgeEvidenceError("Evidence ID already exists with different content.")
            return {
                "status": "DUPLICATE_IGNORED",
                "connection_id": connection_id,
                "evidence_id": evidence_id,
                "evidence_kind": kind,
                "live_order_routing": False,
            }
        connection.execute(
            """
            INSERT INTO broker_bridge_evidence (
                connection_id, participant_user_id, evidence_id,
                evidence_kind, provider_timestamp, received_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                connection_id, owner["participant_user_id"], evidence_id,
                kind, provider_timestamp, received_at, payload_json,
            ),
        )
    return {
        "status": "STORED",
        "connection_id": connection_id,
        "evidence_id": evidence_id,
        "evidence_kind": kind,
        "received_at": received_at,
        "live_order_routing": False,
    }
