"""Single-daily-run maintenance orchestration for TaimoBot.

A database claim makes the job idempotent across API workers. Each claim has
a fencing token so a stale worker cannot complete or fail a newer worker's run.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from . import database
from .maintenance import (
    BACKUP_DIR,
    SAST,
    create_verified_backup,
    is_maintenance_window,
    verify_restore_candidate,
)

RUNNING_LEASE_MINUTES = 15


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
        connection.commit()

    staging_path = BACKUP_DIR / "recovery-staging" / f"candidate-{day_key}.sqlite3"
    try:
        backup_result = create_verified_backup()
        restore_result = verify_restore_candidate(
            Path(backup_result["backup_file"]),
            staging_path,
        )
        staging_path.unlink(missing_ok=True)

        with database.get_connection() as connection:
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
                (
                    datetime.now(SAST).isoformat(),
                    backup_result["backup_file"],
                    day_key,
                    claim_token,
                ),
            )
        if updated.rowcount != 1:
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
            "live_database_replaced": False,
        }
    except Exception as error:
        staging_path.unlink(missing_ok=True)
        with database.get_connection() as connection:
            updated = connection.execute(
                """
                UPDATE maintenance_runs
                SET status = 'FAILED',
                    completed_at = ?,
                    error_summary = ?
                WHERE run_date = ?
                  AND status = 'RUNNING'
                  AND run_token = ?
                """,
                (
                    datetime.now(SAST).isoformat(),
                    str(error)[:500],
                    day_key,
                    claim_token,
                ),
            )
        if updated.rowcount != 1:
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
