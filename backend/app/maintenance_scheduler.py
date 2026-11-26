"""Single-daily-run maintenance orchestration for TaimoBot.

A database claim makes the job idempotent across API workers. Each claim has
a fencing token so a stale worker cannot complete or fail a newer worker's run.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
from threading import Event, Thread
from uuid import uuid4

from . import database
from .maintenance import (
    BACKUP_DIR,
    SAST,
    cleanup_expired_backups,
    create_verified_backup,
    is_maintenance_window,
    verify_restore_candidate,
)

RUNNING_LEASE_MINUTES = 15
LEASE_HEARTBEAT_SECONDS = 30


def _heartbeat_claim(run_date: str, claim_token: str, stop_event: Event, lost_event: Event) -> None:
    """Renew a claim while its worker is alive; fail closed if ownership is lost."""
    while not stop_event.wait(LEASE_HEARTBEAT_SECONDS):
        try:
            renewed_at = datetime.now(SAST).isoformat()
            with database.get_connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                updated = connection.execute(
                    """
                    UPDATE maintenance_runs
                    SET started_at = ?
                    WHERE run_date = ? AND status = 'RUNNING' AND run_token = ?
                    """,
                    (renewed_at, run_date, claim_token),
                )
                connection.commit()
            if updated.rowcount != 1:
                lost_event.set()
                return
        except Exception:
            # A missed heartbeat is treated as lease loss. Never assume ownership
            # when the database cannot confirm it.
            lost_event.set()
            return


def _assert_claim_owned(run_date: str, claim_token: str, lost_event: Event) -> None:
    """Raise unless this process still owns the current RUNNING claim."""
    if lost_event.is_set():
        raise RuntimeError("Maintenance worker lease was lost; stopping safely.")
    with database.get_connection() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM maintenance_runs
            WHERE run_date = ? AND status = 'RUNNING' AND run_token = ?
            """,
            (run_date, claim_token),
        ).fetchone()
    if row is None:
        lost_event.set()
        raise RuntimeError("Maintenance worker claim was superseded; stopping safely.")


@contextmanager
def _claim_owned_deletion_guard(run_date: str, claim_token: str, lost_event: Event):
    """Fence one destructive filesystem operation with a SQLite write lock.

    BEGIN IMMEDIATE prevents another process from reclaiming or replacing the
    maintenance claim until the guarded unlink has completed and committed.
    """
    if lost_event.is_set():
        raise RuntimeError("Maintenance worker lease was lost; stopping safely.")
    with database.get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT 1 FROM maintenance_runs
            WHERE run_date = ? AND status = 'RUNNING' AND run_token = ?
            """,
            (run_date, claim_token),
        ).fetchone()
        if row is None:
            connection.rollback()
            lost_event.set()
            raise RuntimeError("Maintenance worker claim was superseded; stopping safely.")
        try:
            yield
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def _running_claim_is_stale(started_at: str, now: datetime) -> bool:
    """Only reclaim a RUNNING claim whose valid timestamp is beyond its lease."""
    try:
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=SAST)
        else:
            started = started.astimezone(SAST)
        current = now.astimezone(SAST)
        return started <= current - timedelta(minutes=RUNNING_LEASE_MINUTES)
    except (TypeError, ValueError):
        return False


def run_maintenance_for_date(
    run_date: date | None = None,
    *,
    retry_failed: bool = False,
    retry_stale_running: bool = False,
) -> dict:
    """Run one backup + isolated restore verification per SAST day.

    Failed and stale-running claims require explicit operator-controlled
    retries; the scheduler never sets either flag. A per-attempt token fences
    off workers whose claim was explicitly reclaimed.
    """
    now = datetime.now(SAST)
    day = run_date or now.date()
    day_key = day.isoformat()
    started = now.isoformat()
    claim_token = uuid4().hex

    with database.get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_runs (
                run_date TEXT PRIMARY KEY,
                status TEXT NOT NULL
                    CHECK (status IN ('RUNNING', 'COMPLETE', 'FAILED')),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                backup_file TEXT,
                error_summary TEXT,
                run_token TEXT
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(maintenance_runs)")
        }
        if "run_token" not in columns:
            connection.execute("ALTER TABLE maintenance_runs ADD COLUMN run_token TEXT")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS maintenance_run_attempts (
                attempt_token TEXT PRIMARY KEY,
                run_date TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('RUNNING', 'COMPLETE', 'FAILED', 'SUPERSEDED')),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                backup_file TEXT,
                error_summary TEXT
            )
            """
        )

        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT status, started_at FROM maintenance_runs WHERE run_date = ?",
            (day_key,),
        ).fetchone()

        if existing:
            status = existing["status"]
            if status == "COMPLETE":
                return {
                    "status": "SKIPPED",
                    "reason": f"Maintenance for {day_key} is already complete.",
                    "run_date": day_key,
                }
            if status == "RUNNING":
                stale = _running_claim_is_stale(existing["started_at"], now)
                if not stale or not retry_stale_running:
                    reason = (
                        "an explicit stale-run recovery is required"
                        if stale
                        else "a maintenance run is already active"
                    )
                    return {
                        "status": "SKIPPED",
                        "reason": f"Maintenance for {day_key}: {reason}.",
                        "run_date": day_key,
                    }
            if status == "FAILED" and not retry_failed:
                return {
                    "status": "SKIPPED",
                    "reason": (
                        f"Maintenance for {day_key} previously failed; "
                        "an explicit retry is required."
                    ),
                    "run_date": day_key,
                }

        connection.execute(
            """
            INSERT INTO maintenance_runs (
                run_date, status, started_at, completed_at,
                backup_file, error_summary, run_token
            )
            VALUES (?, 'RUNNING', ?, NULL, NULL, NULL, ?)
            ON CONFLICT(run_date) DO UPDATE SET
                status = 'RUNNING',
                started_at = excluded.started_at,
                completed_at = NULL,
                backup_file = NULL,
                error_summary = NULL,
                run_token = excluded.run_token
            """,
            (day_key, started, claim_token),
        )
        connection.execute(
            """
            INSERT INTO maintenance_run_attempts (
                attempt_token, run_date, status, started_at
            ) VALUES (?, ?, 'RUNNING', ?)
            """,
            (claim_token, day_key, started),
        )
        connection.commit()

    staging_path = (
        BACKUP_DIR
        / "recovery-staging"
        / f"candidate-{day_key}-{claim_token}.sqlite3"
    )
    backup_result = None
    heartbeat_stop = Event()
    heartbeat_lost = Event()
    heartbeat_thread = Thread(
        target=_heartbeat_claim,
        args=(day_key, claim_token, heartbeat_stop, heartbeat_lost),
        name=f"taimobot-maintenance-heartbeat-{day_key}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        _assert_claim_owned(day_key, claim_token, heartbeat_lost)
        backup_result = create_verified_backup()
        _assert_claim_owned(day_key, claim_token, heartbeat_lost)
        restore_result = verify_restore_candidate(
            Path(backup_result["backup_file"]),
            staging_path,
        )
        staging_path.unlink(missing_ok=True)
        _assert_claim_owned(day_key, claim_token, heartbeat_lost)

        # Retention cleanup runs only after a fresh backup and isolated restore
        # candidate have both passed verification. One year is the hard minimum.
        retention_result = cleanup_expired_backups(
            backup_dir=BACKUP_DIR,
            retention_days=365,
            dry_run=False,
            deletion_guard=lambda _path: _claim_owned_deletion_guard(
                day_key, claim_token, heartbeat_lost
            ),
        )
        _assert_claim_owned(day_key, claim_token, heartbeat_lost)
        if retention_result.get("status") != "BACKUP_RETENTION_COMPLETE":
            retention_status = str(retention_result.get("status", "UNKNOWN"))
            raise RuntimeError(
                "Backup retention cleanup did not complete safely "
                f"(status: {retention_status})."
            )

        _assert_claim_owned(day_key, claim_token, heartbeat_lost)
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)
        completed_at = datetime.now(SAST).isoformat()
        with database.get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE maintenance_runs
                SET status = 'COMPLETE',
                    completed_at = ?,
                    backup_file = ?,
                    error_summary = NULL
                WHERE run_date = ?
                  AND status = 'RUNNING'
                  AND run_token = ?
                """,
                (completed_at, backup_result["backup_file"], day_key, claim_token),
            )
            if updated.rowcount == 1:
                audited = connection.execute(
                    """
                    UPDATE maintenance_run_attempts
                    SET status = 'COMPLETE', completed_at = ?, backup_file = ?,
                        error_summary = NULL
                    WHERE attempt_token = ? AND status = 'RUNNING'
                    """,
                    (completed_at, backup_result["backup_file"], claim_token),
                )
                if audited.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        "Maintenance completion audit row was missing or not RUNNING."
                    )
                connection.commit()
            else:
                connection.execute(
                    """
                    UPDATE maintenance_run_attempts
                    SET status = 'SUPERSEDED', completed_at = ?, backup_file = ?,
                        error_summary = ?
                    WHERE attempt_token = ? AND status = 'RUNNING'
                    """,
                    (
                        completed_at,
                        backup_result.get("backup_file")
                        if isinstance(backup_result, dict)
                        and backup_result.get("status") == "BACKUP_VERIFIED"
                        else None,
                        "A newer maintenance claim replaced this worker.",
                        claim_token,
                    ),
                )
                connection.commit()
                return {
                    "status": "MAINTENANCE_SUPERSEDED",
                    "run_date": day_key,
                    "reason": "A newer maintenance claim replaced this worker.",
                }

        return {
            "status": "MAINTENANCE_COMPLETE",
            "run_date": day_key,
            "backup": backup_result,
            "restore_verification": restore_result,
            "backup_retention": retention_result,
            "live_database_replaced": False,
        }
    except Exception as error:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=5)
        # A staging-file cleanup failure must not prevent the maintenance
        # failure and audit records from being committed. Preserve both errors
        # so operators can identify the leftover recovery artifact.
        try:
            staging_path.unlink(missing_ok=True)
            cleanup_error = None
        except OSError as staging_cleanup_error:
            cleanup_error = str(staging_cleanup_error)[:200]
        completed_at = datetime.now(SAST).isoformat()
        error_summary = str(error)[:250]
        if cleanup_error:
            error_summary = (
                f"{error_summary}; staging cleanup also failed: {cleanup_error}"
            )
        error_summary = error_summary[:500]
        with database.get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE maintenance_runs
                SET status = 'FAILED',
                    completed_at = ?,
                    backup_file = ?,
                    error_summary = ?
                WHERE run_date = ?
                  AND status = 'RUNNING'
                  AND run_token = ?
                """,
                (
                    completed_at,
                    backup_result.get("backup_file")
                    if isinstance(backup_result, dict)
                    and backup_result.get("status") == "BACKUP_VERIFIED"
                    else None,
                    error_summary,
                    day_key,
                    claim_token,
                ),
            )
            if updated.rowcount == 1:
                audited = connection.execute(
                    """
                    UPDATE maintenance_run_attempts
                    SET status = 'FAILED', completed_at = ?, backup_file = ?,
                        error_summary = ?
                    WHERE attempt_token = ? AND status = 'RUNNING'
                    """,
                    (
                        completed_at,
                        backup_result.get("backup_file")
                        if isinstance(backup_result, dict)
                        and backup_result.get("status") == "BACKUP_VERIFIED"
                        else None,
                        error_summary,
                        claim_token,
                    ),
                )
                if audited.rowcount != 1:
                    raise sqlite3.IntegrityError(
                        "Maintenance failure audit row was missing or not RUNNING."
                    )
                connection.commit()
            else:
                connection.execute(
                    """
                    UPDATE maintenance_run_attempts
                    SET status = 'SUPERSEDED', completed_at = ?, backup_file = ?,
                        error_summary = ?
                    WHERE attempt_token = ? AND status = 'RUNNING'
                    """,
                    (
                        completed_at,
                        backup_result.get("backup_file")
                        if isinstance(backup_result, dict)
                        and backup_result.get("status") == "BACKUP_VERIFIED"
                        else None,
                        "A newer maintenance claim replaced this worker.",
                        claim_token,
                    ),
                )
                connection.commit()
                return {
                    "status": "MAINTENANCE_SUPERSEDED",
                    "run_date": day_key,
                    "reason": "A newer maintenance claim replaced this worker.",
                }
        return {
            "status": "MAINTENANCE_FAILED",
            "run_date": day_key,
            "error": str(error)[:500],
        }


def get_maintenance_status() -> dict:
    """Return the latest persisted maintenance result without exposing file paths."""
    with database.get_connection() as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'maintenance_runs'
            """
        ).fetchone()
        if table is None:
            return {"last_run_date": None, "last_run_status": "NOT_RUN"}

        row = connection.execute(
            """
            SELECT run_date, status, started_at, completed_at, error_summary
            FROM maintenance_runs
            ORDER BY run_date DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        return {"last_run_date": None, "last_run_status": "NOT_RUN"}

    return {
        "last_run_date": row["run_date"],
        "last_run_status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_summary": row["error_summary"],
    }


async def maintenance_scheduler(poll_seconds: int = 30) -> None:
    """Poll the SAST window; database claims prevent duplicate worker runs."""
    while True:
        now = datetime.now(SAST)
        if is_maintenance_window(now):
            await asyncio.to_thread(run_maintenance_for_date, now.date())
        await asyncio.sleep(poll_seconds)
