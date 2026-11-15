import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.maintenance import (
    create_verified_backup,
    is_maintenance_window,
    verify_restore_candidate,
)


class DailyMaintenanceTests(unittest.TestCase):
    def test_maintenance_window_uses_south_african_time(self):
        zone = ZoneInfo("Africa/Johannesburg")
        self.assertTrue(is_maintenance_window(datetime(2026, 11, 15, 23, 0, tzinfo=zone)))
        self.assertTrue(is_maintenance_window(datetime(2026, 11, 15, 23, 59, tzinfo=zone)))
        self.assertFalse(is_maintenance_window(datetime(2026, 11, 16, 0, 0, tzinfo=zone)))
        self.assertFalse(is_maintenance_window(datetime(2026, 11, 15, 22, 59, tzinfo=zone)))

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
