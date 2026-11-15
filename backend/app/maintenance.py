"""Daily maintenance helpers for TaimoBot's paper-trading database.

Backups are created with SQLite's online backup API. Recovery verification
restores a backup into a separate staging database and checks its integrity;
it never overwrites the live database automatically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import database

SAST = ZoneInfo("Africa/Johannesburg")
BACKUP_DIR = Path(__file__).resolve().parents[1] / "backups"


def is_maintenance_window(now: datetime | None = None) -> bool:
    """Return True during the configured SAST maintenance window [23:00, 00:00)."""
    local_now = (now or datetime.now(SAST)).astimezone(SAST)
    return local_now.hour == 23


def create_verified_backup(
    source_path: Path | None = None,
    backup_dir: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Create an online SQLite backup and verify it before publishing the file."""
    source = Path(source_path or database.DB_PATH)
    destination_dir = Path(backup_dir or BACKUP_DIR)
    destination_dir.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        raise FileNotFoundError(f"Live database does not exist: {source}")

    local_now = (now or datetime.now(SAST)).astimezone(SAST)
    stamp = local_now.strftime("%Y%m%d-%H%M%S")
    final_path = destination_dir / f"taimobot-{stamp}.sqlite3"
    temporary_path = destination_dir / f".{final_path.name}.tmp"

    try:
        with sqlite3.connect(str(source), timeout=30) as live:
            with sqlite3.connect(str(temporary_path), timeout=30) as backup:
                live.backup(backup)

        with sqlite3.connect(str(temporary_path), timeout=30) as check:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")

        temporary_path.replace(final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return {
        "status": "BACKUP_VERIFIED",
        "backup_file": str(final_path),
        "integrity_check": "ok",
        "created_at": local_now.isoformat(),
        "timezone": "Africa/Johannesburg",
    }


def verify_restore_candidate(backup_path: Path, staging_path: Path) -> dict:
    """Restore into an isolated staging DB and verify; never replace the live DB."""
    source = Path(backup_path)
    staging = Path(staging_path)
    if not source.is_file():
        raise FileNotFoundError(f"Backup file not found: {source}")

    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.unlink(missing_ok=True)
    try:
        with sqlite3.connect(str(source), timeout=30) as backup:
            with sqlite3.connect(str(staging), timeout=30) as candidate:
                backup.backup(candidate)
        with sqlite3.connect(str(staging), timeout=30) as candidate:
            integrity = candidate.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Restore candidate failed integrity check: {integrity}")
    except Exception:
        staging.unlink(missing_ok=True)
        raise

    return {
        "status": "RESTORE_CANDIDATE_VERIFIED",
        "staging_file": str(staging),
        "integrity_check": "ok",
        "live_database_replaced": False,
    }
