import os
import sqlite3
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
    verify_restore_candidate,
)
from app import database
from app.database import init_db
from app.safety import deactivate_emergency_stop, require_trading_enabled


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


if __name__ == "__main__":
    unittest.main()
