"""Daily maintenance helpers for TaimoBot's paper-trading database.

Backups are created with SQLite's online backup API. Recovery verification
restores a backup into a separate staging database and checks its integrity;
it never overwrites the live database automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import fcntl
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import database

SAST = ZoneInfo("Africa/Johannesburg")
BACKUP_DIR = Path(__file__).resolve().parents[1] / "backups"
_RESTORE_GUARD_FDS: dict[str, int] = {}


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
    if (
        source.resolve() == Path(database.DB_PATH).resolve()
        and is_restore_lock_active()
    ):
        raise RuntimeError(
            "Live database backup blocked: a controlled restore has quiesced the application."
        )
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

    guard_fd = None
    try:
        # Hold a shared process-wide guard for the entire live-database backup.
        # This closes the race between the initial marker check and sqlite3.connect.
        if source.resolve() == Path(database.DB_PATH).resolve():
            guard_fd = database._acquire_connection_guard_fd()
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
    finally:
        if guard_fd is not None:
            os.close(guard_fd)

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
            # A verified backup can be produced before a later maintenance
            # step fails or before its worker is superseded. Keep the newest
            # referenced recovery file for each non-success terminal outcome
            # as well as the latest successful run and all active attempts.
            # Older terminal-outcome files remain eligible after 365 days,
            # avoiding indefinite retention growth from the audit history.
            for terminal_status in ("FAILED", "SUPERSEDED"):
                row = connection.execute(
                    """
                    SELECT backup_file
                    FROM maintenance_run_attempts
                    WHERE status = ? AND backup_file IS NOT NULL
                    ORDER BY completed_at DESC, started_at DESC
                    LIMIT 1
                    """,
                    (terminal_status,),
                ).fetchone()
                if row and row["backup_file"]:
                    protected.add(Path(row["backup_file"]).resolve())
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



def inspect_recovery_candidates(backup_dir: Path | None = None) -> dict:
    """Read-only inventory of managed backups with integrity and schema checks.

    A file is restore-ready only when SQLite integrity, essential TaimoBot
    tables/columns, and foreign-key checks all pass. This does not restore or
    modify any database.
    """
    directory = Path(backup_dir or BACKUP_DIR)
    if not directory.exists():
        return {"status": "NO_BACKUP_DIRECTORY", "candidates": []}
    if not directory.is_dir():
        return {"status": "RECOVERY_INVENTORY_BLOCKED", "candidates": []}

    required_columns = {
        "users": {"user_id", "email", "password_hash", "created_at"},
        "paper_accounts": {"account_id", "starting_balance", "balance"},
        "trades": {
            "trade_id", "account_id", "client_order_id", "symbol",
            "side", "quantity", "entry_price", "stop_loss", "status",
            "risk_amount", "realized_pnl",
        },
        "position_groups": {"group_id", "account_id", "symbol", "side", "status"},
        "copy_relationships": {
            "relationship_id", "source_account_id", "follower_account_id", "status",
        },
        "copy_executions": {
            "copy_execution_id", "relationship_id", "source_trade_id", "status",
        },
        "participant_milestones": {"user_id", "joined_at", "first_trade_at"},
        "system_safety_state": {"singleton_id", "emergency_stop_active"},
    }

    try:
        entries = list(directory.iterdir())
    except OSError:
        return {"status": "RECOVERY_INVENTORY_BLOCKED", "candidates": []}

    candidates = []
    for path in entries:
        if not path.name.startswith("taimobot-") or not path.name.endswith(".sqlite3"):
            continue
        if path.is_symlink() or not path.is_file():
            continue

        issues = []
        integrity = "not_checked"
        missing_schema = []
        foreign_key_violations = None
        try:
            stat = path.stat(follow_symlinks=False)
            with sqlite3.connect(
                f"file:{path.resolve()}?mode=ro", uri=True, timeout=10
            ) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()
                integrity = result[0] if result else "no_result"
                if integrity != "ok":
                    issues.append("SQLITE_INTEGRITY_FAILED")

                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                for table, expected_columns in required_columns.items():
                    if table not in tables:
                        missing_schema.append(table)
                        continue
                    actual_columns = {
                        row[1] for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    missing = expected_columns - actual_columns
                    if missing:
                        missing_schema.append(
                            f"{table}:" + ",".join(sorted(missing))
                        )
                if missing_schema:
                    issues.append("TAIMOBOT_SCHEMA_INCOMPLETE")

                foreign_key_violations = len(
                    connection.execute("PRAGMA foreign_key_check").fetchall()
                )
                if foreign_key_violations:
                    issues.append("FOREIGN_KEY_VIOLATIONS")

            candidates.append({
                "file_name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, SAST).isoformat(),
                "integrity_check": integrity,
                "missing_schema": missing_schema,
                "foreign_key_violations": foreign_key_violations,
                "restore_ready": not issues,
                "issues": issues,
            })
        except (sqlite3.Error, OSError, ValueError):
            candidates.append({
                "file_name": path.name,
                "integrity_check": integrity,
                "missing_schema": missing_schema,
                "foreign_key_violations": foreign_key_violations,
                "restore_ready": False,
                "issues": ["BACKUP_UNREADABLE"],
            })

    candidates.sort(
        key=lambda item: (item.get("modified_at", ""), item["file_name"]),
        reverse=True,
    )
    return {
        "status": "RECOVERY_INVENTORY_COMPLETE",
        "candidate_count": len(candidates),
        "restore_ready_count": sum(1 for item in candidates if item["restore_ready"]),
        "candidates": candidates,
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


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a streaming SHA-256 digest for a managed recovery artifact."""
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    with Path(path).open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()



def restore_lock_path() -> Path:
    """Return the process-shared lock path adjacent to the live database."""
    return Path(database.DB_PATH).with_name(Path(database.DB_PATH).name + ".restore.lock")


def is_restore_lock_active() -> bool:
    """Fail closed if a restore lock exists or cannot be safely inspected."""
    lock_path = restore_lock_path()
    try:
        return lock_path.exists() or lock_path.is_symlink()
    except OSError:
        return True


def acquire_restore_lock(plan_id: str, *, timeout_seconds: float = 1.0) -> dict:
    """Acquire a process-shared restore lock and prove SQLite is quiescent.

    The OS lock is acquired before publishing the marker. This avoids a crash
    window where a marker could be mistaken for an abandoned lock while its
    owner is still waiting to acquire the cross-process guard.
    """
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("A valid restore plan ID is required.")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds < 0:
        raise ValueError("timeout_seconds must be a non-negative number.")

    lock_path = restore_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    payload = {
        "plan_id": plan_id,
        "lock_token": token,
        "pid": os.getpid(),
        "created_at": datetime.now(SAST).isoformat(),
    }
    guard_fd = None
    descriptor = None
    try:
        # Serialize local connection creation and reject leaked local handles.
        with database._CONNECTION_GUARD:
            if database.open_connection_count():
                raise RuntimeError(
                    "Restore lock refused: application database connections remain open."
                )
            guard_fd = os.open(
                str(database.restore_guard_path()), os.O_CREAT | os.O_RDWR, 0o600
            )
            deadline = time.monotonic() + float(timeout_seconds)
            while True:
                try:
                    fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Restore lock refused: database connections remain active in another process."
                        )
                    time.sleep(0.05)

            # With the exclusive guard held, no cooperating application
            # connection can open or remain active in any worker process.
            descriptor = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
                descriptor = None
                lock_file.write(json.dumps(payload, sort_keys=True))
                lock_file.flush()
                os.fsync(lock_file.fileno())

        _RESTORE_GUARD_FDS[token] = guard_fd
        guard_fd = None

        # Retain the explicit SQLite-level check as a second line of defence.
        with sqlite3.connect(str(database.DB_PATH), timeout=float(timeout_seconds)) as connection:
            connection.execute("BEGIN EXCLUSIVE")
            connection.commit()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
            if journal_mode == "wal":
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint and checkpoint[0] != 0:
                    raise RuntimeError("SQLite WAL checkpoint is busy; active readers remain.")

        return {
            "status": "RESTORE_LOCK_ACQUIRED",
            "plan_id": plan_id,
            "lock_token": token,
            "database_quiescent": True,
            "lock_file": str(lock_path),
        }
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if guard_fd is not None:
            os.close(guard_fd)
        held_fd = _RESTORE_GUARD_FDS.pop(token, None)
        if held_fd is not None:
            os.close(held_fd)
        try:
            if lock_path.is_file() and not lock_path.is_symlink():
                current = json.loads(lock_path.read_text(encoding="utf-8"))
                if current.get("lock_token") == token:
                    lock_path.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        raise


def inspect_restore_lock() -> dict:
    """Read-only inspection of a restore marker and its process-shared guard.

    A marker whose OS guard is available is classified as abandoned, but is
    never removed here. Operator review remains mandatory before recovery.
    """
    lock_path = restore_lock_path()
    try:
        metadata = lock_path.lstat()
    except FileNotFoundError:
        return {"status": "NO_RESTORE_LOCK", "marker_present": False}
    except OSError:
        return {"status": "RESTORE_LOCK_UNINSPECTABLE", "marker_present": True}
    if lock_path.is_symlink() or not lock_path.is_file():
        return {"status": "RESTORE_LOCK_UNSAFE_MARKER", "marker_present": True}

    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        plan_id = payload.get("plan_id")
        process_id = payload.get("pid")
        created_at = payload.get("created_at")
        if (
            not isinstance(plan_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", plan_id)
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or not isinstance(created_at, str)
        ):
            raise ValueError("invalid restore marker metadata")
    except (OSError, ValueError, json.JSONDecodeError):
        return {"status": "RESTORE_LOCK_METADATA_UNREADABLE", "marker_present": True}

    guard_fd = None
    try:
        guard_fd = os.open(str(database.restore_guard_path()), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            status = "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW"
        except BlockingIOError:
            status = "RESTORE_LOCK_ACTIVE"
    except OSError:
        status = "RESTORE_LOCK_GUARD_UNINSPECTABLE"
    finally:
        if guard_fd is not None:
            os.close(guard_fd)

    return {
        "status": status,
        "marker_present": True,
        "plan_id": plan_id,
        "process_id": process_id,
        "created_at": created_at,
        "marker_size_bytes": metadata.st_size,
        "automatic_recovery_performed": False,
        "marker_removed": False,
    }


def release_restore_lock(lock_token: str) -> dict:
    """Release only the lock owned by the supplied unpredictable token."""
    if not isinstance(lock_token, str) or not re.fullmatch(r"[a-f0-9]{32}", lock_token):
        raise ValueError("A valid restore lock token is required.")
    lock_path = restore_lock_path()
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError("Restore lock is missing or unsafe.")
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("Restore lock metadata is unreadable; lock remains active.") from error
    if payload.get("lock_token") != lock_token:
        raise RuntimeError("Restore lock ownership mismatch; lock remains active.")
    guard_fd = _RESTORE_GUARD_FDS.pop(lock_token, None)
    if guard_fd is None:
        raise RuntimeError("Restore process-shared lock ownership is unavailable; marker remains active.")
    # Drop the exclusive OS lock while the marker still blocks new handles.
    os.close(guard_fd)
    lock_path.unlink()
    return {"status": "RESTORE_LOCK_RELEASED", "database_quiescent": False}


def inspect_sqlite_sidecars(database_path: Path) -> dict:
    """Read-only inventory of SQLite WAL/SHM files beside a database.

    Any present sidecar requires explicit handling after the restore lock has
    quiesced the application. This helper never deletes, truncates, or opens
    sidecars as databases.
    """
    base = Path(database_path)
    found = []
    for suffix, kind in (("-wal", "WAL"), ("-shm", "SHM")):
        sidecar = Path(str(base) + suffix)
        try:
            metadata = sidecar.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError(f"SQLite {kind} sidecar cannot be inspected safely.") from error
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ValueError(f"SQLite {kind} sidecar is not a regular managed file.")
        found.append({"name": sidecar.name, "kind": kind, "size_bytes": metadata.st_size})
    return {
        "status": "SIDECARS_CLEAR" if not found else "SIDECARS_PRESENT_REQUIRES_LOCKED_HANDLING",
        "sidecars": found,
        "mutation_performed": False,
    }


def verify_restore_execution_readiness(
    source_backup: Path,
    staged_candidate: Path,
    rollback_backup: Path,
    *,
    backup_dir: Path | None = None,
    live_database_path: Path | None = None,
) -> dict:
    """Read-only final preflight for a restore and its rollback recovery point.

    This deliberately does not replace the live database or authorize execution.
    Every artifact must remain inside its designated managed directory and pass
    the full recovery inventory checks.
    """
    managed_backup_dir = Path(backup_dir or BACKUP_DIR).resolve()
    staging_dir = (managed_backup_dir / "recovery-staging").resolve()
    live_path = Path(live_database_path or database.DB_PATH).resolve()
    artifacts = (
        (Path(source_backup), managed_backup_dir, "source"),
        (Path(rollback_backup), managed_backup_dir, "rollback"),
        (Path(staged_candidate), staging_dir, "staged"),
    )
    for artifact, expected_parent, label in artifacts:
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"The {label} recovery artifact is missing or unsafe.")
        if artifact.resolve().parent != expected_parent:
            raise ValueError(f"The {label} recovery artifact is outside its managed directory.")
        if artifact.resolve() == live_path:
            raise ValueError(f"The {label} recovery artifact cannot be the live database.")

    if live_path.is_symlink() or not live_path.is_file():
        raise ValueError("The live database is missing or unsafe.")

    source_inventory = inspect_recovery_candidates(managed_backup_dir)
    staged_inventory = inspect_recovery_candidates(staging_dir)
    if source_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
        raise ValueError("Managed backup inventory is unavailable.")
    if staged_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
        raise ValueError("Staged recovery inventory is unavailable.")

    source_records = {
        item.get("file_name"): item
        for item in source_inventory.get("candidates", [])
    }
    staged_records = {
        item.get("file_name"): item
        for item in staged_inventory.get("candidates", [])
    }
    if not source_records.get(Path(source_backup).name, {}).get("restore_ready"):
        raise ValueError("The selected source backup is not restore-ready.")
    if not source_records.get(Path(rollback_backup).name, {}).get("restore_ready"):
        raise ValueError("The rollback backup is not restore-ready.")
    if not staged_records.get(Path(staged_candidate).name, {}).get("restore_ready"):
        raise ValueError("The staged restore candidate is not restore-ready.")

    sidecar_inventory = inspect_sqlite_sidecars(live_path)

    try:
        with sqlite3.connect(f"file:{live_path}?mode=ro", uri=True, timeout=10) as live:
            live_integrity = live.execute("PRAGMA integrity_check").fetchone()
    except (sqlite3.Error, OSError, ValueError) as error:
        raise ValueError("The current live database cannot be verified.") from error
    if not live_integrity or live_integrity[0] != "ok":
        raise ValueError("The current live database failed its integrity check.")

    return {
        "status": "RESTORE_EXECUTION_PREFLIGHT_PASSED",
        "source_backup_verified": True,
        "staged_candidate_verified": True,
        "rollback_backup_verified": True,
        "live_database_verified": True,
        "sqlite_sidecars": sidecar_inventory,
        "sidecar_handling_required_after_lock": sidecar_inventory["status"] != "SIDECARS_CLEAR",
        "execution_authorized": False,
        "live_database_replaced": False,
    }


def verify_approved_restore_plan(
    plan,
    artifact_hashes,
    *,
    backup_dir: Path | None = None,
    live_database_path: Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Read-only verification that an approved restore plan is still executable."""
    if plan is None or artifact_hashes is None:
        raise ValueError("Approved restore plan or artifact fingerprints are missing.")
    if plan["status"] != "APPROVED":
        raise ValueError("Restore plan must be APPROVED before execution preflight.")
    created_at = datetime.fromisoformat(plan["created_at"])
    if created_at.tzinfo is None:
        raise ValueError("Restore plan timestamp has no timezone.")
    current_time = now or datetime.now(SAST)
    if current_time.tzinfo is None:
        raise ValueError("Preflight clock must include a timezone.")
    age_seconds = (current_time.astimezone(SAST) - created_at.astimezone(SAST)).total_seconds()
    if age_seconds < 0 or age_seconds > 30 * 60:
        raise ValueError("Approved restore plan has expired; prepare a new plan.")
    if is_restore_lock_active():
        raise ValueError("A restore lock is already active; operator investigation is required.")

    managed_backup_dir = Path(backup_dir or BACKUP_DIR).resolve()
    staging_dir = (managed_backup_dir / "recovery-staging").resolve()
    selected_backup = Path(plan["backup_file"])
    staged_candidate = Path(plan["staging_file"])
    rollback_backup = Path(plan["pre_restore_backup_file"])
    for artifact, expected_parent, label in (
        (selected_backup, managed_backup_dir, "source"),
        (staged_candidate, staging_dir, "staged"),
        (rollback_backup, managed_backup_dir, "rollback"),
    ):
        if artifact.is_symlink() or not artifact.is_file() or artifact.resolve().parent != expected_parent:
            raise ValueError(f"The {label} restore artifact is missing or outside its managed directory.")

    current_hashes = (
        sha256_file(selected_backup),
        sha256_file(staged_candidate),
        sha256_file(rollback_backup),
    )
    expected_hashes = (
        artifact_hashes["source_sha256"],
        artifact_hashes["staged_sha256"],
        artifact_hashes["rollback_sha256"],
    )
    if current_hashes != expected_hashes:
        raise ValueError("Restore artifact fingerprint mismatch; prepare and approve a new plan.")

    readiness = verify_restore_execution_readiness(
        selected_backup,
        staged_candidate,
        rollback_backup,
        backup_dir=managed_backup_dir,
        live_database_path=live_database_path,
    )
    if readiness.get("status") != "RESTORE_EXECUTION_PREFLIGHT_PASSED":
        raise ValueError("Restore execution readiness checks did not pass.")
    return {
        "status": "APPROVED_RESTORE_PREFLIGHT_PASSED",
        "plan_id": plan["plan_id"],
        "plan_approved": True,
        "plan_unexpired": True,
        "artifact_fingerprints_match": True,
        "source_backup_verified": True,
        "staged_candidate_verified": True,
        "rollback_backup_verified": True,
        "live_database_verified": True,
        "sqlite_sidecars": readiness.get("sqlite_sidecars", {"status": "NOT_REPORTED", "sidecars": []}),
        "sidecar_handling_required_after_lock": readiness.get("sidecar_handling_required_after_lock", True),
        "execution_authorized": False,
        "live_database_replaced": False,
        "trading_mode": "paper",
    }
