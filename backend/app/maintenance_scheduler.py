"""Single-daily-run maintenance orchestration for TaimoBot.

The API may run in multiple worker processes. A SQLite claim row makes the
daily job idempotent across workers; only one process can claim a local date.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path

from . import database
from .maintenance import (
    BACKUP_DIR,
    SAST,
    create_verified_backup,
    is_maintenance_window,
    verify_restore_candidate,
)


def run_maintenance_for_date(run_date: date | None = None) -> dict:
    """Run and record one backup + isolated restore verification per SAST day."""
    day = run_date or datetime.now(SAST).date()
    day_key = day.isoformat()
    started = datetime.now(SAST).isoformat()

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
                error_summary TEXT
            )
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT status FROM maintenance_runs WHERE run_date = ?",
            (day_key,),
        ).fetchone()

        if existing and existing["status"] in ("RUNNING", "COMPLETE"):
            return {
                "status": "SKIPPED",
                "reason": f"Maintenance for {day_key} is already {existing['status'].lower()}.",
                "run_date": day_key,
            }

        connection.execute(
            """
            INSERT INTO maintenance_runs (
                run_date, status, started_at, completed_at,
                backup_file, error_summary
            )
            VALUES (?, 'RUNNING', ?, NULL, NULL, NULL)
            ON CONFLICT(run_date) DO UPDATE SET
                status = 'RUNNING',
                started_at = excluded.started_at,
                completed_at = NULL,
                backup_file = NULL,
                error_summary = NULL
            """,
            (day_key, started),
        )
        connection.commit()

    staging_path = BACKUP_DIR / "recovery-staging" / f"candidate-{day_key}.sqlite3"
    try:
        backup_result = create_verified_backup()
        restore_result = verify_restore_candidate(
            Path(backup_result["backup_file"]),
            staging_path,
        )
        # The candidate was checked successfully; do not retain a second copy.
        staging_path.unlink(missing_ok=True)

        with database.get_connection() as connection:
            connection.execute(
                """
                UPDATE maintenance_runs
                SET status = 'COMPLETE',
                    completed_at = ?,
                    backup_file = ?,
                    error_summary = NULL
                WHERE run_date = ?
                """,
                (
                    datetime.now(SAST).isoformat(),
                    backup_result["backup_file"],
                    day_key,
                ),
            )

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
            connection.execute(
                """
                UPDATE maintenance_runs
                SET status = 'FAILED',
                    completed_at = ?,
                    error_summary = ?
                WHERE run_date = ?
                """,
                (
                    datetime.now(SAST).isoformat(),
                    str(error)[:500],
                    day_key,
                ),
            )
        return {
            "status": "MAINTENANCE_FAILED",
            "run_date": day_key,
            "error": str(error)[:500],
        }


async def maintenance_scheduler(poll_seconds: int = 30) -> None:
    """Poll the SAST window; the database claim prevents duplicate worker runs."""
    while True:
        now = datetime.now(SAST)
        if is_maintenance_window(now):
            await asyncio.to_thread(run_maintenance_for_date, now.date())
        await asyncio.sleep(poll_seconds)
