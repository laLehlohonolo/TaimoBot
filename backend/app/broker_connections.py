"""Participant-scoped broker connection intents.

This module records a participant's intent to connect a broker platform. It
never stores broker credentials and does not establish a broker session.
"""
import sqlite3
import uuid
from datetime import datetime, timezone

from .broker_catalogue import list_broker_catalogue
from .database import get_connection


CONNECTION_STATES = {"REQUESTED", "PAUSED", "REVOKED"}


def ensure_broker_connection_schema(connection):
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS participant_broker_connections (
            connection_id TEXT PRIMARY KEY,
            participant_user_id TEXT NOT NULL,
            broker_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'REQUESTED'
                CHECK (state IN ('REQUESTED', 'PAUSED', 'REVOKED')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY (participant_user_id) REFERENCES users(user_id)
                ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_participant_broker_connections_owner
        ON participant_broker_connections(participant_user_id, created_at)
        """
    )


def _candidate(broker_id, platform):
    catalogue = list_broker_catalogue()
    broker = next(
        (item for item in catalogue["brokers"] if item["broker_id"] == broker_id),
        None,
    )
    if broker is None:
        raise ValueError("Broker is not present in the candidate catalogue.")
    allowed = {item.casefold() for item in broker["platform_candidates"]}
    if platform.casefold() not in allowed:
        raise ValueError("Platform is not listed for this broker candidate.")
    return broker


def _serialize(row):
    return {
        "connection_id": row["connection_id"],
        "broker_id": row["broker_id"],
        "platform": row["platform"],
        "state": row["state"],
        "integration_status": "CANDIDATE_ONLY",
        "connection_status": "NOT_CONNECTED",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "revoked_at": row["revoked_at"],
        "credentials_required_by_api": False,
        "live_order_routing": False,
    }


def create_connection_intent(participant_user_id, broker_id, platform):
    broker = _candidate(broker_id, platform)
    now = datetime.now(timezone.utc).isoformat()
    connection_id = str(uuid.uuid4())
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO participant_broker_connections (
                connection_id, participant_user_id, broker_id, platform,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'REQUESTED', ?, ?)
            """,
            (connection_id, participant_user_id, broker["broker_id"],
             platform, now, now),
        )
        row = connection.execute(
            """
            SELECT * FROM participant_broker_connections
            WHERE connection_id = ? AND participant_user_id = ?
            """,
            (connection_id, participant_user_id),
        ).fetchone()
    return _serialize(row)


def list_connection_intents(participant_user_id):
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM participant_broker_connections
            WHERE participant_user_id = ?
            ORDER BY created_at DESC, connection_id
            """,
            (participant_user_id,),
        ).fetchall()
    return [_serialize(row) for row in rows]


def transition_connection_intent(participant_user_id, connection_id, action):
    transitions = {
        "pause": ("REQUESTED", "PAUSED", None),
        "resume": ("PAUSED", "REQUESTED", None),
        "revoke": (("REQUESTED", "PAUSED"), "REVOKED", "now"),
    }
    if action not in transitions:
        raise ValueError("Unsupported connection action.")
    allowed_from, target, revoked = transitions[action]
    now = datetime.now(timezone.utc).isoformat()
    allowed_states = (allowed_from,) if isinstance(allowed_from, str) else allowed_from
    placeholders = ",".join("?" for _ in allowed_states)
    with get_connection() as connection:
        cursor = connection.execute(
            f"""
            UPDATE participant_broker_connections
            SET state = ?, updated_at = ?, revoked_at = ?
            WHERE connection_id = ? AND participant_user_id = ?
              AND state IN ({placeholders})
            """,
            (target, now, now if revoked else None, connection_id,
             participant_user_id, *allowed_states),
        )
        if cursor.rowcount != 1:
            exists = connection.execute(
                """
                SELECT 1 FROM participant_broker_connections
                WHERE connection_id = ? AND participant_user_id = ?
                """,
                (connection_id, participant_user_id),
            ).fetchone()
            if exists is None:
                return None
            raise ValueError("Connection intent cannot make that state transition.")
        row = connection.execute(
            """
            SELECT * FROM participant_broker_connections
            WHERE connection_id = ? AND participant_user_id = ?
            """,
            (connection_id, participant_user_id),
        ).fetchone()
    return _serialize(row)
