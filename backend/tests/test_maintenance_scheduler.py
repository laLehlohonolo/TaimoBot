import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import HTTPException
from unittest.mock import patch

from app import database
from app.database import init_db
from app.maintenance import SAST
from app.maintenance_scheduler import run_maintenance_for_date
from app.main import MaintenanceRetryRequest, retry_maintenance
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
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups",
            return_value={"status": "BACKUP_RETENTION_COMPLETE", "deleted": []},
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

    def test_retention_cleanup_runs_only_after_backup_and_restore_verify(self):
        day = date(2026, 11, 19)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"
        call_order = []

        def fake_backup():
            call_order.append("backup")
            backup_dir.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup_file) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        def fake_restore(source, staging):
            call_order.append("restore")
            return {"status": "RESTORE_CANDIDATE_VERIFIED", "live_database_replaced": False}

        def fake_cleanup(**kwargs):
            call_order.append("retention")
            self.assertEqual(kwargs["retention_days"], 365)
            self.assertFalse(kwargs["dry_run"])
            return {"status": "BACKUP_RETENTION_COMPLETE", "deleted": []}

        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup", side_effect=fake_backup
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate", side_effect=fake_restore
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups", side_effect=fake_cleanup
        ):
            result = run_maintenance_for_date(day)

        self.assertEqual(result["status"], "MAINTENANCE_COMPLETE")
        self.assertEqual(call_order, ["backup", "restore", "retention"])
        self.assertEqual(result["backup_retention"]["status"], "BACKUP_RETENTION_COMPLETE")

    def test_blocked_retention_marks_maintenance_run_and_attempt_failed(self):
        day = date(2026, 11, 22)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"

        def fake_backup():
            backup_dir.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup_file) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup", side_effect=fake_backup
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            return_value={"status": "RESTORE_CANDIDATE_VERIFIED"},
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups",
            return_value={
                "status": "BACKUP_RETENTION_BLOCKED",
                "candidates": [],
                "deleted": [],
            },
        ):
            result = run_maintenance_for_date(day)

        self.assertEqual(result["status"], "MAINTENANCE_FAILED")
        self.assertIn("did not complete safely", result["error"])
        with database.get_connection() as connection:
            run = connection.execute(
                "SELECT status, error_summary FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
            attempt = connection.execute(
                "SELECT status, error_summary FROM maintenance_run_attempts WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(attempt["status"], "FAILED")
        self.assertIn("BACKUP_RETENTION_BLOCKED", run["error_summary"])
        self.assertIn("BACKUP_RETENTION_BLOCKED", attempt["error_summary"])

    def test_retention_cleanup_is_skipped_when_restore_verification_fails(self):
        day = date(2026, 11, 18)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"

        def fake_backup():
            backup_dir.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup_file) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        with patch(
            "app.maintenance_scheduler.BACKUP_DIR", backup_dir
        ), patch(
            "app.maintenance_scheduler.create_verified_backup",
            side_effect=fake_backup,
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            side_effect=RuntimeError("restore verification failed"),
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups"
        ) as cleanup:
            result = run_maintenance_for_date(day)

        self.assertEqual(result["status"], "MAINTENANCE_FAILED")
        cleanup.assert_not_called()
        with database.get_connection() as connection:
            run = connection.execute(
                "SELECT status FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
        self.assertEqual(run["status"], "FAILED")

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


    def test_retention_runs_only_after_backup_and_restore_verification_succeed(self):
        day = date(2026, 11, 21)
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
            side_effect=RuntimeError("restore verification failed"),
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups"
        ) as cleanup:
            failed = run_maintenance_for_date(day)
            cleanup.assert_not_called()

        self.assertEqual(failed["status"], "MAINTENANCE_FAILED")

        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup", side_effect=fake_backup
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate",
            return_value={"status": "RESTORE_CANDIDATE_VERIFIED"},
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups",
            return_value={"status": "BACKUP_RETENTION_COMPLETE", "deleted": []},
        ) as cleanup:
            completed = run_maintenance_for_date(day, retry_failed=True)

        self.assertEqual(completed["status"], "MAINTENANCE_COMPLETE")
        cleanup.assert_called_once()
        self.assertEqual(
            completed["backup_retention"]["status"],
            "BACKUP_RETENTION_COMPLETE",
        )

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
                CREATE TABLE maintenance_run_attempts (
                    attempt_token TEXT PRIMARY KEY,
                    run_date TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('RUNNING', 'COMPLETE', 'FAILED', 'SUPERSEDED')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    backup_file TEXT,
                    error_summary TEXT
                )
                """
            )
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


    def test_staging_cleanup_failure_does_not_skip_failure_audit(self):
        day = date(2026, 11, 23)
        backup_dir = self.root / "backups"
        backup_file = backup_dir / "verified.sqlite3"
        real_unlink = Path.unlink
        staging_paths = []

        def fake_backup():
            backup_dir.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(backup_file) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            return {"backup_file": str(backup_file), "status": "BACKUP_VERIFIED"}

        def fake_restore(source, staging):
            staging = Path(staging)
            staging.parent.mkdir(parents=True, exist_ok=True)
            staging.write_bytes(b"staging-candidate")
            staging_paths.append(staging)
            return {"status": "RESTORE_CANDIDATE_VERIFIED"}

        def fail_staging_unlink(path, *args, **kwargs):
            if Path(path) in staging_paths:
                raise PermissionError("staging cleanup denied")
            return real_unlink(path, *args, **kwargs)

        with patch("app.maintenance_scheduler.BACKUP_DIR", backup_dir), patch(
            "app.maintenance_scheduler.create_verified_backup", side_effect=fake_backup
        ), patch(
            "app.maintenance_scheduler.verify_restore_candidate", side_effect=fake_restore
        ), patch(
            "app.maintenance_scheduler.cleanup_expired_backups",
            return_value={"status": "BACKUP_RETENTION_COMPLETE", "deleted": []},
        ), patch.object(Path, "unlink", new=fail_staging_unlink):
            result = run_maintenance_for_date(day)

        self.assertEqual(result["status"], "MAINTENANCE_FAILED")
        self.assertIn("staging cleanup denied", result["error"])
        with database.get_connection() as connection:
            run = connection.execute(
                "SELECT status, error_summary FROM maintenance_runs WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
            attempt = connection.execute(
                "SELECT status, error_summary FROM maintenance_run_attempts WHERE run_date = ?",
                (day.isoformat(),),
            ).fetchone()
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(attempt["status"], "FAILED")
        self.assertIn("staging cleanup denied", run["error_summary"])
        self.assertIn("staging cleanup also failed", attempt["error_summary"])
        self.assertTrue(staging_paths[0].exists())

    def test_admin_retry_endpoint_requires_admin_and_reports_verified_recovery(self):
        day = date(2026, 11, 18)
        with patch(
            "app.maintenance_scheduler.create_verified_backup",
            side_effect=RuntimeError("seed failed maintenance run"),
        ):
            seeded = run_maintenance_for_date(day)
        self.assertEqual(seeded["status"], "MAINTENANCE_FAILED")

        with patch.dict("os.environ", {"TAIMOBOT_ADMIN_EMAILS": "ops@example.com"}), patch(
            "app.maintenance_scheduler.create_verified_backup"
        ) as backup:
            backup_dir = self.root / "backups"
            backup_file = backup_dir / "verified.sqlite3"

            def create_backup():
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_file.write_bytes(b"verified-test-backup")
                return {
                    "status": "BACKUP_VERIFIED",
                    "backup_file": str(backup_file),
                    "integrity_check": "ok",
                    "created_at": "2026-11-18T23:10:00+02:00",
                    "timezone": "Africa/Johannesburg",
                }

            backup.side_effect = create_backup
            with patch(
                "app.maintenance_scheduler.verify_restore_candidate",
                return_value={
                    "status": "RESTORE_CANDIDATE_VERIFIED",
                    "integrity_check": "ok",
                    "live_database_replaced": False,
                },
            ), patch(
                "app.maintenance_scheduler.cleanup_expired_backups",
                return_value={"status": "BACKUP_RETENTION_COMPLETE", "deleted": []},
            ):
                with self.assertRaises(Exception) as denied:
                    retry_maintenance(
                        MaintenanceRetryRequest(run_date=day, retry_mode="FAILED"),
                        {"email": "not-admin@example.com"},
                    )
                self.assertEqual(getattr(denied.exception, "status_code", None), 403)

                result = retry_maintenance(
                    MaintenanceRetryRequest(run_date=day, retry_mode="FAILED"),
                    {"email": "ops@example.com"},
                )

        self.assertEqual(result["status"], "MAINTENANCE_COMPLETE")
        self.assertTrue(result["operator_triggered"])
        self.assertEqual(result["backup"]["integrity_check"], "ok")
        self.assertFalse(result["restore_verification"]["live_database_replaced"])
        self.assertNotIn("backup_file", result["backup"])
        self.assertEqual(backup.call_count, 1)

    def test_admin_retry_endpoint_rejects_missing_run_and_mismatched_state(self):
        day = date(2026, 11, 19)
        admin = {"email": "ops@example.com"}
        with patch.dict("os.environ", {"TAIMOBOT_ADMIN_EMAILS": "ops@example.com"}):
            # The endpoint must safely return 404 even before the scheduler
            # has ever created its maintenance_runs table.
            with self.assertRaises(HTTPException) as missing:
                retry_maintenance(
                    MaintenanceRetryRequest(run_date=day, retry_mode="FAILED"),
                    admin,
                )
            self.assertEqual(missing.exception.status_code, 404)

            with patch(
                "app.maintenance_scheduler.create_verified_backup",
                side_effect=RuntimeError("seed failed maintenance run"),
            ):
                seeded = run_maintenance_for_date(day)
            self.assertEqual(seeded["status"], "MAINTENANCE_FAILED")

            with self.assertRaises(HTTPException) as mismatch:
                retry_maintenance(
                    MaintenanceRetryRequest(run_date=day, retry_mode="STALE_RUNNING"),
                    admin,
                )
            self.assertEqual(mismatch.exception.status_code, 409)

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


    def test_each_retry_uses_an_isolated_restore_staging_file(self):
        day = date(2026, 11, 20)
        staging_paths = []
        verification_attempts = 0

        def verify_to_distinct_staging_files(backup_path, staging_path):
            nonlocal verification_attempts
            verification_attempts += 1
            staging_paths.append(Path(staging_path))
            if verification_attempts == 1:
                raise RuntimeError("simulated restore verification failure")
            return {"status": "RESTORE_CANDIDATE_VERIFIED"}

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
            side_effect=verify_to_distinct_staging_files,
        ):
            failed = run_maintenance_for_date(day)
            retried = run_maintenance_for_date(day, retry_failed=True)

        self.assertEqual(failed["status"], "MAINTENANCE_FAILED")
        self.assertEqual(retried["status"], "MAINTENANCE_COMPLETE")
        self.assertEqual(len(staging_paths), 2)
        self.assertNotEqual(staging_paths[0], staging_paths[1])
        self.assertTrue(all(path.parent == backup_dir / "recovery-staging" for path in staging_paths))
        self.assertTrue(all(not path.exists() for path in staging_paths))


if __name__ == "__main__":
    unittest.main()
