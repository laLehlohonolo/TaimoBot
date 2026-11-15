import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app import database
from app.database import init_db
from app.maintenance_scheduler import run_maintenance_for_date
from app.safety import deactivate_emergency_stop


class MaintenanceSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_db_path = database.DB_PATH
        database.DB_PATH = self.root / "taimobot.sqlite3"
        init_db()
        deactivate_emergency_stop()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_daily_maintenance_runs_once_and_records_completion(self):
        day = date(2026, 11, 15)
        backup_dir = self.root / "backups"

        with patch("app.maintenance.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.BACKUP_DIR", backup_dir
        ):
            first = run_maintenance_for_date(day)
            second = run_maintenance_for_date(day)

        self.assertEqual(first["status"], "MAINTENANCE_COMPLETE")
        self.assertEqual(first["restore_verification"]["status"], "RESTORE_CANDIDATE_VERIFIED")
        self.assertFalse(first["live_database_replaced"])
        self.assertEqual(second["status"], "SKIPPED")
        self.assertTrue(Path(first["backup"]["backup_file"]).is_file())
        self.assertFalse((backup_dir / "recovery-staging" / f"candidate-{day.isoformat()}.sqlite3").exists())

        with sqlite3.connect(database.DB_PATH) as connection:
            row = connection.execute(
                "SELECT status, backup_file FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
        self.assertEqual(row[0], "COMPLETE")
        self.assertEqual(row[1], first["backup"]["backup_file"])


if __name__ == "__main__":
    unittest.main()
