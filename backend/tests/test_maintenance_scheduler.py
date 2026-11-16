import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app import database
from app.database import init_db
from app.maintenance import SAST
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

    def test_stale_running_claim_requires_explicit_recovery(self):
        day = date(2026, 11, 14)
        stale_started = (datetime.now(SAST) - timedelta(minutes=30)).isoformat()
        with database.get_connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    run_date TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETE', 'FAILED')),
                    started_at TEXT NOT NULL, completed_at TEXT, backup_file TEXT, error_summary TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO maintenance_runs (run_date, status, started_at) VALUES (?, 'RUNNING', ?)",
                (day.isoformat(), stale_started),
            )
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"
        def fake_backup():
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file.write_bytes(b"verified-test-backup")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}
        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup", side_effect=fake_backup
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            return_value={"status": "RESTORE_CANDIDATE_VERIFIED"},
        ):
            automatic = run_maintenance_for_date(day)
            result = run_maintenance_for_date(day, retry_stale_running=True)
        self.assertEqual(automatic["status"], "SKIPPED")
        self.assertIn("explicit stale-run recovery", automatic["reason"])
        self.assertEqual(result["status"], "MAINTENANCE_COMPLETE")

    def test_failed_run_is_not_retried_automatically(self):
        day = date(2026, 11, 13)
        with patch(
            "app.maintenance_scheduler.create_verified_backup",
            side_effect=RuntimeError("simulated backup failure"),
        ) as backup:
            first = run_maintenance_for_date(day)
            second = run_maintenance_for_date(day)
        self.assertEqual(first["status"], "MAINTENANCE_FAILED")
        self.assertEqual(second["status"], "SKIPPED")
        self.assertIn("explicit retry", second["reason"])
        self.assertEqual(backup.call_count, 1)
    def test_explicit_failed_retry_preserves_attempt_audit_history(self):
        day = date(2026, 11, 12)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"

        backup_attempts = 0

        def backup_with_one_transient_failure():
            nonlocal backup_attempts
            backup_attempts += 1
            if backup_attempts == 1:
                raise RuntimeError("temporary disk issue")
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file.write_bytes(b"verified-test-backup")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        with patch(
            "app.maintenance_scheduler.create_verified_backup",
            side_effect=backup_with_one_transient_failure,
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            return_value={"status": "RESTORE_CANDIDATE_VERIFIED"},
        ):
            failed = run_maintenance_for_date(day)
            automatic = run_maintenance_for_date(day)
            retried = run_maintenance_for_date(day, retry_failed=True)

        self.assertEqual(failed["status"], "MAINTENANCE_FAILED")
        self.assertEqual(automatic["status"], "SKIPPED")
        self.assertEqual(retried["status"], "MAINTENANCE_COMPLETE")

        with database.get_connection() as connection:
            attempts = connection.execute(
                """
                SELECT status, error_summary
                FROM maintenance_run_attempts
                WHERE run_date = ?
                ORDER BY started_at, attempt_token
                """,
                (day.isoformat(),),
            ).fetchall()
            current = connection.execute(
                "SELECT status FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()

        self.assertEqual([row["status"] for row in attempts], ["FAILED", "COMPLETE"])
        self.assertIn("temporary disk issue", attempts[0]["error_summary"])
        self.assertIsNone(attempts[1]["error_summary"])
        self.assertEqual(current["status"], "COMPLETE")

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


    def test_completion_and_audit_roll_back_together_when_audit_write_fails(self):
        day = date(2026, 11, 17)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"

        def fake_backup():
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file.write_bytes(b"verified-test-backup")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        with database.get_connection() as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_completion_audit
                BEFORE UPDATE OF status ON maintenance_run_attempts
                WHEN NEW.status = 'COMPLETE'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated audit write failure');
                END
                """
            )

        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup", side_effect=fake_backup
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            return_value={"status": "RESTORE_CANDIDATE_VERIFIED"},
        ):
            result = run_maintenance_for_date(day)

        self.assertEqual(result["status"], "MAINTENANCE_FAILED")
        self.assertIn("simulated audit write failure", result["error"])

        with database.get_connection() as connection:
            run = connection.execute(
                "SELECT status, backup_file FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
            attempt = connection.execute(
                """
                SELECT status, error_summary
                FROM maintenance_run_attempts WHERE run_date = ?
                """,
                (day.isoformat(),),
            ).fetchone()

        self.assertEqual(run["status"], "FAILED")
        self.assertIsNone(run["backup_file"])
        self.assertEqual(attempt["status"], "FAILED")
        self.assertIn("simulated audit write failure", attempt["error_summary"])

    def test_reclaimed_claim_fences_off_stale_worker_completion(self):
        day = date(2026, 11, 16)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"

        def reclaim_claim_during_backup():
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_file.write_bytes(b"verified-test-backup")
            with database.get_connection() as connection:
                connection.execute(
                    "UPDATE maintenance_runs SET run_token = ? WHERE run_date = ?",
                    ("newer-worker-token", day.isoformat()),
                )
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup",
            side_effect=reclaim_claim_during_backup,
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            return_value={"status": "RESTORE_CANDIDATE_VERIFIED"},
        ):
            result = run_maintenance_for_date(day)

        self.assertEqual(result["status"], "MAINTENANCE_SUPERSEDED")
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status, run_token FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
        self.assertEqual(row["status"], "RUNNING")
        self.assertEqual(row["run_token"], "newer-worker-token")
        with database.get_connection() as connection:
            attempt = connection.execute(
                """
                SELECT status, error_summary
                FROM maintenance_run_attempts
                WHERE run_date = ?
                """,
                (day.isoformat(),),
            ).fetchone()
        self.assertEqual(attempt["status"], "SUPERSEDED")
        self.assertIn("newer maintenance claim", attempt["error_summary"])


if __name__ == "__main__":
    unittest.main()
