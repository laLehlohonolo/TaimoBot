"""Taimo Security & System Integrity (TSSI) fail-closed trading interlock.

This module is deliberately independent of strategy and execution decisions.
It provides a persistent security lock and append-only event record. It does
not yet claim automated threat detection or remote alert delivery.
"""
from .database import get_connection


def ensure_tssi_schema(connection):
    """Create the independent TSSI state and append-only event journal."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tssi_security_state (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            lock_active INTEGER NOT NULL DEFAULT 0 CHECK (lock_active IN (0, 1)),
            lock_reason TEXT,
            lock_event_id TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        INSERT OR IGNORE INTO tssi_security_state
            (singleton_id, lock_active, lock_reason, lock_event_id)
        VALUES (1, 0, NULL, NULL);

        CREATE TABLE IF NOT EXISTS tssi_security_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL CHECK (event_type IN ('LOCK_ACTIVATED')),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_tssi_security_events_created
            ON tssi_security_events(created_at DESC);

        CREATE TRIGGER IF NOT EXISTS prevent_tssi_event_update
        BEFORE UPDATE ON tssi_security_events
        BEGIN
            SELECT RAISE(ABORT, 'TSSI security events are append-only.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_tssi_event_delete
        BEFORE DELETE ON tssi_security_events
        BEGIN
            SELECT RAISE(ABORT, 'TSSI security events are append-only.');
        END;
        """
    )


def activate_security_lock(reason, event_id, actor="SYSTEM"):
    """Persist a security lock without allowing event-ID reuse to rewrite history."""
    clean_reason = str(reason or "").strip()
    clean_event_id = str(event_id or "").strip()
    clean_actor = str(actor or "").strip()
    if not clean_reason or len(clean_reason) > 1000:
        raise ValueError("A security-lock reason of 1-1000 characters is required.")
    if not clean_event_id or len(clean_event_id) > 128:
        raise ValueError("A security event ID of 1-128 characters is required.")
    if not clean_actor or len(clean_actor) > 128:
        raise ValueError("A security actor of 1-128 characters is required.")

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """
            SELECT event_type, reason, actor
            FROM tssi_security_events WHERE event_id = ?
            """,
            (clean_event_id,),
        ).fetchone()
        if existing is not None and (
            existing["event_type"] != "LOCK_ACTIVATED"
            or existing["reason"] != clean_reason
            or existing["actor"] != clean_actor
        ):
            raise ValueError("TSSI event ID already exists with different event data.")

        if existing is None:
            connection.execute(
                """
                INSERT INTO tssi_security_events
                    (event_id, event_type, reason, actor)
                VALUES (?, 'LOCK_ACTIVATED', ?, ?)
                """,
                (clean_event_id, clean_reason, clean_actor),
            )
        connection.execute(
            """
            UPDATE tssi_security_state
            SET lock_active = 1, lock_reason = ?, lock_event_id = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE singleton_id = 1
            """,
            (clean_reason, clean_event_id),
        )
    return {
        "tssi_lock_active": True,
        "new_orders_allowed": False,
        "event_id": clean_event_id,
        "message": "TSSI security lock activated. Trading entries are blocked.",
    }


def get_security_status():
    """Return a fail-closed status if TSSI state is missing or unreadable."""
    try:
        with get_connection() as connection:
            row = connection.execute(
                """
                SELECT lock_active, lock_reason, lock_event_id, updated_at
                FROM tssi_security_state WHERE singleton_id = 1
                """
            ).fetchone()
    except Exception:
        return {
            "tssi_lock_active": True,
            "tssi_status": "STATE_UNAVAILABLE_FAIL_CLOSED",
            "lock_reason": "TSSI state unavailable",
            "lock_event_id": None,
            "updated_at": None,
        }

    if row is None:
        return {
            "tssi_lock_active": True,
            "tssi_status": "STATE_MISSING_FAIL_CLOSED",
            "lock_reason": "TSSI state missing",
            "lock_event_id": None,
            "updated_at": None,
        }
    return {
        "tssi_lock_active": bool(row["lock_active"]),
        "tssi_status": "LOCKED" if row["lock_active"] else "CLEAR",
        "lock_reason": row["lock_reason"],
        "lock_event_id": row["lock_event_id"],
        "updated_at": row["updated_at"],
    }


def require_security_clear():
    status = get_security_status()
    if status["tssi_lock_active"]:
        raise RuntimeError(
            "Trading blocked by TSSI: " + str(status["lock_reason"])
        )
