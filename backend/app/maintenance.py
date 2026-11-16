"""Daily maintenance helpers for TaimoBot's paper-trading database.

Backups are created with SQLite's online backup API. Recovery verification
restores a backup into a separate staging database and checks its integrity;
it never overwrites the live database automatically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4
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
    # Multiple workers or operator retries can start within the same second.
    # Give every backup attempt its own final and temporary path so SQLite
    # copies cannot overwrite or delete one another during publication.
    attempt_id = uuid4().hex
    final_path = destination_dir / f"taimobot-{stamp}-{attempt_id}.sqlite3"
    temporary_path = destination_dir / f".taimobot-{stamp}-{attempt_id}.tmp"

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


def _protected_backup_paths() -> set[Path]:
    """Return the latest completed recovery point and files held by active attempts.

    A metadata read failure must stop retention rather than risk deleting a
    backup whose lifecycle cannot be established.
    """
    protected: set[Path] = set()
    with database.get_connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "maintenance_runs" in tables:
            row = connection.execute(
                """
                SELECT backup_file
                FROM maintenance_runs
                WHERE status = 'COMPLETE' AND backup_file IS NOT NULL
                ORDER BY completed_at DESC, run_date DESC
                LIMIT 1
                """
            ).fetchone()
            if row and row["backup_file"]:
                protected.add(Path(row["backup_file"]).resolve())
        if "maintenance_run_attempts" in tables:
            rows = connection.execute(
                """
                SELECT backup_file
                FROM maintenance_run_attempts
                WHERE status = 'RUNNING' AND backup_file IS NOT NULL
                """
            ).fetchall()
            protected.update(
                Path(row["backup_file"]).resolve()
                for row in rows if row["backup_file"]
            )
    return protected


def cleanup_expired_backups(
    backup_dir: Path | None = None,
    *,
    now: datetime | None = None,
    retention_days: int = 365,
    dry_run: bool = True,
    live_database_path: Path | None = None,
) -> dict:
    """Safely plan or remove expired managed backups after validating recovery points.

    Only regular, non-symlink TaimoBot backup files in the backup directory are
    considered. At least one verified backup inside the retention window must
    exist before any expired file can be removed. The newest verified backup
    and the live database are always protected. Dry-run is the default.
    """
    if isinstance(retention_days, bool) or not isinstance(retention_days, int) or retention_days < 365:
        raise ValueError("retention_days must be an integer of at least 365 days")

    directory = Path(backup_dir or BACKUP_DIR)
    if not directory.exists():
        return {
            "status": "NO_BACKUP_DIRECTORY",
            "dry_run": dry_run,
            "retention_days": retention_days,
            "candidates": [],
            "deleted": [],
        }
    if not directory.is_dir():
        raise NotADirectoryError(f"Backup path is not a directory: {directory}")

    local_now = (now or datetime.now(SAST)).astimezone(SAST)
    cutoff = local_now.timestamp() - retention_days * 24 * 60 * 60
    live_path = Path(live_database_path or database.DB_PATH).resolve()
    try:
        protected_paths = _protected_backup_paths()
    except (sqlite3.Error, OSError, ValueError) as error:
        return {
            "status": "BACKUP_RETENTION_BLOCKED",
            "reason": "Maintenance backup references could not be checked.",
            "metadata_error": str(error)[:300],
            "dry_run": dry_run,
            "retention_days": retention_days,
            "candidates": [],
            "deleted": [],
            "live_database_replaced": False,
        }
    verified: list[tuple[Path, float, tuple[int, int, int, int]]] = []
    invalid: list[str] = []

    # If the directory cannot be enumerated reliably, do not make any
    # deletion decisions from a partial view of the recovery set.
    try:
        directory_entries = list(directory.iterdir())
    except OSError as error:
        return {
            "status": "BACKUP_RETENTION_BLOCKED",
            "reason": "Backup directory could not be inspected safely.",
            "metadata_error": str(error)[:300],
            "dry_run": dry_run,
            "retention_days": retention_days,
            "candidates": [],
            "deleted": [],
            "live_database_replaced": False,
        }

    for candidate in directory_entries:
        if not candidate.name.startswith("taimobot-") or not candidate.name.endswith(".sqlite3"):
            continue
        try:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            resolved_candidate = candidate.resolve()
            if resolved_candidate == live_path:
                continue
            with sqlite3.connect(f"file:{resolved_candidate}?mode=ro", uri=True, timeout=10) as check:
                integrity = check.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                invalid.append(candidate.name)
                continue
            file_stat = candidate.stat(follow_symlinks=False)
            identity = (file_stat.st_dev, file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns)
            verified.append((candidate, file_stat.st_mtime, identity))
        except (sqlite3.Error, OSError, ValueError):
            # An unreadable or unstable file is never considered for deletion.
            invalid.append(candidate.name)

    verified.sort(key=lambda item: (item[1], item[0].name), reverse=True)
    newest_verified = verified[0][0] if verified else None
    has_recent_recovery_point = any(timestamp >= cutoff for _, timestamp, _ in verified)

    candidates = [
        path for path, timestamp, _ in verified
        if timestamp < cutoff
        and path != newest_verified
        and path.resolve() not in protected_paths
    ]
    if not has_recent_recovery_point:
        candidates = []

    deleted: list[str] = []
    deletion_failures: list[str] = []
    if not dry_run:
        original_identities = {path: identity for path, _, identity in verified}
        for candidate in candidates:
            # Re-check identity, age and integrity immediately before deletion.
            # A replaced file must not inherit the older file's expiry decision.
            try:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                before = candidate.stat(follow_symlinks=False)
                before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                if (
                    before_identity != original_identities[candidate]
                    or before.st_mtime >= cutoff
                    or candidate.resolve() == live_path
                    or candidate.resolve() in protected_paths
                ):
                    continue
                with sqlite3.connect(f"file:{candidate.resolve()}?mode=ro", uri=True, timeout=10) as check:
                    integrity = check.execute("PRAGMA integrity_check").fetchone()
                if not integrity or integrity[0] != "ok":
                    invalid.append(candidate.name)
                    continue
                after = candidate.stat(follow_symlinks=False)
                after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                if (
                    candidate.is_symlink()
                    or after_identity != original_identities[candidate]
                    or after.st_mtime >= cutoff
                ):
                    continue
                candidate.unlink()
                deleted.append(candidate.name)
            except (sqlite3.Error, OSError, ValueError):
                invalid.append(candidate.name)
                deletion_failures.append(candidate.name)

    return {
        "status": (
            "BACKUP_RETENTION_DRY_RUN"
            if dry_run
            else "BACKUP_RETENTION_BLOCKED"
            if deletion_failures
            else "BACKUP_RETENTION_COMPLETE"
        ),
        "deletion_failures": deletion_failures,
        "dry_run": dry_run,
        "retention_days": retention_days,
        "cutoff": datetime.fromtimestamp(cutoff, SAST).isoformat(),
        "verified_backup_count": len(verified),
        "recent_recovery_point_verified": has_recent_recovery_point,
        "newest_verified_backup": newest_verified.name if newest_verified else None,
        "candidates": [path.name for path in candidates],
        "deleted": deleted,
        "invalid_or_unreadable": sorted(set(invalid)),
        "live_database_replaced": False,
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
