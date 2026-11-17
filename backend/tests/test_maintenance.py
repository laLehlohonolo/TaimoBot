import os
import json
import sqlite3
import multiprocessing
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.maintenance import (
    cleanup_expired_backups,
    create_verified_backup,
    is_maintenance_window,
    inspect_recovery_candidates,
    verify_restore_candidate,
    verify_restore_execution_readiness,
    sha256_file,
    acquire_restore_lock,
    release_restore_lock,
    is_restore_lock_active,
    verify_approved_restore_plan,
    inspect_sqlite_sidecars,
    inspect_restore_lock,
    recover_abandoned_restore_lock,
)
from app import database
from app.database import init_db
from app.safety import deactivate_emergency_stop, require_trading_enabled


def _crash_after_acquiring_restore_lock(ready):
    acquire_restore_lock("7" * 32, timeout_seconds=1.0)
    ready.set()
    os._exit(0)

def _hold_database_connection_for_restore_test(ready, release):
    connection = database.get_connection()
    ready.set()
    release.wait(5)
    connection.close()


class DailyMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "maintenance-tests.sqlite3"
        init_db()
        deactivate_emergency_stop()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_maintenance_window_uses_south_african_time(self):
        zone = ZoneInfo("Africa/Johannesburg")
        self.assertTrue(is_maintenance_window(datetime(2026, 11, 15, 23, 0, tzinfo=zone)))
        self.assertTrue(is_maintenance_window(datetime(2026, 11, 15, 23, 59, tzinfo=zone)))
        self.assertFalse(is_maintenance_window(datetime(2026, 11, 16, 0, 0, tzinfo=zone)))
        self.assertFalse(is_maintenance_window(datetime(2026, 11, 15, 22, 59, tzinfo=zone)))

    def test_new_trades_are_blocked_during_maintenance_window(self):
        deactivate_emergency_stop()
        with patch("app.safety.is_maintenance_window", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "maintenance window"):
                require_trading_enabled()

    def test_new_trades_are_allowed_outside_maintenance_when_safety_is_clear(self):
        deactivate_emergency_stop()
        with patch("app.safety.is_maintenance_window", return_value=False):
            require_trading_enabled()

    def test_abandoned_restore_lock_recovery_requires_explicit_confirmation(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        lock_path.write_text(json.dumps({
            "plan_id": "d" * 32,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "e" * 32,
        }), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "confirm=True"):
            recover_abandoned_restore_lock(
                "operator@example.com", "INC-123", confirm=False,
                audit_path=Path(self.temp_dir.name) / "audit.jsonl",
            )
        self.assertTrue(lock_path.exists())

    def test_abandoned_restore_lock_recovery_requires_inactive_guard(self):
        lock = acquire_restore_lock("f" * 32)
        try:
            with self.assertRaisesRegex(RuntimeError, "not confirmed abandoned"):
                recover_abandoned_restore_lock(
                    "operator@example.com", "INC-124", confirm=True,
                    audit_path=Path(self.temp_dir.name) / "audit.jsonl",
                )
            self.assertTrue(Path(lock["lock_file"]).exists())
        finally:
            release_restore_lock(lock["lock_token"])

    def test_abandoned_restore_lock_recovery_audits_before_removing_marker(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        guard_path = database.restore_guard_path()
        lock_path.write_text(json.dumps({
            "plan_id": "a" * 32,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "b" * 32,
        }), encoding="utf-8")
        audit = Path(self.temp_dir.name) / "audit" / "recovery.jsonl"
        live_before = database.DB_PATH.read_bytes()
        result = recover_abandoned_restore_lock(
            "operator@example.com", "INC-125", confirm=True, audit_path=audit,
        )
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["status"], "ABANDONED_RESTORE_LOCK_RECOVERED")
        self.assertEqual(
            [record["event"] for record in records],
            ["RESTORE_LOCK_RECOVERY_STARTED", "RESTORE_LOCK_RECOVERY_COMPLETE"],
        )
        self.assertTrue(all(record["operator_id"] == "operator@example.com" for record in records))
        self.assertTrue(all(record["incident_ref"] == "INC-125" for record in records))
        self.assertFalse(lock_path.exists())
        self.assertEqual(database.DB_PATH.read_bytes(), live_before)
        self.assertFalse(result["database_changed"])
        self.assertFalse(result["sidecars_changed"])
        self.assertFalse(result["live_database_replaced"])
        self.assertTrue(guard_path.exists())

    def test_abandoned_restore_recovery_audit_write_failure_preserves_marker(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        lock_path.write_text(json.dumps({
            "plan_id": "c" * 32,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "d" * 32,
        }), encoding="utf-8")
        audit = Path(self.temp_dir.name) / "audit-failure.jsonl"
        with patch("app.maintenance.os.write", side_effect=OSError("simulated audit disk failure")):
            with self.assertRaisesRegex(OSError, "simulated audit disk failure"):
                recover_abandoned_restore_lock(
                    "operator@example.com", "INC-126", confirm=True, audit_path=audit,
                )
        self.assertTrue(lock_path.exists(), "marker must remain if STARTED audit cannot be written")
        self.assertFalse(audit.exists() and audit.stat().st_size > 0)

    def test_abandoned_restore_recovery_rejects_symlink_audit_file(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        lock_path.write_text(json.dumps({
            "plan_id": "e" * 32,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "f" * 32,
        }), encoding="utf-8")
        target = Path(self.temp_dir.name) / "audit-target.jsonl"
        target.write_text("preserve", encoding="utf-8")
        audit = Path(self.temp_dir.name) / "audit-link.jsonl"
        audit.symlink_to(target)
        with self.assertRaises(OSError):
            recover_abandoned_restore_lock(
                "operator@example.com", "INC-127", confirm=True, audit_path=audit,
            )
        self.assertTrue(lock_path.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve")

    def test_abandoned_restore_recovery_detects_marker_changed_after_inspection(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker = {
            "plan_id": "1" * 32,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "2" * 32,
        }
        lock_path.write_text(json.dumps(marker), encoding="utf-8")
        original_inspect = inspect_restore_lock

        def inspect_then_mutate():
            result = original_inspect()
            marker["plan_id"] = "3" * 32
            lock_path.write_text(json.dumps(marker), encoding="utf-8")
            return result

        audit = Path(self.temp_dir.name) / "marker-race.jsonl"
        with patch("app.maintenance.inspect_restore_lock", side_effect=inspect_then_mutate):
            with self.assertRaisesRegex(RuntimeError, "metadata changed|changed during recovery"):
                recover_abandoned_restore_lock(
                    "operator@example.com", "INC-128", confirm=True, audit_path=audit,
                )
        self.assertTrue(lock_path.exists())
        self.assertFalse(audit.exists())

    def test_restore_lock_inspection_fails_closed_for_unreadable_metadata(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        lock_path.write_text("not-json", encoding="utf-8")
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertTrue(lock_path.exists())

    def test_restore_lock_inspection_rejects_symlink_marker(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        target = Path(self.temp_dir.name) / "restore-marker-target"
        target.write_text("{}", encoding="utf-8")
        lock_path.symlink_to(target)
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_UNSAFE_MARKER")
        self.assertTrue(lock_path.is_symlink())

    def test_restore_lock_inspection_detects_marker_changed_during_guard_probe(self):
        import fcntl as system_fcntl
        import json

        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker = {
            "plan_id": "a" * 32,
            "pid": os.getpid(),
            "created_at": "2026-11-17T10:00:00+02:00",
        }
        lock_path.write_text(json.dumps(marker), encoding="utf-8")
        original_flock = system_fcntl.flock

        def mutate_then_lock(fd, operation):
            marker["plan_id"] = "b" * 32
            lock_path.write_text(json.dumps(marker), encoding="utf-8")
            return original_flock(fd, operation)

        with patch("app.maintenance.fcntl.flock", side_effect=mutate_then_lock):
            result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_CHANGED_DURING_INSPECTION")
        self.assertTrue(lock_path.exists())
        self.assertFalse(result["marker_removed"])
        self.assertFalse(result["automatic_recovery_performed"])

    def test_restore_lock_inspection_rejects_symlink_guard(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker = {
            "plan_id": "c" * 32,
            "pid": os.getpid(),
            "created_at": "2026-11-17T10:00:00+02:00",
        }
        lock_path.write_text(json.dumps(marker), encoding="utf-8")
        guard_path = database.restore_guard_path()
        target = Path(self.temp_dir.name) / "restore-guard-target"
        target.write_bytes(b"")
        guard_path.unlink(missing_ok=True)
        guard_path.symlink_to(target)
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_UNSAFE_GUARD")
        self.assertTrue(guard_path.is_symlink())
        self.assertTrue(lock_path.exists())

    def test_process_crash_leaves_restore_marker_fail_closed(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(
            target=_crash_after_acquiring_restore_lock,
            args=(ready,),
        )
        worker.start()
        worker.join(5)
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        self.assertTrue(ready.is_set())
        self.assertTrue(is_restore_lock_active())
        inspection = inspect_restore_lock()
        self.assertEqual(
            inspection["status"],
            "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW",
        )
        self.assertFalse(inspection["automatic_recovery_performed"])
        self.assertFalse(inspection["marker_removed"])
        with self.assertRaisesRegex(sqlite3.OperationalError, "controlled restore"):
            database.get_connection()
        # An abandoned marker is intentionally not reclaimed automatically.
        # A human operator must inspect recovery state before removing it.
        with self.assertRaises(FileExistsError):
            acquire_restore_lock("8" * 32, timeout_seconds=0.1)

    def test_restore_lock_waits_for_connections_in_another_process(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        worker = context.Process(
            target=_hold_database_connection_for_restore_test,
            args=(ready, release),
        )
        worker.start()
        try:
            self.assertTrue(ready.wait(5), "worker did not open its database connection")
            with self.assertRaisesRegex(RuntimeError, "another process"):
                acquire_restore_lock("5" * 32, timeout_seconds=0.15)
        finally:
            release.set()
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        # A timed-out acquisition must not leave a marker or strand the guard.
        self.assertFalse(is_restore_lock_active())
        with database.get_connection() as connection:
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_live_backup_holds_process_guard_against_restore_race(self):
        def attempt_restore_during_backup(*args, **kwargs):
            return acquire_restore_lock("6" * 32, timeout_seconds=0.1)

        with patch(
            "app.maintenance.sqlite3.connect",
            side_effect=attempt_restore_during_backup,
        ):
            with self.assertRaisesRegex(RuntimeError, "another process"):
                create_verified_backup()
        self.assertFalse(is_restore_lock_active())

    def test_live_backup_is_blocked_while_restore_lock_is_active(self):
        lock = acquire_restore_lock("4" * 32)
        try:
            with self.assertRaisesRegex(RuntimeError, "controlled restore"):
                create_verified_backup()
        finally:
            release_restore_lock(lock["lock_token"])

    def test_backup_is_created_and_integrity_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live.sqlite3"
            backup_dir = root / "backups"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('preserve-me')")

            result = create_verified_backup(
                source_path=source,
                backup_dir=backup_dir,
                now=datetime(2026, 11, 15, 23, 15, tzinfo=ZoneInfo("Africa/Johannesburg")),
            )
            backup = Path(result["backup_file"])
            self.assertEqual(result["status"], "BACKUP_VERIFIED")
            self.assertTrue(backup.is_file())
            with sqlite3.connect(backup) as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(value, "preserve-me")
            self.assertEqual(integrity, "ok")

    def test_concurrent_same_second_backups_publish_distinct_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live.sqlite3"
            backup_dir = root / "backups"
            fixed_now = datetime(2026, 11, 15, 23, 15, tzinfo=ZoneInfo("Africa/Johannesburg"))
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('preserve-me')")

            first = create_verified_backup(source_path=source, backup_dir=backup_dir, now=fixed_now)
            second = create_verified_backup(source_path=source, backup_dir=backup_dir, now=fixed_now)

            first_path = Path(first["backup_file"])
            second_path = Path(second["backup_file"])
            self.assertNotEqual(first_path, second_path)
            self.assertTrue(first_path.is_file())
            self.assertTrue(second_path.is_file())
            self.assertFalse(list(backup_dir.glob("*.tmp")))
            for backup_path in (first_path, second_path):
                with sqlite3.connect(backup_path) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_retention_dry_run_then_deletes_only_expired_verified_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            old_backup = root / "taimobot-20241101-230000-old.sqlite3"
            recent_backup = root / "taimobot-20261115-230000-recent.sqlite3"
            newest_backup = root / "taimobot-20261116-230000-newest.sqlite3"

            for backup_path in (old_backup, recent_backup, newest_backup):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
                    connection.execute("INSERT INTO sample VALUES (?)", (backup_path.name,))

            old_time = now.timestamp() - 400 * 24 * 60 * 60
            recent_time = now.timestamp() - 1 * 24 * 60 * 60
            newest_time = now.timestamp() - 60
            os.utime(old_backup, (old_time, old_time))
            os.utime(recent_backup, (recent_time, recent_time))
            os.utime(newest_backup, (newest_time, newest_time))

            preview = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=True,
                live_database_path=self.original_db_path,
            )
            self.assertEqual(preview["status"], "BACKUP_RETENTION_DRY_RUN")
            self.assertEqual(preview["candidates"], [old_backup.name])
            self.assertEqual(preview["deleted"], [])
            self.assertTrue(old_backup.exists())

            result = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=False,
                live_database_path=self.original_db_path,
            )
            self.assertEqual(result["deleted"], [old_backup.name])
            self.assertFalse(old_backup.exists())
            self.assertTrue(recent_backup.exists())
            self.assertTrue(newest_backup.exists())
            self.assertEqual(result["newest_verified_backup"], newest_backup.name)

    def test_backup_retention_keeps_backup_exactly_at_one_year_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            boundary_backup = root / "taimobot-20251116-120000-boundary.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"
            expired_backup = root / "taimobot-20251116-115959-expired.sqlite3"
            for backup_path in (boundary_backup, recent_backup, expired_backup):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")

            cutoff = now.timestamp() - 365 * 24 * 60 * 60
            os.utime(boundary_backup, (cutoff, cutoff))
            os.utime(expired_backup, (cutoff - 1, cutoff - 1))

            result = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=False,
                live_database_path=self.original_db_path,
            )

            self.assertEqual(result["deleted"], [expired_backup.name])
            self.assertTrue(boundary_backup.exists())
            self.assertTrue(recent_backup.exists())
            self.assertFalse(expired_backup.exists())

    def test_backup_replaced_with_recent_file_during_delete_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            expired = root / "taimobot-20241101-230000-expired.sqlite3"
            recent = root / "taimobot-20261116-110000-recent.sqlite3"
            replacement = root / "replacement.sqlite3"
            for path in (expired, recent, replacement):
                with sqlite3.connect(path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            old_time = now.timestamp() - 400 * 24 * 60 * 60
            os.utime(expired, (old_time, old_time))
            os.utime(recent, (now.timestamp() - 60, now.timestamp() - 60))
            real_connect = sqlite3.connect
            candidate_checks = 0

            def replace_before_final_validation(database_name, *args, **kwargs):
                nonlocal candidate_checks
                if str(expired.resolve()) in str(database_name):
                    candidate_checks += 1
                    if candidate_checks == 2:
                        os.replace(replacement, expired)
                        os.utime(expired, (now.timestamp(), now.timestamp()))
                return real_connect(database_name, *args, **kwargs)

            with patch(
                "app.maintenance.sqlite3.connect",
                side_effect=replace_before_final_validation,
            ):
                result = cleanup_expired_backups(
                    backup_dir=root, now=now, dry_run=False,
                    live_database_path=self.original_db_path,
                )

            self.assertEqual(result["deleted"], [])
            self.assertTrue(expired.exists())
            self.assertGreaterEqual(expired.stat().st_mtime, now.timestamp() - 1)

    def test_backup_retention_reports_blocked_when_expired_backup_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            expired = root / "taimobot-20241101-230000-expired.sqlite3"
            recent = root / "taimobot-20261116-110000-recent.sqlite3"
            for backup_path in (expired, recent):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            os.utime(expired, (now.timestamp() - 400 * 24 * 60 * 60,) * 2)

            with patch.object(Path, "unlink", side_effect=PermissionError("deletion denied")):
                result = cleanup_expired_backups(
                    backup_dir=root, now=now, dry_run=False,
                    live_database_path=self.original_db_path,
                )

            self.assertEqual(result["status"], "BACKUP_RETENTION_BLOCKED")
            self.assertEqual(result["deleted"], [])
            self.assertEqual(result["deletion_failures"], [expired.name])
            self.assertTrue(expired.exists())
            self.assertTrue(recent.exists())

    def test_backup_retention_fails_safe_without_recent_verified_recovery_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            old_backup = root / "taimobot-20241101-230000-old.sqlite3"
            with sqlite3.connect(old_backup) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            old_time = now.timestamp() - 400 * 24 * 60 * 60
            os.utime(old_backup, (old_time, old_time))

            result = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=False,
                live_database_path=self.original_db_path,
            )
            self.assertFalse(result["recent_recovery_point_verified"])
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["deleted"], [])
            self.assertTrue(old_backup.exists())

    def test_backup_retention_preserves_latest_recorded_and_active_attempt_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            latest_recorded = root / "taimobot-20241101-230000-recorded.sqlite3"
            active_attempt = root / "taimobot-20241102-230000-active.sqlite3"
            expired_candidate = root / "taimobot-20241103-230000-expired.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"
            for backup_path in (latest_recorded, active_attempt, expired_candidate, recent_backup):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            old_time = now.timestamp() - 400 * 24 * 60 * 60
            for backup_path in (latest_recorded, active_attempt, expired_candidate):
                os.utime(backup_path, (old_time, old_time))

            with database.get_connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE maintenance_runs (
                        run_date TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        backup_file TEXT,
                        error_summary TEXT,
                        run_token TEXT
                    );
                    CREATE TABLE maintenance_run_attempts (
                        attempt_token TEXT PRIMARY KEY,
                        run_date TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        backup_file TEXT,
                        error_summary TEXT
                    );
                    """
                )
                connection.execute(
                    """
                    INSERT INTO maintenance_runs (
                        run_date, status, started_at, completed_at, backup_file
                    ) VALUES ('2025-10-01', 'COMPLETE', '2025-10-01', '2025-10-01', ?)
                    """,
                    (str(latest_recorded),),
                )
                connection.execute(
                    """
                    INSERT INTO maintenance_run_attempts (
                        attempt_token, run_date, status, started_at, backup_file
                    ) VALUES ('active-attempt', '2026-11-16', 'RUNNING', '2026-11-16', ?)
                    """,
                    (str(active_attempt),),
                )

            result = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=False,
                live_database_path=self.original_db_path,
            )
            self.assertEqual(result["deleted"], [expired_candidate.name])
            self.assertTrue(latest_recorded.exists())
            self.assertTrue(active_attempt.exists())
            self.assertTrue(recent_backup.exists())

    def test_backup_retention_preserves_latest_failed_and_superseded_verified_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            latest_failed = root / "taimobot-20241101-230000-latest-failed.sqlite3"
            older_failed = root / "taimobot-20241102-230000-older-failed.sqlite3"
            latest_superseded = root / "taimobot-20241103-230000-latest-superseded.sqlite3"
            older_superseded = root / "taimobot-20241104-230000-older-superseded.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"
            backups = (
                latest_failed,
                older_failed,
                latest_superseded,
                older_superseded,
                recent_backup,
            )
            for backup_path in backups:
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            old_time = now.timestamp() - 400 * 24 * 60 * 60
            for backup_path in backups[:-1]:
                os.utime(backup_path, (old_time, old_time))

            with database.get_connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE maintenance_run_attempts (
                        attempt_token TEXT PRIMARY KEY,
                        run_date TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        backup_file TEXT,
                        error_summary TEXT
                    );
                    """
                )
                attempts = (
                    ("failed-latest", "FAILED", "2026-11-15T23:10:00+02:00", latest_failed),
                    ("failed-older", "FAILED", "2026-11-14T23:10:00+02:00", older_failed),
                    ("superseded-latest", "SUPERSEDED", "2026-11-15T23:20:00+02:00", latest_superseded),
                    ("superseded-older", "SUPERSEDED", "2026-11-14T23:20:00+02:00", older_superseded),
                )
                connection.executemany(
                    """
                    INSERT INTO maintenance_run_attempts (
                        attempt_token, run_date, status, started_at,
                        completed_at, backup_file
                    ) VALUES (?, '2026-11-15', ?, ?, ?, ?)
                    """,
                    [
                        (token, status, started, started, str(path))
                        for token, status, started, path in attempts
                    ],
                )

            result = cleanup_expired_backups(
                backup_dir=root,
                now=now,
                dry_run=False,
                live_database_path=self.original_db_path,
            )

            self.assertEqual(
                set(result["deleted"]),
                {older_failed.name, older_superseded.name},
            )
            self.assertTrue(latest_failed.exists())
            self.assertTrue(latest_superseded.exists())
            self.assertFalse(older_failed.exists())
            self.assertFalse(older_superseded.exists())
            self.assertTrue(recent_backup.exists())

    def test_backup_retention_fails_closed_when_maintenance_metadata_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            old_backup = root / "taimobot-20241101-230000-old.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"
            for backup_path in (old_backup, recent_backup):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            old_time = now.timestamp() - 400 * 24 * 60 * 60
            os.utime(old_backup, (old_time, old_time))

            with patch(
                "app.maintenance.database.get_connection",
                side_effect=sqlite3.OperationalError("metadata unavailable"),
            ):
                result = cleanup_expired_backups(
                    backup_dir=root, now=now, dry_run=False,
                    live_database_path=self.original_db_path,
                )

            self.assertEqual(result["status"], "BACKUP_RETENTION_BLOCKED")
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["deleted"], [])
            self.assertTrue(old_backup.exists())
            self.assertTrue(recent_backup.exists())

    def test_backup_retention_fails_closed_when_backup_directory_cannot_be_listed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            old_backup = root / "taimobot-20241101-230000-old.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"
            for backup_path in (old_backup, recent_backup):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            old_time = now.timestamp() - 400 * 24 * 60 * 60
            os.utime(old_backup, (old_time, old_time))

            with patch.object(Path, "iterdir", side_effect=PermissionError("permission denied")):
                result = cleanup_expired_backups(
                    backup_dir=root, now=now, dry_run=False,
                    live_database_path=self.original_db_path,
                )

            self.assertEqual(result["status"], "BACKUP_RETENTION_BLOCKED")
            self.assertEqual(result["candidates"], [])
            self.assertEqual(result["deleted"], [])
            self.assertTrue(old_backup.exists())
            self.assertTrue(recent_backup.exists())

    def test_backup_retention_never_deletes_live_database_or_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            live_db = root / "taimobot-20241101-230000-live.sqlite3"
            corrupt_backup = root / "taimobot-20241102-230000-corrupt.sqlite3"
            expired_backup = root / "taimobot-20241103-230000-expired.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"

            with sqlite3.connect(live_db) as connection:
                connection.execute("CREATE TABLE live_marker (value TEXT)")
                connection.execute("INSERT INTO live_marker VALUES ('must-survive')")
            corrupt_backup.write_bytes(b"not a sqlite database")
            for backup_path in (expired_backup, recent_backup):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")

            old_time = now.timestamp() - 400 * 24 * 60 * 60
            os.utime(live_db, (old_time, old_time))
            os.utime(corrupt_backup, (old_time, old_time))
            os.utime(expired_backup, (old_time, old_time))

            result = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=False,
                live_database_path=live_db,
            )

            self.assertEqual(result["deleted"], [expired_backup.name])
            self.assertTrue(live_db.exists())
            self.assertTrue(corrupt_backup.exists())
            self.assertIn(corrupt_backup.name, result["invalid_or_unreadable"])
            with sqlite3.connect(live_db) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM live_marker").fetchone()[0],
                    "must-survive",
                )

    def test_backup_retention_ignores_restore_staging_and_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 11, 16, 12, 0, tzinfo=ZoneInfo("Africa/Johannesburg"))
            old_backup = root / "taimobot-20241101-230000-expired.sqlite3"
            recent_backup = root / "taimobot-20261116-110000-recent.sqlite3"
            staging_dir = root / "recovery-staging"
            staging_dir.mkdir()
            staging_file = staging_dir / "candidate-2026-11-16-attempt.sqlite3"
            temporary_file = root / ".taimobot-backup-in-progress.tmp"

            for backup_path in (old_backup, recent_backup, staging_file):
                with sqlite3.connect(backup_path) as connection:
                    connection.execute("CREATE TABLE sample (value TEXT)")
            temporary_file.write_bytes(b"backup still being written")

            old_time = now.timestamp() - 400 * 24 * 60 * 60
            os.utime(old_backup, (old_time, old_time))

            result = cleanup_expired_backups(
                backup_dir=root, now=now, dry_run=False,
                live_database_path=self.original_db_path,
            )

            self.assertEqual(result["deleted"], [old_backup.name])
            self.assertFalse(old_backup.exists())
            self.assertTrue(recent_backup.exists())
            self.assertTrue(staging_file.exists())
            self.assertTrue(temporary_file.exists())

    def test_backup_retention_rejects_less_than_one_year(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least 365 days"):
                cleanup_expired_backups(backup_dir=Path(directory), retention_days=30)

    def test_recovery_inventory_checks_integrity_schema_and_foreign_keys_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_backup = root / "taimobot-20261116-230000-valid.sqlite3"
            incomplete_backup = root / "taimobot-20261116-220000-incomplete.sqlite3"
            corrupt_backup = root / "taimobot-20261116-210000-corrupt.sqlite3"

            with sqlite3.connect(database.DB_PATH) as source, sqlite3.connect(valid_backup) as target:
                source.backup(target)
            with sqlite3.connect(incomplete_backup) as connection:
                connection.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY)")
            corrupt_backup.write_bytes(b"not a sqlite database")

            before_live_bytes = database.DB_PATH.read_bytes()
            result = inspect_recovery_candidates(root)
            by_name = {item["file_name"]: item for item in result["candidates"]}

            self.assertEqual(result["status"], "RECOVERY_INVENTORY_COMPLETE")
            self.assertEqual(result["candidate_count"], 3)
            self.assertTrue(by_name[valid_backup.name]["restore_ready"])
            self.assertEqual(by_name[valid_backup.name]["integrity_check"], "ok")
            self.assertFalse(by_name[incomplete_backup.name]["restore_ready"])
            self.assertIn("TAIMOBOT_SCHEMA_INCOMPLETE", by_name[incomplete_backup.name]["issues"])
            self.assertFalse(by_name[corrupt_backup.name]["restore_ready"])
            self.assertIn("BACKUP_UNREADABLE", by_name[corrupt_backup.name]["issues"])
            self.assertEqual(database.DB_PATH.read_bytes(), before_live_bytes)
            self.assertFalse(result["live_database_replaced"])

    def test_restore_verification_uses_staging_and_does_not_replace_live_db(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup.sqlite3"
            live = root / "live.sqlite3"
            staging = root / "recovery" / "candidate.sqlite3"
            with sqlite3.connect(backup) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('backup-data')")
            with sqlite3.connect(live) as connection:
                connection.execute("CREATE TABLE live_marker (value TEXT)")
                connection.execute("INSERT INTO live_marker VALUES ('live-data')")

            result = verify_restore_candidate(backup, staging)
            self.assertEqual(result["status"], "RESTORE_CANDIDATE_VERIFIED")
            self.assertFalse(result["live_database_replaced"])
            with sqlite3.connect(staging) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM sample").fetchone()[0],
                    "backup-data",
                )
            with sqlite3.connect(live) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM live_marker").fetchone()[0],
                    "live-data",
                )



    def test_admin_restore_preparation_creates_verified_pre_restore_backup_and_isolated_plan(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore

        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            selected = backup_dir / "taimobot-valid.sqlite3"
            selected.write_bytes(b"selected-backup")
            pre_restore = backup_dir / "taimobot-pre-restore.sqlite3"
            pre_restore.write_bytes(b"pre-restore-backup")

            with patch("app.main.BACKUP_DIR", backup_dir), patch(
                "app.main.require_admin"
            ), patch(
                "app.main.inspect_recovery_candidates",
                side_effect=lambda directory=None: {
                    "status": "RECOVERY_INVENTORY_COMPLETE",
                    "candidates": [{
                        "file_name": (
                            selected.name if directory is None
                            else next(Path(directory).glob("taimobot-restore-*.sqlite3")).name
                        ),
                        "restore_ready": True,
                    }],
                },
            ), patch(
                "app.main.create_verified_backup",
                return_value={"status": "BACKUP_VERIFIED", "backup_file": str(pre_restore)},
            ) as create_backup, patch(
                "app.main.verify_restore_candidate",
                side_effect=lambda backup, staging: (
                    staging.parent.mkdir(parents=True, exist_ok=True),
                    staging.write_bytes(b"verified-staged-copy"),
                    {"status": "RESTORE_CANDIDATE_VERIFIED"},
                )[-1],
            ) as verify_candidate:
                result = prepare_maintenance_restore(
                    MaintenanceRestorePrepareRequest(
                        backup_file_name=selected.name,
                        confirm_preparation=True,
                    ),
                    {"email": "ops@example.com"},
                )

            self.assertEqual(result["status"], "RESTORE_PREPARED")
            self.assertTrue(result["selected_backup_verified"])
            self.assertTrue(result["pre_restore_backup_verified"])
            self.assertTrue(result["staged_candidate_verified"])
            self.assertTrue(result["approval_required"])
            self.assertFalse(result["live_database_replaced"])
            create_backup.assert_called_once()
            verify_candidate.assert_called_once()
            with database.get_connection() as connection:
                plan = connection.execute(
                    "SELECT status, pre_restore_backup_file FROM maintenance_restore_plans WHERE plan_id = ?",
                    (result["plan_id"],),
                ).fetchone()
            self.assertEqual(plan["status"], "PREPARED")
            self.assertEqual(plan["pre_restore_backup_file"], str(pre_restore.resolve()))

    def test_admin_restore_preparation_requires_explicit_confirmation(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException

        with patch("app.main.require_admin"):
            with self.assertRaises(HTTPException) as rejected:
                prepare_maintenance_restore(
                    MaintenanceRestorePrepareRequest(
                        backup_file_name="taimobot-valid.sqlite3",
                        confirm_preparation=False,
                    ),
                    {"email": "ops@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 400)


    def test_restore_approval_revalidates_artifacts_and_only_changes_plan_status(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore

        backup_dir = Path(self.temp_dir.name) / "backups"
        staging_dir = backup_dir / "recovery-staging"
        staging_dir.mkdir(parents=True)
        source = backup_dir / "taimobot-source.sqlite3"
        rollback = backup_dir / "taimobot-rollback.sqlite3"
        staged = staging_dir / "taimobot-restore-plan.sqlite3"
        for artifact in (source, rollback, staged):
            artifact.write_bytes(b"verified-fixture")
        plan_id = "a" * 32
        with database.get_connection() as connection:
            connection.execute(
                """CREATE TABLE maintenance_restore_plans (
                    plan_id TEXT PRIMARY KEY, backup_file TEXT NOT NULL,
                    staging_file TEXT NOT NULL, pre_restore_backup_file TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE maintenance_restore_artifacts (
                    plan_id TEXT PRIMARY KEY REFERENCES maintenance_restore_plans(plan_id),
                    source_sha256 TEXT NOT NULL, staged_sha256 TEXT NOT NULL,
                    rollback_sha256 TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO maintenance_restore_plans
                VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)""",
                (plan_id, str(source), str(staged), str(rollback),
                 datetime.now(ZoneInfo("Africa/Johannesburg")).isoformat(), "ops@example.com"),
            )
            connection.execute(
                """INSERT INTO maintenance_restore_artifacts
                VALUES (?, ?, ?, ?)""",
                (plan_id, sha256_file(source), sha256_file(staged), sha256_file(rollback)),
            )

        def ready_inventory(directory=None):
            names = (
                [source.name, rollback.name] if Path(directory) == backup_dir
                else [staged.name]
            )
            return {
                "status": "RECOVERY_INVENTORY_COMPLETE",
                "candidates": [{"file_name": name, "restore_ready": True} for name in names],
            }

        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=ready_inventory
        ):
            result = approve_maintenance_restore(
                MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                {"email": "ops@example.com"},
            )

        self.assertEqual(result["status"], "RESTORE_APPROVED")
        self.assertFalse(result["restore_application_authorized"])
        self.assertFalse(result["live_database_replaced"])
        with database.get_connection() as connection:
            status = connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "APPROVED")

    def test_restore_approval_blocks_unverified_staging_and_leaves_plan_prepared(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException

        backup_dir = Path(self.temp_dir.name) / "backups"
        staging_dir = backup_dir / "recovery-staging"
        staging_dir.mkdir(parents=True)
        source = backup_dir / "taimobot-source.sqlite3"
        rollback = backup_dir / "taimobot-rollback.sqlite3"
        staged = staging_dir / "taimobot-restore-plan.sqlite3"
        for artifact in (source, rollback, staged):
            artifact.write_bytes(b"fixture")
        plan_id = "b" * 32
        with database.get_connection() as connection:
            connection.execute(
                """CREATE TABLE maintenance_restore_plans (
                    plan_id TEXT PRIMARY KEY, backup_file TEXT NOT NULL,
                    staging_file TEXT NOT NULL, pre_restore_backup_file TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO maintenance_restore_plans
                VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)""",
                (plan_id, str(source), str(staged), str(rollback),
                 datetime.now(ZoneInfo("Africa/Johannesburg")).isoformat(), "ops@example.com"),
            )

        def inventory(directory=None):
            names = [source.name, rollback.name] if Path(directory) == backup_dir else [staged.name]
            return {
                "status": "RECOVERY_INVENTORY_COMPLETE",
                "candidates": [
                    {"file_name": name, "restore_ready": name != staged.name}
                    for name in names
                ],
            }

        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=inventory
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "ops@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        with database.get_connection() as connection:
            status = connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "PREPARED")



    def test_restore_approval_rejects_artifact_replaced_after_preparation(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException

        backup_dir = Path(self.temp_dir.name) / "backups"
        staging_dir = backup_dir / "recovery-staging"
        staging_dir.mkdir(parents=True)
        source = backup_dir / "taimobot-source.sqlite3"
        rollback = backup_dir / "taimobot-rollback.sqlite3"
        staged = staging_dir / "taimobot-restore-plan.sqlite3"
        for artifact in (source, rollback, staged):
            artifact.write_bytes(b"verified-before-preparation")
        plan_id = "c" * 32
        with database.get_connection() as connection:
            connection.execute(
                """CREATE TABLE maintenance_restore_plans (
                    plan_id TEXT PRIMARY KEY, backup_file TEXT NOT NULL,
                    staging_file TEXT NOT NULL, pre_restore_backup_file TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE maintenance_restore_artifacts (
                    plan_id TEXT PRIMARY KEY REFERENCES maintenance_restore_plans(plan_id),
                    source_sha256 TEXT NOT NULL, staged_sha256 TEXT NOT NULL,
                    rollback_sha256 TEXT NOT NULL
                )"""
            )
            connection.execute(
                """INSERT INTO maintenance_restore_plans
                VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)""",
                (plan_id, str(source), str(staged), str(rollback),
                 datetime.now(ZoneInfo("Africa/Johannesburg")).isoformat(), "ops@example.com"),
            )
            connection.execute(
                "INSERT INTO maintenance_restore_artifacts VALUES (?, ?, ?, ?)",
                (plan_id, sha256_file(source), sha256_file(staged), sha256_file(rollback)),
            )

        staged.write_bytes(b"valid-looking-but-replaced-after-preparation")

        def ready_inventory(directory=None):
            names = [source.name, rollback.name] if Path(directory) == backup_dir else [staged.name]
            return {
                "status": "RECOVERY_INVENTORY_COMPLETE",
                "candidates": [{"file_name": name, "restore_ready": True} for name in names],
            }

        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=ready_inventory
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "ops@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertIn("fingerprint", rejected.exception.detail)
        with database.get_connection() as connection:
            status = connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "PREPARED")

    def test_sha256_file_streams_large_artifact_in_small_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "recovery.sqlite3"
            artifact.write_bytes(b"0123456789" * 100)
            self.assertEqual(sha256_file(artifact, chunk_size=7), sha256_file(artifact))
            with self.assertRaises(ValueError):
                sha256_file(artifact, chunk_size=0)


    def test_restore_lock_quiesces_application_connections_and_requires_owner_token(self):
        from app.database import get_connection

        plan_id = "d" * 32
        lock = acquire_restore_lock(plan_id)
        self.assertEqual(lock["status"], "RESTORE_LOCK_ACQUIRED")
        self.assertTrue(lock["database_quiescent"])
        self.assertTrue(is_restore_lock_active())
        self.assertTrue(Path(lock["lock_file"]).is_file())

        with self.assertRaisesRegex(sqlite3.OperationalError, "controlled restore"):
            get_connection()

        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            release_restore_lock("e" * 32)
        self.assertTrue(is_restore_lock_active())

        released = release_restore_lock(lock["lock_token"])
        self.assertEqual(released["status"], "RESTORE_LOCK_RELEASED")
        self.assertFalse(is_restore_lock_active())
        with get_connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1").fetchone())


    def test_restore_lock_refuses_to_start_while_application_connection_is_open(self):
        from app.database import get_connection, open_connection_count

        existing_connection = get_connection()
        try:
            self.assertEqual(open_connection_count(), 1)
            with self.assertRaisesRegex(RuntimeError, "connections remain open"):
                acquire_restore_lock("3" * 32)
            self.assertFalse(is_restore_lock_active())
            self.assertEqual(existing_connection.execute("SELECT 1").fetchone()[0], 1)
        finally:
            existing_connection.close()

        self.assertEqual(open_connection_count(), 0)
        lock = acquire_restore_lock("3" * 32)
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, "controlled restore"):
                get_connection()
        finally:
            release_restore_lock(lock["lock_token"])

    def test_restore_lock_is_exclusive_and_never_reclaims_an_existing_lock(self):
        first = acquire_restore_lock("f" * 32)
        try:
            self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_ACTIVE")
            with self.assertRaisesRegex(RuntimeError, "another process"):
                acquire_restore_lock("1" * 32, timeout_seconds=0.05)
            self.assertTrue(is_restore_lock_active())
        finally:
            release_restore_lock(first["lock_token"])

    def test_restore_lock_fails_closed_when_existing_writer_cannot_quiesce(self):
        from app.database import get_connection

        connection = get_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            with self.assertRaisesRegex(RuntimeError, "connections remain open"):
                acquire_restore_lock("2" * 32, timeout_seconds=0.01)
            self.assertFalse(is_restore_lock_active())
        finally:
            connection.rollback()
            connection.close()

    def test_restore_lock_rejects_invalid_plan_and_release_tokens(self):
        with self.assertRaisesRegex(ValueError, "plan ID"):
            acquire_restore_lock("../unsafe")
        with self.assertRaisesRegex(ValueError, "lock token"):
            release_restore_lock("not-a-token")

    def test_restore_execution_preflight_verifies_source_stage_rollback_and_live_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-restore-plan.sqlite3"

            for target in (source, rollback, staged):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)

            before_live = database.DB_PATH.read_bytes()
            result = verify_restore_execution_readiness(
                source, staged, rollback,
                backup_dir=backup_dir,
                live_database_path=database.DB_PATH,
            )

            self.assertEqual(result["status"], "RESTORE_EXECUTION_PREFLIGHT_PASSED")
            self.assertTrue(result["source_backup_verified"])
            self.assertTrue(result["staged_candidate_verified"])
            self.assertTrue(result["rollback_backup_verified"])
            self.assertTrue(result["live_database_verified"])
            self.assertFalse(result["execution_authorized"])
            self.assertFalse(result["live_database_replaced"])
            self.assertEqual(database.DB_PATH.read_bytes(), before_live)

    def test_restore_execution_preflight_fails_closed_when_rollback_is_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-restore-plan.sqlite3"

            for target in (source, staged):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)
            rollback.write_bytes(b"corrupt rollback")

            with self.assertRaisesRegex(ValueError, "rollback backup is not restore-ready"):
                verify_restore_execution_readiness(
                    source, staged, rollback,
                    backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(database.DB_PATH.is_file())


    def test_approved_restore_plan_preflight_rechecks_fingerprints_and_readiness(self):
        zone = ZoneInfo("Africa/Johannesburg")
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            source.write_bytes(b"source artifact")
            rollback.write_bytes(b"rollback artifact")
            staged.write_bytes(b"staged artifact")
            plan = {
                "plan_id": "a" * 32,
                "backup_file": str(source),
                "staging_file": str(staged),
                "pre_restore_backup_file": str(rollback),
                "status": "APPROVED",
                "created_at": datetime(2026, 11, 16, 12, 0, tzinfo=zone).isoformat(),
            }
            fingerprints = {
                "source_sha256": sha256_file(source),
                "staged_sha256": sha256_file(staged),
                "rollback_sha256": sha256_file(rollback),
            }
            with patch("app.maintenance.is_restore_lock_active", return_value=False), patch(
                "app.maintenance.verify_restore_execution_readiness",
                return_value={"status": "RESTORE_EXECUTION_PREFLIGHT_PASSED"},
            ) as readiness:
                result = verify_approved_restore_plan(
                    plan, fingerprints, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                    now=datetime(2026, 11, 16, 12, 5, tzinfo=zone),
                )
            self.assertEqual(result["status"], "APPROVED_RESTORE_PREFLIGHT_PASSED")
            self.assertTrue(result["plan_approved"])
            self.assertTrue(result["artifact_fingerprints_match"])
            self.assertFalse(result["execution_authorized"])
            self.assertFalse(result["live_database_replaced"])
            readiness.assert_called_once()

    def test_approved_restore_plan_preflight_blocks_expiry_and_fingerprint_mismatch(self):
        zone = ZoneInfo("Africa/Johannesburg")
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            source.write_bytes(b"source")
            rollback.write_bytes(b"rollback")
            staged.write_bytes(b"staged")
            plan = {
                "plan_id": "b" * 32,
                "backup_file": str(source),
                "staging_file": str(staged),
                "pre_restore_backup_file": str(rollback),
                "status": "APPROVED",
                "created_at": datetime(2026, 11, 16, 12, 0, tzinfo=zone).isoformat(),
            }
            fingerprints = {
                "source_sha256": sha256_file(source),
                "staged_sha256": sha256_file(staged),
                "rollback_sha256": sha256_file(rollback),
            }
            with patch("app.maintenance.is_restore_lock_active", return_value=False):
                with self.assertRaisesRegex(ValueError, "expired"):
                    verify_approved_restore_plan(
                        plan, fingerprints, backup_dir=backup_dir,
                        now=datetime(2026, 11, 16, 12, 31, tzinfo=zone),
                    )
                staged.write_bytes(b"tampered staged artifact")
                with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                    verify_approved_restore_plan(
                        plan, fingerprints, backup_dir=backup_dir,
                        now=datetime(2026, 11, 16, 12, 5, tzinfo=zone),
                    )


    def test_sqlite_sidecar_inventory_reports_wal_and_shm_without_mutating(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / "live.sqlite3"
            live.write_bytes(b"live-db")
            clear = inspect_sqlite_sidecars(live)
            self.assertEqual(clear["status"], "SIDECARS_CLEAR")
            wal = Path(str(live) + "-wal")
            shm = Path(str(live) + "-shm")
            wal.write_bytes(b"wal-pending-data")
            shm.write_bytes(b"shared-memory")
            before = (wal.read_bytes(), shm.read_bytes())
            result = inspect_sqlite_sidecars(live)
            self.assertEqual(result["status"], "SIDECARS_PRESENT_REQUIRES_LOCKED_HANDLING")
            self.assertEqual({item["kind"] for item in result["sidecars"]}, {"WAL", "SHM"})
            self.assertFalse(result["mutation_performed"])
            self.assertEqual((wal.read_bytes(), shm.read_bytes()), before)

    def test_sqlite_sidecar_inventory_fails_closed_for_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            live = Path(directory) / "live.sqlite3"
            live.write_bytes(b"live-db")
            target = Path(directory) / "outside"
            target.write_bytes(b"outside-data")
            wal = Path(str(live) + "-wal")
            wal.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "not a regular managed file"):
                inspect_sqlite_sidecars(live)
            self.assertEqual(target.read_bytes(), b"outside-data")

    def test_restore_execution_preflight_reports_sidecars_for_locked_handling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-restore-plan.sqlite3"
            for target in (source, rollback, staged):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)
            wal = Path(str(database.DB_PATH) + "-wal")
            wal.write_bytes(b"")
            try:
                result = verify_restore_execution_readiness(
                    source, staged, rollback,
                    backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
                self.assertTrue(result["sidecar_handling_required_after_lock"])
                self.assertEqual(
                    result["sqlite_sidecars"]["status"],
                    "SIDECARS_PRESENT_REQUIRES_LOCKED_HANDLING",
                )
                self.assertTrue(wal.exists())
            finally:
                wal.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
