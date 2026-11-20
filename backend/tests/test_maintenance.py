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
    verify_restore_execution_readiness_under_lock,
    sha256_file,
    acquire_restore_lock,
    release_restore_lock,
    is_restore_lock_active,
    verify_approved_restore_plan,
    inspect_sqlite_sidecars,
    inspect_restore_lock,
    recover_abandoned_restore_lock,
    append_restore_execution_event,
    verify_restore_execution_journal,
    inspect_restore_execution_state,
    reconcile_restore_execution_artifacts,
    _restore_journal_digest,
)
from app import database, maintenance as maintenance_module
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


    def _write_abandoned_restore_marker(self, plan_id="a" * 32):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        lock_path.write_text(json.dumps({
            "plan_id": plan_id,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "b" * 32,
        }), encoding="utf-8")
        return lock_path

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

    def test_abandoned_restore_recovery_records_started_if_completion_audit_fails(self):
        lock_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        lock_path.write_text(json.dumps({
            "plan_id": "4" * 32,
            "pid": 999999,
            "created_at": "2026-11-17T10:00:00+02:00",
            "lock_token": "5" * 32,
        }), encoding="utf-8")
        audit = Path(self.temp_dir.name) / "completion-audit-failure.jsonl"
        real_write = os.write
        calls = {"count": 0}

        def fail_second_audit_write(fd, data):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("simulated completion audit failure")
            return real_write(fd, data)

        with patch("app.maintenance.os.write", side_effect=fail_second_audit_write):
            with self.assertRaisesRegex(OSError, "completion audit failure"):
                recover_abandoned_restore_lock(
                    "operator@example.com", "INC-129", confirm=True, audit_path=audit,
                )
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["event"] for record in records], ["RESTORE_LOCK_RECOVERY_STARTED"])
        self.assertFalse(lock_path.exists(), "marker is already removed after durable STARTED audit")
        self.assertTrue(all(record["database_changed"] is False for record in records))

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

    def test_recovery_inventory_skips_database_with_wal_sidecar_and_preserves_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid_backup = root / "taimobot-20261116-230000-sidecar.sqlite3"
            with sqlite3.connect(database.DB_PATH) as source, sqlite3.connect(valid_backup) as target:
                source.backup(target)
            sidecar = Path(str(valid_backup) + "-wal")
            sidecar.write_bytes(b"preserve-recovery-wal")
            result = inspect_recovery_candidates(root)
            self.assertEqual(result["candidate_count"], 1)
            candidate = result["candidates"][0]
            self.assertEqual(candidate["file_name"], valid_backup.name)
            self.assertFalse(candidate["restore_ready"])
            self.assertEqual(candidate["integrity_check"], "SKIPPED_SIDECARS_PRESENT")
            self.assertIn("SQLITE_SIDECARS_PRESENT", candidate["issues"])
            self.assertEqual(sidecar.read_bytes(), b"preserve-recovery-wal")
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
                        "sha256": sha256_file(selected),
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


    def test_restore_execution_state_flags_interrupted_start_without_auto_resume(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-STATE-001",
            details={"live_database_replaced": False}, journal_path=journal,
        )
        before = journal.read_bytes()
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["status"], "RESTORE_EXECUTION_REVIEW_REQUIRED")
        self.assertEqual(result["executions"][0]["classification"],
                         "INTERRUPTED_BEFORE_PUBLISH_REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(result["executions"][0]["evidence_status"], "INCOMPLETE_REQUIRES_OPERATOR_REVIEW")
        self.assertTrue(result["evidence_review"][0]["events"][0]["issues"])
        self.assertEqual(journal.read_bytes(), before)

    def test_restore_execution_state_keeps_recovery_required_terminal_for_operator_review(self):
        journal = Path(self.temp_dir.name) / "restore-recovery-required.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "c" * 32,
            "operator@example.com", "INC-RECOVERY-001", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_RECOVERY_REQUIRED", "a" * 32, "c" * 32,
            "operator@example.com", "INC-RECOVERY-001",
            details={"reason": "rollback evidence could not be verified"},
            journal_path=journal,
        )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_EXECUTION_REVIEW_REQUIRED")
        self.assertEqual(result["executions"][0]["classification"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertTrue(result["executions"][0]["terminal"])
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)

    def test_restore_execution_state_flags_published_but_unconfirmed_database(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "b" * 32, "c" * 32,
            "operator@example.com", "INC-STATE-002", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "b" * 32, "c" * 32,
            "operator@example.com", "INC-STATE-002", journal_path=journal,
        )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["executions"][0]["classification"],
                         "PUBLISHED_STATE_UNCONFIRMED_REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(result["status"], "RESTORE_EXECUTION_REVIEW_REQUIRED")

    def test_restore_execution_state_flags_incomplete_rollback(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "d" * 32, "e" * 32,
            "operator@example.com", "INC-STATE-003", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_ROLLBACK_STARTED", "d" * 32, "e" * 32,
            "operator@example.com", "INC-STATE-003", journal_path=journal,
        )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["executions"][0]["classification"],
                         "ROLLBACK_INCOMPLETE_REQUIRES_OPERATOR_REVIEW")

    def test_restore_execution_state_requires_database_reconciliation_after_terminal_event(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "f" * 32, "1" * 32,
            "operator@example.com", "INC-STATE-004", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "f" * 32, "1" * 32,
            "operator@example.com", "INC-STATE-004", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", "f" * 32, "1" * 32,
            "operator@example.com", "INC-STATE-004", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_COMPLETE", "f" * 32, "1" * 32,
            "operator@example.com", "INC-STATE-004", journal_path=journal,
        )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["executions"][0]["classification"],
                         "COMPLETED_REQUIRES_DATABASE_RECONCILIATION")
        self.assertEqual(result["status"], "RESTORE_EXECUTION_REVIEW_REQUIRED")
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_execution_state_detects_semantically_invalid_event_order(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "2" * 32, "3" * 32,
            "operator@example.com", "INC-STATE-005", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_ROLLBACK_VERIFIED", "2" * 32, "3" * 32,
            "operator@example.com", "INC-STATE-005", journal_path=journal,
        )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_execution_state_rejects_operator_or_incident_identity_changes(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "9" * 32, "8" * 32,
            "operator-one", "INC-IDENTITY-001", journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "9" * 32, "8" * 32,
            "operator-two", "INC-IDENTITY-001", journal_path=journal,
        )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn("identity changed", result["semantic_errors"][0]["reason"])
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])

    def test_restore_event_evidence_keeps_pre_and_post_publish_hashes_distinct(self):
        journal = Path(self.temp_dir.name) / "restore-evidence-states.audit.jsonl"
        initial = {
            "live_database_sha256": "1" * 64,
            "source_backup_sha256": "2" * 64,
            "staged_candidate_sha256": "3" * 64,
            "rollback_backup_sha256": "4" * 64,
        }
        published = {
            "staged_candidate_sha256": "3" * 64,
            "published_database_sha256": "5" * 64,
        }
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "7" * 32, "6" * 32,
            "operator@example.com", "INC-EVIDENCE-STATES",
            details={"artifacts": initial}, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "7" * 32, "6" * 32,
            "operator@example.com", "INC-EVIDENCE-STATES",
            details={"artifacts": published}, journal_path=journal,
        )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(result["executions"][0]["evidence_status"], "EVENT_EVIDENCE_PRESENT")
        self.assertEqual(result["executions"][0]["events_missing_evidence"], 0)
        self.assertEqual(result["evidence_review"], [])
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_execution_state_handles_multiple_independent_attempts(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        for plan, token, incident in (("4" * 32, "5" * 32, "INC-A"), ("6" * 32, "7" * 32, "INC-B")):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", plan, token,
                "operator@example.com", incident, journal_path=journal,
            )
        result = inspect_restore_execution_state(journal)
        self.assertEqual(len(result["executions"]), 2)
        self.assertTrue(all(item["classification"].startswith("INTERRUPTED") for item in result["executions"]))

    def test_restore_evidence_reconciliation_matches_all_recorded_artifacts(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live.sqlite3",
            "source_backup": root / "source.sqlite3",
            "staged_candidate": root / "staged.sqlite3",
            "rollback_backup": root / "rollback.sqlite3",
        }
        for name, path in paths.items():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "8" * 32, "9" * 32,
            "operator@example.com", "INC-EVIDENCE-001",
            details={"artifacts": hashes}, journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts(
            "8" * 32, "9" * 32, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_MATCHED")
        self.assertEqual(
            [item["fingerprint_status"] for item in result["artifacts"]],
            ["MATCH", "MATCH", "MATCH", "MATCH"],
        )
        self.assertTrue(all(item["integrity_check"] == "ok" for item in result["artifacts"]))
        self.assertFalse(result["database_state_verified"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_evidence_reconciliation_skips_any_artifact_with_sqlite_sidecars(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live-sidecar.sqlite3",
            "source_backup": root / "source-sidecar.sqlite3",
            "staged_candidate": root / "staged-sidecar.sqlite3",
            "rollback_backup": root / "rollback-sidecar.sqlite3",
        }
        for name, path in paths.items():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-sidecar.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "d" * 32, "e" * 32,
            "operator@example.com", "INC-EVIDENCE-SIDECAR",
            details={"artifacts": hashes}, journal_path=journal,
        )
        sidecar = Path(str(paths["source_backup"]) + "-wal")
        sidecar.write_bytes(b"preserve-this-sidecar")
        result = reconcile_restore_execution_artifacts(
            "d" * 32, "e" * 32, paths, journal_path=journal,
        )
        source_result = next(item for item in result["artifacts"] if item["artifact"] == "source_backup")
        self.assertEqual(source_result["fingerprint_status"], "MATCH")
        self.assertEqual(source_result["integrity_check"], "SKIPPED_SIDECARS_PRESENT")
        self.assertEqual(sidecar.read_bytes(), b"preserve-this-sidecar")
        self.assertEqual(
            result["artifact_sidecars"]["source_backup"]["status"],
            "SIDECARS_PRESENT_REQUIRES_LOCKED_HANDLING",
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertFalse(result["recoverability_verified"])
        self.assertFalse(result["database_state_verified"])
        self.assertEqual(result["evidence_scope"], "FILE_FINGERPRINTS_AND_SQLITE_INTEGRITY_ONLY")

    def test_restore_evidence_reconciliation_surfaces_incomplete_event_evidence(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live-incomplete.sqlite3",
            "source_backup": root / "source-incomplete.sqlite3",
            "staged_candidate": root / "staged-incomplete.sqlite3",
            "rollback_backup": root / "rollback-incomplete.sqlite3",
        }
        for name, path in paths.items():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-incomplete.audit.jsonl"
        plan_id, token = "3" * 32, "4" * 32
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-INCOMPLETE",
            details={"artifacts": hashes}, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-INCOMPLETE",
            details={"integrity_check": "ok", "foreign_key_violations": 0},
            journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts(
            plan_id, token, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertEqual(len(result["event_evidence_review"]), 1)
        self.assertEqual(result["event_evidence_review"][0]["event"], "RESTORE_POSTCHECK_PASSED")
        self.assertTrue(any("schema_check" in issue for issue in result["event_evidence_review"][0]["issues"]))
        self.assertTrue(any("installed_database_sha256" in issue for issue in result["event_evidence_review"][0]["issues"]))
        self.assertFalse(result["recoverability_verified"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_evidence_reconciliation_accepts_consistent_terminal_fingerprints(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live-consistent.sqlite3",
            "source_backup": root / "source-consistent.sqlite3",
            "staged_candidate": root / "staged-consistent.sqlite3",
            "rollback_backup": root / "rollback-consistent.sqlite3",
        }
        for name, path in paths.items():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        live_hash = hashes["live_database_sha256"]
        journal = root / "restore-consistent.audit.jsonl"
        plan_id, token = "1" * 32, "2" * 32
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-CONSISTENT",
            details={"artifacts": hashes}, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-CONSISTENT",
            details={
                "integrity_check": "ok",
                "foreign_key_violations": 0,
                "schema_check": "passed",
                "installed_database_sha256": live_hash,
            }, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_COMPLETE", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-CONSISTENT",
            details={"final_database_sha256": live_hash}, journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts(
            plan_id, token, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_MATCHED")
        self.assertEqual(result["lifecycle_consistency_issues"], [])
        self.assertEqual(result["terminal_event"], "RESTORE_EXECUTION_COMPLETE")
        self.assertFalse(result["recoverability_verified"])
        self.assertFalse(result["database_state_verified"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_evidence_reconciliation_flags_terminal_live_fingerprint_disagreement(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live-terminal.sqlite3",
            "source_backup": root / "source-terminal.sqlite3",
            "staged_candidate": root / "staged-terminal.sqlite3",
            "rollback_backup": root / "rollback-terminal.sqlite3",
        }
        for name, path in paths.items():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        live_hash = hashes["live_database_sha256"]
        journal = root / "restore-terminal.audit.jsonl"
        plan_id, token = "f" * 32, "e" * 32
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-TERMINAL",
            details={"artifacts": hashes}, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-TERMINAL",
            details={
                "integrity_check": "ok",
                "foreign_key_violations": 0,
                "schema_check": "passed",
                "installed_database_sha256": live_hash,
            }, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_COMPLETE", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-TERMINAL",
            details={"final_database_sha256": "0" * 64}, journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts(
            plan_id, token, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertEqual(result["terminal_event"], "RESTORE_EXECUTION_COMPLETE")
        self.assertTrue(any("postcheck and completion" in issue for issue in result["lifecycle_consistency_issues"]))
        self.assertTrue(any("current live database fingerprint" in issue for issue in result["lifecycle_consistency_issues"]))
        self.assertFalse(result["recoverability_verified"])
        self.assertFalse(result["database_state_verified"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_evidence_reconciliation_detects_path_replacement_during_integrity_check(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live-race.sqlite3",
            "source_backup": root / "source-race.sqlite3",
            "staged_candidate": root / "staged-race.sqlite3",
            "rollback_backup": root / "rollback-race.sqlite3",
        }
        for name, path in paths.items():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-race.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-EVIDENCE-RACE",
            details={"artifacts": hashes}, journal_path=journal,
        )
        replacement = root / "replacement.sqlite3"
        with sqlite3.connect(replacement) as connection:
            connection.execute("CREATE TABLE replacement_probe (value TEXT)")
        real_connect = sqlite3.connect
        replaced = {"done": False}

        def replace_before_sqlite_open(database_uri, *args, **kwargs):
            if str(paths["source_backup"].absolute()) in str(database_uri) and not replaced["done"]:
                os.replace(replacement, paths["source_backup"])
                replaced["done"] = True
            return real_connect(database_uri, *args, **kwargs)

        with patch("app.maintenance.sqlite3.connect", side_effect=replace_before_sqlite_open):
            result = reconcile_restore_execution_artifacts(
                "a" * 32, "b" * 32, paths, journal_path=journal,
            )
        self.assertTrue(replaced["done"])
        source_result = next(item for item in result["artifacts"] if item["artifact"] == "source_backup")
        self.assertEqual(source_result["fingerprint_status"], "UNSAFE_OR_MISSING")
        self.assertIn("changed", source_result["error"])
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertFalse(result["database_state_verified"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_restore_evidence_reconciliation_detects_changed_artifact(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live.sqlite3",
            "source_backup": root / "source.sqlite3",
            "staged_candidate": root / "staged.sqlite3",
            "rollback_backup": root / "rollback.sqlite3",
        }
        for path in paths.values():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-EVIDENCE-002",
            details={"artifacts": hashes}, journal_path=journal,
        )
        with sqlite3.connect(paths["source_backup"]) as connection:
            connection.execute("INSERT INTO evidence_probe VALUES ('changed')")
        result = reconcile_restore_execution_artifacts(
            "a" * 32, "b" * 32, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        statuses = {item["artifact"]: item["fingerprint_status"] for item in result["artifacts"]}
        self.assertEqual(statuses["source_backup"], "MISMATCH")
        self.assertEqual(statuses["live_database"], "MATCH")

    def test_restore_evidence_reconciliation_refuses_unrecorded_fingerprints(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live.sqlite3",
            "source_backup": root / "source.sqlite3",
            "staged_candidate": root / "staged.sqlite3",
            "rollback_backup": root / "rollback.sqlite3",
        }
        for path in paths.values():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
        journal = root / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "c" * 32, "d" * 32,
            "operator@example.com", "INC-EVIDENCE-003", journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts(
            "c" * 32, "d" * 32, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertTrue(all(item["fingerprint_status"] == "NOT_RECORDED" for item in result["artifacts"]))

    def test_restore_evidence_reconciliation_rejects_conflicting_journal_fingerprints(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live.sqlite3",
            "source_backup": root / "source.sqlite3",
            "staged_candidate": root / "staged.sqlite3",
            "rollback_backup": root / "rollback.sqlite3",
        }
        for path in paths.values():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "e" * 32, "f" * 32,
            "operator@example.com", "INC-EVIDENCE-004",
            details={"artifacts": hashes}, journal_path=journal,
        )
        conflicting = dict(hashes)
        conflicting["source_backup_sha256"] = "0" * 64
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "e" * 32, "f" * 32,
            "operator@example.com", "INC-EVIDENCE-004",
            details={"artifacts": conflicting}, journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts(
            "e" * 32, "f" * 32, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertEqual(result["conflicting_fingerprints"], ["source_backup"])
        source = next(item for item in result["artifacts"] if item["artifact"] == "source_backup")
        self.assertEqual(source["fingerprint_status"], "CONFLICTING_JOURNAL_EVIDENCE")

    def test_restore_evidence_reconciliation_refuses_symlinked_artifact(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live.sqlite3",
            "source_backup": root / "source.sqlite3",
            "staged_candidate": root / "staged.sqlite3",
            "rollback_backup": root / "rollback.sqlite3",
        }
        for path in paths.values():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "1" * 32, "2" * 32,
            "operator@example.com", "INC-EVIDENCE-005",
            details={"artifacts": hashes}, journal_path=journal,
        )
        target = root / "source-target.sqlite3"
        paths["source_backup"].rename(target)
        paths["source_backup"].symlink_to(target)
        result = reconcile_restore_execution_artifacts(
            "1" * 32, "2" * 32, paths, journal_path=journal,
        )
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        source = next(item for item in result["artifacts"] if item["artifact"] == "source_backup")
        self.assertEqual(source["fingerprint_status"], "UNSAFE_OR_MISSING")

    def test_restore_evidence_reconciliation_does_not_open_live_db_with_wal_sidecar(self):
        root = Path(self.temp_dir.name)
        paths = {
            "live_database": root / "live.sqlite3",
            "source_backup": root / "source.sqlite3",
            "staged_candidate": root / "staged.sqlite3",
            "rollback_backup": root / "rollback.sqlite3",
        }
        for path in paths.values():
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
        hashes = {f"{name}_sha256": sha256_file(path) for name, path in paths.items()}
        journal = root / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "3" * 32, "4" * 32,
            "operator@example.com", "INC-EVIDENCE-006",
            details={"artifacts": hashes}, journal_path=journal,
        )
        Path(str(paths["live_database"]) + "-wal").write_bytes(b"preserve-me")
        result = reconcile_restore_execution_artifacts(
            "3" * 32, "4" * 32, paths, journal_path=journal,
        )
        live = next(item for item in result["artifacts"] if item["artifact"] == "live_database")
        self.assertEqual(live["integrity_check"], "SKIPPED_SIDECARS_PRESENT")
        self.assertEqual(Path(str(paths["live_database"]) + "-wal").read_bytes(), b"preserve-me")
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")

    def test_restore_execution_journal_rejects_oversized_or_non_json_details_without_creating_file(self):
        journal = Path(self.temp_dir.name) / "restore-invalid-details.audit.jsonl"
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                "operator@example.com", "INC-INVALID-DETAILS",
                details={"unsupported": object()}, journal_path=journal,
            )
        with self.assertRaisesRegex(ValueError, "JSON serializable"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                "operator@example.com", "INC-INVALID-DETAILS",
                details={"non_finite": float("nan")}, journal_path=journal,
            )
        with self.assertRaisesRegex(ValueError, "8 KiB limit"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                "operator@example.com", "INC-INVALID-DETAILS",
                details={"payload": "x" * 8192}, journal_path=journal,
            )
        self.assertFalse(journal.exists())

    def test_restore_execution_journal_rejects_nonstandard_json_constants(self):
        journal = Path(self.temp_dir.name) / "restore-nonstandard-constant.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-NONSTANDARD-JSON",
            details={"note": "safe"}, journal_path=journal,
        )
        raw = journal.read_bytes().replace(b'"safe"', b'NaN')
        journal.write_bytes(raw)
        verified = verify_restore_execution_journal(journal)
        self.assertEqual(verified["status"], "RESTORE_JOURNAL_INVALID")
        self.assertIn("non-standard JSON constant", verified["reason"])

    def test_restore_execution_journal_verifier_rejects_invalid_operator_and_incident_fields(self):
        for field, invalid_value in (
            ("operator_id", "o" * 201),
            ("operator_id", "operator\nforged"),
            ("incident_ref", "i" * 201),
            ("incident_ref", "INC-1\x00forged"),
            ("timestamp", "not-a-timestamp"),
            ("timestamp", "2026-11-18T12:00:00"),
        ):
            with self.subTest(field=field, invalid_value=repr(invalid_value)):
                journal = Path(self.temp_dir.name) / f"invalid-{field}-{len(invalid_value)}.audit.jsonl"
                append_restore_execution_event(
                    "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                    "operator@example.com", "INC-VALID",
                    journal_path=journal,
                )
                record = json.loads(journal.read_text(encoding="utf-8"))
                record[field] = invalid_value
                record["record_sha256"] = _restore_journal_digest(record)
                journal.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                verified = verify_restore_execution_journal(journal)
                self.assertEqual(verified["status"], "RESTORE_JOURNAL_INVALID")
                expected_reason = (
                    "invalid restore journal timestamp"
                    if field == "timestamp" and invalid_value == "not-a-timestamp"
                    else "restore journal timestamp must include a timezone offset"
                    if field == "timestamp"
                    else "invalid restore journal record schema"
                )
                self.assertEqual(verified["reason"], expected_reason)

    def test_restore_execution_journal_rejects_duplicate_json_keys(self):
        journal = Path(self.temp_dir.name) / "restore-duplicate-key.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-DUPLICATE-KEY",
            journal_path=journal,
        )
        record = json.loads(journal.read_text(encoding="utf-8"))
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        raw = canonical.replace(
            '"event":"RESTORE_EXECUTION_STARTED"',
            '"event":"RESTORE_EXECUTION_STARTED","event":"RESTORE_EXECUTION_STARTED"',
            1,
        )
        journal.write_text(raw + chr(10), encoding="utf-8")
        verified = verify_restore_execution_journal(journal)
        self.assertEqual(verified["status"], "RESTORE_JOURNAL_INVALID")
        self.assertIn("duplicate JSON object key", verified["reason"])

    def test_restore_execution_journal_rejects_duplicate_nested_json_keys(self):
        journal = Path(self.temp_dir.name) / "restore-duplicate-nested-key.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-DUPLICATE-NESTED-KEY",
            details={"artifacts": {"source_backup_sha256": "1" * 64}},
            journal_path=journal,
        )
        record = json.loads(journal.read_text(encoding="utf-8"))
        canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
        raw = canonical.replace(
            '"source_backup_sha256":"' + ("1" * 64) + '"',
            '"source_backup_sha256":"' + ("1" * 64)
            + '","source_backup_sha256":"' + ("1" * 64) + '"',
            1,
        )
        journal.write_text(raw + chr(10), encoding="utf-8")

        verified = verify_restore_execution_journal(journal)

        self.assertEqual(verified["status"], "RESTORE_JOURNAL_INVALID")
        self.assertIn("duplicate JSON object key", verified["reason"])

    def test_restore_execution_journal_appends_and_verifies_hash_chain(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        first = append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-RESTORE-001",
            details={"trading_mode": "paper", "live_database_replaced": False},
            journal_path=journal,
        )
        second = append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-RESTORE-001",
            details={"integrity_check": "ok"},
            journal_path=journal,
        )
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        verified = verify_restore_execution_journal(journal)
        self.assertEqual(verified["status"], "RESTORE_JOURNAL_VERIFIED")
        self.assertEqual(verified["record_count"], 2)
        self.assertEqual(verified["last_record_sha256"], second["record_sha256"])

    def test_restore_execution_journal_fsync_failure_is_treated_as_ambiguous(self):
        journal = Path(self.temp_dir.name) / "restore-fsync-failure.audit.jsonl"
        with patch("app.maintenance.os.fsync", side_effect=OSError("simulated journal fsync failure")):
            with self.assertRaisesRegex(OSError, "fsync failure"):
                append_restore_execution_event(
                    "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                    "operator@example.com", "INC-FSYNC-001", journal_path=journal,
                )

        # A complete record may be visible even though durable storage was not
        # confirmed. Treat it as an interrupted execution; never auto-resume.
        verification = verify_restore_execution_journal(journal)
        self.assertEqual(verification["status"], "RESTORE_JOURNAL_VERIFIED")
        state = inspect_restore_execution_state(journal)
        self.assertEqual(state["status"], "RESTORE_EXECUTION_REVIEW_REQUIRED")
        self.assertEqual(state["executions"][0]["classification"],
                         "INTERRUPTED_BEFORE_PUBLISH_REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(state["automatic_action"], "NONE")
        self.assertFalse(state["live_database_replaced"])

    def test_restore_execution_journal_partial_write_fails_closed(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        original_write = os.write
        interrupted = {"done": False}

        def partial_then_fail(descriptor, payload):
            if not interrupted["done"]:
                interrupted["done"] = True
                original_write(descriptor, payload[:24])
                raise OSError("simulated process interruption during journal append")
            return original_write(descriptor, payload)

        with patch("app.maintenance.os.write", side_effect=partial_then_fail):
            with self.assertRaisesRegex(OSError, "simulated process interruption"):
                append_restore_execution_event(
                    "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                    "operator@example.com", "INC-PARTIAL-WRITE",
                    journal_path=journal,
                )

        verification = verify_restore_execution_journal(journal)
        self.assertEqual(verification["status"], "RESTORE_JOURNAL_INVALID")
        self.assertEqual(verification["reason"], "incomplete final record")
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_COMPLETE", "a" * 32, "b" * 32,
                "operator@example.com", "INC-PARTIAL-WRITE",
                journal_path=journal,
            )

    def test_restore_execution_journal_detects_tampering_and_refuses_append(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "c" * 32, "d" * 32,
            "operator@example.com", "INC-RESTORE-002",
            details={"stage": "before"},
            journal_path=journal,
        )
        content = journal.read_text(encoding="utf-8")
        journal.write_text(content.replace("before", "after"), encoding="utf-8")
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_INVALID",
        )
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_COMPLETE", "c" * 32, "d" * 32,
                "operator@example.com", "INC-RESTORE-002",
                journal_path=journal,
            )

    def test_restore_execution_journal_rejects_symlinks_and_invalid_events(self):
        target = Path(self.temp_dir.name) / "real-journal.jsonl"
        link = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        target.write_text("", encoding="utf-8")
        link.symlink_to(target)
        with self.assertRaises(OSError):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "e" * 32, "f" * 32,
                "operator@example.com", "INC-RESTORE-003",
                journal_path=link,
            )
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_UNKNOWN", "e" * 32, "f" * 32,
                "operator@example.com", "INC-RESTORE-003",
                journal_path=target,
            )

    def test_restore_execution_journal_rejects_hash_valid_record_with_invalid_schema(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        record = {
            "sequence": 1,
            "timestamp": "2026-11-18T09:00:00+02:00",
            "event": "RESTORE_EXECUTION_STARTED",
            "execution_token": "a" * 32,
            "operator_id": "operator@example.com",
            "incident_ref": "INC-SCHEMA-001",
            "details": {},
            "previous_record_sha256": "0" * 64,
        }
        record["record_sha256"] = _restore_journal_digest(record)
        journal.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        result = verify_restore_execution_journal(journal)
        self.assertEqual(result["status"], "RESTORE_JOURNAL_INVALID")
        self.assertIn("schema", result["reason"])

    def test_restore_execution_journal_rejects_boolean_sequence(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        record = {
            "sequence": True,
            "timestamp": "2026-11-18T09:00:00+02:00",
            "event": "RESTORE_EXECUTION_STARTED",
            "plan_id": "b" * 32,
            "execution_token": "c" * 32,
            "operator_id": "operator@example.com",
            "incident_ref": "INC-SCHEMA-002",
            "details": {},
            "previous_record_sha256": "0" * 64,
        }
        record["record_sha256"] = _restore_journal_digest(record)
        journal.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        result = verify_restore_execution_journal(journal)
        self.assertEqual(result["status"], "RESTORE_JOURNAL_INVALID")
        self.assertIn("sequence", result["reason"])

    def test_restore_execution_journal_rejects_incomplete_final_record(self):
        journal = Path(self.temp_dir.name) / "restore-execution.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "1" * 32, "2" * 32,
            "operator@example.com", "INC-RESTORE-004",
            journal_path=journal,
        )
        with journal.open("ab") as stream:
            stream.write(b'{"sequence":2')
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_INVALID",
        )

    def test_restore_lock_fsyncs_parent_directory_before_reporting_acquired(self):
        real_fsync = os.fsync
        observed = {"directory_synced": False}
        def inspect_fsync(fd):
            if maintenance_module.stat.S_ISDIR(os.fstat(fd).st_mode):
                observed["directory_synced"] = True
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=inspect_fsync):
            lock = acquire_restore_lock("1" * 32)
        self.assertTrue(observed["directory_synced"])
        self.assertTrue(Path(lock["lock_file"]).is_file())
        release_restore_lock(lock["lock_token"])

    def test_restore_lock_directory_publish_sync_failure_removes_exact_marker(self):
        real_fsync = os.fsync
        calls = {"count": 0}
        def fail_publish_sync(fd):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("publish directory sync failed")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_publish_sync):
            with self.assertRaisesRegex(OSError, "publish directory sync failed"):
                acquire_restore_lock("2" * 32)
        self.assertGreaterEqual(calls["count"], 3)
        self.assertFalse(maintenance_module.restore_lock_path().exists())
        self.assertFalse(is_restore_lock_active())

    def test_restore_lock_marker_file_sync_failure_never_reports_success(self):
        real_fsync = os.fsync
        calls = {"count": 0}
        def fail_marker_sync(fd):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("marker file sync failed")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_marker_sync):
            with self.assertRaisesRegex(OSError, "marker file sync failed"):
                acquire_restore_lock("3" * 32)
        self.assertFalse(maintenance_module.restore_lock_path().exists())
        self.assertFalse(is_restore_lock_active())

    def test_restore_lock_preexisting_marker_is_never_overwritten(self):
        marker = maintenance_module.restore_lock_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"plan_id": "4" * 32, "lock_token": "5" * 32, "pid": 1, "created_at": "2026-11-20T00:00:00+02:00"}).encode()
        marker.write_bytes(original)
        with self.assertRaises(FileExistsError):
            acquire_restore_lock("6" * 32)
        self.assertEqual(marker.read_bytes(), original)

    def test_restore_lock_marker_symlink_collision_preserves_target(self):
        marker = maintenance_module.restore_lock_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        target = Path(self.temp_dir.name) / "marker-target"
        target.write_bytes(b"untouched")
        marker.symlink_to(target)
        with self.assertRaises(FileExistsError):
            acquire_restore_lock("7" * 32)
        self.assertEqual(target.read_bytes(), b"untouched")
        self.assertTrue(marker.is_symlink())

    def test_restore_lock_process_guard_is_held_during_publish_directory_sync(self):
        real_fsync = os.fsync
        checked = {"blocked": False}
        def check_guard(fd):
            if maintenance_module.stat.S_ISDIR(os.fstat(fd).st_mode):
                with self.assertRaisesRegex(RuntimeError, "connections remain active"):
                    acquire_restore_lock("8" * 32, timeout_seconds=0)
                checked["blocked"] = True
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=check_guard):
            lock = acquire_restore_lock("9" * 32)
        self.assertTrue(checked["blocked"])
        release_restore_lock(lock["lock_token"])

    def test_restore_lock_failed_publication_does_not_change_database(self):
        before = database.DB_PATH.read_bytes()
        real_fsync = os.fsync
        calls = {"count": 0}
        def fail_directory(fd):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("directory persistence unavailable")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_directory):
            with self.assertRaises(OSError):
                acquire_restore_lock("a" * 32)
        self.assertEqual(database.DB_PATH.read_bytes(), before)

    def test_restore_lock_failed_publication_preserves_sqlite_sidecars(self):
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"wal-evidence")
        shm.write_bytes(b"shm-evidence")
        real_fsync = os.fsync
        calls = {"count": 0}
        def fail_directory(fd):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("directory persistence unavailable")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_directory):
            with self.assertRaises(OSError):
                acquire_restore_lock("b" * 32)
        self.assertEqual(wal.read_bytes(), b"wal-evidence")
        self.assertEqual(shm.read_bytes(), b"shm-evidence")

    def test_restore_lock_failed_publication_releases_guard_for_new_attempt(self):
        real_fsync = os.fsync
        calls = {"count": 0}
        def fail_directory(fd):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("directory persistence unavailable")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_directory):
            with self.assertRaises(OSError):
                acquire_restore_lock("c" * 32)
        self.assertFalse(is_restore_lock_active())
        next_lock = acquire_restore_lock("d" * 32)
        self.assertTrue(is_restore_lock_active())
        release_restore_lock(next_lock["lock_token"])

    def test_restore_lock_publish_directory_sync_precedes_sidecar_inventory(self):
        real_inspect = maintenance_module.inspect_sqlite_sidecars
        observed = {"synced": False}
        real_fsync = os.fsync
        def track_fsync(fd):
            if maintenance_module.stat.S_ISDIR(os.fstat(fd).st_mode):
                observed["synced"] = True
            return real_fsync(fd)
        def inspect_after_sync(path):
            self.assertTrue(observed["synced"])
            return real_inspect(path)
        with patch("app.maintenance.os.fsync", side_effect=track_fsync):
            with patch("app.maintenance.inspect_sqlite_sidecars", side_effect=inspect_after_sync):
                lock = acquire_restore_lock("e" * 32)
        release_restore_lock(lock["lock_token"])

    def test_restore_lock_preserves_wal_and_shm_and_does_not_claim_external_quiescence(self):
        external = sqlite3.connect(database.DB_PATH, timeout=5)
        try:
            external.execute("PRAGMA journal_mode=WAL")
            external.execute("PRAGMA wal_autocheckpoint=0")
            external.execute("CREATE TABLE IF NOT EXISTS external_writer_probe (value TEXT)")
            external.execute("INSERT INTO external_writer_probe VALUES ('keep-wal')")
            external.commit()
            wal_path = Path(str(database.DB_PATH) + "-wal")
            shm_path = Path(str(database.DB_PATH) + "-shm")
            self.assertTrue(wal_path.exists())
            self.assertTrue(shm_path.exists())
            wal_before = wal_path.read_bytes()

            lock = acquire_restore_lock("9" * 32)
            try:
                self.assertFalse(lock["database_quiescent"])
                self.assertTrue(lock["application_connections_quiescent"])
                self.assertFalse(lock["external_process_quiescence_verified"])
                self.assertEqual(lock["sqlite_sidecars"]["status"], "SIDECARS_PRESENT_REQUIRES_LOCKED_HANDLING")
                self.assertTrue(wal_path.exists())
                self.assertTrue(shm_path.exists())
                self.assertEqual(wal_path.read_bytes(), wal_before)
            finally:
                release_restore_lock(lock["lock_token"])
        finally:
            external.close()

    def test_restore_lock_quiesces_application_connections_and_requires_owner_token(self):
        from app.database import get_connection

        plan_id = "d" * 32
        lock = acquire_restore_lock(plan_id)
        self.assertEqual(lock["status"], "RESTORE_LOCK_ACQUIRED")
        self.assertTrue(lock["application_connections_quiescent"])
        self.assertFalse(lock["external_process_quiescence_verified"])
        self.assertFalse(lock["database_quiescent"])
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


    def test_restore_lock_guard_remains_held_until_marker_directory_is_synced(self):
        lock = acquire_restore_lock("c" * 32)
        real_fsync = __import__("os").fsync
        checked = {"during_sync": False}

        def check_guard_before_sync(descriptor):
            with self.assertRaisesRegex(RuntimeError, "connections remain active"):
                acquire_restore_lock("d" * 32, timeout_seconds=0)
            checked["during_sync"] = True
            return real_fsync(descriptor)

        with patch("app.maintenance.os.fsync", side_effect=check_guard_before_sync):
            released = release_restore_lock(lock["lock_token"])

        self.assertTrue(checked["during_sync"])
        self.assertTrue(released["marker_removed"])
        self.assertTrue(released["directory_synced"])
        self.assertFalse(is_restore_lock_active())
        self.assertFalse(Path(lock["lock_file"]).exists())


    def test_restore_lock_directory_sync_failure_keeps_process_guard_held(self):
        import app.maintenance as maintenance_module

        lock = acquire_restore_lock("e" * 32)
        with patch("app.maintenance.os.fsync", side_effect=OSError("simulated directory sync failure")):
            with self.assertRaisesRegex(RuntimeError, "marker removal was not confirmed durable"):
                release_restore_lock(lock["lock_token"])

        self.assertFalse(Path(lock["lock_file"]).exists())
        with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
            database.get_connection()

        # Test-only cleanup: emulate process teardown after the fail-closed
        # condition has been verified. Production recovery remains operator-led.
        guard_fd = maintenance_module._RESTORE_GUARD_FDS.pop(lock["lock_token"])
        os.close(guard_fd)


    def test_restore_lock_unlink_failure_keeps_guard_and_allows_safe_retry(self):
        lock = acquire_restore_lock("f" * 32)
        with patch("app.maintenance.os.unlink", side_effect=OSError("simulated marker unlink failure")):
            with self.assertRaisesRegex(RuntimeError, "marker remains active"):
                release_restore_lock(lock["lock_token"])

        self.assertTrue(Path(lock["lock_file"]).is_file())
        self.assertTrue(is_restore_lock_active())
        with self.assertRaisesRegex(sqlite3.OperationalError, "controlled restore"):
            database.get_connection()

        released = release_restore_lock(lock["lock_token"])
        self.assertEqual(released["status"], "RESTORE_LOCK_RELEASED")
        self.assertFalse(Path(lock["lock_file"]).exists())
        self.assertFalse(is_restore_lock_active())


    def test_restore_lock_marker_close_failure_keeps_process_guard_held(self):
        import app.maintenance as maintenance_module

        lock = acquire_restore_lock("a" * 32)
        real_close = os.close
        failed_fd = {"value": None}
        successfully_closed = []

        def fail_marker_close(descriptor):
            if descriptor != maintenance_module._RESTORE_GUARD_FDS[lock["lock_token"]] and failed_fd["value"] is None:
                failed_fd["value"] = descriptor
                raise OSError("simulated marker descriptor close failure")
            result = real_close(descriptor)
            successfully_closed.append(descriptor)
            return result

        with patch("app.maintenance.os.close", side_effect=fail_marker_close):
            with self.assertRaisesRegex(RuntimeError, "inspection descriptors safely"):
                release_restore_lock(lock["lock_token"])

        self.assertIsNotNone(failed_fd["value"])
        self.assertEqual(len(successfully_closed), 1)  # directory descriptor still closes
        with self.assertRaises(OSError):
            os.fstat(successfully_closed[0])
        self.assertFalse(Path(lock["lock_file"]).exists())
        self.assertIn(lock["lock_token"], maintenance_module._RESTORE_GUARD_FDS)
        with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
            database.get_connection()

        # Test-only cleanup: close the descriptor whose close was fault-injected.
        real_close(failed_fd["value"])
        guard_fd = maintenance_module._RESTORE_GUARD_FDS.pop(lock["lock_token"], None)
        if guard_fd is not None:
            real_close(guard_fd)


    def test_restore_lock_guard_close_failure_retains_ownership_and_blocks_database(self):
        import app.maintenance as maintenance_module

        lock = acquire_restore_lock("b" * 32)
        guard_fd = maintenance_module._RESTORE_GUARD_FDS[lock["lock_token"]]
        real_close = os.close

        def fail_guard_close(descriptor):
            if descriptor == guard_fd:
                raise OSError("simulated process guard close failure")
            return real_close(descriptor)

        with patch("app.maintenance.os.close", side_effect=fail_guard_close):
            with self.assertRaisesRegex(RuntimeError, "process guard close failed"):
                release_restore_lock(lock["lock_token"])

        self.assertFalse(Path(lock["lock_file"]).exists())
        self.assertIn(lock["lock_token"], maintenance_module._RESTORE_GUARD_FDS)
        with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
            database.get_connection()

        # Test-only cleanup: emulate process teardown after fail-closed state.
        maintenance_module._RESTORE_GUARD_FDS.pop(lock["lock_token"])
        real_close(guard_fd)


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

    def test_restore_lock_rejects_symlinked_guard_without_touching_target(self):
        guard_path = database.restore_guard_path()
        guard_path.parent.mkdir(parents=True, exist_ok=True)
        if guard_path.exists() or guard_path.is_symlink():
            guard_path.unlink()
        target = Path(self.temp_dir.name) / "unrelated-guard-target"
        target.write_bytes(b"do-not-modify")
        guard_path.symlink_to(target)
        with self.assertRaises(OSError):
            acquire_restore_lock("a" * 32)
        self.assertEqual(target.read_bytes(), b"do-not-modify")
        self.assertTrue(guard_path.is_symlink())
        marker_path = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        self.assertFalse(marker_path.exists())

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

    def test_restore_execution_preflight_rejects_incomplete_live_taimobot_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            live_path = root / "live.sqlite3"
            for target in (source, rollback, staged, live_path):
                with sqlite3.connect(database.DB_PATH) as original, sqlite3.connect(target) as copy:
                    original.backup(copy)
            with sqlite3.connect(live_path) as live:
                live.execute("DROP TABLE trades")
            with self.assertRaisesRegex(ValueError, "missing required TaimoBot schema.*trades"):
                verify_restore_execution_readiness(
                    source, staged, rollback,
                    backup_dir=backup_dir,
                    live_database_path=live_path,
                )
            with sqlite3.connect(live_path) as live:
                tables = {
                    row[0] for row in live.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertNotIn("trades", tables)

    def test_locked_restore_preflight_requires_external_process_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            for target in (source, rollback, staged):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)
            plan_id = "a" * 32
            lock = acquire_restore_lock(plan_id)
            try:
                with self.assertRaisesRegex(ValueError, "external SQLite processes"):
                    verify_restore_execution_readiness_under_lock(
                        source, staged, rollback,
                        plan_id=plan_id,
                        lock_token=lock["lock_token"],
                        external_processes_stopped=False,
                        backup_dir=backup_dir,
                        live_database_path=database.DB_PATH,
                    )
            finally:
                release_restore_lock(lock["lock_token"])

    def test_locked_restore_preflight_rechecks_artifacts_under_matching_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            for target in (source, rollback, staged):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)
            plan_id = "b" * 32
            lock = acquire_restore_lock(plan_id)
            try:
                result = verify_restore_execution_readiness_under_lock(
                    source, staged, rollback,
                    plan_id=plan_id,
                    lock_token=lock["lock_token"],
                    external_processes_stopped=True,
                    expected_artifact_hashes={
                        "source_sha256": sha256_file(source),
                        "staged_sha256": sha256_file(staged),
                        "rollback_sha256": sha256_file(rollback),
                    },
                    backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
                self.assertEqual(result["status"], "RESTORE_EXECUTION_LOCKED_PREFLIGHT_PASSED")
                self.assertTrue(result["restore_lock_verified"])
                self.assertTrue(result["external_processes_stopped_operator_confirmed"])
                self.assertFalse(result["execution_authorized"])
                self.assertFalse(result["live_database_replaced"])
            finally:
                release_restore_lock(lock["lock_token"])

    def test_locked_restore_preflight_rejects_wrong_lock_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            for target in (source, rollback, staged):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)
            plan_id = "c" * 32
            lock = acquire_restore_lock(plan_id)
            try:
                with self.assertRaisesRegex(RuntimeError, "matching active restore lock"):
                    verify_restore_execution_readiness_under_lock(
                        source, staged, rollback,
                        plan_id=plan_id,
                        lock_token="d" * 32,
                        external_processes_stopped=True,
                        expected_artifact_hashes={
                            "source_sha256": sha256_file(source),
                            "staged_sha256": sha256_file(staged),
                            "rollback_sha256": sha256_file(rollback),
                        },
                        backup_dir=backup_dir,
                        live_database_path=database.DB_PATH,
                    )
            finally:
                release_restore_lock(lock["lock_token"])

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
                with self.assertRaisesRegex(ValueError, "WAL/SHM sidecars"):
                    verify_restore_execution_readiness(
                        source, staged, rollback,
                        backup_dir=backup_dir,
                        live_database_path=database.DB_PATH,
                    )
                self.assertTrue(wal.exists())
                self.assertEqual(wal.read_bytes(), b"")
            finally:
                wal.unlink(missing_ok=True)


    def test_restore_execution_inspector_rejects_skipped_lifecycle_stages_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-skipped-stage.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-SKIPPED-STAGE",
            details={"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_COMPLETE", "a" * 32, "b" * 32,
            "operator@example.com", "INC-SKIPPED-STAGE",
            details={"final_database_sha256": "5" * 64},
            journal_path=journal,
        )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn(
            "invalid event transition RESTORE_EXECUTION_STARTED -> RESTORE_EXECUTION_COMPLETE",
            result["semantic_errors"][0]["reason"],
        )
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_execution_inspector_rejects_operator_identity_change_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-identity-change.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-IDENTITY-CHANGE",
            details={"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "a" * 32, "b" * 32,
            "different-operator@example.com", "INC-IDENTITY-CHANGE",
            details={"artifacts": {
                "staged_candidate_sha256": "3" * 64,
                "published_database_sha256": "5" * 64,
            }},
            journal_path=journal,
        )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn("operator or incident identity changed",
                      result["semantic_errors"][0]["reason"])
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)


    def test_restore_execution_inspector_rejects_timestamp_regression_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-timestamp-regression.audit.jsonl"
        zone = ZoneInfo("Africa/Johannesburg")
        timestamps = iter([
            datetime(2026, 11, 18, 12, 1, tzinfo=zone),
            datetime(2026, 11, 18, 12, 0, tzinfo=zone),
        ])

        class ControlledDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = next(timestamps)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

        with patch("app.maintenance.datetime", ControlledDateTime):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "c" * 32, "d" * 32,
                "operator@example.com", "INC-TIMESTAMP-REGRESSION",
                details={"artifacts": {
                    "live_database_sha256": "1" * 64,
                    "source_backup_sha256": "2" * 64,
                    "staged_candidate_sha256": "3" * 64,
                    "rollback_backup_sha256": "4" * 64,
                }},
                journal_path=journal,
            )
            append_restore_execution_event(
                "RESTORE_SOURCE_PUBLISHED", "c" * 32, "d" * 32,
                "operator@example.com", "INC-TIMESTAMP-REGRESSION",
                details={"artifacts": {
                    "staged_candidate_sha256": "3" * 64,
                    "published_database_sha256": "5" * 64,
                }},
                journal_path=journal,
            )

        before = journal.read_bytes()
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn(
            "invalid or non-monotonic execution timestamp",
            result["semantic_errors"][0]["reason"],
        )
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_execution_inspector_rejects_reused_token_across_plans_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-reused-token.audit.jsonl"
        for plan_id, digest_start in (("e" * 32, "1"), ("f" * 32, "5")):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", plan_id, "a" * 32,
                "operator@example.com", "INC-REUSED-TOKEN",
                details={"artifacts": {
                    "live_database_sha256": digest_start * 64,
                    "source_backup_sha256": "2" * 64,
                    "staged_candidate_sha256": "3" * 64,
                    "rollback_backup_sha256": "4" * 64,
                }},
                journal_path=journal,
            )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertTrue(any(
            "execution token reused across different restore plans" in item["reason"]
            for item in result["semantic_errors"]
        ))
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)


    def test_restore_execution_inspector_rejects_events_after_terminal_state_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-after-terminal.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "1" * 32, "2" * 32,
            "operator@example.com", "INC-AFTER-TERMINAL",
            details={"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_RECOVERY_REQUIRED", "1" * 32, "2" * 32,
            "operator@example.com", "INC-AFTER-TERMINAL",
            details={"reason": "Operator review required."},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "1" * 32, "2" * 32,
            "operator@example.com", "INC-AFTER-TERMINAL",
            details={"artifacts": {
                "staged_candidate_sha256": "3" * 64,
                "published_database_sha256": "5" * 64,
            }},
            journal_path=journal,
        )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn(
            "invalid event transition RESTORE_EXECUTION_RECOVERY_REQUIRED -> RESTORE_SOURCE_PUBLISHED",
            result["semantic_errors"][0]["reason"],
        )
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_execution_inspector_rejects_duplicate_started_event_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-duplicate-started.audit.jsonl"
        details = {"artifacts": {
            "live_database_sha256": "1" * 64,
            "source_backup_sha256": "2" * 64,
            "staged_candidate_sha256": "3" * 64,
            "rollback_backup_sha256": "4" * 64,
        }}
        for _ in range(2):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "3" * 32, "4" * 32,
                "operator@example.com", "INC-DUPLICATE-STARTED",
                details=details,
                journal_path=journal,
            )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn(
            "invalid event transition RESTORE_EXECUTION_STARTED -> RESTORE_EXECUTION_STARTED",
            result["semantic_errors"][0]["reason"],
        )
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_execution_inspector_accepts_complete_rollback_lifecycle_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-valid-rollback.audit.jsonl"
        plan_id, token = "5" * 32, "6" * 32
        operator, incident = "operator@example.com", "INC-VALID-ROLLBACK"
        events = [
            ("RESTORE_EXECUTION_STARTED", {"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }}),
            ("RESTORE_ROLLBACK_STARTED", {"artifacts": {
                "rollback_backup_sha256": "4" * 64,
            }}),
            ("RESTORE_ROLLBACK_VERIFIED", {
                "integrity_check": "ok",
                "foreign_key_violations": 0,
                "restored_database_sha256": "7" * 64,
            }),
            ("RESTORE_EXECUTION_ROLLED_BACK", {
                "restored_database_sha256": "7" * 64,
            }),
        ]
        for event, details in events:
            append_restore_execution_event(
                event, plan_id, token, operator, incident,
                details=details, journal_path=journal,
            )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(
            result["status"],
            "RESTORE_EXECUTION_REVIEW_REQUIRED",
        )
        self.assertEqual(result["executions"][0]["last_event"], "RESTORE_EXECUTION_ROLLED_BACK")
        self.assertEqual(
            result["executions"][0]["classification"],
            "ROLLED_BACK_REQUIRES_DATABASE_RECONCILIATION",
        )
        self.assertEqual(result["executions"][0]["evidence_status"], "EVENT_EVIDENCE_PRESENT")
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)


    def test_restore_execution_inspector_accepts_complete_success_lifecycle_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-valid-success.audit.jsonl"
        plan_id, token = "7" * 32, "8" * 32
        operator, incident = "operator@example.com", "INC-VALID-SUCCESS"
        installed_hash = "9" * 64
        events = [
            ("RESTORE_EXECUTION_STARTED", {"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }}),
            ("RESTORE_SOURCE_PUBLISHED", {"artifacts": {
                "staged_candidate_sha256": "3" * 64,
                "published_database_sha256": "5" * 64,
            }}),
            ("RESTORE_POSTCHECK_PASSED", {
                "integrity_check": "ok",
                "foreign_key_violations": 0,
                "schema_check": "passed",
                "installed_database_sha256": installed_hash,
            }),
            ("RESTORE_EXECUTION_COMPLETE", {
                "final_database_sha256": installed_hash,
            }),
        ]
        for event, details in events:
            append_restore_execution_event(
                event, plan_id, token, operator, incident,
                details=details, journal_path=journal,
            )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(
            result["status"],
            "RESTORE_EXECUTION_REVIEW_REQUIRED",
        )
        self.assertEqual(result["executions"][0]["last_event"], "RESTORE_EXECUTION_COMPLETE")
        self.assertEqual(
            result["executions"][0]["classification"],
            "COMPLETED_REQUIRES_DATABASE_RECONCILIATION",
        )
        self.assertEqual(result["executions"][0]["evidence_status"], "EVENT_EVIDENCE_PRESENT")
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_execution_inspector_rejects_events_after_success_terminal_read_only(self):
        journal = Path(self.temp_dir.name) / "restore-after-success.audit.jsonl"
        plan_id, token = "9" * 32, "a" * 32
        operator, incident = "operator@example.com", "INC-AFTER-SUCCESS"
        events = [
            ("RESTORE_EXECUTION_STARTED", {"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }}),
            ("RESTORE_SOURCE_PUBLISHED", {"artifacts": {
                "staged_candidate_sha256": "3" * 64,
                "published_database_sha256": "5" * 64,
            }}),
            ("RESTORE_POSTCHECK_PASSED", {
                "integrity_check": "ok",
                "foreign_key_violations": 0,
                "schema_check": "passed",
                "installed_database_sha256": "6" * 64,
            }),
            ("RESTORE_EXECUTION_COMPLETE", {
                "final_database_sha256": "6" * 64,
            }),
            ("RESTORE_ROLLBACK_STARTED", {"artifacts": {
                "rollback_backup_sha256": "4" * 64,
            }}),
        ]
        for event, details in events:
            append_restore_execution_event(
                event, plan_id, token, operator, incident,
                details=details, journal_path=journal,
            )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertIn(
            "invalid event transition RESTORE_EXECUTION_COMPLETE -> RESTORE_ROLLBACK_STARTED",
            result["semantic_errors"][0]["reason"],
        )
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_execution_inspector_rejects_reusing_plan_with_new_execution_token(self):
        journal = Path(self.temp_dir.name) / "restore-plan-reuse.audit.jsonl"
        plan_id = "b" * 32
        operator, incident = "operator@example.com", "INC-PLAN-REUSE"
        artifacts = {
            "live_database_sha256": "1" * 64,
            "source_backup_sha256": "2" * 64,
            "staged_candidate_sha256": "3" * 64,
            "rollback_backup_sha256": "4" * 64,
        }
        for token in ("c" * 32, "d" * 32):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", plan_id, token, operator, incident,
                details={"artifacts": artifacts}, journal_path=journal,
            )
        before = journal.read_bytes()

        result = inspect_restore_execution_state(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_SEMANTIC_INVALID")
        self.assertTrue(any(
            "restore plan reused with multiple execution tokens" in item["reason"]
            for item in result["semantic_errors"]
        ))
        self.assertEqual(result["automatic_action"], "NONE")
        self.assertFalse(result["live_database_replaced"])
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )


    def test_restore_journal_rejects_excessive_json_nesting_without_raising(self):
        journal = Path(self.temp_dir.name) / "restore-deep-json.audit.jsonl"
        journal.write_text('{"nested":' + "[" * 3000 + "0" + "]" * 3000 + "}" + chr(10), encoding="utf-8")

        result = verify_restore_execution_journal(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_INVALID")
        self.assertEqual(result["record_count"], 0)
        self.assertTrue(result.get("reason"))
        self.assertIn("recursion", result["reason"].lower())


    def test_restore_journal_append_refuses_to_cross_size_limit(self):
        journal = Path(self.temp_dir.name) / "restore-append-limit.audit.jsonl"
        artifacts = {
            "live_database_sha256": "1" * 64,
            "source_backup_sha256": "2" * 64,
            "staged_candidate_sha256": "3" * 64,
            "rollback_backup_sha256": "4" * 64,
        }
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
            "operator@example.com", "INC-APPEND-LIMIT",
            details={"artifacts": artifacts}, journal_path=journal,
        )
        before = journal.read_bytes()
        with patch("app.maintenance._RESTORE_JOURNAL_MAX_BYTES", len(before)):
            with self.assertRaisesRegex(ValueError, "exceed the size limit"):
                append_restore_execution_event(
                    "RESTORE_EXECUTION_RECOVERY_REQUIRED", "a" * 32, "b" * 32,
                    "operator@example.com", "INC-APPEND-LIMIT",
                    details={"reason": "test"}, journal_path=journal,
                )
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(
            verify_restore_execution_journal(journal)["status"],
            "RESTORE_JOURNAL_VERIFIED",
        )

    def test_restore_journal_partial_write_fails_closed_and_refuses_future_appends(self):
        journal = Path(self.temp_dir.name) / "restore-partial-write.audit.jsonl"
        real_write = __import__("os").write
        calls = {"count": 0}

        def interrupted_write(descriptor, data):
            calls["count"] += 1
            if calls["count"] == 1:
                return real_write(descriptor, data[: max(1, len(data) // 2)])
            raise OSError("simulated disk write failure")

        with patch("app.maintenance.os.write", side_effect=interrupted_write):
            with self.assertRaisesRegex(OSError, "simulated disk write failure"):
                append_restore_execution_event(
                    "RESTORE_EXECUTION_STARTED", "a" * 32, "b" * 32,
                    "operator@example.com", "INC-PARTIAL-WRITE",
                    details={"artifacts": {
                        "live_database_sha256": "1" * 64,
                        "source_backup_sha256": "2" * 64,
                        "staged_candidate_sha256": "3" * 64,
                        "rollback_backup_sha256": "4" * 64,
                    }},
                    journal_path=journal,
                )

        result = verify_restore_execution_journal(journal)
        self.assertEqual(result["status"], "RESTORE_JOURNAL_INVALID")
        before = journal.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_RECOVERY_REQUIRED", "a" * 32, "b" * 32,
                "operator@example.com", "INC-PARTIAL-WRITE",
                details={"reason": "manual review required"}, journal_path=journal,
            )
        self.assertEqual(journal.read_bytes(), before)

    def test_restore_journal_rejects_file_over_global_size_limit(self):
        journal = Path(self.temp_dir.name) / "restore-oversized.audit.jsonl"
        with journal.open("wb") as handle:
            handle.truncate((16 * 1024 * 1024) + 1)

        result = verify_restore_execution_journal(journal)

        self.assertEqual(result["status"], "RESTORE_JOURNAL_INVALID")
        self.assertEqual(result["record_count"], 0)
        self.assertIn("size limit", result.get("reason", ""))

    def create_byte_corrupted_sqlite_copy(self, target):
        target = Path(target)
        with sqlite3.connect(database.DB_PATH) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        with sqlite3.connect(target) as connection:
            connection.execute(
                "CREATE TABLE corruption_probe (probe_id INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO corruption_probe(payload) VALUES (?)",
                [("x" * 2048,) for _ in range(200)],
            )
            root_page = connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE name = 'corruption_probe'"
            ).fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        self.assertGreater(root_page, 1)
        with target.open("r+b") as artifact:
            artifact.seek((root_page - 1) * page_size)
            artifact.write(b"\\xff")
            artifact.flush()
            os.fsync(artifact.fileno())
        return target

    def create_valid_restore_artifacts(self, root):
        backup_dir = Path(root) / "backups"
        staging_dir = backup_dir / "recovery-staging"
        staging_dir.mkdir(parents=True)
        source = backup_dir / "taimobot-source.sqlite3"
        rollback = backup_dir / "taimobot-rollback.sqlite3"
        staged = staging_dir / "taimobot-staged.sqlite3"
        for target in (source, rollback, staged):
            with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                live.backup(copy)
        return backup_dir, source, staged, rollback


    def test_byte_corrupted_sqlite_backup_is_not_restore_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "taimobot-valid.sqlite3"
            corrupt = root / "taimobot-byte-corrupt.sqlite3"
            with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(valid) as copy:
                live.backup(copy)
            self.create_byte_corrupted_sqlite_copy(corrupt)
            live_before = database.DB_PATH.read_bytes()
            result = inspect_recovery_candidates(root)
            records = {item["file_name"]: item for item in result["candidates"]}
            self.assertTrue(records[valid.name]["restore_ready"])
            self.assertFalse(records[corrupt.name]["restore_ready"])
            self.assertIn("SQLITE_INTEGRITY_FAILED", records[corrupt.name]["issues"])
            self.assertEqual(result["restore_ready_count"], 1)
            self.assertEqual(database.DB_PATH.read_bytes(), live_before)
            self.assertFalse(result["live_database_replaced"])

    def test_byte_corrupted_source_backup_blocks_restore_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir, source, staged, rollback = self.create_valid_restore_artifacts(directory)
            self.create_byte_corrupted_sqlite_copy(source)
            with self.assertRaisesRegex(ValueError, "source backup is not restore-ready"):
                verify_restore_execution_readiness(
                    source, staged, rollback, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(database.DB_PATH.is_file())

    def test_byte_corrupted_staged_candidate_blocks_restore_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir, source, staged, rollback = self.create_valid_restore_artifacts(directory)
            self.create_byte_corrupted_sqlite_copy(staged)
            with self.assertRaisesRegex(ValueError, "staged restore candidate is not restore-ready"):
                verify_restore_execution_readiness(
                    source, staged, rollback, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(database.DB_PATH.is_file())

    def test_byte_corrupted_rollback_backup_blocks_restore_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir, source, staged, rollback = self.create_valid_restore_artifacts(directory)
            self.create_byte_corrupted_sqlite_copy(rollback)
            with self.assertRaisesRegex(ValueError, "rollback backup is not restore-ready"):
                verify_restore_execution_readiness(
                    source, staged, rollback, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(database.DB_PATH.is_file())

    def test_byte_corrupted_live_database_blocks_restore_preflight_without_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir, source, staged, rollback = self.create_valid_restore_artifacts(directory)
            corrupt_live = Path(directory) / "corrupt-live.sqlite3"
            self.create_byte_corrupted_sqlite_copy(corrupt_live)
            corrupt_before = corrupt_live.read_bytes()
            with self.assertRaisesRegex(ValueError, "live database failed its integrity check"):
                verify_restore_execution_readiness(
                    source, staged, rollback, backup_dir=backup_dir,
                    live_database_path=corrupt_live,
                )
            self.assertEqual(corrupt_live.read_bytes(), corrupt_before)
            self.assertTrue(database.DB_PATH.is_file())

    def test_restore_candidate_verification_rejects_page_corruption_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "taimobot-corrupt.sqlite3"
            staging = Path(directory) / "recovery-staging" / "candidate.sqlite3"
            self.create_byte_corrupted_sqlite_copy(corrupt)
            live_before = database.DB_PATH.read_bytes()
            with self.assertRaises(Exception):
                verify_restore_candidate(corrupt, staging)
            self.assertFalse(staging.exists())
            self.assertEqual(database.DB_PATH.read_bytes(), live_before)

    def test_inventory_identifies_exact_valid_candidate_among_corrupt_neighbours(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "taimobot-approved.sqlite3"
            corrupt_a = root / "taimobot-neighbour-a.sqlite3"
            corrupt_b = root / "taimobot-neighbour-b.sqlite3"
            with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(valid) as copy:
                live.backup(copy)
            self.create_byte_corrupted_sqlite_copy(corrupt_a)
            self.create_byte_corrupted_sqlite_copy(corrupt_b)
            result = inspect_recovery_candidates(root)
            records = {item["file_name"]: item for item in result["candidates"]}
            self.assertEqual(result["candidate_count"], 3)
            self.assertEqual(result["restore_ready_count"], 1)
            self.assertTrue(records[valid.name]["restore_ready"])
            self.assertFalse(records[corrupt_a.name]["restore_ready"])
            self.assertFalse(records[corrupt_b.name]["restore_ready"])
            self.assertEqual(records[valid.name]["file_name"], valid.name)

    def test_byte_corrupted_candidate_with_wal_sidecar_is_skipped_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = root / "taimobot-corrupt-with-wal.sqlite3"
            self.create_byte_corrupted_sqlite_copy(corrupt)
            wal = Path(str(corrupt) + "-wal")
            wal.write_bytes(b"forensic-wal-content")
            wal_before = wal.read_bytes()
            result = inspect_recovery_candidates(root)
            record = result["candidates"][0]
            self.assertFalse(record["restore_ready"])
            self.assertEqual(record["integrity_check"], "SKIPPED_SIDECARS_PRESENT")
            self.assertIn("SQLITE_SIDECARS_PRESENT", record["issues"])
            self.assertEqual(wal.read_bytes(), wal_before)

    def test_byte_corruption_after_inventory_is_rejected_by_final_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir, source, staged, rollback = self.create_valid_restore_artifacts(directory)
            initial = inspect_recovery_candidates(backup_dir)
            records = {item["file_name"]: item for item in initial["candidates"]}
            self.assertTrue(records[source.name]["restore_ready"])
            self.create_byte_corrupted_sqlite_copy(source)
            with self.assertRaisesRegex(ValueError, "source backup is not restore-ready"):
                verify_restore_execution_readiness(
                    source, staged, rollback, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(database.DB_PATH.is_file())

    def test_corrupt_restore_candidate_never_changes_live_database_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            corrupt = Path(directory) / "taimobot-corrupt-source.sqlite3"
            self.create_byte_corrupted_sqlite_copy(corrupt)
            live_before = sha256_file(database.DB_PATH)
            inventory = inspect_recovery_candidates(directory)
            self.assertFalse(inventory["candidates"][0]["restore_ready"])
            self.assertEqual(sha256_file(database.DB_PATH), live_before)
            self.assertFalse(inventory["live_database_replaced"])


    def prepare_restore_identity_fixture(
        self, backup_dir, selected_name, candidates,
        *, inventory_hook=None, staging_hook=None, include_fingerprint=True
    ):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore

        backup_dir = Path(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        pre_restore = backup_dir / "taimobot-pre-restore.sqlite3"
        pre_restore.write_bytes(b"pre-restore-snapshot")

        def inventory(directory=None):
            if directory is None or Path(directory).resolve() == backup_dir.resolve():
                records = []
                for name, ready in candidates:
                    path = backup_dir / name
                    record = {"file_name": name, "restore_ready": ready}
                    if include_fingerprint and path.is_file() and not path.is_symlink():
                        record["sha256"] = sha256_file(path)
                    records.append(record)
                result = {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": records}
                if inventory_hook:
                    inventory_hook()
                return result
            staged_file = next(Path(directory).glob("taimobot-restore-*.sqlite3"))
            return {
                "status": "RECOVERY_INVENTORY_COMPLETE",
                "candidates": [{
                    "file_name": staged_file.name,
                    "restore_ready": True,
                    "sha256": sha256_file(staged_file),
                }],
            }

        def create_backup():
            return {"status": "BACKUP_VERIFIED", "backup_file": str(pre_restore)}

        def stage_candidate(backup, staging):
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.write_bytes(backup.read_bytes())
            if staging_hook:
                staging_hook()
            return {"status": "RESTORE_CANDIDATE_VERIFIED"}

        with patch("app.main.BACKUP_DIR", backup_dir), patch(
            "app.main.require_admin"
        ), patch(
            "app.main.inspect_recovery_candidates", side_effect=inventory
        ), patch(
            "app.main.create_verified_backup", side_effect=create_backup
        ) as create_backup_mock, patch(
            "app.main.verify_restore_candidate", side_effect=stage_candidate
        ) as stage_mock:
            result = prepare_maintenance_restore(
                MaintenanceRestorePrepareRequest(
                    backup_file_name=selected_name,
                    confirm_preparation=True,
                ),
                {"email": "restore-operator@example.com"},
            )
        return result, create_backup_mock, stage_mock


    def test_restore_preparation_selects_exact_candidate_from_multiple_valid_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            first = backup_dir / "taimobot-first.sqlite3"
            selected = backup_dir / "taimobot-selected.sqlite3"
            first.write_bytes(b"first-candidate")
            selected.write_bytes(b"selected-candidate")
            result, _, stage = self.prepare_restore_identity_fixture(
                backup_dir, selected.name,
                [(first.name, True), (selected.name, True)]
            )
            self.assertEqual(result["selected_backup"], selected.name)
            stage.assert_called_once()
            self.assertEqual(stage.call_args.args[0], selected)
            with database.get_connection() as connection:
                plan = connection.execute(
                    "SELECT backup_file FROM maintenance_restore_plans WHERE plan_id = ?",
                    (result["plan_id"],)
                ).fetchone()
            self.assertEqual(Path(plan["backup_file"]), selected.resolve())

    def test_restore_preparation_does_not_fallback_when_named_candidate_is_missing(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            available = backup_dir / "taimobot-available.sqlite3"
            available.write_bytes(b"available")
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates",
                return_value={"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": available.name, "restore_ready": True, "sha256": sha256_file(available)}
                ]},
            ), patch("app.main.create_verified_backup") as create_backup:
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name="taimobot-not-selected.sqlite3",
                            confirm_preparation=True,
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 404)
            create_backup.assert_not_called()

    def test_restore_preparation_rejects_inventory_candidate_not_restore_ready(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            corrupt = backup_dir / "taimobot-corrupt.sqlite3"
            corrupt.write_bytes(b"corrupt")
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates",
                return_value={"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": corrupt.name, "restore_ready": False}
                ]},
            ), patch("app.main.create_verified_backup") as create_backup:
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name=corrupt.name,
                            confirm_preparation=True,
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 409)
            create_backup.assert_not_called()

    def test_restore_preparation_rejects_path_traversal_backup_name(self):
        from app.main import MaintenanceRestorePrepareRequest
        with self.assertRaises(ValueError):
            MaintenanceRestorePrepareRequest(
                backup_file_name="../taimobot-outside.sqlite3",
                confirm_preparation=True,
            )


    def test_restore_preparation_rejects_symlinked_selected_backup(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            outside = Path(directory) / "outside.sqlite3"
            outside.write_bytes(b"outside")
            selected = backup_dir / "taimobot-symlink.sqlite3"
            selected.symlink_to(outside)
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates",
                return_value={"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": selected.name, "restore_ready": True, "sha256": sha256_file(outside)}
                ]},
            ), patch("app.main.create_verified_backup") as create_backup:
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name=selected.name, confirm_preparation=True
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 409)
            create_backup.assert_not_called()

    def test_restore_preparation_rejects_symlink_swap_after_inventory(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            selected = backup_dir / "taimobot-swap.sqlite3"
            selected.write_bytes(b"selected-before-inventory")
            outside = Path(directory) / "outside.sqlite3"
            outside.write_bytes(b"outside-target")

            def inventory(directory=None):
                if directory is None or Path(directory).resolve() == backup_dir.resolve():
                    fingerprint = sha256_file(selected)
                    selected.unlink()
                    selected.symlink_to(outside)
                    return {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                        {"file_name": selected.name, "restore_ready": True, "sha256": fingerprint}
                    ]}
                staged_file = next(Path(directory).glob("taimobot-restore-*.sqlite3"))
                return {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": staged_file.name, "restore_ready": True, "sha256": sha256_file(staged_file)}
                ]}
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates", side_effect=inventory
            ), patch("app.main.create_verified_backup") as create_backup:
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name=selected.name, confirm_preparation=True
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 409)
            create_backup.assert_not_called()

    def test_restore_preparation_rejects_regular_file_replacement_after_inventory(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            selected = backup_dir / "taimobot-regular-swap.sqlite3"
            selected.write_bytes(b"original-backup")

            def inventory(directory=None):
                if directory is None or Path(directory).resolve() == backup_dir.resolve():
                    fingerprint = sha256_file(selected)
                    selected.write_bytes(b"replacement-backup")
                    return {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                        {"file_name": selected.name, "restore_ready": True, "sha256": fingerprint}
                    ]}
                staged_file = next(Path(directory).glob("taimobot-restore-*.sqlite3"))
                return {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": staged_file.name, "restore_ready": True, "sha256": sha256_file(staged_file)}
                ]}
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates", side_effect=inventory
            ), patch("app.main.create_verified_backup") as create_backup:
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name=selected.name, confirm_preparation=True
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 409)
            self.assertIn("changed after recovery inventory", rejected.exception.detail)
            create_backup.assert_not_called()

    def test_restore_preparation_requires_inventory_fingerprint(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            selected = backup_dir / "taimobot-no-fingerprint.sqlite3"
            selected.write_bytes(b"selected")
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates",
                return_value={"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": selected.name, "restore_ready": True}
                ]},
            ), patch("app.main.create_verified_backup") as create_backup:
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name=selected.name, confirm_preparation=True
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 409)
            create_backup.assert_not_called()

    def test_restore_preparation_rechecks_source_fingerprint_after_staging(self):
        from app.main import MaintenanceRestorePrepareRequest, prepare_maintenance_restore
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            selected = backup_dir / "taimobot-changed-during-stage.sqlite3"
            selected.write_bytes(b"original-source")
            pre_restore = backup_dir / "taimobot-pre-restore.sqlite3"
            pre_restore.write_bytes(b"rollback")
            def inventory(directory=None):
                if directory is None or Path(directory).resolve() == backup_dir.resolve():
                    return {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                        {"file_name": selected.name, "restore_ready": True, "sha256": sha256_file(selected)}
                    ]}
                staged_file = next(Path(directory).glob("taimobot-restore-*.sqlite3"))
                return {"status": "RECOVERY_INVENTORY_COMPLETE", "candidates": [
                    {"file_name": staged_file.name, "restore_ready": True, "sha256": sha256_file(staged_file)}
                ]}
            def stage_and_mutate(source, staging):
                staging.parent.mkdir(parents=True, exist_ok=True)
                staging.write_bytes(source.read_bytes())
                source.write_bytes(b"source-changed-during-stage")
                return {"status": "RESTORE_CANDIDATE_VERIFIED"}
            with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
                "app.main.inspect_recovery_candidates", side_effect=inventory
            ), patch(
                "app.main.create_verified_backup",
                return_value={"status": "BACKUP_VERIFIED", "backup_file": str(pre_restore)}
            ), patch("app.main.verify_restore_candidate", side_effect=stage_and_mutate):
                with self.assertRaises(HTTPException) as rejected:
                    prepare_maintenance_restore(
                        MaintenanceRestorePrepareRequest(
                            backup_file_name=selected.name, confirm_preparation=True
                        ),
                        {"email": "restore-operator@example.com"},
                    )
            self.assertEqual(rejected.exception.status_code, 500)
            self.assertIn("changed while", rejected.exception.detail)
            with database.get_connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'maintenance_restore_plans'"
                    ).fetchone()[0],
                    0,
                )

    def test_restore_preparation_persists_operator_and_exact_candidate_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory) / "backups"
            backup_dir.mkdir()
            selected = backup_dir / "taimobot-operator-selected.sqlite3"
            selected.write_bytes(b"operator-selected")
            result, _, _ = self.prepare_restore_identity_fixture(
                backup_dir, selected.name, [(selected.name, True)]
            )
            with database.get_connection() as connection:
                plan = connection.execute(
                    "SELECT backup_file, created_by, status FROM maintenance_restore_plans WHERE plan_id = ?",
                    (result["plan_id"],)
                ).fetchone()
                artifacts = connection.execute(
                    "SELECT source_sha256 FROM maintenance_restore_artifacts WHERE plan_id = ?",
                    (result["plan_id"],)
                ).fetchone()
            self.assertEqual(plan["backup_file"], str(selected.resolve()))
            self.assertEqual(plan["created_by"], "restore-operator@example.com")
            self.assertEqual(plan["status"], "PREPARED")
            self.assertEqual(artifacts["source_sha256"], sha256_file(selected))


    def _create_restore_approval_fixture(self, plan_id, *, status="PREPARED"):
        backup_dir = Path(self.temp_dir.name) / "backups"
        staging_dir = backup_dir / "recovery-staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        source = backup_dir / f"{plan_id}-source.sqlite3"
        rollback = backup_dir / f"{plan_id}-rollback.sqlite3"
        staged = staging_dir / f"{plan_id}-staged.sqlite3"
        source.write_bytes(b"source-" + plan_id.encode())
        rollback.write_bytes(b"rollback-" + plan_id.encode())
        staged.write_bytes(b"staged-" + plan_id.encode())
        with database.get_connection() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS maintenance_restore_plans (
                    plan_id TEXT PRIMARY KEY, backup_file TEXT NOT NULL,
                    staging_file TEXT NOT NULL, pre_restore_backup_file TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS maintenance_restore_artifacts (
                    plan_id TEXT PRIMARY KEY REFERENCES maintenance_restore_plans(plan_id) ON DELETE CASCADE,
                    source_sha256 TEXT NOT NULL, staged_sha256 TEXT NOT NULL,
                    rollback_sha256 TEXT NOT NULL
                )"""
            )
            connection.execute(
                "INSERT INTO maintenance_restore_plans VALUES (?, ?, ?, ?, ?, ?, ?)",
                (plan_id, str(source), str(staged), str(rollback), status,
                 datetime.now(ZoneInfo("Africa/Johannesburg")).isoformat(), "creator@example.com"),
            )
            connection.execute(
                "INSERT INTO maintenance_restore_artifacts VALUES (?, ?, ?, ?)",
                (plan_id, sha256_file(source), sha256_file(staged), sha256_file(rollback)),
            )
        return backup_dir, source, staged, rollback

    def _ready_restore_inventory(self, backup_dir, source, staged, rollback):
        def inventory(directory=None):
            if Path(directory).resolve() == backup_dir.resolve():
                names = [source.name, rollback.name]
            else:
                names = [staged.name]
            return {
                "status": "RECOVERY_INVENTORY_COMPLETE",
                "candidates": [{"file_name": name, "restore_ready": True} for name in names],
            }
        return inventory


    def test_restore_approval_rejects_source_symlink_swap_during_inventory(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException
        plan_id = "d" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        outside = Path(self.temp_dir.name) / "outside-source.sqlite3"
        outside.write_bytes(source.read_bytes())
        inventory = self._ready_restore_inventory(backup_dir, source, staged, rollback)
        calls = 0
        def swap(directory=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                source.unlink()
                source.symlink_to(outside)
            return inventory(directory)
        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=swap
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "PREPARED")

    def test_restore_approval_rejects_staging_symlink_swap_during_inventory(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException
        plan_id = "e" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        outside = Path(self.temp_dir.name) / "outside-staged.sqlite3"
        outside.write_bytes(staged.read_bytes())
        inventory = self._ready_restore_inventory(backup_dir, source, staged, rollback)
        calls = 0
        def swap(directory=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                staged.unlink()
                staged.symlink_to(outside)
            return inventory(directory)
        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=swap
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "PREPARED")

    def test_restore_approval_rejects_rollback_symlink_swap_during_inventory(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException
        plan_id = "f" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        outside = Path(self.temp_dir.name) / "outside-rollback.sqlite3"
        outside.write_bytes(rollback.read_bytes())
        inventory = self._ready_restore_inventory(backup_dir, source, staged, rollback)
        calls = 0
        def swap(directory=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                rollback.unlink()
                rollback.symlink_to(outside)
            return inventory(directory)
        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=swap
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "PREPARED")

    def test_restore_approval_compare_and_set_rejects_concurrent_plan_status_change(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException
        plan_id = "1" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        inventory = self._ready_restore_inventory(backup_dir, source, staged, rollback)
        calls = 0
        def change_status(directory=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                with database.get_connection() as connection:
                    connection.execute(
                        "UPDATE maintenance_restore_plans SET status='EXPIRED' WHERE plan_id=?",
                        (plan_id,),
                    )
            return inventory(directory)
        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=change_status
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "EXPIRED")

    def test_restore_approval_compare_and_set_rejects_plan_deleted_during_verification(self):
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException
        plan_id = "2" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        inventory = self._ready_restore_inventory(backup_dir, source, staged, rollback)
        calls = 0
        def delete_plan(directory=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                with database.get_connection() as connection:
                    connection.execute(
                        "DELETE FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
                    )
            return inventory(directory)
        with patch("app.main.BACKUP_DIR", backup_dir), patch("app.main.require_admin"), patch(
            "app.main.inspect_recovery_candidates", side_effect=delete_plan
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        with database.get_connection() as connection:
            self.assertIsNone(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone())

    def test_restore_preflight_rejects_prepared_plan_without_mutating_it(self):
        from app.main import MaintenanceRestorePreflightRequest, preflight_maintenance_restore
        from fastapi import HTTPException
        plan_id = "3" * 32
        self._create_restore_approval_fixture(plan_id, status="PREPARED")
        with patch("app.main.require_admin"):
            with self.assertRaises(HTTPException) as rejected:
                preflight_maintenance_restore(
                    MaintenanceRestorePreflightRequest(plan_id=plan_id),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertIn("APPROVED", rejected.exception.detail)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "PREPARED")

    def test_restore_preflight_reports_read_only_non_authorizing_success(self):
        from app.main import MaintenanceRestorePreflightRequest, preflight_maintenance_restore
        plan_id = "4" * 32
        self._create_restore_approval_fixture(plan_id, status="APPROVED")
        expected = {
            "status": "APPROVED_RESTORE_PREFLIGHT_PASSED",
            "plan_approved": True,
            "execution_authorized": False,
            "live_database_replaced": False,
            "trading_mode": "paper",
        }
        with patch("app.main.require_admin"), patch(
            "app.main.verify_approved_restore_plan", return_value=expected
        ) as verify:
            result = preflight_maintenance_restore(
                MaintenanceRestorePreflightRequest(plan_id=plan_id),
                {"email": "approver@example.com"},
            )
        self.assertEqual(result, expected)
        verify.assert_called_once()
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "APPROVED")

    def test_restore_preflight_active_lock_blocks_without_plan_mutation(self):
        from app.main import MaintenanceRestorePreflightRequest, preflight_maintenance_restore
        from fastapi import HTTPException
        plan_id = "5" * 32
        self._create_restore_approval_fixture(plan_id, status="APPROVED")
        with patch("app.main.require_admin"), patch(
            "app.main.verify_approved_restore_plan", side_effect=ValueError("A restore lock is already active.")
        ) as verify:
            with self.assertRaises(HTTPException) as rejected:
                preflight_maintenance_restore(
                    MaintenanceRestorePreflightRequest(plan_id=plan_id),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertIn("restore lock", rejected.exception.detail)
        verify.assert_called_once()
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "APPROVED")

    def test_restore_preflight_rejects_missing_artifact_fingerprint_row(self):
        from app.main import MaintenanceRestorePreflightRequest, preflight_maintenance_restore
        from fastapi import HTTPException
        plan_id = "6" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        with database.get_connection() as connection:
            connection.execute(
                "DELETE FROM maintenance_restore_artifacts WHERE plan_id=?", (plan_id,)
            )
        with patch("app.main.require_admin"), patch("app.main.verify_approved_restore_plan") as verify:
            with self.assertRaises(HTTPException) as rejected:
                preflight_maintenance_restore(
                    MaintenanceRestorePreflightRequest(plan_id=plan_id),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        verify.assert_not_called()

    def test_restore_preflight_fingerprint_failure_does_not_change_approved_plan(self):
        from app.main import MaintenanceRestorePreflightRequest, preflight_maintenance_restore
        from fastapi import HTTPException
        plan_id = "7" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id, status="APPROVED")
        staged.write_bytes(b"changed-after-approval")
        with patch("app.main.require_admin"), patch(
            "app.main.verify_approved_restore_plan", side_effect=ValueError("Restore artifact fingerprint mismatch.")
        ):
            with self.assertRaises(HTTPException) as rejected:
                preflight_maintenance_restore(
                    MaintenanceRestorePreflightRequest(plan_id=plan_id),
                    {"email": "approver@example.com"},
                )
        self.assertEqual(rejected.exception.status_code, 409)
        self.assertIn("fingerprint", rejected.exception.detail)
        with database.get_connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()["status"], "APPROVED")

    def test_sha256_file_rejects_symlink_without_following_target(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "outside.sqlite3"
            link = Path(directory) / "linked.sqlite3"
            target.write_bytes(b"must-not-be-followed")
            link.symlink_to(target)
            with self.assertRaises(OSError):
                sha256_file(link)
            self.assertEqual(target.read_bytes(), b"must-not-be-followed")


    def _make_locked_restore_artifacts(self, suffix):
        root = Path(self.temp_dir.name) / f"locked-restore-{suffix}"
        backup_dir = root / "backups"
        staging_dir = backup_dir / "recovery-staging"
        staging_dir.mkdir(parents=True, exist_ok=True)
        source = backup_dir / "taimobot-source.sqlite3"
        rollback = backup_dir / "taimobot-rollback.sqlite3"
        staged = staging_dir / "taimobot-staged.sqlite3"
        for target in (source, rollback, staged):
            with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                live.backup(copy)
        hashes = {
            "source_sha256": sha256_file(source),
            "staged_sha256": sha256_file(staged),
            "rollback_sha256": sha256_file(rollback),
        }
        return backup_dir, source, staged, rollback, hashes

    def test_locked_preflight_requires_all_three_expected_fingerprints(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("missing-hashes")
        lock = acquire_restore_lock("8" * 32)
        try:
            with self.assertRaisesRegex(ValueError, "fingerprints are required"):
                verify_restore_execution_readiness_under_lock(
                    source, staged, rollback, plan_id="8" * 32,
                    lock_token=lock["lock_token"], external_processes_stopped=True,
                    backup_dir=backup_dir, live_database_path=database.DB_PATH,
                )
            self.assertTrue(is_restore_lock_active())
        finally:
            release_restore_lock(lock["lock_token"])

    def test_locked_preflight_rejects_source_fingerprint_mismatch(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("source-hash")
        lock = acquire_restore_lock("9" * 32)
        try:
            wrong = dict(hashes)
            wrong["source_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                verify_restore_execution_readiness_under_lock(
                    source, staged, rollback, plan_id="9" * 32,
                    lock_token=lock["lock_token"], external_processes_stopped=True,
                    expected_artifact_hashes=wrong, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(is_restore_lock_active())
        finally:
            release_restore_lock(lock["lock_token"])

    def test_locked_preflight_rejects_staged_fingerprint_mismatch(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("staged-hash")
        lock = acquire_restore_lock("a" * 32)
        try:
            wrong = dict(hashes)
            wrong["staged_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                verify_restore_execution_readiness_under_lock(
                    source, staged, rollback, plan_id="a" * 32,
                    lock_token=lock["lock_token"], external_processes_stopped=True,
                    expected_artifact_hashes=wrong, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
        finally:
            release_restore_lock(lock["lock_token"])

    def test_locked_preflight_rejects_rollback_fingerprint_mismatch(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("rollback-hash")
        lock = acquire_restore_lock("b" * 32)
        try:
            wrong = dict(hashes)
            wrong["rollback_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                verify_restore_execution_readiness_under_lock(
                    source, staged, rollback, plan_id="b" * 32,
                    lock_token=lock["lock_token"], external_processes_stopped=True,
                    expected_artifact_hashes=wrong, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
        finally:
            release_restore_lock(lock["lock_token"])

    def test_locked_preflight_detects_artifact_change_during_inventory(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("during-inventory")
        lock = acquire_restore_lock("c" * 32)
        original = inspect_recovery_candidates
        calls = 0
        def mutate_after_inventory(directory):
            nonlocal calls
            result = original(directory)
            calls += 1
            if calls == 2:
                with staged.open("ab") as stream:
                    stream.write(b"changed-during-preflight")
            return result
        try:
            with patch("app.maintenance.inspect_recovery_candidates", side_effect=mutate_after_inventory):
                with self.assertRaisesRegex(ValueError, "changed during locked preflight"):
                    verify_restore_execution_readiness_under_lock(
                        source, staged, rollback, plan_id="c" * 32,
                        lock_token=lock["lock_token"], external_processes_stopped=True,
                        expected_artifact_hashes=hashes, backup_dir=backup_dir,
                        live_database_path=database.DB_PATH,
                    )
            self.assertTrue(is_restore_lock_active())
        finally:
            release_restore_lock(lock["lock_token"])

    def test_locked_preflight_rejects_plan_id_not_bound_to_lock(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("wrong-plan")
        lock = acquire_restore_lock("d" * 32)
        try:
            with self.assertRaisesRegex(RuntimeError, "matching active restore lock"):
                verify_restore_execution_readiness_under_lock(
                    source, staged, rollback, plan_id="e" * 32,
                    lock_token=lock["lock_token"], external_processes_stopped=True,
                    expected_artifact_hashes=hashes, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
        finally:
            release_restore_lock(lock["lock_token"])

    def test_locked_preflight_requires_explicit_external_process_acknowledgement(self):
        backup_dir, source, staged, rollback, hashes = self._make_locked_restore_artifacts("no-ack")
        lock = acquire_restore_lock("f" * 32)
        try:
            with self.assertRaisesRegex(ValueError, "external SQLite processes"):
                verify_restore_execution_readiness_under_lock(
                    source, staged, rollback, plan_id="f" * 32,
                    lock_token=lock["lock_token"], external_processes_stopped=False,
                    expected_artifact_hashes=hashes, backup_dir=backup_dir,
                    live_database_path=database.DB_PATH,
                )
            self.assertTrue(is_restore_lock_active())
        finally:
            release_restore_lock(lock["lock_token"])

    def test_released_lock_token_cannot_release_a_later_lock(self):
        first = acquire_restore_lock("1" * 32)
        old_token = first["lock_token"]
        release_restore_lock(old_token)
        second = acquire_restore_lock("2" * 32)
        try:
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                release_restore_lock(old_token)
            self.assertEqual(inspect_restore_lock()["plan_id"], "2" * 32)
            self.assertTrue(is_restore_lock_active())
        finally:
            release_restore_lock(second["lock_token"])

    def test_tampered_lock_marker_cannot_be_released_by_original_owner(self):
        lock = acquire_restore_lock("3" * 32)
        marker = Path(lock["lock_file"])
        original = marker.read_text(encoding="utf-8")
        try:
            payload = json.loads(original)
            payload["lock_token"] = "4" * 32
            marker.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                release_restore_lock(lock["lock_token"])
            self.assertTrue(marker.exists())
            self.assertTrue(is_restore_lock_active())
        finally:
            marker.write_text(original, encoding="utf-8")
            release_restore_lock(lock["lock_token"])

    def test_second_lock_cannot_replace_first_plan_or_owner(self):
        first = acquire_restore_lock("5" * 32)
        try:
            with self.assertRaisesRegex(RuntimeError, "another process"):
                acquire_restore_lock("6" * 32, timeout_seconds=0.02)
            state = inspect_restore_lock()
            self.assertEqual(state["status"], "RESTORE_LOCK_ACTIVE")
            self.assertEqual(state["plan_id"], "5" * 32)
            self.assertEqual(state["lock_token"], first["lock_token"])
        finally:
            release_restore_lock(first["lock_token"])

    def test_restore_lock_is_not_published_when_database_connection_is_open(self):
        connection = database.get_connection()
        try:
            with self.assertRaisesRegex(RuntimeError, "connections remain open"):
                acquire_restore_lock("7" * 32)
            self.assertFalse(is_restore_lock_active())
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
        finally:
            connection.close()


    def _seed_restore_journal(self, path):
        return append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "8" * 32, "9" * 32,
            "operator@example.com", "INC-JOURNAL-IDENTITY",
            details={"artifacts": {
                "live_database_sha256": "1" * 64,
                "source_backup_sha256": "2" * 64,
                "staged_candidate_sha256": "3" * 64,
                "rollback_backup_sha256": "4" * 64,
            }},
            journal_path=path,
        )

    def test_journal_append_rejects_regular_path_replacement_during_verification(self):
        path = Path(self.temp_dir.name) / "restore-identity.jsonl"
        self._seed_restore_journal(path)
        original_bytes = path.read_bytes()
        displaced = path.with_name("displaced-journal.jsonl")
        external = path.with_name("replacement-journal.jsonl")
        external.write_bytes(original_bytes)
        real_verify = __import__("app.maintenance", fromlist=["verify_restore_execution_journal"]).verify_restore_execution_journal
        def replace_then_verify(journal_path, **kwargs):
            Path(journal_path).replace(displaced)
            external.replace(Path(journal_path))
            return real_verify(journal_path, **kwargs)
        with patch("app.maintenance.verify_restore_execution_journal", side_effect=replace_then_verify):
            with self.assertRaisesRegex(RuntimeError, "changed during verification"):
                append_restore_execution_event(
                    "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
                    "operator@example.com", "INC-JOURNAL-IDENTITY",
                    journal_path=path,
                )
        self.assertEqual(displaced.read_bytes(), original_bytes)
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_journal_append_rejects_symlink_swap_during_verification(self):
        path = Path(self.temp_dir.name) / "restore-symlink-race.jsonl"
        self._seed_restore_journal(path)
        original_bytes = path.read_bytes()
        displaced = path.with_name("displaced-symlink-journal.jsonl")
        external = path.with_name("external-journal.jsonl")
        external.write_bytes(original_bytes)
        real_verify = __import__("app.maintenance", fromlist=["verify_restore_execution_journal"]).verify_restore_execution_journal
        def replace_then_verify(journal_path, **kwargs):
            Path(journal_path).replace(displaced)
            Path(journal_path).symlink_to(external)
            return real_verify(journal_path, **kwargs)
        try:
            with patch("app.maintenance.verify_restore_execution_journal", side_effect=replace_then_verify):
                with self.assertRaises(OSError):
                    append_restore_execution_event(
                        "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
                        "operator@example.com", "INC-JOURNAL-IDENTITY",
                        journal_path=path,
                    )
            self.assertTrue(path.is_symlink())
            self.assertEqual(displaced.read_bytes(), original_bytes)
            self.assertEqual(external.read_bytes(), original_bytes)
        finally:
            path.unlink(missing_ok=True)

    def test_journal_append_rejects_path_deletion_during_verification(self):
        path = Path(self.temp_dir.name) / "restore-deleted-race.jsonl"
        self._seed_restore_journal(path)
        displaced = path.with_name("deleted-race-original.jsonl")
        real_verify = __import__("app.maintenance", fromlist=["verify_restore_execution_journal"]).verify_restore_execution_journal
        def delete_then_verify(journal_path, **kwargs):
            Path(journal_path).replace(displaced)
            return real_verify(journal_path, **kwargs)
        with patch("app.maintenance.verify_restore_execution_journal", side_effect=delete_then_verify):
            with self.assertRaises((FileNotFoundError, RuntimeError)):
                append_restore_execution_event(
                    "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
                    "operator@example.com", "INC-JOURNAL-IDENTITY",
                    journal_path=path,
                )
        self.assertFalse(path.exists())
        self.assertEqual(len(verify_restore_execution_journal(displaced, include_records=True)["records"]), 1)

    def test_journal_append_rejects_in_place_write_during_verification(self):
        path = Path(self.temp_dir.name) / "restore-in-place-race.jsonl"
        self._seed_restore_journal(path)
        real_verify = __import__("app.maintenance", fromlist=["verify_restore_execution_journal"]).verify_restore_execution_journal
        def mutate_then_verify(journal_path, **kwargs):
            with Path(journal_path).open("ab") as stream:
                stream.write(b"external-mutation")
            return real_verify(journal_path, **kwargs)
        with patch("app.maintenance.verify_restore_execution_journal", side_effect=mutate_then_verify):
            with self.assertRaisesRegex(RuntimeError, "changed during verification"):
                append_restore_execution_event(
                    "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
                    "operator@example.com", "INC-JOURNAL-IDENTITY",
                    journal_path=path,
                )
        self.assertTrue(path.read_bytes().endswith(b"external-mutation"))

    def test_journal_append_rejects_non_regular_directory_path(self):
        path = Path(self.temp_dir.name) / "journal-directory"
        path.mkdir()
        with self.assertRaises(OSError):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "1" * 32, "2" * 32,
                "operator@example.com", "INC-JOURNAL-DIR",
                journal_path=path,
            )

    def test_journal_append_continues_hash_chain_after_identity_checked_verification(self):
        path = Path(self.temp_dir.name) / "restore-chain-identity.jsonl"
        first = self._seed_restore_journal(path)
        second = append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
            "operator@example.com", "INC-JOURNAL-IDENTITY",
            details={"artifacts": {
                "staged_candidate_sha256": "3" * 64,
                "published_database_sha256": "5" * 64,
            }},
            journal_path=path,
        )
        verified = verify_restore_execution_journal(path, include_records=True)
        self.assertEqual(verified["status"], "RESTORE_JOURNAL_VERIFIED")
        self.assertEqual(verified["record_count"], 2)
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(verified["records"][1]["previous_record_sha256"], first["record_sha256"])

    def test_journal_append_refuses_corrupt_existing_chain_without_modifying_bytes(self):
        path = Path(self.temp_dir.name) / "restore-corrupt-chain.jsonl"
        self._seed_restore_journal(path)
        with path.open("ab") as stream:
            stream.write(b"corrupt")
        before = path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            append_restore_execution_event(
                "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
                "operator@example.com", "INC-JOURNAL-IDENTITY",
                journal_path=path,
            )
        self.assertEqual(path.read_bytes(), before)

    def test_journal_append_refuses_symlink_before_open(self):
        target = Path(self.temp_dir.name) / "journal-target.jsonl"
        target.write_text("", encoding="utf-8")
        link = Path(self.temp_dir.name) / "journal-link.jsonl"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            append_restore_execution_event(
                "RESTORE_EXECUTION_STARTED", "1" * 32, "2" * 32,
                "operator@example.com", "INC-JOURNAL-SYMLINK",
                journal_path=link,
            )
        self.assertEqual(target.read_bytes(), b"")

    def test_journal_append_refuses_oversized_event_without_appending(self):
        path = Path(self.temp_dir.name) / "restore-large-event.jsonl"
        self._seed_restore_journal(path)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "8 KiB"):
            append_restore_execution_event(
                "RESTORE_SOURCE_PUBLISHED", "8" * 32, "9" * 32,
                "operator@example.com", "INC-JOURNAL-IDENTITY",
                details={"payload": "x" * 9000},
                journal_path=path,
            )
        self.assertEqual(path.read_bytes(), before)

    def test_journal_append_rejects_invalid_event_without_creating_file(self):
        path = Path(self.temp_dir.name) / "restore-invalid-event.jsonl"
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            append_restore_execution_event(
                "RESTORE_EXECUTION_FINISHED", "1" * 32, "2" * 32,
                "operator@example.com", "INC-JOURNAL-INVALID",
                journal_path=path,
            )
        self.assertFalse(path.exists())


    def test_recovery_audit_success_writes_verifiable_hash_chain(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "recovery-chain.jsonl"
        recover_abandoned_restore_lock("operator", "INC-CHAIN-1", confirm=True, audit_path=audit)
        with audit.open("rb") as stream:
            verified = maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(verified["record_count"], 2)
        self.assertEqual([r["sequence"] for r in records], [1, 2])
        self.assertEqual(records[1]["previous_record_sha256"], records[0]["record_sha256"])
        self.assertFalse(marker.exists())

    def test_recovery_audit_chains_a_later_recovery_after_prior_complete(self):
        audit = Path(self.temp_dir.name) / "recovery-multiple.jsonl"
        for index in range(2):
            self._write_abandoned_restore_marker(plan_id=str(index + 1) * 32)
            recover_abandoned_restore_lock("operator", f"INC-CHAIN-{index}", confirm=True, audit_path=audit)
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["sequence"] for r in records], [1, 2, 3, 4])
        self.assertEqual(records[2]["previous_record_sha256"], records[1]["record_sha256"])
        self.assertEqual(records[3]["previous_record_sha256"], records[2]["record_sha256"])

    def test_recovery_refuses_audit_record_tampering_without_removing_marker(self):
        audit = Path(self.temp_dir.name) / "recovery-tampered.jsonl"
        self._write_abandoned_restore_marker()
        recover_abandoned_restore_lock("operator", "INC-TAMPER-1", confirm=True, audit_path=audit)
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        rows[0]["operator_id"] = "tampered"
        audit.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        marker = self._write_abandoned_restore_marker("c" * 32)
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            recover_abandoned_restore_lock("operator", "INC-TAMPER-2", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())

    def test_recovery_refuses_incomplete_audit_tail_without_removing_marker(self):
        audit = Path(self.temp_dir.name) / "recovery-truncated.jsonl"
        audit.write_bytes(b'{"sequence":1')
        marker = self._write_abandoned_restore_marker()
        with self.assertRaisesRegex(RuntimeError, "incomplete final record"):
            recover_abandoned_restore_lock("operator", "INC-TRUNCATED", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertEqual(audit.read_bytes(), b'{"sequence":1')

    def test_recovery_refuses_duplicate_json_keys_in_audit(self):
        audit = Path(self.temp_dir.name) / "recovery-duplicate.jsonl"
        audit.write_text('{"sequence":1,"sequence":1}\n', encoding="utf-8")
        marker = self._write_abandoned_restore_marker()
        with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key"):
            recover_abandoned_restore_lock("operator", "INC-DUPLICATE", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())

    def test_recovery_refuses_broken_audit_hash_chain(self):
        audit = Path(self.temp_dir.name) / "recovery-broken-chain.jsonl"
        self._write_abandoned_restore_marker()
        recover_abandoned_restore_lock("operator", "INC-CHAIN-A", confirm=True, audit_path=audit)
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        rows[1]["previous_record_sha256"] = "0" * 64
        audit.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        marker = self._write_abandoned_restore_marker("d" * 32)
        with self.assertRaisesRegex(RuntimeError, "hash-chain link mismatch"):
            recover_abandoned_restore_lock("operator", "INC-CHAIN-B", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())

    def test_recovery_refuses_audit_claiming_database_mutation(self):
        audit = Path(self.temp_dir.name) / "recovery-db-claim.jsonl"
        self._write_abandoned_restore_marker()
        recover_abandoned_restore_lock("operator", "INC-DB-A", confirm=True, audit_path=audit)
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        rows[0]["database_changed"] = True
        audit.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        marker = self._write_abandoned_restore_marker("e" * 32)
        with self.assertRaisesRegex(RuntimeError, "integrity verification"):
            recover_abandoned_restore_lock("operator", "INC-DB-B", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())

    def test_recovery_refuses_unknown_audit_event(self):
        audit = Path(self.temp_dir.name) / "recovery-unknown.jsonl"
        audit.write_text('{"sequence":1,"previous_record_sha256":"' + "0" * 64 + '","event":"RESTORE_LOCK_RECOVERY_SKIPPED"}\n', encoding="utf-8")
        marker = self._write_abandoned_restore_marker()
        with self.assertRaisesRegex(RuntimeError, "unknown recovery audit event"):
            recover_abandoned_restore_lock("operator", "INC-UNKNOWN", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())

    def test_recovery_refuses_oversized_audit_before_appending(self):
        audit = Path(self.temp_dir.name) / "recovery-oversized.jsonl"
        audit.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
        marker = self._write_abandoned_restore_marker()
        with self.assertRaisesRegex(RuntimeError, "size limit"):
            recover_abandoned_restore_lock("operator", "INC-OVERSIZED", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())

    def test_recovery_audit_write_failure_preserves_abandoned_marker(self):
        audit = Path(self.temp_dir.name) / "recovery-write-failure.jsonl"
        marker = self._write_abandoned_restore_marker()
        with patch("app.maintenance.os.write", side_effect=OSError("audit storage unavailable")):
            with self.assertRaisesRegex(OSError, "audit storage unavailable"):
                recover_abandoned_restore_lock("operator", "INC-WRITE-FAIL", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertEqual(audit.read_bytes(), b"")



    def _create_recovery_audit_pair(self, audit, plan_id="a" * 32):
        self._write_abandoned_restore_marker(plan_id)
        recover_abandoned_restore_lock("operator", "INC-PAIR", confirm=True, audit_path=audit)
        return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]

    def test_recovery_journal_accepts_only_a_completed_matching_pair(self):
        audit = Path(self.temp_dir.name) / "pair-valid.jsonl"
        self._create_recovery_audit_pair(audit)
        with audit.open("rb") as stream:
            verified = maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())
        self.assertEqual(verified["record_count"], 2)

    def test_unmatched_started_blocks_next_recovery_and_preserves_new_marker(self):
        audit = Path(self.temp_dir.name) / "pair-unmatched.jsonl"
        rows = self._create_recovery_audit_pair(audit)
        audit.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
        marker = self._write_abandoned_restore_marker("c" * 32)
        before = audit.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unmatched STARTED"):
            recover_abandoned_restore_lock("operator", "INC-RETRY", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertEqual(audit.read_bytes(), before)

    def test_recovery_journal_rejects_complete_without_started(self):
        audit = Path(self.temp_dir.name) / "pair-orphan-complete.jsonl"
        rows = self._create_recovery_audit_pair(audit)
        orphan = dict(rows[1], sequence=1, previous_record_sha256="0" * 64)
        orphan["record_sha256"] = maintenance_module._restore_lock_recovery_record_digest(orphan)
        audit.write_text(json.dumps(orphan, sort_keys=True) + "\n", encoding="utf-8")
        with audit.open("rb") as stream:
            with self.assertRaisesRegex(RuntimeError, "COMPLETE event has no matching STARTED"):
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())

    def test_recovery_journal_rejects_duplicate_started_events(self):
        audit = Path(self.temp_dir.name) / "pair-duplicate-start.jsonl"
        rows = self._create_recovery_audit_pair(audit)
        second = dict(rows[0], sequence=2, previous_record_sha256=rows[0]["record_sha256"])
        second["record_sha256"] = maintenance_module._restore_lock_recovery_record_digest(second)
        audit.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in (rows[0], second)), encoding="utf-8")
        with audit.open("rb") as stream:
            with self.assertRaisesRegex(RuntimeError, "duplicate STARTED"):
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())

    def test_recovery_journal_rejects_mismatched_operator_incident_or_plan(self):
        for field, value in (("operator_id", "other"), ("incident_ref", "INC-OTHER"), ("plan_id", "f" * 32)):
            audit = Path(self.temp_dir.name) / f"pair-mismatch-{field}.jsonl"
            rows = self._create_recovery_audit_pair(audit)
            rows[1][field] = value
            rows[1]["record_sha256"] = maintenance_module._restore_lock_recovery_record_digest(rows[1])
            audit.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
            with audit.open("rb") as stream:
                with self.assertRaisesRegex(RuntimeError, "identity does not match"):
                    maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())

    def test_recovery_journal_rejects_marker_fingerprint_replacement(self):
        audit = Path(self.temp_dir.name) / "pair-marker-replacement.jsonl"
        rows = self._create_recovery_audit_pair(audit)
        rows[1]["marker_sha256"] = "f" * 64
        rows[1]["record_sha256"] = maintenance_module._restore_lock_recovery_record_digest(rows[1])
        audit.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        with audit.open("rb") as stream:
            with self.assertRaisesRegex(RuntimeError, "identity does not match"):
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())

    def test_active_restore_guard_blocks_recovery_even_with_completed_history(self):
        audit = Path(self.temp_dir.name) / "pair-active-lock.jsonl"
        self._create_recovery_audit_pair(audit)
        lock = acquire_restore_lock("d" * 32)
        try:
            with self.assertRaisesRegex(RuntimeError, "not confirmed abandoned"):
                recover_abandoned_restore_lock("operator", "INC-ACTIVE", confirm=True, audit_path=audit)
            self.assertTrue(Path(lock["lock_file"]).exists())
        finally:
            release_restore_lock(lock["lock_token"])

    def test_recovery_cleanup_preserves_source_staged_and_rollback_artifacts(self):
        audit = Path(self.temp_dir.name) / "pair-preserve-artifacts.jsonl"
        artifacts = {}
        for name in ("source-backup.sqlite3", "staged.sqlite3", "rollback.sqlite3"):
            path = Path(self.temp_dir.name) / name
            path.write_bytes((name + "-preserve").encode())
            artifacts[path] = path.read_bytes()
        self._write_abandoned_restore_marker()
        recover_abandoned_restore_lock("operator", "INC-PRESERVE", confirm=True, audit_path=audit)
        for path, content in artifacts.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertFalse(database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock").exists())

    def test_recovery_audit_requires_manual_review_after_process_restart(self):
        audit = Path(self.temp_dir.name) / "pair-process-restart.jsonl"
        rows = self._create_recovery_audit_pair(audit)
        audit.write_text(json.dumps(rows[0], sort_keys=True) + "\n", encoding="utf-8")
        before = database.DB_PATH.read_bytes()
        marker = self._write_abandoned_restore_marker("e" * 32)
        with self.assertRaisesRegex(RuntimeError, "unmatched STARTED"):
            recover_abandoned_restore_lock("operator", "INC-AFTER-RESTART", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertEqual(database.DB_PATH.read_bytes(), before)

    def test_recovery_audit_pair_verification_is_idempotent_and_read_only(self):
        audit = Path(self.temp_dir.name) / "pair-idempotent.jsonl"
        self._create_recovery_audit_pair(audit)
        before = audit.read_bytes()
        database_before = database.DB_PATH.read_bytes()
        for _ in range(3):
            with audit.open("rb") as stream:
                self.assertEqual(maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())["record_count"], 2)
        self.assertEqual(audit.read_bytes(), before)
        self.assertEqual(database.DB_PATH.read_bytes(), database_before)




    def test_partial_restore_marker_empty_file_is_manual_review_only(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b"")
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(marker.read_bytes(), before)
        self.assertTrue(result["marker_present"])

    def test_partial_restore_marker_truncated_json_is_not_repaired(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b'{"plan_id":"aaaaaaaa')
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_invalid_utf8_is_preserved(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(bytes([255, 254, 123]))
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_duplicate_json_keys_is_not_accepted(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_text('{"plan_id":"' + "a" * 32 + '","plan_id":"' + "b" * 32 + '","pid":1,"created_at":"2026-11-20T00:00:00+02:00"}', encoding="utf-8")
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_json_scalar_is_not_accepted(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_text("[]", encoding="utf-8")
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_oversize_is_not_read_or_removed(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b"x" * 65537)
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_recovery_requires_valid_abandoned_state(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b'{"plan_id":')
        before = marker.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "not confirmed abandoned"):
            recover_abandoned_restore_lock("operator@example.com", "INC-PARTIAL-01", confirm=True, audit_path=Path(self.temp_dir.name) / "partial-audit.jsonl")
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_recovery_does_not_create_audit(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b'{"plan_id":')
        audit = Path(self.temp_dir.name) / "partial-audit.jsonl"
        with self.assertRaises(RuntimeError):
            recover_abandoned_restore_lock("operator@example.com", "INC-PARTIAL-02", confirm=True, audit_path=audit)
        self.assertFalse(audit.exists())
        self.assertTrue(marker.exists())

    def test_partial_restore_marker_blocks_new_restore_lock_attempt(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b'{"interrupted":')
        before = marker.read_bytes()
        with self.assertRaises(OSError):
            acquire_restore_lock("c" * 32, timeout_seconds=0)
        self.assertEqual(marker.read_bytes(), before)

    def test_partial_restore_marker_keeps_database_and_sidecars_unchanged(self):
        marker = database.DB_PATH.with_name(database.DB_PATH.name + ".restore.lock")
        marker.write_bytes(b'{"truncated":')
        live_before = database.DB_PATH.read_bytes()
        sidecars = []
        for suffix in ("-wal", "-shm"):
            path = Path(str(database.DB_PATH) + suffix)
            if path.exists():
                sidecars.append((path, path.read_bytes()))
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertEqual(database.DB_PATH.read_bytes(), live_before)
        for path, contents in sidecars:
            self.assertEqual(path.read_bytes(), contents)
        self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main()
