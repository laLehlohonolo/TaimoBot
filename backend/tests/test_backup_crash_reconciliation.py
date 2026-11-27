import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app import database
from app.database import init_db
from app.maintenance import (
    create_verified_backup,
    inspect_recovery_artifact_inventory,
)


class BackupCrashReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "taimobot.sqlite3"
        init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_directory_fsync_crash_artifact_is_reported_and_preserved(self):
        root = Path(self.temp_dir.name)
        backup_dir = root / "backups"
        journal = root / "restore-execution.audit.jsonl"
        fixed_now = datetime(
            2026, 11, 26, 4, 0, tzinfo=ZoneInfo("Africa/Johannesburg")
        )

        real_fsync = os.fsync
        calls = 0

        def fail_after_atomic_publish(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                metadata = os.fstat(descriptor)
                self.assertTrue(__import__("stat").S_ISDIR(metadata.st_mode))
                raise OSError("simulated crash-window directory fsync failure")
            return real_fsync(descriptor)

        with patch(
            "app.maintenance.os.fsync",
            side_effect=fail_after_atomic_publish,
        ):
            with self.assertRaisesRegex(
                OSError, "directory fsync failure"
            ):
                create_verified_backup(
                    backup_dir=backup_dir,
                    now=fixed_now,
                )

        published = list(backup_dir.glob("taimobot-*.sqlite3"))
        self.assertEqual(len(published), 1)
        artifact = published[0]
        original_bytes = artifact.read_bytes()
        self.assertFalse(list(backup_dir.glob(".*.tmp")))

        inventory = inspect_recovery_artifact_inventory(
            backup_dir,
            journal_path=journal,
        )

        self.assertEqual(
            inventory["status"],
            "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED",
        )
        self.assertEqual(inventory["orphaned_count"], 1)
        self.assertEqual(
            inventory["orphaned_artifacts"][0]["file_name"],
            artifact.name,
        )
        record = next(
            item for item in inventory["artifacts"]
            if item["file_name"] == artifact.name
        )
        self.assertEqual(record["status"], "ORPHANED")
        self.assertEqual(record["integrity_check"], "ok")
        self.assertTrue(inventory["read_only"])
        self.assertFalse(inventory["automatic_deletion"])
        self.assertFalse(inventory["automatic_repair"])
        self.assertFalse(inventory["live_database_replaced"])
        self.assertTrue(artifact.is_file())
        self.assertEqual(artifact.read_bytes(), original_bytes)
        self.assertFalse(list(backup_dir.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
