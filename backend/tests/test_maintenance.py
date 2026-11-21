import os
import json
import hashlib
import sqlite3
import shutil
import multiprocessing
import tempfile
from contextlib import contextmanager
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
    inspect_recovery_artifact_inventory,
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

def _hold_restore_lock_until_release(ready, release):
    lock = acquire_restore_lock("6" * 32, timeout_seconds=1.0)
    ready.set()
    if not release.wait(5):
        os._exit(75)
    release_restore_lock(lock["lock_token"])


def _exit_during_restore_marker_directory_sync():
    real_fsync = os.fsync
    def crash_on_directory_sync(fd):
        if maintenance_module.stat.S_ISDIR(os.fstat(fd).st_mode):
            os._exit(73)
        return real_fsync(fd)
    os.fsync = crash_on_directory_sync
    acquire_restore_lock("4" * 32, timeout_seconds=1.0)
    os._exit(74)


def _attempt_restore_recovery(results, operator, audit):
    try:
        recover_abandoned_restore_lock(
            operator, "INC-CONCURRENT-RECOVERY", confirm=True, audit_path=audit,
        )
        results.put(("recovered", operator))
    except Exception as error:
        results.put(("refused", type(error).__name__, str(error)))

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


    def _make_inventory_backup(self, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        with sqlite3.connect(database.DB_PATH) as source, sqlite3.connect(path) as target:
            source.backup(target)
        return path

    def _create_inventory_metadata_tables(self):
        with database.get_connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    run_date TEXT PRIMARY KEY, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, completed_at TEXT,
                    backup_file TEXT, error_summary TEXT, run_token TEXT
                );
                CREATE TABLE IF NOT EXISTS maintenance_run_attempts (
                    attempt_token TEXT PRIMARY KEY, run_date TEXT NOT NULL,
                    status TEXT NOT NULL, started_at TEXT NOT NULL,
                    completed_at TEXT, backup_file TEXT, error_summary TEXT
                );
                CREATE TABLE IF NOT EXISTS maintenance_restore_plans (
                    plan_id TEXT PRIMARY KEY, backup_file TEXT NOT NULL,
                    staging_file TEXT NOT NULL, pre_restore_backup_file TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_restore_artifacts (
                    plan_id TEXT PRIMARY KEY, source_sha256 TEXT NOT NULL,
                    staged_sha256 TEXT NOT NULL, rollback_sha256 TEXT NOT NULL
                );
            """)

    def _insert_inventory_restore_plan(self, source, staged, rollback, plan_id="a" * 32):
        self._create_inventory_metadata_tables()
        hashes = [maintenance_module.sha256_file(item) for item in (source, staged, rollback)]
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO maintenance_restore_plans
                   (plan_id, backup_file, staging_file, pre_restore_backup_file,
                    status, created_at, created_by)
                   VALUES (?, ?, ?, ?, 'PREPARED', ?, 'operator')""",
                (plan_id, str(source), str(staged), str(rollback), "2026-11-20T00:00:00+02:00"),
            )
            connection.execute(
                """INSERT INTO maintenance_restore_artifacts
                   (plan_id, source_sha256, staged_sha256, rollback_sha256)
                   VALUES (?, ?, ?, ?)""",
                (plan_id, *hashes),
            )
        return hashes

    def test_recovery_artifact_inventory_reports_empty_directory(self):
        root = Path(self.temp_dir.name) / "empty-backups"
        root.mkdir()
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["status"], "RECOVERY_ARTIFACT_INVENTORY_COMPLETE")
        self.assertEqual(result["artifact_count"], 0)
        self.assertTrue(result["read_only"])

    def test_recovery_artifact_inventory_reports_valid_orphan_without_deleting(self):
        root = Path(self.temp_dir.name) / "orphan-backups"
        orphan = self._make_inventory_backup(root, "taimobot-orphan.sqlite3")
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["orphaned_count"], 1)
        self.assertEqual(result["orphaned_artifacts"][0]["file_name"], orphan.name)
        self.assertTrue(orphan.exists())

    def test_recovery_artifact_inventory_matches_maintenance_run_references(self):
        root = Path(self.temp_dir.name) / "run-backups"
        backup = self._make_inventory_backup(root, "taimobot-run.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO maintenance_runs
                   (run_date, status, started_at, completed_at, backup_file)
                   VALUES ('2026-11-20', 'COMPLETE', '2026-11-20', '2026-11-20', ?)""",
                (str(backup),),
            )
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["orphaned_count"], 0)
        self.assertEqual(result["artifacts"][0]["status"], "REFERENCED")

    def test_recovery_artifact_inventory_matches_failed_attempt_references(self):
        root = Path(self.temp_dir.name) / "attempt-backups"
        backup = self._make_inventory_backup(root, "taimobot-failed.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO maintenance_run_attempts
                   (attempt_token, run_date, status, started_at, completed_at, backup_file)
                   VALUES ('attempt-1', '2026-11-20', 'FAILED', '2026-11-20',
                           '2026-11-20', ?)""",
                (str(backup),),
            )
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["orphaned_count"], 0)
        self.assertEqual(result["artifacts"][0]["status"], "REFERENCED")

    def test_recovery_artifact_inventory_matches_all_restore_plan_roles(self):
        root = Path(self.temp_dir.name) / "plan-backups"
        staging_dir = root / "recovery-staging"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback.sqlite3")
        staged = self._make_inventory_backup(staging_dir, "taimobot-staged.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["orphaned_count"], 0)
        self.assertEqual(result["artifact_count"], 3)
        self.assertTrue(all(item["status"] == "REFERENCED" for item in result["artifacts"]))

    def test_recovery_artifact_inventory_reports_missing_maintenance_run_file(self):
        root = Path(self.temp_dir.name) / "missing-run"
        root.mkdir()
        missing = root / "taimobot-missing.sqlite3"
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO maintenance_runs
                   (run_date, status, started_at, completed_at, backup_file)
                   VALUES ('2026-11-20', 'COMPLETE', '2026-11-20', '2026-11-20', ?)""",
                (str(missing),),
            )
        result = inspect_recovery_artifact_inventory(root)
        self.assertIn("REFERENCED_RECOVERY_ARTIFACT_MISSING", result["issues"])
        self.assertEqual(result["missing_references"][0]["status"], "REFERENCED_FILE_MISSING")

    def test_recovery_artifact_inventory_reports_missing_restore_plan_files(self):
        root = Path(self.temp_dir.name) / "missing-plan"
        root.mkdir()
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                """INSERT INTO maintenance_restore_plans
                   (plan_id, backup_file, staging_file, pre_restore_backup_file,
                    status, created_at, created_by)
                   VALUES (?, ?, ?, ?, 'PREPARED', '2026-11-20', 'operator')""",
                ("b" * 32, str(root / "taimobot-source.sqlite3"),
                 str(root / "recovery-staging" / "taimobot-staged.sqlite3"),
                 str(root / "taimobot-rollback.sqlite3")),
            )
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(len(result["missing_references"]), 3)
        self.assertIn("REFERENCED_RECOVERY_ARTIFACT_MISSING", result["issues"])

    def test_recovery_artifact_inventory_detects_restore_plan_fingerprint_mismatch(self):
        root = Path(self.temp_dir.name) / "hash-mismatch"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback.sqlite3")
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-staged.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE extra_evidence (value TEXT)")
        result = inspect_recovery_artifact_inventory(root)
        self.assertIn("RESTORE_PLAN_FINGERPRINT_MISMATCH", result["issues"])
        self.assertTrue(any("RESTORE_PLAN_FINGERPRINT_MISMATCH" in item["issues"] for item in result["artifacts"]))

    def test_recovery_artifact_inventory_accepts_matching_restore_plan_fingerprints(self):
        root = Path(self.temp_dir.name) / "hash-match"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback.sqlite3")
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-staged.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        result = inspect_recovery_artifact_inventory(root)
        self.assertNotIn("RESTORE_PLAN_FINGERPRINT_MISMATCH", result["issues"])
        self.assertEqual(result["orphaned_count"], 0)

    def test_recovery_artifact_inventory_marks_corrupt_orphan_for_review(self):
        root = Path(self.temp_dir.name) / "corrupt-orphan"
        root.mkdir()
        corrupt = root / "taimobot-corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["artifacts"][0]["status"], "REVIEW_REQUIRED")
        self.assertIn("ARTIFACT_UNREADABLE", result["artifacts"][0]["issues"])
        self.assertEqual(result["orphaned_count"], 0)

    def test_recovery_artifact_inventory_marks_symlink_as_unsafe(self):
        root = Path(self.temp_dir.name) / "symlink-artifact"
        root.mkdir()
        target = self._make_inventory_backup(root, "target.sqlite3")
        link = root / "taimobot-link.sqlite3"
        link.symlink_to(target)
        result = inspect_recovery_artifact_inventory(root)
        link_record = next(item for item in result["artifacts"] if item["file_name"] == link.name)
        self.assertEqual(link_record["status"], "UNSAFE")
        self.assertTrue(target.exists())

    def test_recovery_artifact_inventory_reports_temporary_files_as_incomplete(self):
        root = Path(self.temp_dir.name) / "temporary-artifact"
        root.mkdir()
        temporary = root / ".taimobot-backup-in-progress.tmp"
        temporary.write_bytes(b"partial")
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["artifacts"][0]["status"], "INCOMPLETE")
        self.assertEqual(result["artifacts"][0]["artifact_type"], "INCOMPLETE_TEMPORARY")

    def test_recovery_artifact_inventory_detects_orphaned_staging_candidate(self):
        root = Path(self.temp_dir.name) / "staged-orphan"
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-stage.sqlite3")
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["orphaned_count"], 1)
        self.assertEqual(result["orphaned_artifacts"][0]["artifact_type"], "STAGED")
        self.assertTrue(staged.exists())

    def test_recovery_artifact_inventory_blocks_invalid_execution_journal(self):
        root = Path(self.temp_dir.name) / "invalid-journal-backups"
        root.mkdir()
        journal = Path(self.temp_dir.name) / "invalid-restore-journal.jsonl"
        journal.write_text('{"broken":true}\n', encoding="utf-8")
        result = inspect_recovery_artifact_inventory(root, journal_path=journal)
        self.assertEqual(result["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertIn("RESTORE_EXECUTION_JOURNAL_UNTRUSTED", result["issues"])

    def test_recovery_artifact_inventory_reports_journal_plan_without_database_plan(self):
        root = Path(self.temp_dir.name) / "journal-plan"
        root.mkdir()
        journal = Path(self.temp_dir.name) / "journal-plan.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "c" * 32, "d" * 32,
            "operator", "incident-1", details={}, journal_path=journal,
        )
        result = inspect_recovery_artifact_inventory(root, journal_path=journal)
        self.assertIn("JOURNAL_PLAN_WITHOUT_DATABASE_PLAN", result["issues"])
        self.assertEqual(result["missing_references"][0]["status"], "DATABASE_PLAN_MISSING")

    def test_recovery_artifact_inventory_blocks_incomplete_metadata_schema(self):
        root = Path(self.temp_dir.name) / "bad-metadata"
        root.mkdir()
        with database.get_connection() as connection:
            connection.execute("CREATE TABLE maintenance_runs (run_date TEXT)")
        result = inspect_recovery_artifact_inventory(root)
        self.assertEqual(result["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertIn("MAINTENANCE_METADATA_UNREADABLE", result["issues"])

    def test_recovery_artifact_inventory_detects_hard_link_aliases(self):
        root = Path(self.temp_dir.name) / "hardlink-inventory"
        first = self._make_inventory_backup(root, "taimobot-first.sqlite3")
        second = root / "taimobot-second.sqlite3"
        os.link(first, second)
        result = inspect_recovery_artifact_inventory(root)
        self.assertIn("DUPLICATE_ARTIFACT_IDENTITY", result["issues"])
        self.assertTrue(any("HARD_LINK_ALIAS" in item["issues"] for item in result["artifacts"]))

    def test_recovery_artifact_inventory_detects_journal_append_during_scan(self):
        root = Path(self.temp_dir.name) / "journal-race"
        self._make_inventory_backup(root, "taimobot-race.sqlite3")
        journal = Path(self.temp_dir.name) / "journal-race.jsonl"
        original_hash = maintenance_module.sha256_file
        appended = {"done": False}
        def hash_and_append(path):
            digest = original_hash(path)
            if not appended["done"]:
                appended["done"] = True
                append_restore_execution_event(
                    "RESTORE_EXECUTION_STARTED", "e" * 32, "f" * 32,
                    "operator", "incident-race", details={}, journal_path=journal,
                )
            return digest
        with patch("app.maintenance.sha256_file", side_effect=hash_and_append):
            result = inspect_recovery_artifact_inventory(root, journal_path=journal)
        self.assertIn("RESTORE_EXECUTION_JOURNAL_CHANGED_DURING_INVENTORY", result["issues"])

    def test_recovery_artifact_inventory_protects_live_database_path(self):
        root = Path(self.temp_dir.name) / "live-db-inventory"
        root.mkdir()
        live = root / "taimobot-live.sqlite3"
        old_db = database.DB_PATH
        database.DB_PATH = live
        try:
            init_db()
            result = inspect_recovery_artifact_inventory(root)
            self.assertEqual(result["artifacts"][0]["status"], "PROTECTED_LIVE_DATABASE")
            self.assertTrue(live.exists())
        finally:
            database.DB_PATH = old_db

    def test_recovery_artifact_inventory_does_not_mutate_files_database_or_journal(self):
        root = Path(self.temp_dir.name) / "read-only-inventory"
        backup = self._make_inventory_backup(root, "taimobot-read-only.sqlite3")
        journal = Path(self.temp_dir.name) / "read-only-journal.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "1" * 32, "2" * 32,
            "operator", "incident-read-only", details={}, journal_path=journal,
        )
        before_file = backup.read_bytes()
        before_db = database.DB_PATH.read_bytes()
        before_journal = journal.read_bytes()
        inspect_recovery_artifact_inventory(root, journal_path=journal)
        self.assertEqual(backup.read_bytes(), before_file)
        self.assertEqual(database.DB_PATH.read_bytes(), before_db)
        self.assertEqual(journal.read_bytes(), before_journal)

    def test_recovery_artifact_inventory_blocks_symlinked_backup_directory(self):
        real = Path(self.temp_dir.name) / "real-backups"
        real.mkdir()
        link = Path(self.temp_dir.name) / "linked-backups"
        link.symlink_to(real, target_is_directory=True)
        result = inspect_recovery_artifact_inventory(link)
        self.assertEqual(result["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertIn("BACKUP_DIRECTORY_UNSAFE", result["issues"])


    def _call_recovery_inventory_api(self, user=None, admin_emails="ops@example.com", method="get"):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.auth_routes import current_user

        previous = dict(app.dependency_overrides)
        if user is not None:
            app.dependency_overrides[current_user] = lambda: user
        try:
            with patch.dict(os.environ, {"TAIMOBOT_ADMIN_EMAILS": admin_emails}):
                client = TestClient(app)
                response = getattr(client, method)("/maintenance/recovery-artifact-inventory")
                client.close()
                return response
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_recovery_inventory_api_requires_authentication(self):
        response = self._call_recovery_inventory_api()
        self.assertEqual(response.status_code, 401)

    def test_recovery_inventory_api_rejects_invalid_bearer_token(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        response = client.get(
            "/maintenance/recovery-artifact-inventory",
            headers={"Authorization": "Bearer invalid-session-token"},
        )
        client.close()
        self.assertEqual(response.status_code, 401)

    def test_recovery_inventory_api_rejects_non_admin_user(self):
        response = self._call_recovery_inventory_api(
            {"user_id": "user-1", "email": "trader@example.com"}
        )
        self.assertEqual(response.status_code, 403)

    def test_recovery_inventory_api_requires_explicit_admin_allowlist(self):
        response = self._call_recovery_inventory_api(
            {"user_id": "admin-1", "email": "ops@example.com"},
            admin_emails="",
        )
        self.assertEqual(response.status_code, 403)

    def test_recovery_inventory_api_normalizes_admin_email_case(self):
        with patch("app.main.inspect_recovery_artifact_inventory", return_value={
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }) as inventory:
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "OPS@EXAMPLE.COM"},
                admin_emails=" ops@example.com ",
            )
        self.assertEqual(response.status_code, 200)
        inventory.assert_called_once_with()

    def test_recovery_inventory_api_calls_inventory_once_for_admin(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload) as inventory:
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        inventory.assert_called_once_with()

    def test_recovery_inventory_api_does_not_call_inventory_for_non_admin(self):
        with patch("app.main.inspect_recovery_artifact_inventory") as inventory:
            response = self._call_recovery_inventory_api(
                {"user_id": "user-1", "email": "trader@example.com"}
            )
        self.assertEqual(response.status_code, 403)
        inventory.assert_not_called()

    def test_recovery_inventory_api_returns_inventory_status(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": ["REFERENCED_RECOVERY_ARTIFACT_MISSING"],
            "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED")

    def test_recovery_inventory_api_preserves_orphan_details(self):
        orphan = {"file_name": "taimobot-orphan.sqlite3", "status": "ORPHANED"}
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED",
            "artifacts": [orphan], "orphaned_artifacts": [orphan],
            "missing_references": [], "issues": [], "read_only": True,
            "automatic_deletion": False, "automatic_repair": False,
            "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.json()["orphaned_artifacts"], [orphan])

    def test_recovery_inventory_api_preserves_missing_reference_details(self):
        missing = {"path": "/backup/missing.sqlite3", "status": "REFERENCED_FILE_MISSING"}
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED",
            "artifacts": [], "orphaned_artifacts": [],
            "missing_references": [missing], "issues": [], "read_only": True,
            "automatic_deletion": False, "automatic_repair": False,
            "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.json()["missing_references"], [missing])

    def test_recovery_inventory_api_preserves_diagnostic_issues(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": ["RESTORE_EXECUTION_JOURNAL_UNTRUSTED"],
            "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.json()["issues"], ["RESTORE_EXECUTION_JOURNAL_UNTRUSTED"])

    def test_recovery_inventory_api_exposes_read_only_contract(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        body = response.json()
        self.assertTrue(body["read_only"])
        self.assertFalse(body["automatic_deletion"])
        self.assertFalse(body["automatic_repair"])
        self.assertFalse(body["live_database_replaced"])

    def test_recovery_inventory_api_does_not_expose_restore_execution_controls(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        body = response.json()
        for forbidden in ("delete", "repair", "restore", "execute", "approval_token"):
            self.assertNotIn(forbidden, body)

    def test_recovery_inventory_api_does_not_trigger_backup_creation(self):
        from app import main as main_module
        with patch("app.main.inspect_recovery_artifact_inventory", return_value={
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }), patch.object(main_module, "create_verified_backup") as create_backup:
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        create_backup.assert_not_called()

    def test_recovery_inventory_api_does_not_trigger_backup_cleanup(self):
        with patch("app.main.inspect_recovery_artifact_inventory", return_value={
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }), patch("app.maintenance.cleanup_expired_backups") as cleanup:
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        cleanup.assert_not_called()

    def test_recovery_inventory_api_does_not_trigger_restore_recovery(self):
        with patch("app.main.inspect_recovery_artifact_inventory", return_value={
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }), patch("app.maintenance.recover_abandoned_restore_lock") as recover:
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        recover.assert_not_called()

    def test_recovery_inventory_api_repeated_gets_are_read_only(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload) as inventory:
            first = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
            second = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(inventory.call_count, 2)

    def test_recovery_inventory_api_rejects_post_method(self):
        response = self._call_recovery_inventory_api(
            {"user_id": "admin-1", "email": "ops@example.com"}, method="post"
        )
        self.assertEqual(response.status_code, 405)

    def test_recovery_inventory_api_returns_blocked_diagnostic_without_mutation(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_BLOCKED",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": ["MAINTENANCE_METADATA_UNREADABLE"], "read_only": True,
            "automatic_deletion": False, "automatic_repair": False,
            "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload), patch(
            "app.main.create_verified_backup"
        ) as backup:
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        backup.assert_not_called()

    def test_recovery_inventory_api_returns_json_and_expected_content_type(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertIsInstance(response.json(), dict)

    def test_recovery_inventory_api_does_not_change_maintenance_database(self):
        before = database.DB_PATH.read_bytes()
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(database.DB_PATH.read_bytes(), before)

    def test_recovery_inventory_api_response_does_not_claim_live_database_replacement(self):
        payload = {
            "status": "RECOVERY_ARTIFACT_INVENTORY_COMPLETE",
            "artifacts": [], "orphaned_artifacts": [], "missing_references": [],
            "issues": [], "read_only": True, "automatic_deletion": False,
            "automatic_repair": False, "live_database_replaced": False,
        }
        with patch("app.main.inspect_recovery_artifact_inventory", return_value=payload):
            response = self._call_recovery_inventory_api(
                {"user_id": "admin-1", "email": "ops@example.com"}
            )
        self.assertIs(response.json()["live_database_replaced"], False)

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
            "RESTORE_SOURCE_PUBLISHED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-INCOMPLETE",
            details={"artifacts": {
                "staged_candidate_sha256": hashes["staged_candidate_sha256"],
                "published_database_sha256": hashes["live_database_sha256"],
            }}, journal_path=journal,
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
            "RESTORE_SOURCE_PUBLISHED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-CONSISTENT",
            details={"artifacts": {
                "staged_candidate_sha256": hashes["staged_candidate_sha256"],
                "published_database_sha256": live_hash,
            }}, journal_path=journal,
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
            "RESTORE_SOURCE_PUBLISHED", plan_id, token,
            "operator@example.com", "INC-EVIDENCE-TERMINAL",
            details={"artifacts": {
                "staged_candidate_sha256": hashes["staged_candidate_sha256"],
                "published_database_sha256": live_hash,
            }}, journal_path=journal,
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



    def _assert_concurrent_audit_append_trial(self, trial):
        root = Path(self.temp_dir.name) / ("parallel-audit-" + str(trial))
        root.mkdir()
        db_paths = [root / "operator-a.sqlite3", root / "operator-b.sqlite3"]
        for index, db_path in enumerate(db_paths):
            marker_path = db_path.with_name(db_path.name + ".restore.lock")
            marker_path.write_text(json.dumps({
                "plan_id": str(index + 1) * 32,
                "lock_token": str(index + 3) * 32,
                "pid": 999999,
                "created_at": "2026-11-20T10:00:00+02:00",
            }), encoding="utf-8")
        audit = root / "shared-recovery.audit.jsonl"
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()

        def recover_worker(db_path, incident):
            try:
                database.DB_PATH = Path(db_path)
                start.wait(5)
                outcome = recover_abandoned_restore_lock(
                    "operator-" + incident, incident, confirm=True, audit_path=audit
                )
                results.put(("ok", outcome.get("status")))
            except BaseException as error:
                results.put(("error", type(error).__name__ + ": " + str(error)))

        workers = [
            context.Process(target=recover_worker, args=(str(db_paths[0]), "PAR-" + str(trial) + "-A")),
            context.Process(target=recover_worker, args=(str(db_paths[1]), "PAR-" + str(trial) + "-B")),
        ]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(15)
        self.assertTrue(all(not worker.is_alive() for worker in workers), "recovery worker timed out")
        self.assertEqual([worker.exitcode for worker in workers], [0, 0])
        outcomes = [results.get(timeout=2) for _ in workers]
        self.assertEqual(sorted(outcomes), [("ok", "ABANDONED_RESTORE_LOCK_RECOVERED"), ("ok", "ABANDONED_RESTORE_LOCK_RECOVERED")])
        self.assertTrue(all(not db.with_name(db.name + ".restore.lock").exists() for db in db_paths))
        with audit.open("rb") as stream:
            verified = maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(verified["record_count"], 4)
        self.assertEqual([record["sequence"] for record in records], [1, 2, 3, 4])
        self.assertEqual(
            [record["event"] for record in records],
            ["RESTORE_LOCK_RECOVERY_STARTED", "RESTORE_LOCK_RECOVERY_COMPLETE"] * 2,
        )
        for index in (0, 2):
            self.assertEqual(records[index + 1]["previous_record_sha256"], records[index]["record_sha256"])
            self.assertEqual(records[index + 1]["incident_ref"], records[index]["incident_ref"])
        self.assertEqual(records[2]["previous_record_sha256"], records[1]["record_sha256"])

    def test_concurrent_recovery_audit_appends_trial_01(self):
        self._assert_concurrent_audit_append_trial(1)

    def test_concurrent_recovery_audit_appends_trial_02(self):
        self._assert_concurrent_audit_append_trial(2)

    def test_concurrent_recovery_audit_appends_trial_03(self):
        self._assert_concurrent_audit_append_trial(3)

    def test_concurrent_recovery_audit_appends_trial_04(self):
        self._assert_concurrent_audit_append_trial(4)

    def test_concurrent_recovery_audit_appends_trial_05(self):
        self._assert_concurrent_audit_append_trial(5)

    def test_concurrent_recovery_audit_appends_trial_06(self):
        self._assert_concurrent_audit_append_trial(6)

    def test_concurrent_recovery_audit_appends_trial_07(self):
        self._assert_concurrent_audit_append_trial(7)

    def test_concurrent_recovery_audit_appends_trial_08(self):
        self._assert_concurrent_audit_append_trial(8)

    def test_concurrent_recovery_audit_appends_trial_09(self):
        self._assert_concurrent_audit_append_trial(9)

    def test_concurrent_recovery_audit_appends_trial_10(self):
        self._assert_concurrent_audit_append_trial(10)

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



    def _fail_restore_release_directory_sync_once(self, token):
        with patch("app.maintenance.os.fsync", side_effect=OSError("simulated release directory sync failure")):
            with self.assertRaisesRegex(RuntimeError, "marker removal was not confirmed durable"):
                release_restore_lock(token)

    def test_restore_release_retry_finishes_directory_sync_after_prior_unlink(self):
        lock = acquire_restore_lock("1" * 32)
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        self.assertFalse(Path(lock["lock_file"]).exists())
        with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
            database.get_connection()
        released = release_restore_lock(lock["lock_token"])
        self.assertEqual(released["status"], "RESTORE_LOCK_RELEASED")
        self.assertTrue(released["directory_synced"])
        self.assertTrue(released["resumed_after_unlink"])
        self.assertFalse(is_restore_lock_active())

    def test_restore_release_retry_keeps_guard_when_directory_sync_fails_again(self):
        lock = acquire_restore_lock("2" * 32)
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        with patch("app.maintenance.os.fsync", side_effect=OSError("second directory sync failure")):
            with self.assertRaisesRegex(RuntimeError, "marker removal was not confirmed durable"):
                release_restore_lock(lock["lock_token"])
        with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
            database.get_connection()
        released = release_restore_lock(lock["lock_token"])
        self.assertTrue(released["resumed_after_unlink"])
        self.assertFalse(is_restore_lock_active())

    def test_restore_release_retry_never_removes_replacement_marker(self):
        lock = acquire_restore_lock("3" * 32)
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        marker = Path(lock["lock_file"])
        replacement = json.dumps({
            "plan_id": "4" * 32, "lock_token": "5" * 32,
            "pid": 999999, "created_at": "2026-11-20T00:00:00+02:00",
        })
        marker.write_text(replacement, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            release_restore_lock(lock["lock_token"])
        self.assertEqual(marker.read_text(encoding="utf-8"), replacement)
        self.assertTrue(is_restore_lock_active())
        marker.unlink()
        release_restore_lock(lock["lock_token"])

    def test_restore_release_retry_rejects_symlink_replacement(self):
        lock = acquire_restore_lock("6" * 32)
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        marker = Path(lock["lock_file"])
        target = Path(self.temp_dir.name) / "marker-target"
        target.write_text("preserve-target", encoding="utf-8")
        marker.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "release could not be completed safely"):
            release_restore_lock(lock["lock_token"])
        self.assertTrue(marker.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "preserve-target")
        self.assertTrue(is_restore_lock_active())
        marker.unlink()
        release_restore_lock(lock["lock_token"])

    def test_restore_release_retry_requires_original_in_process_guard_token(self):
        lock = acquire_restore_lock("7" * 32)
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        with self.assertRaisesRegex(RuntimeError, "ownership is unavailable"):
            release_restore_lock("8" * 32)
        with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
            database.get_connection()
        release_restore_lock(lock["lock_token"])

    def test_restore_release_without_guard_never_releases_missing_marker(self):
        with self.assertRaisesRegex(RuntimeError, "ownership is unavailable"):
            release_restore_lock("9" * 32)
        self.assertFalse(maintenance_module.restore_lock_path().exists())

    def test_restore_release_rejects_duplicate_json_keys_without_unlinking_marker(self):
        lock = acquire_restore_lock("a" * 32)
        marker = Path(lock["lock_file"])
        original = marker.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        tampered = json.dumps(payload)[:-1] + ',"lock_token":"' + "b" * 32 + '"}'
        marker.write_text(tampered, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "metadata is unreadable"):
            release_restore_lock(lock["lock_token"])
        self.assertEqual(marker.read_text(encoding="utf-8"), tampered)
        self.assertTrue(is_restore_lock_active())
        marker.write_bytes(original)
        release_restore_lock(lock["lock_token"])

    def test_restore_release_rejects_non_object_json_without_unlinking_marker(self):
        lock = acquire_restore_lock("c" * 32)
        marker = Path(lock["lock_file"])
        original = marker.read_bytes()
        marker.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
            release_restore_lock(lock["lock_token"])
        self.assertEqual(marker.read_text(encoding="utf-8"), "[]")
        self.assertTrue(is_restore_lock_active())
        marker.write_bytes(original)
        release_restore_lock(lock["lock_token"])

    def test_restore_release_retry_syncs_parent_directory_before_guard_release(self):
        lock = acquire_restore_lock("d" * 32)
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        real_fsync = os.fsync
        observed = {"directory": False}
        def verify_sync(fd):
            if maintenance_module.stat.S_ISDIR(os.fstat(fd).st_mode):
                with self.assertRaisesRegex(sqlite3.OperationalError, "quiescing"):
                    database.get_connection()
                observed["directory"] = True
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=verify_sync):
            released = release_restore_lock(lock["lock_token"])
        self.assertTrue(observed["directory"])
        self.assertTrue(released["resumed_after_unlink"])
        self.assertFalse(is_restore_lock_active())

    def test_restore_release_retry_preserves_database_and_sqlite_sidecars(self):
        lock = acquire_restore_lock("e" * 32)
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"wal-preserve-on-release-retry")
        shm.write_bytes(b"shm-preserve-on-release-retry")
        database_before = database.DB_PATH.read_bytes()
        self._fail_restore_release_directory_sync_once(lock["lock_token"])
        release_restore_lock(lock["lock_token"])
        self.assertEqual(database.DB_PATH.read_bytes(), database_before)
        self.assertEqual(wal.read_bytes(), b"wal-preserve-on-release-retry")
        self.assertEqual(shm.read_bytes(), b"shm-preserve-on-release-retry")
        self.assertFalse(is_restore_lock_active())



    def test_recovery_syncs_separate_audit_directory_before_marker_unlink(self):
        marker = self._write_abandoned_restore_marker()
        audit_dir = Path(self.temp_dir.name) / "separate-audit"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        events = []
        real_fsync = os.fsync
        audit_dir_stat = audit_dir.stat()
        real_unlink = Path.unlink
        def trace_fsync(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (audit_dir_stat.st_dev, audit_dir_stat.st_ino):
                events.append("audit-directory-fsync")
            return real_fsync(fd)
        def trace_unlink(path, *args, **kwargs):
            if path == marker:
                events.append("marker-unlink")
            return real_unlink(path, *args, **kwargs)
        with patch("app.maintenance.os.fsync", side_effect=trace_fsync), patch.object(Path, "unlink", trace_unlink):
            recover_abandoned_restore_lock("operator", "INC-AUDIT-DIR-ORDER", confirm=True, audit_path=audit)
        self.assertLess(events.index("audit-directory-fsync"), events.index("marker-unlink"))
        self.assertFalse(marker.exists())

    def test_recovery_audit_directory_sync_failure_preserves_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit_dir = Path(self.temp_dir.name) / "separate-audit"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        before = database.DB_PATH.read_bytes()
        directory = audit_dir.stat()
        real_fsync = os.fsync
        def fail_audit_directory(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (directory.st_dev, directory.st_ino):
                raise OSError("simulated audit directory sync failure")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_audit_directory):
            with self.assertRaisesRegex(OSError, "audit directory sync failure"):
                recover_abandoned_restore_lock("operator", "INC-AUDIT-DIR-FAIL", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertEqual(audit.read_bytes(), b"")
        self.assertEqual(database.DB_PATH.read_bytes(), before)

    def test_recovery_audit_directory_sync_failure_does_not_write_started_event(self):
        marker = self._write_abandoned_restore_marker()
        audit_dir = Path(self.temp_dir.name) / "separate-audit"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        directory = audit_dir.stat()
        real_fsync = os.fsync
        def fail_audit_directory(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (directory.st_dev, directory.st_ino):
                raise OSError("audit directory not durable")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_audit_directory):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-NO-START", confirm=True, audit_path=audit)
        self.assertEqual(audit.read_bytes(), b"")
        self.assertTrue(marker.exists())

    def test_recovery_started_audit_fsync_failure_preserves_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "started-fsync.jsonl"
        real_fsync = os.fsync
        def fail_audit_file(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISREG(info.st_mode) and os.path.samefile(f"/proc/self/fd/{fd}", audit):
                raise OSError("started audit fsync failure")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_audit_file):
            with self.assertRaisesRegex(OSError, "started audit fsync failure"):
                recover_abandoned_restore_lock("operator", "INC-START-FSYNC", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertTrue(audit.exists())

    def test_recovery_marker_directory_sync_failure_leaves_pending_started_for_review(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "marker-dir-fsync.jsonl"
        marker_dir = marker.parent.stat()
        real_fsync = os.fsync
        def fail_marker_directory(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (marker_dir.st_dev, marker_dir.st_ino):
                # The audit file is in a distinct subdirectory; fail only the marker directory sync.
                raise OSError("marker directory sync failure")
            return real_fsync(fd)
        audit_dir = Path(self.temp_dir.name) / "audit-subdir"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        with patch("app.maintenance.os.fsync", side_effect=fail_marker_directory):
            with self.assertRaisesRegex(OSError, "marker directory sync failure"):
                recover_abandoned_restore_lock("operator", "INC-MARKER-DIR", confirm=True, audit_path=audit)
        self.assertFalse(marker.exists())
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["event"] for row in records], ["RESTORE_LOCK_RECOVERY_STARTED"])
        with audit.open("rb") as stream:
            with self.assertRaisesRegex(RuntimeError, "unmatched STARTED"):
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())

    def test_recovery_marker_unlink_failure_leaves_started_audit_and_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "unlink-failure.jsonl"
        real_unlink = Path.unlink
        def fail_marker_unlink(path, *args, **kwargs):
            if path == marker:
                raise OSError("simulated marker unlink failure")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_marker_unlink):
            with self.assertRaisesRegex(OSError, "marker unlink failure"):
                recover_abandoned_restore_lock("operator", "INC-UNLINK-FAIL", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["event"] for row in records], ["RESTORE_LOCK_RECOVERY_STARTED"])

    def test_recovery_after_unlink_before_complete_is_not_automatically_resumed(self):
        marker = self._write_abandoned_restore_marker()
        audit_dir = Path(self.temp_dir.name) / "audit-subdir"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        marker_dir = marker.parent.stat()
        real_fsync = os.fsync
        def fail_marker_dir_sync(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (marker_dir.st_dev, marker_dir.st_ino):
                raise OSError("simulate host interruption boundary")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_marker_dir_sync):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-INTERRUPTED", confirm=True, audit_path=audit)
        before_audit = audit.read_bytes()
        before_db = database.DB_PATH.read_bytes()
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(RuntimeError, "not confirmed abandoned"):
            recover_abandoned_restore_lock("operator", "INC-RETRY", confirm=True, audit_path=audit)
        self.assertEqual(audit.read_bytes(), before_audit)
        self.assertEqual(database.DB_PATH.read_bytes(), before_db)

    def test_recovery_pending_started_blocks_replacement_marker_after_interruption(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "pending-replacement.jsonl"
        real_unlink = Path.unlink
        def fail_unlink(path, *args, **kwargs):
            if path == marker:
                raise OSError("interrupted before marker removal")
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_unlink):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-PENDING", confirm=True, audit_path=audit)
        pending = audit.read_bytes()
        marker.write_text(json.dumps({
            "plan_id": "c" * 32, "pid": 999998,
            "created_at": "2026-11-20T00:00:00+02:00", "lock_token": "d" * 32,
        }), encoding="utf-8")
        replacement = marker.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unmatched STARTED"):
            recover_abandoned_restore_lock("operator", "INC-REPLACEMENT", confirm=True, audit_path=audit)
        self.assertEqual(marker.read_bytes(), replacement)
        self.assertEqual(audit.read_bytes(), pending)

    def test_recovery_crash_boundaries_preserve_database_and_sidecars(self):
        marker = self._write_abandoned_restore_marker()
        audit_dir = Path(self.temp_dir.name) / "audit-subdir"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"wal-recovery-boundary")
        shm.write_bytes(b"shm-recovery-boundary")
        database_before = database.DB_PATH.read_bytes()
        marker_dir = marker.parent.stat()
        real_fsync = os.fsync
        def fail_marker_dir_sync(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (marker_dir.st_dev, marker_dir.st_ino):
                raise OSError("crash boundary")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_marker_dir_sync):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-PRESERVE-DB", confirm=True, audit_path=audit)
        self.assertEqual(database.DB_PATH.read_bytes(), database_before)
        self.assertEqual(wal.read_bytes(), b"wal-recovery-boundary")
        self.assertEqual(shm.read_bytes(), b"shm-recovery-boundary")

    def test_recovery_crash_boundary_preserves_backup_and_restore_artifacts(self):
        marker = self._write_abandoned_restore_marker()
        audit_dir = Path(self.temp_dir.name) / "audit-subdir"
        audit_dir.mkdir()
        audit = audit_dir / "recovery.jsonl"
        artifacts = {}
        for name in ("source-backup.sqlite3", "staged.sqlite3", "rollback.sqlite3"):
            path = Path(self.temp_dir.name) / name
            path.write_bytes((name + "-unchanged").encode())
            artifacts[path] = path.read_bytes()
        marker_dir = marker.parent.stat()
        real_fsync = os.fsync
        def fail_marker_dir_sync(fd):
            info = os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (marker_dir.st_dev, marker_dir.st_ino):
                raise OSError("crash boundary")
            return real_fsync(fd)
        with patch("app.maintenance.os.fsync", side_effect=fail_marker_dir_sync):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-PRESERVE-ARTIFACTS", confirm=True, audit_path=audit)
        for path, content in artifacts.items():
            self.assertEqual(path.read_bytes(), content)



    def test_dead_child_process_restore_lock_requires_explicit_recovery(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=_crash_after_acquiring_restore_lock, args=(ready,))
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exitcode, 0)
        self.assertTrue(ready.is_set())
        marker = maintenance_module.restore_lock_path()
        self.assertTrue(marker.exists())
        self.assertEqual(inspect_restore_lock()["status"], "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW")
        audit = Path(self.temp_dir.name) / "dead-child.audit.jsonl"
        result = recover_abandoned_restore_lock("operator", "INC-DEAD-CHILD", confirm=True, audit_path=audit)
        self.assertEqual(result["status"], "ABANDONED_RESTORE_LOCK_RECOVERED")
        self.assertFalse(marker.exists())
        self.assertEqual([json.loads(line)["event"] for line in audit.read_text().splitlines()], ["RESTORE_LOCK_RECOVERY_STARTED", "RESTORE_LOCK_RECOVERY_COMPLETE"])

    def test_sigkill_releases_kernel_guard_but_keeps_marker_for_review(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        worker = context.Process(target=_hold_restore_lock_until_release, args=(ready, release))
        worker.start()
        self.assertTrue(ready.wait(5))
        marker = maintenance_module.restore_lock_path()
        self.assertTrue(marker.exists())
        worker.kill()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(marker.exists())
        self.assertEqual(inspect_restore_lock()["status"], "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW")
        with self.assertRaises(FileExistsError):
            acquire_restore_lock("8" * 32, timeout_seconds=0.1)

    def test_live_child_guard_blocks_operator_recovery(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        worker = context.Process(target=_hold_restore_lock_until_release, args=(ready, release))
        worker.start()
        audit = Path(self.temp_dir.name) / "live-child.audit.jsonl"
        try:
            self.assertTrue(ready.wait(5))
            with self.assertRaisesRegex(RuntimeError, "not confirmed abandoned|guard is active"):
                recover_abandoned_restore_lock("operator", "INC-LIVE-CHILD", confirm=True, audit_path=audit)
            self.assertTrue(maintenance_module.restore_lock_path().exists())
            self.assertFalse(audit.exists())
        finally:
            release.set()
            worker.join(5)
            if worker.is_alive():
                worker.terminate()
                worker.join(5)
        self.assertEqual(worker.exitcode, 0)

    def test_child_process_normal_release_removes_marker_and_unlocks_database(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()
        worker = context.Process(target=_hold_restore_lock_until_release, args=(ready, release))
        worker.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_ACTIVE")
            release.set()
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(worker.exitcode, 0)
            self.assertFalse(maintenance_module.restore_lock_path().exists())
            self.assertEqual(inspect_restore_lock()["status"], "NO_RESTORE_LOCK")
            connection = database.get_connection()
            connection.close()
        finally:
            if worker.is_alive():
                release.set()
                worker.terminate()
                worker.join(5)

    def test_crash_after_marker_file_sync_leaves_recoverable_operator_review_state(self):
        context = multiprocessing.get_context("fork")
        worker = context.Process(target=_exit_during_restore_marker_directory_sync)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker.exitcode, 73)
        marker = maintenance_module.restore_lock_path()
        self.assertTrue(marker.exists())
        self.assertEqual(inspect_restore_lock()["status"], "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW")
        audit = Path(self.temp_dir.name) / "after-publish-crash.audit.jsonl"
        result = recover_abandoned_restore_lock("operator", "INC-PUBLISH-CRASH", confirm=True, audit_path=audit)
        self.assertTrue(result["marker_removed"])
        self.assertEqual(len(audit.read_text().splitlines()), 2)

    def test_process_death_recovery_writes_verifiable_hash_chained_audit(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=_crash_after_acquiring_restore_lock, args=(ready,))
        worker.start()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        audit = Path(self.temp_dir.name) / "death-chain.audit.jsonl"
        recover_abandoned_restore_lock("operator", "INC-DEATH-CHAIN", confirm=True, audit_path=audit)
        with audit.open("rb") as stream:
            verified = maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())
        self.assertEqual(verified["record_count"], 2)

    def test_process_death_recovery_preserves_database_and_sqlite_sidecars(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=_crash_after_acquiring_restore_lock, args=(ready,))
        worker.start()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"crash-recovery-wal")
        shm.write_bytes(b"crash-recovery-shm")
        db_before = database.DB_PATH.read_bytes()
        recover_abandoned_restore_lock("operator", "INC-DEATH-SIDECARS", confirm=True, audit_path=Path(self.temp_dir.name) / "sidecars.audit.jsonl")
        self.assertEqual(database.DB_PATH.read_bytes(), db_before)
        self.assertEqual(wal.read_bytes(), b"crash-recovery-wal")
        self.assertEqual(shm.read_bytes(), b"crash-recovery-shm")

    def test_process_death_recovery_preserves_backup_staged_and_rollback_files(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=_crash_after_acquiring_restore_lock, args=(ready,))
        worker.start()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        artifacts = {}
        for name in ("dead-source.sqlite3", "dead-staged.sqlite3", "dead-rollback.sqlite3"):
            path = Path(self.temp_dir.name) / name
            path.write_bytes(name.encode())
            artifacts[path] = path.read_bytes()
        recover_abandoned_restore_lock("operator", "INC-DEATH-ARTIFACTS", confirm=True, audit_path=Path(self.temp_dir.name) / "artifacts.audit.jsonl")
        for path, expected in artifacts.items():
            self.assertEqual(path.read_bytes(), expected)

    def test_two_operator_recovery_processes_cannot_both_recover_same_marker(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=_crash_after_acquiring_restore_lock, args=(ready,))
        worker.start()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        audit = Path(self.temp_dir.name) / "concurrent-recovery.audit.jsonl"
        results = context.Queue()
        operators = [
            context.Process(target=_attempt_restore_recovery, args=(results, f"operator-{index}", audit))
            for index in range(2)
        ]
        for process in operators:
            process.start()
        for process in operators:
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(5)
        outcomes = [results.get(timeout=2) for _ in range(2)]
        self.assertEqual(sum(item[0] == "recovered" for item in outcomes), 1)
        self.assertEqual(len(audit.read_text().splitlines()), 2)
        self.assertFalse(maintenance_module.restore_lock_path().exists())

    def test_explicit_recovery_after_process_death_allows_new_lock_without_touching_db(self):
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        worker = context.Process(target=_crash_after_acquiring_restore_lock, args=(ready,))
        worker.start()
        worker.join(5)
        self.assertEqual(worker.exitcode, 0)
        before = database.DB_PATH.read_bytes()
        recover_abandoned_restore_lock("operator", "INC-REACQUIRE", confirm=True, audit_path=Path(self.temp_dir.name) / "reacquire.audit.jsonl")
        new_lock = acquire_restore_lock("9" * 32)
        self.assertEqual(new_lock["status"], "RESTORE_LOCK_ACQUIRED")
        self.assertEqual(database.DB_PATH.read_bytes(), before)
        release_restore_lock(new_lock["lock_token"])



    def test_current_pid_without_kernel_guard_is_not_treated_as_active(self):
        marker = self._write_abandoned_restore_marker()
        payload = json.loads(marker.read_text())
        payload["pid"] = os.getpid()
        marker.write_text(json.dumps(payload))
        self.assertEqual(inspect_restore_lock()["status"], "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW")

    def test_large_reused_or_unrelated_pid_does_not_override_unlocked_guard(self):
        marker = self._write_abandoned_restore_marker()
        payload = json.loads(marker.read_text())
        payload["pid"] = 2**31 - 1
        marker.write_text(json.dumps(payload))
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(result["process_id"], 2**31 - 1)

    def test_live_kernel_guard_overrides_dead_pid_metadata(self):
        lock = acquire_restore_lock("1" * 32)
        marker = maintenance_module.restore_lock_path()
        try:
            payload = json.loads(marker.read_text())
            payload["pid"] = 2**31 - 1
            marker.write_text(json.dumps(payload))
            self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_ACTIVE")
        finally:
            release_restore_lock(lock["lock_token"])

    def test_live_kernel_guard_overrides_current_pid_metadata(self):
        lock = acquire_restore_lock("2" * 32)
        marker = maintenance_module.restore_lock_path()
        try:
            payload = json.loads(marker.read_text())
            payload["pid"] = os.getpid()
            marker.write_text(json.dumps(payload))
            self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_ACTIVE")
        finally:
            release_restore_lock(lock["lock_token"])

    def test_live_kernel_guard_overrides_different_positive_pid_metadata(self):
        lock = acquire_restore_lock("3" * 32)
        marker = maintenance_module.restore_lock_path()
        try:
            payload = json.loads(marker.read_text())
            payload["pid"] = 123456789
            marker.write_text(json.dumps(payload))
            self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_ACTIVE")
        finally:
            release_restore_lock(lock["lock_token"])

    def test_zero_pid_is_rejected_as_unreadable_marker_metadata(self):
        marker = self._write_abandoned_restore_marker()
        payload = json.loads(marker.read_text())
        payload["pid"] = 0
        marker.write_text(json.dumps(payload))
        self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_METADATA_UNREADABLE")

    def test_negative_pid_is_rejected_as_unreadable_marker_metadata(self):
        marker = self._write_abandoned_restore_marker()
        payload = json.loads(marker.read_text())
        payload["pid"] = -1
        marker.write_text(json.dumps(payload))
        self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_METADATA_UNREADABLE")

    def test_boolean_pid_is_rejected_as_unreadable_marker_metadata(self):
        marker = self._write_abandoned_restore_marker()
        payload = json.loads(marker.read_text())
        payload["pid"] = True
        marker.write_text(json.dumps(payload))
        self.assertEqual(inspect_restore_lock()["status"], "RESTORE_LOCK_METADATA_UNREADABLE")

    def test_dead_pid_marker_requires_explicit_recovery_not_automatic_cleanup(self):
        marker = self._write_abandoned_restore_marker()
        before = marker.read_bytes()
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW")
        self.assertFalse(result["automatic_recovery_performed"])
        self.assertFalse(result["marker_removed"])
        self.assertEqual(marker.read_bytes(), before)

    def test_recovery_refuses_pid_metadata_change_after_inspection(self):
        lock = acquire_restore_lock("4" * 32)
        marker = maintenance_module.restore_lock_path()
        try:
            payload = json.loads(marker.read_text())
            payload["pid"] = os.getpid()
            marker.write_text(json.dumps(payload))
            with self.assertRaisesRegex(RuntimeError, "not confirmed abandoned"):
                recover_abandoned_restore_lock("operator", "INC-PID-CHANGE", confirm=True, audit_path=Path(self.temp_dir.name) / "pid-change.audit.jsonl")
            self.assertTrue(marker.exists())
        finally:
            payload = json.loads(marker.read_text())
            payload["pid"] = 999999
            marker.write_text(json.dumps(payload))
            release_restore_lock(lock["lock_token"])


    def _inject_recovery_audit_write_fault(self, target_event, mode, audit):
        real_write = os.write
        state = {"target_started": False, "partial_written": False, "triggered": False}
        def injected(fd, data):
            try:
                is_audit = audit.exists() and os.path.samefile(audit, f"/proc/self/fd/{fd}")
            except (OSError, FileNotFoundError):
                is_audit = False
            payload = bytes(data)
            if not is_audit:
                return real_write(fd, data)
            if mode == "short":
                state["triggered"] = True
                return real_write(fd, payload[:max(1, len(payload) // 3)])
            if not state["triggered"] and ('"event": "' + target_event + '"').encode() in payload:
                state["triggered"] = True
                if mode == "zero":
                    return 0
                if mode == "partial":
                    state["partial_written"] = True
                    return real_write(fd, payload[:max(1, len(payload) // 3)])
            if state["partial_written"]:
                state["partial_written"] = False
                raise OSError("simulated interrupted audit append")
            return real_write(fd, data)
        return injected, state

    def test_recovery_audit_short_writes_loop_until_both_records_are_complete(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "short-write.jsonl"
        injected, state = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "short", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            result = recover_abandoned_restore_lock("operator", "INC-SHORT-WRITES", confirm=True, audit_path=audit)
        self.assertEqual(result["status"], "ABANDONED_RESTORE_LOCK_RECOVERED")
        self.assertTrue(state["triggered"])
        self.assertFalse(marker.exists())
        with audit.open("rb") as stream:
            self.assertEqual(maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())["record_count"], 2)

    def test_started_partial_append_preserves_abandoned_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "started-partial.jsonl"
        injected, state = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaisesRegex(OSError, "interrupted audit append"):
                recover_abandoned_restore_lock("operator", "INC-START-PARTIAL", confirm=True, audit_path=audit)
        self.assertTrue(state["triggered"])
        self.assertTrue(marker.exists())
        self.assertFalse(audit.read_bytes().endswith(b"\\n"))

    def test_started_partial_append_is_detected_as_incomplete_record(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "started-incomplete.jsonl"
        injected, _ = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-START-INCOMPLETE", confirm=True, audit_path=audit)
        with audit.open("rb") as stream:
            with self.assertRaisesRegex(RuntimeError, "incomplete final record"):
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())
        self.assertTrue(marker.exists())

    def test_started_partial_append_blocks_retry_without_truncation(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "started-retry.jsonl"
        injected, _ = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-START-RETRY", confirm=True, audit_path=audit)
        before = audit.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "incomplete final record"):
            recover_abandoned_restore_lock("operator", "INC-START-RETRY-2", confirm=True, audit_path=audit)
        self.assertEqual(audit.read_bytes(), before)
        self.assertTrue(marker.exists())

    def test_zero_length_started_write_fails_closed(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "started-zero.jsonl"
        injected, state = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "zero", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaisesRegex(OSError, "Could not complete recovery audit write"):
                recover_abandoned_restore_lock("operator", "INC-START-ZERO", confirm=True, audit_path=audit)
        self.assertTrue(state["triggered"])
        self.assertTrue(marker.exists())
        self.assertEqual(audit.read_bytes(), b"")

    def test_complete_partial_append_leaves_marker_absent_and_tail_invalid(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "complete-partial.jsonl"
        injected, state = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_COMPLETE", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaisesRegex(OSError, "interrupted audit append"):
                recover_abandoned_restore_lock("operator", "INC-COMPLETE-PARTIAL", confirm=True, audit_path=audit)
        self.assertTrue(state["triggered"])
        self.assertFalse(marker.exists())
        self.assertFalse(audit.read_bytes().endswith(b"\\n"))
        with audit.open("rb") as stream:
            with self.assertRaisesRegex(RuntimeError, "incomplete final record"):
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())

    def test_complete_partial_append_blocks_future_recovery_and_preserves_new_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "complete-retry.jsonl"
        injected, _ = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_COMPLETE", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-COMPLETE-RETRY", confirm=True, audit_path=audit)
        self.assertFalse(marker.exists())
        replacement = self._write_abandoned_restore_marker(plan_id="c" * 32)
        damaged_audit = audit.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "incomplete final record"):
            recover_abandoned_restore_lock("operator", "INC-COMPLETE-RETRY-2", confirm=True, audit_path=audit)
        self.assertTrue(replacement.exists())
        self.assertEqual(audit.read_bytes(), damaged_audit)

    def test_started_partial_append_preserves_database_and_sqlite_sidecars(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "started-sidecars.jsonl"
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"recovery-audit-wal")
        shm.write_bytes(b"recovery-audit-shm")
        db_before = database.DB_PATH.read_bytes()
        injected, _ = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-START-SIDECARS", confirm=True, audit_path=audit)
        self.assertTrue(marker.exists())
        self.assertEqual(database.DB_PATH.read_bytes(), db_before)
        self.assertEqual(wal.read_bytes(), b"recovery-audit-wal")
        self.assertEqual(shm.read_bytes(), b"recovery-audit-shm")

    def test_complete_partial_append_preserves_database_and_sqlite_sidecars(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "complete-sidecars.jsonl"
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"recovery-audit-wal")
        shm.write_bytes(b"recovery-audit-shm")
        db_before = database.DB_PATH.read_bytes()
        injected, _ = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_COMPLETE", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-COMPLETE-SIDECARS", confirm=True, audit_path=audit)
        self.assertFalse(marker.exists())
        self.assertEqual(database.DB_PATH.read_bytes(), db_before)
        self.assertEqual(wal.read_bytes(), b"recovery-audit-wal")
        self.assertEqual(shm.read_bytes(), b"recovery-audit-shm")

    def test_partial_audit_tail_is_never_automatically_truncated_or_repaired(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "no-auto-repair.jsonl"
        injected, _ = self._inject_recovery_audit_write_fault("RESTORE_LOCK_RECOVERY_STARTED", "partial", audit)
        with patch("app.maintenance.os.write", side_effect=injected):
            with self.assertRaises(OSError):
                recover_abandoned_restore_lock("operator", "INC-NO-REPAIR", confirm=True, audit_path=audit)
        before = audit.read_bytes()
        with self.assertRaises(RuntimeError):
            with audit.open("rb") as stream:
                maintenance_module._verify_restore_lock_recovery_audit_fd(stream.fileno())
        self.assertEqual(audit.read_bytes(), before)
        self.assertTrue(marker.exists())


    def _is_audit_fd_for_test(self, fd, audit):
        try:
            return audit.exists() and os.path.samefile(audit, f"/proc/self/fd/{fd}")
        except OSError:
            return False

    def test_started_fsync_failure_keeps_marker_and_unmatched_audit(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"started-barrier.jsonl"; real=os.fsync
        def fail(fd):
            if self._is_audit_fd_for_test(fd,audit): raise OSError("STARTED barrier failed")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fail), self.assertRaisesRegex(OSError,"STARTED barrier"):
            recover_abandoned_restore_lock("operator","INC-START-BARRIER",confirm=True,audit_path=audit)
        self.assertTrue(marker.exists())
        with audit.open("rb") as f:
            with self.assertRaisesRegex(RuntimeError,"unmatched STARTED"): maintenance_module._verify_restore_lock_recovery_audit_fd(f.fileno())

    def test_started_fsync_failure_blocks_retry_without_audit_mutation(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"started-retry-barrier.jsonl"; real=os.fsync
        def fail(fd):
            if self._is_audit_fd_for_test(fd,audit): raise OSError("STARTED sync failed")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fail), self.assertRaises(OSError):
            recover_abandoned_restore_lock("operator","INC-START-RETRY-BARRIER",confirm=True,audit_path=audit)
        before=audit.read_bytes()
        with self.assertRaisesRegex(RuntimeError,"integrity verification failed"):
            recover_abandoned_restore_lock("operator","INC-START-RETRY-BARRIER-2",confirm=True,audit_path=audit)
        self.assertEqual(audit.read_bytes(),before); self.assertTrue(marker.exists())

    def test_started_fsync_precedes_marker_unlink(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"started-order-fsync.jsonl"; events=[]; real=os.fsync; unlink=Path.unlink
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit): events.append("audit-fsync")
            return real(fd)
        def un(path,*a,**k):
            if path==marker: events.append("marker-unlink")
            return unlink(path,*a,**k)
        with patch("app.maintenance.os.fsync",side_effect=fs),patch.object(Path,"unlink",un):
            recover_abandoned_restore_lock("operator","INC-START-ORDER-FSYNC",confirm=True,audit_path=audit)
        self.assertLess(events.index("audit-fsync"),events.index("marker-unlink"))

    def test_marker_directory_fsync_precedes_complete_append(self):
        marker=self._write_abandoned_restore_marker(); d=Path(self.temp_dir.name)/"audit-dir"; d.mkdir(); audit=d/"events.jsonl"; md=marker.parent.stat(); events=[]; fs=os.fsync; wr=os.write
        def sync(fd):
            st=os.fstat(fd)
            if maintenance_module.stat.S_ISDIR(st.st_mode) and (st.st_dev,st.st_ino)==(md.st_dev,md.st_ino): events.append("marker-dir-sync")
            return fs(fd)
        def write(fd,data):
            if self._is_audit_fd_for_test(fd,audit) and b'"event": "RESTORE_LOCK_RECOVERY_COMPLETE"' in bytes(data): events.append("complete-write")
            return wr(fd,data)
        with patch("app.maintenance.os.fsync",side_effect=sync),patch("app.maintenance.os.write",side_effect=write):
            recover_abandoned_restore_lock("operator","INC-MARKER-DIR-ORDER",confirm=True,audit_path=audit)
        self.assertLess(events.index("marker-dir-sync"),events.index("complete-write"))

    def test_complete_fsync_is_final_barrier_before_success_return(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"complete-order-fsync.jsonl"; events=[]; real=os.fsync
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit): events.append("audit-fsync")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fs):
            result=recover_abandoned_restore_lock("operator","INC-COMPLETE-ORDER-FSYNC",confirm=True,audit_path=audit); events.append("return")
        self.assertEqual(events,["audit-fsync","audit-fsync","return"]); self.assertEqual(result["status"],"ABANDONED_RESTORE_LOCK_RECOVERED"); self.assertFalse(marker.exists())

    def test_started_fsync_failure_preserves_database_and_sidecars(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"started-preserve.jsonl"; wal=Path(str(database.DB_PATH)+"-wal"); shm=Path(str(database.DB_PATH)+"-shm"); wal.write_bytes(b"wal"); shm.write_bytes(b"shm"); before=database.DB_PATH.read_bytes(); real=os.fsync
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit): raise OSError("STARTED sync fault")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fs),self.assertRaises(OSError):
            recover_abandoned_restore_lock("operator","INC-START-PRESERVE",confirm=True,audit_path=audit)
        self.assertTrue(marker.exists()); self.assertEqual(database.DB_PATH.read_bytes(),before); self.assertEqual(wal.read_bytes(),b"wal"); self.assertEqual(shm.read_bytes(),b"shm")

    def test_complete_fsync_failure_preserves_database_and_sidecars(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"complete-preserve.jsonl"; wal=Path(str(database.DB_PATH)+"-wal"); shm=Path(str(database.DB_PATH)+"-shm"); wal.write_bytes(b"wal"); shm.write_bytes(b"shm"); before=database.DB_PATH.read_bytes(); real=os.fsync; n={"v":0}
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit):
                n["v"]+=1
                if n["v"]==2: raise OSError("COMPLETE sync fault")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fs),self.assertRaisesRegex(OSError,"COMPLETE sync fault"):
            recover_abandoned_restore_lock("operator","INC-COMPLETE-PRESERVE",confirm=True,audit_path=audit)
        self.assertFalse(marker.exists()); self.assertEqual(database.DB_PATH.read_bytes(),before); self.assertEqual(wal.read_bytes(),b"wal"); self.assertEqual(shm.read_bytes(),b"shm")

    def test_complete_fsync_failure_never_returns_success(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"complete-no-success.jsonl"; real=os.fsync; n={"v":0}; returned=[]
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit):
                n["v"]+=1
                if n["v"]==2: raise OSError("COMPLETE durability unknown")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fs):
            with self.assertRaisesRegex(OSError,"durability unknown"):
                returned.append(recover_abandoned_restore_lock("operator","INC-COMPLETE-NO-SUCCESS",confirm=True,audit_path=audit))
        self.assertEqual(returned,[]); self.assertFalse(marker.exists())

    def test_complete_fsync_failure_cannot_be_automatically_retried_or_repaired(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"complete-no-repair.jsonl"; real=os.fsync; n={"v":0}
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit):
                n["v"]+=1
                if n["v"]==2: raise OSError("COMPLETE fsync failed")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fs),self.assertRaises(OSError):
            recover_abandoned_restore_lock("operator","INC-COMPLETE-NO-REPAIR",confirm=True,audit_path=audit)
        before=audit.read_bytes()
        with self.assertRaisesRegex(RuntimeError,"not confirmed abandoned"):
            recover_abandoned_restore_lock("operator","INC-COMPLETE-NO-REPAIR-RETRY",confirm=True,audit_path=audit)
        self.assertEqual(audit.read_bytes(),before); self.assertFalse(marker.exists())

    def test_unsynced_complete_record_loss_is_detected_as_unmatched_started(self):
        marker=self._write_abandoned_restore_marker(); audit=Path(self.temp_dir.name)/"complete-loss.jsonl"; real=os.fsync; n={"v":0}
        def fs(fd):
            if self._is_audit_fd_for_test(fd,audit):
                n["v"]+=1
                if n["v"]==2: raise OSError("simulated power-loss boundary")
            return real(fd)
        with patch("app.maintenance.os.fsync",side_effect=fs),self.assertRaises(OSError):
            recover_abandoned_restore_lock("operator","INC-COMPLETE-LOSS",confirm=True,audit_path=audit)
        self.assertFalse(marker.exists()); raw=audit.read_bytes(); end=raw.find(b"\n")+1; self.assertGreater(end,0); audit.write_bytes(raw[:end])
        with audit.open("rb") as f:
            with self.assertRaisesRegex(RuntimeError,"unmatched STARTED"): maintenance_module._verify_restore_lock_recovery_audit_fd(f.fileno())



    def _write_diagnostic_audit(self, audit, events, marker_bytes=None):
        previous = "0" * 64
        records = []
        for sequence, event in enumerate(events, 1):
            record = {
                "sequence": sequence,
                "previous_record_sha256": previous,
                "event": event,
                "timestamp": "2026-11-20T07:00:00+02:00",
                "operator_id": "operator-test",
                "incident_ref": "INC-DIAGNOSTIC-TEST",
                "plan_id": "a" * 32,
                "marker_sha256": hashlib.sha256(marker_bytes or b"original-marker").hexdigest(),
                "marker_file": database.DB_PATH.name + ".restore.lock",
                "database_changed": False,
                "sidecars_changed": False,
            }
            record["record_sha256"] = maintenance_module._restore_lock_recovery_record_digest(record)
            previous = record["record_sha256"]
            records.append(record)
        audit.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in records), encoding="utf-8")
        return records

    def test_diagnostic_missing_audit_does_not_create_file(self):
        audit = Path(self.temp_dir.name) / "missing-diagnostic.jsonl"
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "NO_RECOVERY_AUDIT")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["automatic_repair"])
        self.assertFalse(audit.exists())

    def test_diagnostic_empty_audit_reports_empty_without_mutation(self):
        audit = Path(self.temp_dir.name) / "empty-diagnostic.jsonl"
        audit.write_bytes(b"")
        before = audit.read_bytes()
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "RECOVERY_AUDIT_EMPTY")
        self.assertEqual(result["record_count"], 0)
        self.assertEqual(audit.read_bytes(), before)

    def test_diagnostic_valid_completed_history_reports_consistent(self):
        audit = Path(self.temp_dir.name) / "valid-diagnostic.jsonl"
        self._write_diagnostic_audit(audit, ["RESTORE_LOCK_RECOVERY_STARTED", "RESTORE_LOCK_RECOVERY_COMPLETE"])
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "RECOVERY_AUDIT_VALID")
        self.assertEqual(result["record_count"], 2)
        self.assertIsNone(result["pending_recovery"])
        self.assertEqual(result["marker_state"], "NO_MARKER")

    def test_diagnostic_unmatched_started_matches_existing_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "pending-same-marker.jsonl"
        self._write_diagnostic_audit(audit, ["RESTORE_LOCK_RECOVERY_STARTED"], marker.read_bytes())
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(result["marker_state"], "MATCHES_INTERRUPTED_RECOVERY")
        self.assertEqual(result["pending_recovery"]["incident_ref"], "INC-DIAGNOSTIC-TEST")

    def test_diagnostic_unmatched_started_reports_missing_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "pending-missing-marker.jsonl"
        self._write_diagnostic_audit(audit, ["RESTORE_LOCK_RECOVERY_STARTED"], marker.read_bytes())
        marker.unlink()
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW")
        self.assertEqual(result["marker_state"], "MARKER_ABSENT")

    def test_diagnostic_unmatched_started_never_confuses_replacement_marker(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "pending-replacement-marker.jsonl"
        self._write_diagnostic_audit(audit, ["RESTORE_LOCK_RECOVERY_STARTED"], marker.read_bytes())
        marker.write_text(json.dumps({"plan_id": "c" * 32, "pid": 999999, "created_at": "2026-11-17T10:00:00+02:00", "lock_token": "d" * 32}))
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["marker_state"], "DIFFERENT_MARKER_PRESENT")
        self.assertEqual(result["status"], "RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW")

    def test_diagnostic_malformed_or_partial_audit_is_invalid_and_unchanged(self):
        audit = Path(self.temp_dir.name) / "partial-diagnostic.jsonl"
        audit.write_bytes(b'{"sequence":1')
        before = audit.read_bytes()
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "RECOVERY_AUDIT_INVALID")
        self.assertEqual(audit.read_bytes(), before)

    def test_diagnostic_rejects_symlink_without_touching_target(self):
        target = Path(self.temp_dir.name) / "audit-target.jsonl"
        target.write_text("sensitive target remains unchanged")
        link = Path(self.temp_dir.name) / "audit-link.jsonl"
        link.symlink_to(target)
        before = target.read_bytes()
        result = maintenance_module.inspect_restore_lock_recovery_audit(link)
        self.assertEqual(result["status"], "RECOVERY_AUDIT_UNREADABLE")
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(link.is_symlink())

    def test_diagnostic_returns_busy_when_writer_holds_exclusive_audit_lock(self):
        audit = Path(self.temp_dir.name) / "busy-diagnostic.jsonl"
        audit.write_bytes(b"")
        writer_fd = os.open(str(audit), os.O_RDONLY)
        try:
            maintenance_module.fcntl.flock(writer_fd, maintenance_module.fcntl.LOCK_EX | maintenance_module.fcntl.LOCK_NB)
            result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        finally:
            os.close(writer_fd)
        self.assertEqual(result["status"], "RECOVERY_AUDIT_BUSY")
        self.assertTrue(result["read_only"])

    def test_diagnostic_never_changes_database_sidecars_marker_or_audit(self):
        marker = self._write_abandoned_restore_marker()
        audit = Path(self.temp_dir.name) / "read-only-preservation.jsonl"
        self._write_diagnostic_audit(audit, ["RESTORE_LOCK_RECOVERY_STARTED"], marker.read_bytes())
        wal = Path(str(database.DB_PATH) + "-wal")
        shm = Path(str(database.DB_PATH) + "-shm")
        wal.write_bytes(b"diagnostic-wal")
        shm.write_bytes(b"diagnostic-shm")
        snapshots = {
            "db": database.DB_PATH.read_bytes(),
            "wal": wal.read_bytes(),
            "shm": shm.read_bytes(),
            "marker": marker.read_bytes(),
            "audit": audit.read_bytes(),
        }
        result = maintenance_module.inspect_restore_lock_recovery_audit(audit)
        self.assertEqual(result["status"], "RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW")
        self.assertFalse(result["automatic_repair"])
        self.assertEqual(database.DB_PATH.read_bytes(), snapshots["db"])
        self.assertEqual(wal.read_bytes(), snapshots["wal"])
        self.assertEqual(shm.read_bytes(), snapshots["shm"])
        self.assertEqual(marker.read_bytes(), snapshots["marker"])
        self.assertEqual(audit.read_bytes(), snapshots["audit"])


    def _consistency_inputs(self, *, audit_status="RECOVERY_AUDIT_VALID",
                             pending=None, marker_state="NO_MARKER",
                             marker_status="NO_RESTORE_LOCK", marker_plan=None,
                             execution_status="NO_RECORDED_RESTORE_EXECUTION",
                             executions=None):
        audit = {"status": audit_status, "pending_recovery": pending,
                 "marker_state": marker_state, "read_only": True}
        execution = {"status": execution_status, "executions": executions or [],
                     "automatic_action": "NONE", "live_database_replaced": False}
        marker = {"status": marker_status, "plan_id": marker_plan}
        return audit, execution, marker

    def _run_consistency(self, values):
        audit, execution, marker = values
        with patch("app.maintenance.inspect_restore_lock_recovery_audit", return_value=audit), \
             patch("app.maintenance.inspect_restore_execution_state", return_value=execution), \
             patch("app.maintenance.inspect_restore_lock", return_value=marker):
            return maintenance_module.inspect_restore_recovery_consistency()

    def test_consistency_clean_completed_histories_need_no_action(self):
        result = self._run_consistency(self._consistency_inputs())
        self.assertEqual(result["decision"], "NO_ACTION")
        self.assertEqual(result["status"], "RESTORE_RECOVERY_CONSISTENT")

    def test_consistency_pending_started_with_matching_marker_blocks_retry(self):
        pending = {"plan_id": "a" * 32}
        result = self._run_consistency(self._consistency_inputs(
            audit_status="RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW",
            pending=pending, marker_state="MATCHES_INTERRUPTED_RECOVERY",
            marker_status="ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW",
            marker_plan="a" * 32))
        self.assertEqual(result["decision"], "OPERATOR_REVIEW_REQUIRED")
        self.assertIn("DO_NOT_RETRY", result["recommended_action"])

    def test_consistency_pending_started_with_absent_marker_never_auto_completes(self):
        result = self._run_consistency(self._consistency_inputs(
            audit_status="RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW",
            pending={"plan_id": "a" * 32}, marker_state="MARKER_ABSENT"))
        self.assertEqual(result["decision"], "OPERATOR_REVIEW_REQUIRED")
        self.assertFalse(result["automatic_resume"])

    def test_consistency_replacement_marker_plan_conflict_is_reported(self):
        result = self._run_consistency(self._consistency_inputs(
            audit_status="RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW",
            pending={"plan_id": "a" * 32}, marker_state="DIFFERENT_MARKER_PRESENT",
            marker_status="ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW",
            marker_plan="b" * 32))
        self.assertTrue(any("conflicts" in reason for reason in result["reasons"]))

    def test_consistency_nonterminal_execution_requires_operator_review(self):
        result = self._run_consistency(self._consistency_inputs(
            execution_status="RESTORE_EXECUTION_REVIEW_REQUIRED",
            executions=[{"plan_id": "a" * 32, "terminal": False}]))
        self.assertEqual(result["decision"], "OPERATOR_REVIEW_REQUIRED")

    def test_consistency_terminal_execution_still_requires_reconciliation(self):
        result = self._run_consistency(self._consistency_inputs(
            execution_status="RESTORE_EXECUTION_TERMINAL_EVENTS_RECORDED",
            executions=[{"plan_id": "a" * 32, "terminal": True}]))
        self.assertEqual(result["decision"], "OPERATOR_REVIEW_REQUIRED")
        self.assertTrue(any("reconciliation" in reason for reason in result["reasons"]))

    def test_consistency_same_plan_in_both_journals_is_explicit_conflict(self):
        result = self._run_consistency(self._consistency_inputs(
            audit_status="RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW",
            pending={"plan_id": "a" * 32}, marker_state="MATCHES_INTERRUPTED_RECOVERY",
            marker_status="ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW",
            marker_plan="a" * 32,
            executions=[{"plan_id": "a" * 32, "terminal": False}],
            execution_status="RESTORE_EXECUTION_REVIEW_REQUIRED"))
        self.assertTrue(any("both histories" in reason for reason in result["reasons"]))

    def test_consistency_invalid_audit_and_journal_fail_closed(self):
        result = self._run_consistency(self._consistency_inputs(
            audit_status="RECOVERY_AUDIT_INVALID",
            execution_status="RESTORE_EXECUTION_INSPECTION_BLOCKED"))
        self.assertEqual(result["decision"], "OPERATOR_REVIEW_REQUIRED")

    def test_consistency_unsafe_marker_fails_closed(self):
        result = self._run_consistency(self._consistency_inputs(
            marker_status="RESTORE_LOCK_UNSAFE_MARKER"))
        self.assertEqual(result["decision"], "OPERATOR_REVIEW_REQUIRED")

    def test_consistency_is_read_only_and_never_authorizes_repair(self):
        result = self._run_consistency(self._consistency_inputs(
            audit_status="RECOVERY_INTERRUPTED_REQUIRES_OPERATOR_REVIEW",
            pending={"plan_id": "a" * 32}, marker_state="MARKER_ABSENT"))
        self.assertTrue(result["read_only"])
        self.assertFalse(result["automatic_repair"])
        self.assertFalse(result["automatic_resume"])
        self.assertFalse(result["database_changed"])
        self.assertFalse(result["sidecars_changed"])




    def _make_restore_artifact_evidence_fixture(self, root, plan_id="a" * 32, token="b" * 32):
        paths = {
            "live_database": root / "evidence-live.sqlite3",
            "source_backup": root / "evidence-source.sqlite3",
            "staged_candidate": root / "evidence-staged.sqlite3",
            "rollback_backup": root / "evidence-rollback.sqlite3",
        }
        for name, artifact in paths.items():
            with sqlite3.connect(artifact) as connection:
                connection.execute("CREATE TABLE evidence_probe (value TEXT)")
                connection.execute("INSERT INTO evidence_probe VALUES (?)", (name,))
        hashes = {f"{name}_sha256": sha256_file(artifact) for name, artifact in paths.items()}
        journal = root / "restore-artifact-cross-check.audit.jsonl"
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", plan_id, token, "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": hashes}, journal_path=journal,
        )
        return paths, hashes, journal

    def test_artifact_cross_check_rejects_staged_hash_disagreement_at_publish(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": {
                "staged_candidate_sha256": "c" * 64,
                "published_database_sha256": hashes["live_database_sha256"],
            }}, journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertTrue(any("staged-candidate fingerprint" in issue for issue in result["lifecycle_consistency_issues"]))

    def test_artifact_cross_check_rejects_published_hash_vs_postcheck_disagreement(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": {
                "staged_candidate_sha256": hashes["staged_candidate_sha256"],
                "published_database_sha256": "c" * 64,
            }}, journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"integrity_check": "ok", "foreign_key_violations": 0,
                     "schema_check": "passed", "installed_database_sha256": "d" * 64},
            journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertTrue(any("source-published evidence" in issue for issue in result["lifecycle_consistency_issues"]))
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")

    def test_artifact_cross_check_rejects_rollback_start_backup_disagreement(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        append_restore_execution_event(
            "RESTORE_ROLLBACK_STARTED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": {"rollback_backup_sha256": "c" * 64}},
            journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertTrue(any("rollback-start backup" in issue for issue in result["lifecycle_consistency_issues"]))

    def test_artifact_cross_check_rejects_rollback_verified_hash_vs_backup(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        append_restore_execution_event(
            "RESTORE_ROLLBACK_STARTED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": {"rollback_backup_sha256": hashes["rollback_backup_sha256"]}},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_ROLLBACK_VERIFIED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"integrity_check": "ok", "foreign_key_violations": 0,
                     "restored_database_sha256": "c" * 64},
            journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertTrue(any("rollback verification fingerprint" in issue for issue in result["lifecycle_consistency_issues"]))

    def test_artifact_cross_check_rejects_same_path_for_multiple_roles(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        paths["source_backup"] = paths["live_database"]
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        source = next(item for item in result["artifacts"] if item["artifact"] == "source_backup")
        self.assertEqual(source["fingerprint_status"], "UNSAFE_OR_MISSING")
        self.assertIn("aliases", source["error"])

    def test_artifact_cross_check_rejects_hard_link_aliases(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        paths["source_backup"].unlink()
        os.link(paths["live_database"], paths["source_backup"])
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        source = next(item for item in result["artifacts"] if item["artifact"] == "source_backup")
        self.assertEqual(source["fingerprint_status"], "UNSAFE_OR_MISSING")
        self.assertIn("aliases", source["error"])

    def test_artifact_cross_check_blocks_semantically_invalid_plan_token_reuse(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", "a" * 32, "c" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": hashes}, journal_path=journal,
        )
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_RECONCILIATION_BLOCKED")
        self.assertTrue(result["semantic_errors"])
        self.assertEqual(result["automatic_action"], "NONE")

    def test_artifact_cross_check_detects_journal_append_during_file_inspection(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        real_inspect = maintenance_module.inspect_sqlite_sidecars
        changed = {"done": False}
        def append_during_inspection(artifact):
            if not changed["done"]:
                changed["done"] = True
                append_restore_execution_event(
                    "RESTORE_EXECUTION_STARTED", "d" * 32, "e" * 32,
                    "other-operator", "INC-CONCURRENT-APPEND",
                    journal_path=journal,
                )
            return real_inspect(artifact)
        with patch("app.maintenance.inspect_sqlite_sidecars", side_effect=append_during_inspection):
            result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertTrue(changed["done"])
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertTrue(any("journal changed" in issue for issue in result["lifecycle_consistency_issues"]))

    def test_artifact_cross_check_detects_sidecar_created_during_integrity_inspection(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        real_inspect = maintenance_module.inspect_sqlite_sidecars
        calls = {"count": 0}
        def add_sidecar_on_recheck(artifact):
            calls["count"] += 1
            if calls["count"] == 5:
                Path(str(artifact) + "-wal").write_bytes(b"appeared-during-read")
            return real_inspect(artifact)
        with patch("app.maintenance.inspect_sqlite_sidecars", side_effect=add_sidecar_on_recheck):
            result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        live = next(item for item in result["artifacts"] if item["artifact"] == "live_database")
        self.assertEqual(live["fingerprint_status"], "UNSAFE_OR_MISSING")
        self.assertIn("sidecars changed", live["error"])
        sidecar = Path(str(paths["live_database"]) + "-wal")
        self.assertEqual(sidecar.read_bytes(), b"appeared-during-read")

    def test_artifact_cross_check_preserves_all_files_and_journal_on_conflict(self):
        root = Path(self.temp_dir.name)
        paths, hashes, journal = self._make_restore_artifact_evidence_fixture(root)
        append_restore_execution_event(
            "RESTORE_SOURCE_PUBLISHED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"artifacts": {"staged_candidate_sha256": hashes["staged_candidate_sha256"],
                                   "published_database_sha256": "f" * 64}},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_POSTCHECK_PASSED", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"integrity_check": "ok", "foreign_key_violations": 0,
                     "schema_check": "passed", "installed_database_sha256": "f" * 64},
            journal_path=journal,
        )
        append_restore_execution_event(
            "RESTORE_EXECUTION_COMPLETE", "a" * 32, "b" * 32,
            "operator-test", "INC-CROSS-CHECK",
            details={"final_database_sha256": "f" * 64}, journal_path=journal,
        )
        before = {name: artifact.read_bytes() for name, artifact in paths.items()}
        journal_before = journal.read_bytes()
        result = reconcile_restore_execution_artifacts("a" * 32, "b" * 32, paths, journal_path=journal)
        self.assertEqual(result["status"], "RESTORE_EVIDENCE_REVIEW_REQUIRED")
        self.assertEqual({name: artifact.read_bytes() for name, artifact in paths.items()}, before)
        self.assertEqual(journal.read_bytes(), journal_before)
        self.assertFalse(result["database_state_verified"])
        self.assertEqual(result["automatic_action"], "NONE")


    def _call_real_inventory_api(self, root, user=None):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.auth_routes import current_user
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[current_user] = lambda: user or {
            "user_id": "inventory-admin", "email": "ops@example.com"
        }
        try:
            with patch.dict(os.environ, {"TAIMOBOT_ADMIN_EMAILS": "ops@example.com"}):
                with patch.object(maintenance_module, "BACKUP_DIR", Path(root)):
                    with TestClient(app) as client:
                        return client.get("/maintenance/recovery-artifact-inventory")
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)

    def test_real_inventory_api_reports_actual_orphan_backup(self):
        root = Path(self.temp_dir.name) / "api-real-orphan"
        backup = self._make_inventory_backup(root, "taimobot-orphan.sqlite3")
        response = self._call_real_inventory_api(root)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["orphaned_count"], 1)
        self.assertEqual(payload["orphaned_artifacts"][0]["file_name"], backup.name)
        self.assertEqual(payload["orphaned_artifacts"][0]["integrity_check"], "ok")

    def test_real_inventory_api_recognizes_completed_maintenance_reference(self):
        root = Path(self.temp_dir.name) / "api-completed-run"
        backup = self._make_inventory_backup(root, "taimobot-complete.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(backup),),
            )
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["artifacts"] if x["file_name"] == backup.name)
        self.assertEqual(item["status"], "REFERENCED")
        self.assertEqual(item["references"][0]["source"], "maintenance_runs")

    def test_real_inventory_api_reports_missing_completed_backup(self):
        root = Path(self.temp_dir.name) / "api-missing-completed"
        root.mkdir()
        missing = root / "taimobot-missing.sqlite3"
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(missing),),
            )
        payload = self._call_real_inventory_api(root).json()
        self.assertIn("REFERENCED_RECOVERY_ARTIFACT_MISSING", payload["issues"])
        self.assertTrue(any(x["status"] == "REFERENCED_FILE_MISSING" for x in payload["missing_references"]))

    def test_real_inventory_api_recognizes_failed_attempt_backup(self):
        root = Path(self.temp_dir.name) / "api-failed-attempt"
        backup = self._make_inventory_backup(root, "taimobot-failed-attempt.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('attempt-real','2026-11-20','FAILED','start','end',?)",
                (str(backup),),
            )
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["artifacts"] if x["file_name"] == backup.name)
        self.assertEqual(item["status"], "REFERENCED")

    def test_real_inventory_api_recognizes_superseded_attempt_backup(self):
        root = Path(self.temp_dir.name) / "api-superseded"
        backup = self._make_inventory_backup(root, "taimobot-superseded.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('attempt-old','2026-11-19','SUPERSEDED','start','end',?)",
                (str(backup),),
            )
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["artifacts"] if x["file_name"] == backup.name)
        self.assertEqual(item["status"], "REFERENCED")

    def test_real_inventory_api_recognizes_all_prepared_restore_artifact_roles(self):
        root = Path(self.temp_dir.name) / "api-restore-roles"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback.sqlite3")
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-staged.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        payload = self._call_real_inventory_api(root).json()
        self.assertEqual(payload["artifact_count"], 3)
        self.assertTrue(all(x["status"] == "REFERENCED" for x in payload["artifacts"]))
        self.assertEqual(payload["orphaned_count"], 0)

    def test_real_inventory_api_reports_missing_staged_candidate(self):
        root = Path(self.temp_dir.name) / "api-missing-staged"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback.sqlite3")
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-stage-to-remove.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        staged.unlink()
        payload = self._call_real_inventory_api(root).json()
        self.assertTrue(any(x["status"] == "REFERENCED_FILE_MISSING" and x["source"] == "maintenance_restore_plans.staged" for x in payload["missing_references"]))

    def test_real_inventory_api_reports_missing_rollback_backup(self):
        root = Path(self.temp_dir.name) / "api-missing-rollback"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-staged.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback-to-remove.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        rollback.unlink()
        payload = self._call_real_inventory_api(root).json()
        self.assertTrue(any(x["status"] == "REFERENCED_FILE_MISSING" and x["source"] == "maintenance_restore_plans.rollback" for x in payload["missing_references"]))

    def test_real_inventory_api_detects_changed_restore_source_fingerprint(self):
        root = Path(self.temp_dir.name) / "api-fingerprint-change"
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        rollback = self._make_inventory_backup(root, "taimobot-rollback.sqlite3")
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-staged.sqlite3")
        self._insert_inventory_restore_plan(source, staged, rollback)
        with sqlite3.connect(source) as connection:
            connection.execute("CREATE TABLE changed_after_plan (value TEXT)")
        payload = self._call_real_inventory_api(root).json()
        self.assertIn("RESTORE_PLAN_FINGERPRINT_MISMATCH", payload["issues"])
        item = next(x for x in payload["artifacts"] if x["file_name"] == source.name)
        self.assertEqual(item["status"], "REVIEW_REQUIRED")

    def test_real_inventory_api_classifies_corrupt_sqlite_as_review_not_orphan(self):
        root = Path(self.temp_dir.name) / "api-corrupt"
        root.mkdir()
        corrupt = root / "taimobot-corrupt.sqlite3"
        corrupt.write_bytes(b"not a sqlite database")
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["artifacts"] if x["file_name"] == corrupt.name)
        self.assertEqual(item["status"], "REVIEW_REQUIRED")
        self.assertIn("ARTIFACT_UNREADABLE", item["issues"])
        self.assertEqual(payload["orphaned_count"], 0)

    def test_real_inventory_api_classifies_symlink_as_unsafe_without_following(self):
        root = Path(self.temp_dir.name) / "api-symlink"
        target = self._make_inventory_backup(root, "taimobot-target.sqlite3")
        link = root / "taimobot-link.sqlite3"
        link.symlink_to(target)
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["artifacts"] if x["file_name"] == link.name)
        self.assertEqual(item["status"], "UNSAFE")
        self.assertTrue(target.is_file())

    def test_real_inventory_api_detects_hardlink_alias_identity(self):
        root = Path(self.temp_dir.name) / "api-hardlink"
        first = self._make_inventory_backup(root, "taimobot-first.sqlite3")
        second = root / "taimobot-second.sqlite3"
        os.link(first, second)
        payload = self._call_real_inventory_api(root).json()
        self.assertIn("DUPLICATE_ARTIFACT_IDENTITY", payload["issues"])
        self.assertTrue(any("HARD_LINK_ALIAS" in x["issues"] for x in payload["artifacts"]))

    def test_real_inventory_api_reports_orphaned_staging_candidate(self):
        root = Path(self.temp_dir.name) / "api-staging-orphan"
        staged = self._make_inventory_backup(root / "recovery-staging", "taimobot-unreferenced-stage.sqlite3")
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["orphaned_artifacts"] if x["file_name"] == staged.name)
        self.assertEqual(item["artifact_type"], "STAGED")

    def test_real_inventory_api_reports_incomplete_temporary_artifact(self):
        root = Path(self.temp_dir.name) / "api-temp"
        root.mkdir()
        temporary = root / ".taimobot-backup-in-progress.tmp"
        temporary.write_bytes(b"partial")
        payload = self._call_real_inventory_api(root).json()
        item = next(x for x in payload["artifacts"] if x["file_name"] == temporary.name)
        self.assertEqual(item["status"], "INCOMPLETE")
        self.assertEqual(item["artifact_type"], "INCOMPLETE_TEMPORARY")

    def test_real_inventory_api_blocks_invalid_restore_journal(self):
        root = Path(self.temp_dir.name) / "api-invalid-journal"
        root.mkdir()
        journal = database.DB_PATH.with_name("restore-execution.audit.jsonl")
        journal.write_text('{"invalid":true}\\n', encoding="utf-8")
        payload = self._call_real_inventory_api(root).json()
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertIn("RESTORE_EXECUTION_JOURNAL_UNTRUSTED", payload["issues"])

    def test_real_inventory_api_orders_artifacts_deterministically(self):
        root = Path(self.temp_dir.name) / "api-order"
        self._make_inventory_backup(root, "taimobot-zeta.sqlite3")
        self._make_inventory_backup(root, "taimobot-alpha.sqlite3")
        payload = self._call_real_inventory_api(root).json()
        self.assertEqual([x["file_name"] for x in payload["artifacts"]], ["taimobot-alpha.sqlite3", "taimobot-zeta.sqlite3"])

    def test_real_inventory_api_protects_live_database_when_inside_backup_root(self):
        root = Path(self.temp_dir.name) / "api-live-db"
        root.mkdir()
        live = root / "taimobot-live.sqlite3"
        original = database.DB_PATH
        shutil.copy2(original, live)
        try:
            database.DB_PATH = live
            payload = self._call_real_inventory_api(root).json()
        finally:
            database.DB_PATH = original
        item = next(x for x in payload["artifacts"] if x["file_name"] == live.name)
        self.assertEqual(item["status"], "PROTECTED_LIVE_DATABASE")
        self.assertFalse(payload["live_database_replaced"])

    def test_real_inventory_api_repeated_requests_preserve_database_backup_and_journal(self):
        root = Path(self.temp_dir.name) / "api-readonly-repeat"
        backup = self._make_inventory_backup(root, "taimobot-readonly.sqlite3")
        journal = database.DB_PATH.with_name("restore-execution.audit.jsonl")
        journal.write_text("", encoding="utf-8")
        before = (database.DB_PATH.read_bytes(), backup.read_bytes(), journal.read_bytes())
        first = self._call_real_inventory_api(root)
        second = self._call_real_inventory_api(root)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual((database.DB_PATH.read_bytes(), backup.read_bytes(), journal.read_bytes()), before)

    def test_real_inventory_api_flags_reference_outside_managed_root(self):
        root = Path(self.temp_dir.name) / "api-outside-root"
        root.mkdir()
        outside = Path(self.temp_dir.name) / "outside.sqlite3"
        outside.write_bytes(b"external")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(outside),),
            )
        payload = self._call_real_inventory_api(root).json()
        self.assertIn("REFERENCE_OUTSIDE_MANAGED_BACKUP_DIRECTORIES", payload["issues"])
        self.assertTrue(any(x["status"] == "OUTSIDE_MANAGED_DIRECTORY" for x in payload["missing_references"]))

    def test_real_inventory_api_keeps_orphan_and_issue_classification_separate(self):
        root = Path(self.temp_dir.name) / "api-mixed-classification"
        valid = self._make_inventory_backup(root, "taimobot-valid-orphan.sqlite3")
        corrupt = root / "taimobot-corrupt.sqlite3"
        corrupt.write_bytes(b"broken")
        payload = self._call_real_inventory_api(root).json()
        self.assertIn(valid.name, [x["file_name"] for x in payload["orphaned_artifacts"]])
        corrupt_item = next(x for x in payload["artifacts"] if x["file_name"] == corrupt.name)
        self.assertEqual(corrupt_item["status"], "REVIEW_REQUIRED")
        self.assertNotIn(corrupt.name, [x["file_name"] for x in payload["orphaned_artifacts"]])


def _make_real_inventory_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("race-" + scenario)
        staging = root / "recovery-staging"
        if scenario.startswith("staging_") and scenario != "staging_create":
            root.mkdir(parents=True, exist_ok=True)
            backup = self._make_inventory_backup(staging, "taimobot-stage.sqlite3")
        else:
            backup = self._make_inventory_backup(root, "taimobot-race.sqlite3")
        moved = root.with_name(root.name + "-held")
        outside = Path(self.temp_dir.name) / ("outside-" + scenario)
        outside.mkdir()
        external = outside / "taimobot-external.sqlite3"
        external.write_bytes(b"external")
        replacement = self._make_inventory_backup(
            Path(self.temp_dir.name) / ("replacement-" + scenario),
            "replacement.sqlite3",
        )
        original_hash = maintenance_module.sha256_file
        changed = {"done": False}

        def mutate(path):
            digest = original_hash(path)
            if changed["done"]:
                return digest
            expected = backup
            if Path(path) != expected:
                return digest
            changed["done"] = True
            if scenario == "file_append" or scenario == "staging_append":
                with Path(path).open("ab") as stream:
                    stream.write(b"changed")
            elif scenario == "file_replace" or scenario == "staging_replace_file":
                os.replace(replacement, path)
            elif scenario == "file_delete" or scenario == "staging_delete_file":
                Path(path).unlink()
            elif scenario == "file_rename":
                Path(path).rename(Path(path).with_name("taimobot-renamed.sqlite3"))
            elif scenario == "root_rename":
                root.rename(moved)
            elif scenario == "root_symlink":
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
            elif scenario == "root_regular_file":
                root.rename(moved)
                root.write_text("not a directory", encoding="utf-8")
            elif scenario == "root_add":
                (root / "taimobot-late.sqlite3").write_bytes(b"late")
            elif scenario == "root_remove":
                backup.unlink()
            elif scenario == "staging_symlink":
                staging.rename(root / "staging-held")
                staging.symlink_to(outside, target_is_directory=True)
            elif scenario == "staging_remove":
                staging.rename(root / "staging-held")
            elif scenario == "staging_create":
                staging.mkdir()
            elif scenario == "staging_add":
                (staging / "taimobot-late.sqlite3").write_bytes(b"late")
            elif scenario == "staging_file_symlink":
                target = self._make_inventory_backup(outside, "target.sqlite3")
                Path(path).unlink()
                Path(path).symlink_to(target)
            elif scenario == "root_swap_retry":
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
            elif scenario == "outside_preserved":
                (outside / "important.txt").write_text("untouched", encoding="utf-8")
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
            elif scenario == "file_touch":
                stat_result = Path(path).stat()
                os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1000000))
            elif scenario == "staging_file_touch":
                stat_result = Path(path).stat()
                os.utime(path, ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1000000))
            return digest

        with patch.object(maintenance_module, "sha256_file", side_effect=mutate):
            first = self._call_real_inventory_api(root).json()

        if scenario == "root_swap_retry":
            root.unlink()
            moved.rename(root)
            second = self._call_real_inventory_api(root).json()
            self.assertEqual(first["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
            self.assertEqual(second["orphaned_count"], 1)
            return
        if scenario == "outside_preserved":
            self.assertEqual(first["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
            self.assertEqual((outside / "important.txt").read_text(encoding="utf-8"), "untouched")
            return
        self.assertEqual(first["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertEqual(first.get("orphaned_count", 0), 0)
        if scenario in {"file_append", "file_touch", "staging_append", "staging_file_touch"}:
            self.assertIn("ARTIFACT_CHANGED_DURING_FINAL_CHECK", first["issues"])
        else:
            self.assertIn("ARTIFACT_DIRECTORY_CHANGED_DURING_INVENTORY", first["issues"])
    return test


_REAL_INVENTORY_RACE_CASES = [
    ("test_real_inventory_race_detects_file_append", "file_append"),
    ("test_real_inventory_race_detects_atomic_file_replacement", "file_replace"),
    ("test_real_inventory_race_blocks_file_deletion", "file_delete"),
    ("test_real_inventory_race_blocks_file_rename", "file_rename"),
    ("test_real_inventory_race_blocks_root_rename", "root_rename"),
    ("test_real_inventory_race_blocks_root_symlink_swap", "root_symlink"),
    ("test_real_inventory_race_blocks_root_regular_file_swap", "root_regular_file"),
    ("test_real_inventory_race_blocks_new_backup_entry", "root_add"),
    ("test_real_inventory_race_blocks_removed_backup_entry", "root_remove"),
    ("test_real_inventory_race_blocks_staging_symlink_swap", "staging_symlink"),
    ("test_real_inventory_race_blocks_staging_directory_removal", "staging_remove"),
    ("test_real_inventory_race_blocks_staging_directory_creation", "staging_create"),
    ("test_real_inventory_race_detects_staged_file_append", "staging_append"),
    ("test_real_inventory_race_blocks_staged_file_deletion", "staging_delete_file"),
    ("test_real_inventory_race_blocks_staged_file_symlink_swap", "staging_file_symlink"),
    ("test_real_inventory_race_blocks_new_staging_entry", "staging_add"),
    ("test_real_inventory_race_detects_file_timestamp_change", "file_touch"),
    ("test_real_inventory_race_detects_staged_timestamp_change", "staging_file_touch"),
    ("test_real_inventory_race_recovers_after_transient_root_swap", "root_swap_retry"),
    ("test_real_inventory_race_preserves_external_directory", "outside_preserved"),
]
for _test_name, _scenario in _REAL_INVENTORY_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_race_test(_scenario))


def _make_real_inventory_metadata_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("metadata-race-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-metadata-race.sqlite3")
        self._create_inventory_metadata_tables()
        baseline_hash = maintenance_module.sha256_file(backup)
        plan_id = "a" * 32
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-19','COMPLETE','start','end',?)",
                (str(backup),),
            )
            connection.execute(
                "INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('baseline-attempt','2026-11-19','FAILED','start','end',?)",
                (str(backup),),
            )
            connection.execute(
                "INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','2026-11-20','operator')",
                (plan_id, str(backup), str(backup), str(backup)),
            )
            connection.execute(
                "INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)",
                (plan_id, baseline_hash, baseline_hash, baseline_hash),
            )

        def mutate_metadata():
            with database.get_connection() as connection:
                if scenario == "insert_run":
                    connection.execute(
                        "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                        (str(backup),),
                    )
                elif scenario == "insert_attempt":
                    connection.execute(
                        "INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('new-attempt','2026-11-20','FAILED','start','end',?)",
                        (str(backup),),
                    )
                elif scenario == "update_run_status":
                    connection.execute("UPDATE maintenance_runs SET status='FAILED' WHERE run_date='2026-11-19'")
                elif scenario == "delete_run":
                    connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-19'")
                elif scenario == "change_run_path":
                    connection.execute("UPDATE maintenance_runs SET backup_file=? WHERE run_date='2026-11-19'", (str(root / "other.sqlite3"),))
                elif scenario == "insert_plan":
                    connection.execute(
                        "INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','2026-11-20','operator')",
                        ("b" * 32, str(backup), str(backup), str(backup)),
                    )
                elif scenario == "update_plan_status":
                    connection.execute("UPDATE maintenance_restore_plans SET status='FAILED' WHERE plan_id=?", (plan_id,))
                elif scenario == "delete_plan":
                    connection.execute("DELETE FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,))
                elif scenario == "change_plan_path":
                    connection.execute("UPDATE maintenance_restore_plans SET backup_file=? WHERE plan_id=?", (str(root / "other.sqlite3"), plan_id))
                elif scenario == "insert_artifact":
                    connection.execute(
                        "INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)",
                        ("b" * 32, baseline_hash, baseline_hash, baseline_hash),
                    )
                elif scenario == "update_artifact_hash":
                    connection.execute("UPDATE maintenance_restore_artifacts SET source_sha256=? WHERE plan_id=?", ("b" * 64, plan_id))
                elif scenario == "delete_artifact":
                    connection.execute("DELETE FROM maintenance_restore_artifacts WHERE plan_id=?", (plan_id,))
                elif scenario == "insert_superseded_attempt":
                    connection.execute(
                        "INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('superseded-attempt','2026-11-18','SUPERSEDED','start','end',?)",
                        (str(backup),),
                    )
                elif scenario == "update_attempt_status":
                    connection.execute("UPDATE maintenance_run_attempts SET status='COMPLETE' WHERE attempt_token='baseline-attempt'")
                elif scenario == "delete_attempt":
                    connection.execute("DELETE FROM maintenance_run_attempts WHERE attempt_token='baseline-attempt'")
                elif scenario == "change_attempt_path":
                    connection.execute("UPDATE maintenance_run_attempts SET backup_file=? WHERE attempt_token='baseline-attempt'", (str(root / "other.sqlite3"),))
                elif scenario == "update_run_timestamp":
                    connection.execute("UPDATE maintenance_runs SET completed_at='changed' WHERE run_date='2026-11-19'")
                elif scenario == "update_plan_operator":
                    connection.execute("UPDATE maintenance_restore_plans SET created_by='changed-operator' WHERE plan_id=?", (plan_id,))
                elif scenario == "insert_second_run":
                    connection.execute(
                        "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-18','SUPERSEDED','start','end',?)",
                        (str(backup),),
                    )
                elif scenario == "insert_plan_and_artifacts":
                    new_plan = "c" * 32
                    connection.execute(
                        "INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','2026-11-20','operator')",
                        (new_plan, str(backup), str(backup), str(backup)),
                    )
                    connection.execute(
                        "INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)",
                        (new_plan, baseline_hash, baseline_hash, baseline_hash),
                    )

        original_hash = maintenance_module.sha256_file
        changed = {"done": False}
        def mutate_during_scan(path):
            digest = original_hash(path)
            if Path(path) == backup and not changed["done"]:
                changed["done"] = True
                mutate_metadata()
            return digest

        with patch.object(maintenance_module, "sha256_file", side_effect=mutate_during_scan):
            payload = self._call_real_inventory_api(root).json()
        self.assertTrue(changed["done"])
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertIn("MAINTENANCE_METADATA_CHANGED_DURING_INVENTORY", payload["issues"])
        self.assertEqual(payload["orphaned_artifacts"], [])
    return test


_REAL_INVENTORY_METADATA_RACE_CASES = [
    ("test_metadata_race_insert_completed_run", "insert_run"),
    ("test_metadata_race_insert_failed_attempt", "insert_attempt"),
    ("test_metadata_race_update_run_status", "update_run_status"),
    ("test_metadata_race_delete_run_reference", "delete_run"),
    ("test_metadata_race_change_run_backup_path", "change_run_path"),
    ("test_metadata_race_insert_restore_plan", "insert_plan"),
    ("test_metadata_race_update_restore_plan_status", "update_plan_status"),
    ("test_metadata_race_delete_restore_plan", "delete_plan"),
    ("test_metadata_race_change_restore_plan_path", "change_plan_path"),
    ("test_metadata_race_insert_restore_artifact_fingerprints", "insert_artifact"),
    ("test_metadata_race_update_restore_artifact_fingerprint", "update_artifact_hash"),
    ("test_metadata_race_delete_restore_artifact_fingerprints", "delete_artifact"),
    ("test_metadata_race_insert_superseded_attempt", "insert_superseded_attempt"),
    ("test_metadata_race_update_attempt_status", "update_attempt_status"),
    ("test_metadata_race_delete_attempt_reference", "delete_attempt"),
    ("test_metadata_race_change_attempt_backup_path", "change_attempt_path"),
    ("test_metadata_race_update_run_completion_timestamp", "update_run_timestamp"),
    ("test_metadata_race_update_restore_plan_operator", "update_plan_operator"),
    ("test_metadata_race_insert_second_maintenance_run", "insert_second_run"),
    ("test_metadata_race_insert_plan_and_artifact_atomically", "insert_plan_and_artifacts"),
]
for _test_name, _scenario in _REAL_INVENTORY_METADATA_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_metadata_race_test(_scenario))





if __name__ == "__main__":
    unittest.main()

def _make_real_inventory_journal_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("journal-race-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-journal-race.sqlite3")
        self._create_inventory_metadata_tables()
        baseline_hash = maintenance_module.sha256_file(backup)
        plan_id = "d" * 32
        token = "e" * 32
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','2026-11-20','operator')",
                (plan_id, str(backup), str(backup), str(backup)),
            )
            connection.execute(
                "INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)",
                (plan_id, baseline_hash, baseline_hash, baseline_hash),
            )
        journal = Path(database.DB_PATH).parent / "restore-execution.audit.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.unlink(missing_ok=True)
        append_restore_execution_event(
            "RESTORE_EXECUTION_STARTED", plan_id, token,
            "operator@example.com", "INC-JOURNAL-RACE",
            details={"live_database_replaced": False}, journal_path=journal,
        )
        original_bytes = journal.read_bytes()

        def mutate_journal():
            if scenario == "append_valid_event":
                append_restore_execution_event("RESTORE_EXECUTION_RECOVERY_REQUIRED", plan_id, token,
                    "operator@example.com", "INC-JOURNAL-RACE",
                    details={"reason": "concurrent recovery decision"}, journal_path=journal)
            elif scenario == "append_invalid_json":
                with journal.open("ab") as handle: handle.write(b'{"broken":true}\\n')
            elif scenario == "truncate_tail":
                journal.write_bytes(original_bytes[:-3])
            elif scenario == "delete_journal":
                journal.unlink()
            elif scenario == "replace_valid_chain":
                replacement = journal.with_suffix(".replacement")
                append_restore_execution_event("RESTORE_EXECUTION_STARTED", "f" * 32, "1" * 32,
                    "operator@example.com", "INC-REPLACEMENT", journal_path=replacement)
                os.replace(replacement, journal)
            elif scenario == "replace_empty":
                replacement = journal.with_suffix(".replacement")
                replacement.write_bytes(b"")
                os.replace(replacement, journal)
            elif scenario == "append_partial_record":
                with journal.open("ab") as handle: handle.write(b'{"sequence":2')
            elif scenario == "tamper_existing_record":
                changed = bytearray(original_bytes)
                changed[changed.index(b"operator@example.com")] ^= 1
                journal.write_bytes(changed)
            elif scenario == "truncate_to_empty":
                journal.write_bytes(b"")
            elif scenario == "duplicate_first_record":
                with journal.open("ab") as handle: handle.write(original_bytes)
            elif scenario == "append_wrong_hash":
                with journal.open("ab") as handle:
                    handle.write(b'{"sequence":2,"record_sha256":"' + b"0" * 64 + b'"}\\n')
            elif scenario == "append_wrong_sequence":
                append_restore_execution_event("RESTORE_EXECUTION_RECOVERY_REQUIRED", plan_id, token,
                    "operator@example.com", "INC-JOURNAL-RACE",
                    details={"reason": "sequence test"}, journal_path=journal)
                journal.write_bytes(journal.read_bytes().replace(b'"sequence":2', b'"sequence":9', 1))
            elif scenario == "append_unknown_event":
                append_restore_execution_event("RESTORE_EXECUTION_RECOVERY_REQUIRED", plan_id, token,
                    "operator@example.com", "INC-JOURNAL-RACE",
                    details={"reason": "event test"}, journal_path=journal)
                journal.write_bytes(journal.read_bytes().replace(b"RESTORE_EXECUTION_RECOVERY_REQUIRED", b"UNKNOWN_EVENT", 1))
            elif scenario == "replace_with_directory":
                journal.unlink()
                journal.mkdir()
            elif scenario == "symlink_swap":
                replacement = journal.with_suffix(".replacement")
                replacement.write_bytes(original_bytes + b"tampered\\n")
                journal.unlink()
                journal.symlink_to(replacement)
            elif scenario == "append_duplicate_sequence":
                with journal.open("ab") as handle: handle.write(original_bytes.splitlines()[-1] + b"\\n")
            elif scenario == "rewrite_same_length":
                changed = bytearray(original_bytes)
                changed[0] = ord("{") if changed[0] != ord("{") else ord("[")
                journal.write_bytes(changed)
            elif scenario == "append_unterminated_record":
                with journal.open("ab") as handle: handle.write(b'{"sequence":2}')
            elif scenario == "replace_with_other_plan":
                replacement = journal.with_suffix(".replacement")
                append_restore_execution_event("RESTORE_EXECUTION_STARTED", "2" * 32, "3" * 32,
                    "operator@example.com", "INC-OTHER-PLAN", journal_path=replacement)
                os.replace(replacement, journal)
            elif scenario == "append_valid_second_plan":
                append_restore_execution_event("RESTORE_EXECUTION_STARTED", "4" * 32, "5" * 32,
                    "operator@example.com", "INC-SECOND-PLAN", journal_path=journal)

        original_hash = maintenance_module.sha256_file
        changed = {"done": False}
        def mutate_during_scan(path):
            digest = original_hash(path)
            if Path(path) == backup and not changed["done"]:
                changed["done"] = True
                mutate_journal()
            return digest

        try:
            with patch.object(maintenance_module, "sha256_file", side_effect=mutate_during_scan):
                response = self._call_real_inventory_api(root)
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(changed["done"])
            self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_REVIEW_REQUIRED")
            self.assertIn("RESTORE_EXECUTION_JOURNAL_CHANGED_DURING_INVENTORY", payload["issues"])
        finally:
            if journal.is_dir() and not journal.is_symlink():
                journal.rmdir()
            else:
                journal.unlink(missing_ok=True)
    return test


_REAL_INVENTORY_JOURNAL_RACE_CASES = [
    ("test_journal_race_append_valid_event", "append_valid_event"),
    ("test_journal_race_append_invalid_json", "append_invalid_json"),
    ("test_journal_race_truncate_tail", "truncate_tail"),
    ("test_journal_race_delete_journal", "delete_journal"),
    ("test_journal_race_replace_valid_chain", "replace_valid_chain"),
    ("test_journal_race_replace_empty", "replace_empty"),
    ("test_journal_race_append_partial_record", "append_partial_record"),
    ("test_journal_race_tamper_existing_record", "tamper_existing_record"),
    ("test_journal_race_truncate_to_empty", "truncate_to_empty"),
    ("test_journal_race_duplicate_first_record", "duplicate_first_record"),
    ("test_journal_race_append_wrong_hash", "append_wrong_hash"),
    ("test_journal_race_append_wrong_sequence", "append_wrong_sequence"),
    ("test_journal_race_append_unknown_event", "append_unknown_event"),
    ("test_journal_race_replace_with_directory", "replace_with_directory"),
    ("test_journal_race_symlink_swap", "symlink_swap"),
    ("test_journal_race_append_duplicate_sequence", "append_duplicate_sequence"),
    ("test_journal_race_rewrite_same_length", "rewrite_same_length"),
    ("test_journal_race_append_unterminated_record", "append_unterminated_record"),
    ("test_journal_race_replace_with_other_plan", "replace_with_other_plan"),
    ("test_journal_race_append_valid_second_plan", "append_valid_second_plan"),
]
for _test_name, _scenario in _REAL_INVENTORY_JOURNAL_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_journal_race_test(_scenario))


def _make_real_inventory_database_identity_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("database-identity-race-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-db-identity.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(backup),),
            )

        db_path = Path(database.DB_PATH)
        original_hash = maintenance_module.sha256_file
        changed = {"done": False}

        def replace_database_during_scan(path):
            digest = original_hash(path)
            if Path(path) == backup and not changed["done"]:
                changed["done"] = True
                replacement = db_path.with_name(db_path.name + ".identity-replacement")
                replacement.write_bytes(db_path.read_bytes())
                if scenario == "symlink_swap":
                    displaced = db_path.with_name(db_path.name + ".identity-original")
                    os.replace(db_path, displaced)
                    db_path.symlink_to(displaced)
                elif scenario == "rename_replacement":
                    os.replace(replacement, db_path.with_name(db_path.name + ".identity-next"))
                    os.replace(db_path.with_name(db_path.name + ".identity-next"), db_path)
                elif scenario == "replace_twice":
                    os.replace(replacement, db_path)
                    second = db_path.with_name(db_path.name + ".identity-second")
                    second.write_bytes(db_path.read_bytes())
                    os.replace(second, db_path)
                else:
                    os.replace(replacement, db_path)
            return digest

        with patch.object(maintenance_module, "sha256_file", side_effect=replace_database_during_scan):
            response = self._call_real_inventory_api(root)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(changed["done"])
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertEqual(payload["orphaned_artifacts"], [])
        self.assertTrue(
            "LIVE_DATABASE_IDENTITY_CHANGED_DURING_INVENTORY" in payload["issues"]
            or "LIVE_DATABASE_IDENTITY_UNSAFE_DURING_INVENTORY" in payload["issues"]
        )
        self.assertTrue(backup.exists())
    return test


_REAL_INVENTORY_DATABASE_IDENTITY_RACE_CASES = [
    ("test_database_identity_race_atomic_replace_01", "atomic_replace_01"),
    ("test_database_identity_race_atomic_replace_02", "atomic_replace_02"),
    ("test_database_identity_race_atomic_replace_03", "atomic_replace_03"),
    ("test_database_identity_race_atomic_replace_04", "atomic_replace_04"),
    ("test_database_identity_race_atomic_replace_05", "atomic_replace_05"),
    ("test_database_identity_race_atomic_replace_06", "atomic_replace_06"),
    ("test_database_identity_race_atomic_replace_07", "atomic_replace_07"),
    ("test_database_identity_race_atomic_replace_08", "atomic_replace_08"),
    ("test_database_identity_race_atomic_replace_09", "atomic_replace_09"),
    ("test_database_identity_race_atomic_replace_10", "atomic_replace_10"),
    ("test_database_identity_race_atomic_replace_11", "atomic_replace_11"),
    ("test_database_identity_race_atomic_replace_12", "atomic_replace_12"),
    ("test_database_identity_race_atomic_replace_13", "atomic_replace_13"),
    ("test_database_identity_race_atomic_replace_14", "atomic_replace_14"),
    ("test_database_identity_race_atomic_replace_15", "atomic_replace_15"),
    ("test_database_identity_race_atomic_replace_16", "atomic_replace_16"),
    ("test_database_identity_race_atomic_replace_17", "atomic_replace_17"),
    ("test_database_identity_race_atomic_replace_18", "atomic_replace_18"),
    ("test_database_identity_race_atomic_replace_19", "atomic_replace_19"),
    ("test_database_identity_race_symlink_swap", "symlink_swap"),
]
for _test_name, _scenario in _REAL_INVENTORY_DATABASE_IDENTITY_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_database_identity_race_test(_scenario))



def _make_real_inventory_postcheck_metadata_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("postcheck-metadata-race-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-postcheck-metadata.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(backup),),
            )
            if scenario == "attempt_delete":
                connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('baseline-delete','2026-11-20','FAILED','s','e',?)", (str(backup),))
            elif scenario == "plan_delete":
                connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','baseline','operator')", ("9" * 32, str(backup), str(backup), str(backup)))
            elif scenario == "artifact_delete":
                connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)", ("9" * 32, "a" * 64, "b" * 64, "c" * 64))
        original_verify = maintenance_module.verify_restore_execution_journal
        calls = {"count": 0, "changed": False}

        def mutate_after_final_journal_check(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 3 and not calls["changed"]:
                calls["changed"] = True
                with database.get_connection() as connection:
                    if scenario == "run_insert":
                        connection.execute("INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-19','SUPERSEDED','s','e',?)", (str(backup),))
                    elif scenario == "run_update":
                        connection.execute("UPDATE maintenance_runs SET completed_at='late-change' WHERE run_date='2026-11-20'")
                    elif scenario == "run_delete":
                        connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-20'")
                    elif scenario == "run_path":
                        connection.execute("UPDATE maintenance_runs SET backup_file=? WHERE run_date='2026-11-20'", (str(root / "late.sqlite3"),))
                    elif scenario == "attempt_insert":
                        connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('late-attempt','2026-11-20','FAILED','s','e',?)", (str(backup),))
                    elif scenario == "attempt_update":
                        connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('late-attempt','2026-11-20','FAILED','s','e',?)", (str(backup),))
                        connection.execute("UPDATE maintenance_run_attempts SET status='COMPLETE' WHERE attempt_token='late-attempt'")
                    elif scenario == "attempt_delete":
                        connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('late-attempt','2026-11-20','FAILED','s','e',?)", (str(backup),))
                        connection.execute("DELETE FROM maintenance_run_attempts WHERE attempt_token='baseline-delete'")
                    elif scenario == "attempt_path":
                        connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('late-attempt','2026-11-20','FAILED','s','e',?)", (str(backup),))
                        connection.execute("UPDATE maintenance_run_attempts SET backup_file=? WHERE attempt_token='late-attempt'", (str(root / "late-attempt.sqlite3"),))
                    elif scenario == "plan_insert":
                        connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','late','operator')", ("f" * 32, str(backup), str(backup), str(backup)))
                    elif scenario == "plan_update":
                        connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','late','operator')", ("f" * 32, str(backup), str(backup), str(backup)))
                        connection.execute("UPDATE maintenance_restore_plans SET status='FAILED' WHERE plan_id=?", ("f" * 32,))
                    elif scenario == "plan_delete":
                        connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','late','operator')", ("f" * 32, str(backup), str(backup), str(backup)))
                        connection.execute("DELETE FROM maintenance_restore_plans WHERE plan_id=?", ("9" * 32,))
                    elif scenario == "plan_path":
                        connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','late','operator')", ("f" * 32, str(backup), str(backup), str(backup)))
                        connection.execute("UPDATE maintenance_restore_plans SET staging_file=? WHERE plan_id=?", (str(root / "late-stage.sqlite3"), "f" * 32))
                    elif scenario == "artifact_insert":
                        connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)", ("f" * 32, "a" * 64, "b" * 64, "c" * 64))
                    elif scenario == "artifact_update":
                        connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)", ("f" * 32, "a" * 64, "b" * 64, "c" * 64))
                        connection.execute("UPDATE maintenance_restore_artifacts SET source_sha256=? WHERE plan_id=?", ("d" * 64, "f" * 32))
                    elif scenario == "artifact_delete":
                        connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)", ("f" * 32, "a" * 64, "b" * 64, "c" * 64))
                        connection.execute("DELETE FROM maintenance_restore_artifacts WHERE plan_id=?", ("9" * 32,))
                    elif scenario == "plan_operator":
                        connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','late','operator')", ("f" * 32, str(backup), str(backup), str(backup)))
                        connection.execute("UPDATE maintenance_restore_plans SET created_by='late-operator' WHERE plan_id=?", ("f" * 32,))
                    elif scenario == "run_timestamp":
                        connection.execute("UPDATE maintenance_runs SET started_at='late-start' WHERE run_date='2026-11-20'")
                    elif scenario == "schema_change":
                        connection.execute("ALTER TABLE maintenance_runs ADD COLUMN late_inventory_metadata_change TEXT")
                    elif scenario == "atomic_multi_table":
                        connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file) VALUES ('atomic-late','2026-11-20','SUPERSEDED','s','e',?)", (str(backup),))
                        connection.execute("UPDATE maintenance_runs SET status='SUPERSEDED' WHERE run_date='2026-11-20'")
                    elif scenario == "restore_artifact_roles":
                        connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES (?,?,?,?, 'PREPARED','late','operator')", ("e" * 32, str(backup), str(backup), str(backup)))
                        connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES (?,?,?,?)", ("e" * 32, "1" * 64, "2" * 64, "3" * 64))
            return result

        with patch.object(maintenance_module, "verify_restore_execution_journal", side_effect=mutate_after_final_journal_check):
            response = self._call_real_inventory_api(root)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(calls["changed"], f"mutation did not run; verify calls={calls['count']}")
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertIn("MAINTENANCE_METADATA_CHANGED_BEFORE_RESPONSE", payload["issues"])
        self.assertEqual(payload["orphaned_artifacts"], [])
    return test


_REAL_INVENTORY_POSTCHECK_METADATA_RACE_CASES = [
    ("test_postcheck_metadata_race_run_insert", "run_insert"),
    ("test_postcheck_metadata_race_run_update", "run_update"),
    ("test_postcheck_metadata_race_run_delete", "run_delete"),
    ("test_postcheck_metadata_race_run_path", "run_path"),
    ("test_postcheck_metadata_race_attempt_insert", "attempt_insert"),
    ("test_postcheck_metadata_race_attempt_update", "attempt_update"),
    ("test_postcheck_metadata_race_attempt_delete", "attempt_delete"),
    ("test_postcheck_metadata_race_attempt_path", "attempt_path"),
    ("test_postcheck_metadata_race_plan_insert", "plan_insert"),
    ("test_postcheck_metadata_race_plan_update", "plan_update"),
    ("test_postcheck_metadata_race_plan_delete", "plan_delete"),
    ("test_postcheck_metadata_race_plan_path", "plan_path"),
    ("test_postcheck_metadata_race_artifact_insert", "artifact_insert"),
    ("test_postcheck_metadata_race_artifact_update", "artifact_update"),
    ("test_postcheck_metadata_race_artifact_delete", "artifact_delete"),
    ("test_postcheck_metadata_race_plan_operator", "plan_operator"),
    ("test_postcheck_metadata_race_run_timestamp", "run_timestamp"),
    ("test_postcheck_metadata_race_schema_change", "schema_change"),
    ("test_postcheck_metadata_race_atomic_multi_table", "atomic_multi_table"),
    ("test_postcheck_metadata_race_restore_artifact_roles", "restore_artifact_roles"),
]
for _test_name, _scenario in _REAL_INVENTORY_POSTCHECK_METADATA_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_postcheck_metadata_race_test(_scenario))

def _make_real_inventory_combined_final_window_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("combined-final-window-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-combined.sqlite3")
        if scenario == "staging_remove":
            staging = root / "recovery-staging"
            staging.mkdir()
            self._make_inventory_backup(staging, "baseline-staged.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(backup),),
            )
        original_verify = maintenance_module.verify_restore_execution_journal
        calls = {"count": 0}
        def mutate(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            calls["count"] += 1
            if calls["count"] == 3:
                with database.get_connection() as connection:
                    if scenario == "add":
                        self._make_inventory_backup(root, "taimobot-added.sqlite3")
                    elif scenario == "remove":
                        backup.unlink()
                        connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-20'")
                    elif scenario == "rename":
                        moved = root / "taimobot-moved.sqlite3"
                        backup.rename(moved)
                        connection.execute("UPDATE maintenance_runs SET backup_file=? WHERE run_date='2026-11-20'", (str(moved),))
                    elif scenario == "replace":
                        replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "replacement", "taimobot-replacement.sqlite3")
                        replacement.replace(backup)
                    elif scenario == "edit":
                        with sqlite3.connect(backup) as db:
                            db.execute("CREATE TABLE late_change (value TEXT)")
                    elif scenario == "temp_add":
                        (root / ".taimobot-late.tmp").write_text("partial")
                    elif scenario == "symlink":
                        backup.unlink()
                        backup.symlink_to(root / "missing-target.sqlite3")
                    elif scenario == "nested":
                        (root / "late-directory").mkdir()
                    elif scenario == "same_name_swap":
                        replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "replacement-same", "taimobot-new.sqlite3")
                        replacement.replace(backup)
                    elif scenario == "staging_add":
                        staging = root / "recovery-staging"
                        staging.mkdir()
                        self._make_inventory_backup(staging, "late-stage.sqlite3")
                    elif scenario == "staging_remove":
                        staging = root / "recovery-staging"
                        for child in staging.iterdir(): child.unlink()
                        staging.rmdir()
                    elif scenario == "staging_replace":
                        staging = root / "recovery-staging"
                        staging.mkdir()
                        staged = self._make_inventory_backup(staging, "staged.sqlite3")
                        replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "stage-replacement", "new-stage.sqlite3")
                        replacement.replace(staged)
                    elif scenario == "staging_edit":
                        staging = root / "recovery-staging"
                        staging.mkdir()
                        staged = self._make_inventory_backup(staging, "staged.sqlite3")
                        with sqlite3.connect(staged) as db: db.execute("CREATE TABLE late_change (value TEXT)")
                    elif scenario == "db_update":
                        connection.execute("UPDATE maintenance_runs SET completed_at='changed' WHERE run_date='2026-11-20'")
                    elif scenario == "db_delete":
                        connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-20'")
                    elif scenario == "db_insert":
                        connection.execute("INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-19','FAILED','s','e',?)", (str(backup),))
                    elif scenario == "db_and_add":
                        self._make_inventory_backup(root, "taimobot-db-add.sqlite3")
                        connection.execute("UPDATE maintenance_runs SET completed_at='both' WHERE run_date='2026-11-20'")
                    elif scenario == "db_and_remove":
                        backup.unlink()
                        connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-20'")
                    elif scenario == "db_and_staging":
                        staging = root / "recovery-staging"
                        staging.mkdir()
                        self._make_inventory_backup(staging, "paired-stage.sqlite3")
                        connection.execute("UPDATE maintenance_runs SET completed_at='both-staging' WHERE run_date='2026-11-20'")
                    elif scenario == "file_permission":
                        backup.chmod(0o600)
                        connection.execute("UPDATE maintenance_runs SET completed_at='permission' WHERE run_date='2026-11-20'")
                    elif scenario == "multi_replace":
                        replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "replacement-many", "taimobot-many.sqlite3")
                        replacement.replace(backup)
                        connection.execute("UPDATE maintenance_runs SET completed_at='multi' WHERE run_date='2026-11-20'")
            return result
        with patch.object(maintenance_module, "verify_restore_execution_journal", side_effect=mutate):
            response = self._call_real_inventory_api(root)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls["count"], 3)
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertEqual(payload["orphaned_artifacts"], [])
        self.assertTrue(
            any(code in payload["issues"] for code in (
                "ARTIFACT_DIRECTORY_CHANGED_DURING_INVENTORY",
                "ARTIFACT_CHANGED_DURING_FINAL_CHECK",
                "MAINTENANCE_METADATA_CHANGED_BEFORE_RESPONSE",
            )),
            payload["issues"],
        )
    return test


_REAL_INVENTORY_COMBINED_FINAL_WINDOW_RACE_CASES = [
    ("test_combined_final_window_add", "add"),
    ("test_combined_final_window_remove", "remove"),
    ("test_combined_final_window_rename", "rename"),
    ("test_combined_final_window_replace", "replace"),
    ("test_combined_final_window_edit", "edit"),
    ("test_combined_final_window_temp_add", "temp_add"),
    ("test_combined_final_window_symlink", "symlink"),
    ("test_combined_final_window_nested", "nested"),
    ("test_combined_final_window_same_name_swap", "same_name_swap"),
    ("test_combined_final_window_staging_add", "staging_add"),
    ("test_combined_final_window_staging_remove", "staging_remove"),
    ("test_combined_final_window_staging_replace", "staging_replace"),
    ("test_combined_final_window_staging_edit", "staging_edit"),
    ("test_combined_final_window_db_update", "db_update"),
    ("test_combined_final_window_db_delete", "db_delete"),
    ("test_combined_final_window_db_insert", "db_insert"),
    ("test_combined_final_window_db_and_add", "db_and_add"),
    ("test_combined_final_window_db_and_remove", "db_and_remove"),
    ("test_combined_final_window_db_and_staging", "db_and_staging"),
    ("test_combined_final_window_file_permission", "file_permission"),
]
for _test_name, _scenario in _REAL_INVENTORY_COMBINED_FINAL_WINDOW_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_combined_final_window_race_test(_scenario))


def _make_real_inventory_final_consistency_fence_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("final-fence-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-fence.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(backup),),
            )

        if scenario.startswith("fs_"):
            original_get_connection = database.get_connection
            calls = {"fingerprint_reads": 0, "changed": False}

            class ConnectionProxy:
                def __init__(self, connection):
                    self.connection = connection
                def __enter__(self):
                    self.connection.__enter__()
                    return self
                def __exit__(self, *args):
                    return self.connection.__exit__(*args)
                def execute(self, sql, *args):
                    cursor = self.connection.execute(sql, *args)
                    if sql.strip() == "SELECT * FROM maintenance_runs":
                        calls["fingerprint_reads"] += 1
                        if calls["fingerprint_reads"] == 2 and not calls["changed"]:
                            calls["changed"] = True
                            if scenario == "fs_add":
                                (root / "taimobot-late.sqlite3").write_bytes(backup.read_bytes())
                            elif scenario == "fs_remove":
                                backup.unlink()
                            elif scenario == "fs_replace":
                                replacement = self_test._make_inventory_backup(
                                    Path(self_test.temp_dir.name) / "fence-replace", "replacement.sqlite3"
                                )
                                replacement.replace(backup)
                            elif scenario == "fs_edit":
                                with sqlite3.connect(backup) as db:
                                    db.execute("CREATE TABLE fence_edit (value TEXT)")
                            elif scenario == "fs_symlink":
                                backup.unlink()
                                backup.symlink_to(root / "missing.sqlite3")
                            elif scenario == "fs_nested":
                                (root / "late-directory").mkdir()
                            elif scenario == "fs_chmod":
                                backup.chmod(0o600)
                            elif scenario == "fs_temp":
                                (root / ".taimobot-partial.tmp").write_text("partial")
                            elif scenario == "fs_rename":
                                backup.rename(root / "taimobot-renamed.sqlite3")
                            elif scenario == "fs_stage":
                                staging = root / "recovery-staging"
                                staging.mkdir()
                                (staging / "late-stage.sqlite3").write_bytes(backup.read_bytes())
                    return cursor
                def __getattr__(self, name):
                    return getattr(self.connection, name)

            self_test = self
            with patch.object(database, "get_connection", side_effect=lambda: ConnectionProxy(original_get_connection())):
                response = self._call_real_inventory_api(root)
            self.assertTrue(calls["changed"], f"final metadata fingerprint hook not reached: {calls}")
        else:
            original_hash = maintenance_module.sha256_file
            calls = {"hash": 0, "changed": False}
            def mutate_metadata_during_second_fence(path):
                digest = original_hash(path)
                calls["hash"] += 1
                if calls["hash"] == 3 and not calls["changed"]:
                    calls["changed"] = True
                    with database.get_connection() as connection:
                        if scenario == "db_update":
                            connection.execute("UPDATE maintenance_runs SET completed_at='fence-update' WHERE run_date='2026-11-20'")
                        elif scenario == "db_delete":
                            connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-20'")
                        elif scenario == "db_insert":
                            connection.execute("INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-19','FAILED','s','e',?)", (str(backup),))
                        elif scenario == "db_path":
                            connection.execute("UPDATE maintenance_runs SET backup_file=? WHERE run_date='2026-11-20'", (str(root / "other.sqlite3"),))
                        elif scenario == "db_attempt_insert":
                            connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file,error_summary) VALUES ('attempt-fence','2026-11-20','FAILED','s','e',?,'x')", (str(backup),))
                        elif scenario == "db_attempt_update":
                            connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file,error_summary) VALUES ('attempt-fence','2026-11-20','FAILED','s','e',?,'x')", (str(backup),))
                            connection.execute("UPDATE maintenance_run_attempts SET error_summary='updated' WHERE attempt_token='attempt-fence'")
                        elif scenario == "db_schema":
                            connection.execute("ALTER TABLE maintenance_runs ADD COLUMN fence_marker TEXT")
                        elif scenario == "db_plan":
                            connection.execute("INSERT INTO maintenance_restore_plans (plan_id,backup_file,staging_file,pre_restore_backup_file,status,created_at,created_by) VALUES ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',?,?,?,'PREPARED','2026-11-20T00:00:00+02:00','operator')", (str(backup),str(root/'stage.sqlite3'),str(root/'rollback.sqlite3')))
                        elif scenario == "db_artifact":
                            connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id,source_sha256,staged_sha256,rollback_sha256) VALUES ('cccccccccccccccccccccccccccccccc',?,?,?)", ("a"*64,"b"*64,"c"*64))
                        elif scenario == "db_batch":
                            connection.execute("UPDATE maintenance_runs SET completed_at='batch' WHERE run_date='2026-11-20'")
                            connection.execute("INSERT INTO maintenance_run_attempts (attempt_token,run_date,status,started_at,completed_at,backup_file,error_summary) VALUES ('attempt-batch','2026-11-20','FAILED','s','e',?,'x')", (str(backup),))
                return digest
            with patch.object(maintenance_module, "sha256_file", side_effect=mutate_metadata_during_second_fence):
                response = self._call_real_inventory_api(root)
            self.assertTrue(calls["changed"], f"second filesystem fence hash not reached: {calls}")

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED", payload)
        self.assertEqual(payload["orphaned_artifacts"], [])
    return test


_REAL_INVENTORY_FINAL_CONSISTENCY_FENCE_CASES = [
    ("test_final_fence_fs_add", "fs_add"),
    ("test_final_fence_fs_remove", "fs_remove"),
    ("test_final_fence_fs_replace", "fs_replace"),
    ("test_final_fence_fs_edit", "fs_edit"),
    ("test_final_fence_fs_symlink", "fs_symlink"),
    ("test_final_fence_fs_nested", "fs_nested"),
    ("test_final_fence_fs_chmod", "fs_chmod"),
    ("test_final_fence_fs_temp", "fs_temp"),
    ("test_final_fence_fs_rename", "fs_rename"),
    ("test_final_fence_fs_stage", "fs_stage"),
    ("test_final_fence_db_update", "db_update"),
    ("test_final_fence_db_delete", "db_delete"),
    ("test_final_fence_db_insert", "db_insert"),
    ("test_final_fence_db_path", "db_path"),
    ("test_final_fence_db_attempt_insert", "db_attempt_insert"),
    ("test_final_fence_db_attempt_update", "db_attempt_update"),
    ("test_final_fence_db_schema", "db_schema"),
    ("test_final_fence_db_plan", "db_plan"),
    ("test_final_fence_db_artifact", "db_artifact"),
    ("test_final_fence_db_batch", "db_batch"),
]
for _test_name, _scenario in _REAL_INVENTORY_FINAL_CONSISTENCY_FENCE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_final_consistency_fence_test(_scenario))

def _make_real_inventory_terminal_fence_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("terminal-fence-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-terminal.sqlite3")
        self._create_inventory_metadata_tables()
        with database.get_connection() as connection:
            connection.execute(
                "INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-20','COMPLETE','start','end',?)",
                (str(backup),),
            )
        original_get_connection = database.get_connection
        original_sha256_file = maintenance_module.sha256_file
        calls = {"connections": 0, "hashes": 0, "changed": False}

        def mutate_database(connection):
            if scenario == "db_update":
                connection.execute("UPDATE maintenance_runs SET completed_at='terminal-update' WHERE run_date='2026-11-20'")
            elif scenario == "db_delete":
                connection.execute("DELETE FROM maintenance_runs WHERE run_date='2026-11-20'")
            elif scenario == "db_insert":
                connection.execute("INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-19','FAILED','s','e',?)", (str(backup),))
            elif scenario == "db_path":
                connection.execute("UPDATE maintenance_runs SET backup_file=? WHERE run_date='2026-11-20'", (str(root / "other.sqlite3"),))
            elif scenario == "db_status":
                connection.execute("UPDATE maintenance_runs SET status='FAILED' WHERE run_date='2026-11-20'")
            elif scenario == "db_timestamp":
                connection.execute("UPDATE maintenance_runs SET started_at='changed' WHERE run_date='2026-11-20'")
            elif scenario == "db_multi":
                connection.execute("UPDATE maintenance_runs SET status='SUPERSEDED',completed_at='multi' WHERE run_date='2026-11-20'")
            elif scenario == "db_second_row":
                connection.execute("INSERT INTO maintenance_runs (run_date,status,started_at,completed_at,backup_file) VALUES ('2026-11-18','COMPLETE','s','e',?)", (str(backup),))
            elif scenario == "db_clear_path":
                connection.execute("UPDATE maintenance_runs SET backup_file=NULL WHERE run_date='2026-11-20'")
            elif scenario == "db_schema":
                connection.execute("ALTER TABLE maintenance_runs ADD COLUMN terminal_marker TEXT")
            calls["changed"] = True

        @contextmanager
        def connection_with_terminal_mutation(*args, **kwargs):
            calls["connections"] += 1
            with original_get_connection(*args, **kwargs) as connection:
                yield connection
            # The third connection is the last metadata read before the
            # terminal filesystem fence. Mutating here models a writer landing
            # immediately after that snapshot.
            if scenario.startswith("fs_") and calls["connections"] == 3 and not calls["changed"]:
                if scenario == "fs_append":
                    with backup.open("ab") as handle: handle.write(b"late append")
                elif scenario == "fs_replace":
                    replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "terminal-replace", "replacement.sqlite3")
                    replacement.replace(backup)
                elif scenario == "fs_delete":
                    backup.unlink()
                elif scenario == "fs_touch":
                    os.utime(backup, None)
                elif scenario == "fs_add":
                    self._make_inventory_backup(root, "late-added.sqlite3")
                elif scenario == "fs_symlink":
                    backup.unlink()
                    backup.symlink_to(root / "missing-terminal-target.sqlite3")
                elif scenario == "fs_chmod":
                    backup.chmod(0o600)
                elif scenario == "fs_nested":
                    (root / "late-nested").mkdir()
                elif scenario == "fs_rename":
                    backup.rename(root / "renamed-terminal.sqlite3")
                elif scenario == "fs_staging":
                    staging = root / "recovery-staging"
                    staging.mkdir()
                    self._make_inventory_backup(staging, "late-stage.sqlite3")
                calls["changed"] = True

        def sha256_with_database_mutation(path, *args, **kwargs):
            result = original_sha256_file(path, *args, **kwargs)
            calls["hashes"] += 1
            # One scanned artifact: calls 1-3 are the original scan and the
            # first two fences; call 4 is the terminal filesystem fence.
            if scenario.startswith("db_") and calls["hashes"] == 4 and not calls["changed"]:
                with original_get_connection() as connection:
                    mutate_database(connection)
            return result

        patches = [
            patch.object(database, "get_connection", side_effect=connection_with_terminal_mutation),
            patch.object(maintenance_module, "sha256_file", side_effect=sha256_with_database_mutation),
        ]
        with patches[0], patches[1]:
            response = self._call_real_inventory_api(root)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(calls["changed"], f"mutation did not run: {calls}")
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED")
        self.assertEqual(payload["orphaned_artifacts"], [])
        expected = "ARTIFACT_CHANGED_DURING_FINAL_CHECK" if scenario.startswith("fs_") else "MAINTENANCE_METADATA_CHANGED_BEFORE_RESPONSE"
        self.assertIn(expected, payload["issues"], payload["issues"])
    return test


_REAL_INVENTORY_TERMINAL_FENCE_RACE_CASES = [
    ("test_terminal_fence_fs_append", "fs_append"),
    ("test_terminal_fence_fs_replace", "fs_replace"),
    ("test_terminal_fence_fs_delete", "fs_delete"),
    ("test_terminal_fence_fs_touch", "fs_touch"),
    ("test_terminal_fence_fs_add", "fs_add"),
    ("test_terminal_fence_fs_symlink", "fs_symlink"),
    ("test_terminal_fence_fs_chmod", "fs_chmod"),
    ("test_terminal_fence_fs_nested", "fs_nested"),
    ("test_terminal_fence_fs_rename", "fs_rename"),
    ("test_terminal_fence_fs_staging", "fs_staging"),
    ("test_terminal_fence_db_update", "db_update"),
    ("test_terminal_fence_db_delete", "db_delete"),
    ("test_terminal_fence_db_insert", "db_insert"),
    ("test_terminal_fence_db_path", "db_path"),
    ("test_terminal_fence_db_status", "db_status"),
    ("test_terminal_fence_db_timestamp", "db_timestamp"),
    ("test_terminal_fence_db_multi", "db_multi"),
    ("test_terminal_fence_db_second_row", "db_second_row"),
    ("test_terminal_fence_db_clear_path", "db_clear_path"),
    ("test_terminal_fence_db_schema", "db_schema"),
]
for _test_name, _scenario in _REAL_INVENTORY_TERMINAL_FENCE_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_terminal_fence_race_test(_scenario))

def _make_real_inventory_hash_to_sqlite_open_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("hash-open-race-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-hash-open.sqlite3")
        external = self._make_inventory_backup(Path(self.temp_dir.name) / ("external-" + scenario), "external.sqlite3")
        self._create_inventory_metadata_tables()
        original_hash = maintenance_module.sha256_file
        calls = {"hash": 0, "changed": False}
        def swap_after_hash(path, *args, **kwargs):
            digest = original_hash(path, *args, **kwargs)
            calls["hash"] += 1
            if path.name == backup.name and calls["hash"] == 1 and not calls["changed"]:
                calls["changed"] = True
                if scenario == "symlink_external":
                    backup.unlink()
                    backup.symlink_to(external)
                elif scenario == "replace_valid":
                    replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "replace-valid", backup.name)
                    replacement.replace(backup)
                elif scenario == "delete":
                    backup.unlink()
                elif scenario == "append":
                    with backup.open("ab") as handle: handle.write(b"raced")
                elif scenario == "truncate":
                    backup.write_bytes(b"not a sqlite database")
                elif scenario == "touch":
                    os.utime(backup, None)
                elif scenario == "chmod":
                    backup.chmod(0o600)
                elif scenario == "rename":
                    backup.rename(root / "renamed.sqlite3")
                elif scenario == "hardlink":
                    alias = root / "taimobot-hardlink.sqlite3"
                    os.link(backup, alias)
                elif scenario == "directory":
                    backup.unlink()
                    backup.mkdir()
                elif scenario == "temp_add":
                    (root / ".taimobot-raced.tmp").write_text("partial")
                elif scenario == "nested_add":
                    (root / "raced-directory").mkdir()
                elif scenario == "same_bytes_replace":
                    replacement = Path(self.temp_dir.name) / "same-bytes.sqlite3"
                    replacement.write_bytes(backup.read_bytes())
                    replacement.replace(backup)
                elif scenario == "symlink_dangling":
                    backup.unlink()
                    backup.symlink_to(root / "missing-target.sqlite3")
                elif scenario == "external_copy":
                    external_copy = Path(self.temp_dir.name) / "external-copy.sqlite3"
                    external_copy.write_bytes(external.read_bytes())
                    external_copy.replace(backup)
                elif scenario == "swap_twice":
                    replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "swap-twice", backup.name)
                    replacement.replace(backup)
                    backup.rename(root / "taimobot-original.sqlite3")
                    replacement2 = self._make_inventory_backup(Path(self.temp_dir.name) / "swap-twice-second", backup.name)
                    replacement2.replace(backup)
                elif scenario == "symlink_loop":
                    backup.unlink()
                    backup.symlink_to(backup)
                elif scenario == "sidecar":
                    Path(str(backup) + "-wal").write_bytes(b"wal")
                elif scenario == "directory_swap":
                    moved = root / "original.sqlite3"
                    backup.rename(moved)
                    replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "directory-swap", backup.name)
                    replacement.replace(backup)
                elif scenario == "replace_external_symlink":
                    backup.unlink()
                    backup.symlink_to(external)
            return digest
        with patch.object(maintenance_module, "sha256_file", side_effect=swap_after_hash):
            response = self._call_real_inventory_api(root)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(calls["changed"], f"race hook not reached: {calls}")
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED", payload)
        self.assertEqual(payload["orphaned_artifacts"], [])
    return test


_REAL_INVENTORY_HASH_TO_SQLITE_OPEN_RACE_CASES = [
    ("test_hash_open_symlink_external", "symlink_external"),
    ("test_hash_open_replace_valid", "replace_valid"),
    ("test_hash_open_delete", "delete"),
    ("test_hash_open_append", "append"),
    ("test_hash_open_truncate", "truncate"),
    ("test_hash_open_touch", "touch"),
    ("test_hash_open_chmod", "chmod"),
    ("test_hash_open_rename", "rename"),
    ("test_hash_open_hardlink", "hardlink"),
    ("test_hash_open_directory", "directory"),
    ("test_hash_open_temp_add", "temp_add"),
    ("test_hash_open_nested_add", "nested_add"),
    ("test_hash_open_same_bytes_replace", "same_bytes_replace"),
    ("test_hash_open_symlink_dangling", "symlink_dangling"),
    ("test_hash_open_external_copy", "external_copy"),
    ("test_hash_open_swap_twice", "swap_twice"),
    ("test_hash_open_symlink_loop", "symlink_loop"),
    ("test_hash_open_sidecar", "sidecar"),
    ("test_hash_open_directory_swap", "directory_swap"),
    ("test_hash_open_replace_external_symlink", "replace_external_symlink"),
]
for _test_name, _scenario in _REAL_INVENTORY_HASH_TO_SQLITE_OPEN_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_hash_to_sqlite_open_race_test(_scenario))


class RecoveryArtifactStreamingHashRaceTests(unittest.TestCase):
    """Deterministically mutate a real file while sha256_file streams it."""
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "streamed.sqlite3"
        self.original = b"TAIMOBOT-RECOVERY-ARTIFACT-" * 512
        self.path.write_bytes(self.original)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _mutate(self, scenario):
        if scenario == "append":
            with self.path.open("ab") as handle: handle.write(b"late")
        elif scenario == "truncate":
            self.path.write_bytes(b"short")
        elif scenario == "same_size_rewrite":
            self.path.write_bytes(b"X" * len(self.original))
        elif scenario == "touch":
            os.utime(self.path, None)
        elif scenario == "chmod":
            self.path.chmod(0o600)
        elif scenario == "unlink":
            self.path.unlink()
        elif scenario == "rename":
            self.path.rename(self.root / "renamed.sqlite3")
        elif scenario == "replace":
            replacement = self.root / "replacement.sqlite3"
            replacement.write_bytes(self.original)
            replacement.replace(self.path)
        elif scenario == "symlink":
            self.path.unlink()
            self.path.symlink_to(self.root / "missing-target")
        elif scenario == "hardlink":
            os.link(self.path, self.root / "second-link.sqlite3")
        elif scenario == "directory":
            self.path.unlink()
            self.path.mkdir()
        elif scenario == "replace_same_bytes":
            replacement = self.root / "same-bytes.sqlite3"
            replacement.write_bytes(self.original)
            replacement.replace(self.path)
        elif scenario == "write_restore":
            with self.path.open("r+b") as handle:
                handle.seek(0); handle.write(b"Z" * 64); handle.flush()
                handle.seek(0); handle.write(self.original[:64]); handle.flush()
        elif scenario == "append_then_truncate":
            with self.path.open("ab") as handle: handle.write(b"temporary")
            with self.path.open("r+b") as handle: handle.truncate(len(self.original))
        elif scenario == "replace_external":
            external = self.root / "external.sqlite3"
            external.write_bytes(b"E" * len(self.original))
            external.replace(self.path)
        elif scenario == "symlink_external":
            external = self.root / "external.sqlite3"
            external.write_bytes(self.original)
            self.path.unlink(); self.path.symlink_to(external)
        elif scenario == "hardlink_replace":
            os.link(self.path, self.root / "alias.sqlite3")
            replacement = self.root / "replacement.sqlite3"
            replacement.write_bytes(self.original); replacement.replace(self.path)
        elif scenario == "atomic_swap_twice":
            first = self.root / "first.sqlite3"; first.write_bytes(b"A" * len(self.original)); first.replace(self.path)
            second = self.root / "second.sqlite3"; second.write_bytes(b"B" * len(self.original)); second.replace(self.path)
        elif scenario == "utime_restore":
            before = self.path.stat()
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif scenario == "hardlink_create_remove":
            alias = self.root / "temporary-link.sqlite3"
            os.link(self.path, alias); alias.unlink()
        elif scenario == "truncate_restore":
            with self.path.open("r+b") as handle:
                handle.truncate(len(self.original) // 2)
                handle.truncate(len(self.original))

    def _assert_stream_mutation_rejected(self, scenario):
        real_fdopen = os.fdopen
        calls = {"read": 0, "mutated": False}
        self_outer = self
        class MutatingReader:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def read(self, size=-1):
                chunk = self.stream.read(size)
                calls["read"] += 1
                if calls["read"] == 1 and not calls["mutated"]:
                    calls["mutated"] = True
                    self_outer._mutate(scenario)
                return chunk
        def wrapped_fdopen(fd, *args, **kwargs):
            return MutatingReader(real_fdopen(fd, *args, **kwargs))
        with patch.object(maintenance_module.os, "fdopen", side_effect=wrapped_fdopen):
            with self.assertRaises(OSError, msg=scenario):
                sha256_file(self.path, chunk_size=64)
        self.assertTrue(calls["mutated"], scenario)
        self.assertGreaterEqual(calls["read"], 1)

    def test_stream_append_rejected(self): self._assert_stream_mutation_rejected("append")
    def test_stream_truncate_rejected(self): self._assert_stream_mutation_rejected("truncate")
    def test_stream_same_size_rewrite_rejected(self): self._assert_stream_mutation_rejected("same_size_rewrite")
    def test_stream_touch_rejected(self): self._assert_stream_mutation_rejected("touch")
    def test_stream_chmod_rejected(self): self._assert_stream_mutation_rejected("chmod")
    def test_stream_unlink_rejected(self): self._assert_stream_mutation_rejected("unlink")
    def test_stream_rename_rejected(self): self._assert_stream_mutation_rejected("rename")
    def test_stream_replace_rejected(self): self._assert_stream_mutation_rejected("replace")
    def test_stream_symlink_rejected(self): self._assert_stream_mutation_rejected("symlink")
    def test_stream_hardlink_rejected(self): self._assert_stream_mutation_rejected("hardlink")
    def test_stream_directory_rejected(self): self._assert_stream_mutation_rejected("directory")
    def test_stream_replace_same_bytes_rejected(self): self._assert_stream_mutation_rejected("replace_same_bytes")
    def test_stream_write_restore_rejected(self): self._assert_stream_mutation_rejected("write_restore")
    def test_stream_append_then_truncate_rejected(self): self._assert_stream_mutation_rejected("append_then_truncate")
    def test_stream_external_replace_rejected(self): self._assert_stream_mutation_rejected("replace_external")
    def test_stream_external_symlink_rejected(self): self._assert_stream_mutation_rejected("symlink_external")
    def test_stream_hardlink_replace_rejected(self): self._assert_stream_mutation_rejected("hardlink_replace")
    def test_stream_atomic_swap_twice_rejected(self): self._assert_stream_mutation_rejected("atomic_swap_twice")
    def test_stream_utime_restore_rejected(self): self._assert_stream_mutation_rejected("utime_restore")
    def test_stream_hardlink_create_remove_rejected(self): self._assert_stream_mutation_rejected("hardlink_create_remove")


class RecoveryArtifactRestoredMutationRaceTests(unittest.TestCase):
    """Prove transient edits cannot evade descriptor metadata checks by being restored."""
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "restored.sqlite3"
        self.original = (b"stable-recovery-payload-" * 4096)
        self.path.write_bytes(self.original)
        self.original_stat = self.path.stat()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _restore_mtime(self):
        os.utime(self.path, ns=(self.original_stat.st_atime_ns, self.original_stat.st_mtime_ns))

    def _mutate_restore(self, case):
        if case == "rewrite_head":
            with self.path.open("r+b") as f: f.write(b"X" * 128); f.flush(); f.seek(0); f.write(self.original[:128])
        elif case == "rewrite_middle":
            with self.path.open("r+b") as f: f.seek(4096); f.write(b"Y" * 128); f.flush(); f.seek(4096); f.write(self.original[4096:4224])
        elif case == "rewrite_tail":
            offset=len(self.original)-128
            with self.path.open("r+b") as f: f.seek(offset); f.write(b"Z"*128); f.flush(); f.seek(offset); f.write(self.original[offset:])
        elif case == "rewrite_two_regions":
            with self.path.open("r+b") as f:
                f.seek(0); f.write(b"A"*32); f.flush(); f.seek(0); f.write(self.original[:32])
                f.seek(8192); f.write(b"B"*32); f.flush(); f.seek(8192); f.write(self.original[8192:8224])
        elif case == "truncate_extend":
            with self.path.open("r+b") as f: f.truncate(100); f.truncate(len(self.original)); f.seek(100); f.write(self.original[100:])
        elif case == "append_truncate":
            with self.path.open("ab") as f: f.write(b"transient")
            with self.path.open("r+b") as f: f.truncate(len(self.original))
        elif case == "append_truncate_restore_mtime":
            with self.path.open("ab") as f: f.write(b"transient")
            with self.path.open("r+b") as f: f.truncate(len(self.original))
            self._restore_mtime()
        elif case == "rewrite_restore_mtime":
            with self.path.open("r+b") as f: f.write(b"C"*256); f.flush(); f.seek(0); f.write(self.original[:256])
            self._restore_mtime()
        elif case == "chmod_roundtrip":
            self.path.chmod(0o600); self.path.chmod(0o644)
        elif case == "hardlink_roundtrip":
            alias=self.root/"alias"; os.link(self.path,alias); alias.unlink()
        elif case == "rename_roundtrip":
            moved=self.root/"moved"; self.path.rename(moved); moved.rename(self.path)
        elif case == "replace_same_content":
            replacement=self.root/"replacement"; replacement.write_bytes(self.original); replacement.replace(self.path)
        elif case == "restore_original_inode":
            backup=self.root/"original-inode"; os.link(self.path,backup)
            replacement=self.root/"replacement"; replacement.write_bytes(b"Q"*len(self.original)); replacement.replace(self.path)
            backup.replace(self.path)
        elif case == "write_flush_fsync_restore":
            with self.path.open("r+b") as f:
                f.seek(2048); f.write(b"D"*64); f.flush(); os.fsync(f.fileno())
                f.seek(2048); f.write(self.original[2048:2112]); f.flush(); os.fsync(f.fileno())
            self._restore_mtime()
        elif case == "write_then_restore_atime_mtime":
            with self.path.open("r+b") as f: f.seek(512); f.write(b"E"*64); f.flush(); f.seek(512); f.write(self.original[512:576])
            os.utime(self.path, ns=(self.original_stat.st_atime_ns, self.original_stat.st_mtime_ns))
        elif case == "truncate_extend_restore_times":
            with self.path.open("r+b") as f: f.truncate(1); f.truncate(len(self.original)); f.seek(1); f.write(self.original[1:])
            self._restore_mtime()
        elif case == "replace_twice_original_inode":
            backup=self.root/"saved"; os.link(self.path,backup)
            one=self.root/"one"; one.write_bytes(b"1"*len(self.original)); one.replace(self.path)
            two=self.root/"two"; two.write_bytes(b"2"*len(self.original)); two.replace(self.path)
            backup.replace(self.path)
        elif case == "link_count_roundtrip_twice":
            for index in range(2):
                alias=self.root/f"alias-{index}"; os.link(self.path,alias); alias.unlink()
        elif case == "rewrite_and_restore_multiple_chunks":
            with self.path.open("r+b") as f:
                for offset in (0, 4096, 8192, 12288):
                    f.seek(offset); f.write(b"R"*64); f.flush()
                    f.seek(offset); f.write(self.original[offset:offset+64]); f.flush()
            self._restore_mtime()
        elif case == "metadata_only_roundtrip":
            os.utime(self.path, ns=(self.original_stat.st_atime_ns, self.original_stat.st_mtime_ns+5000000))
            os.utime(self.path, ns=(self.original_stat.st_atime_ns, self.original_stat.st_mtime_ns))

    def _assert_transient_change_rejected(self, case):
        real_fdopen=os.fdopen
        state={"read":0,"changed":False}
        owner=self
        class MutatingReader:
            def __init__(self, stream): self.stream=stream
            def __enter__(self): return self
            def __exit__(self,*args): return self.stream.__exit__(*args)
            def read(self,size=-1):
                chunk=self.stream.read(size)
                state["read"]+=1
                if state["read"]==1 and not state["changed"]:
                    state["changed"]=True
                    owner._mutate_restore(case)
                return chunk
        def wrapped(fd,*args,**kwargs): return MutatingReader(real_fdopen(fd,*args,**kwargs))
        with patch.object(maintenance_module.os,"fdopen",side_effect=wrapped):
            with self.assertRaises(OSError,msg=case):
                sha256_file(self.path,chunk_size=64)
        self.assertTrue(state["changed"],case)
        self.assertGreaterEqual(state["read"],1)
        if self.path.is_file():
            self.assertEqual(self.path.read_bytes(),self.original,case)

    def test_transient_head_rewrite_rejected(self): self._assert_transient_change_rejected("rewrite_head")
    def test_transient_middle_rewrite_rejected(self): self._assert_transient_change_rejected("rewrite_middle")
    def test_transient_tail_rewrite_rejected(self): self._assert_transient_change_rejected("rewrite_tail")
    def test_transient_two_region_rewrite_rejected(self): self._assert_transient_change_rejected("rewrite_two_regions")
    def test_transient_truncate_extend_rejected(self): self._assert_transient_change_rejected("truncate_extend")
    def test_transient_append_truncate_rejected(self): self._assert_transient_change_rejected("append_truncate")
    def test_transient_append_truncate_restored_mtime_rejected(self): self._assert_transient_change_rejected("append_truncate_restore_mtime")
    def test_transient_rewrite_restored_mtime_rejected(self): self._assert_transient_change_rejected("rewrite_restore_mtime")
    def test_transient_chmod_roundtrip_rejected(self): self._assert_transient_change_rejected("chmod_roundtrip")
    def test_transient_hardlink_roundtrip_rejected(self): self._assert_transient_change_rejected("hardlink_roundtrip")
    def test_transient_rename_roundtrip_rejected(self): self._assert_transient_change_rejected("rename_roundtrip")
    def test_transient_same_content_replacement_rejected(self): self._assert_transient_change_rejected("replace_same_content")
    def test_transient_original_inode_restore_rejected(self): self._assert_transient_change_rejected("restore_original_inode")
    def test_transient_fsync_rewrite_restore_rejected(self): self._assert_transient_change_rejected("write_flush_fsync_restore")
    def test_transient_rewrite_restore_times_rejected(self): self._assert_transient_change_rejected("write_then_restore_atime_mtime")
    def test_transient_truncate_restore_times_rejected(self): self._assert_transient_change_rejected("truncate_extend_restore_times")
    def test_transient_two_replacements_restore_inode_rejected(self): self._assert_transient_change_rejected("replace_twice_original_inode")
    def test_transient_double_link_roundtrip_rejected(self): self._assert_transient_change_rejected("link_count_roundtrip_twice")
    def test_transient_multi_chunk_rewrite_restore_rejected(self): self._assert_transient_change_rejected("rewrite_and_restore_multiple_chunks")
    def test_transient_metadata_roundtrip_rejected(self): self._assert_transient_change_rejected("metadata_only_roundtrip")


def _make_real_inventory_post_terminal_metadata_fs_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("post-terminal-metadata-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-post-terminal.sqlite3")
        staging = root / "recovery-staging"
        if scenario == "staging_remove":
            staging.mkdir()
            self._make_inventory_backup(staging, "preexisting-stage.sqlite3")
        self._create_inventory_metadata_tables()
        original_get_connection = database.get_connection
        calls = {"connections": 0, "mutated": False}

        def mutate():
            if scenario == "append":
                with backup.open("ab") as handle: handle.write(b"late-after-terminal-metadata")
            elif scenario == "replace":
                replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "post-terminal-replacement", backup.name)
                replacement.replace(backup)
            elif scenario == "delete":
                backup.unlink()
            elif scenario == "touch":
                os.utime(backup, None)
            elif scenario == "chmod":
                backup.chmod(0o600)
            elif scenario == "symlink":
                backup.unlink(); backup.symlink_to(root / "missing-after-terminal.sqlite3")
            elif scenario == "same_bytes_replace":
                replacement = Path(self.temp_dir.name) / "same-bytes-after-terminal.sqlite3"
                replacement.write_bytes(backup.read_bytes()); replacement.replace(backup)
            elif scenario == "rename":
                backup.rename(root / "renamed-after-terminal.sqlite3")
            elif scenario == "hardlink":
                os.link(backup, root / "late-hardlink.sqlite3")
            elif scenario == "directory":
                backup.unlink(); backup.mkdir()
            elif scenario == "temp_add":
                (root / ".taimobot-late-terminal.tmp").write_text("partial")
            elif scenario == "staging_add":
                staging.mkdir(); self._make_inventory_backup(staging, "late-staged.sqlite3")
            elif scenario == "staging_remove":
                if staging.exists(): shutil.rmtree(staging)
            elif scenario == "external_symlink":
                external = self._make_inventory_backup(Path(self.temp_dir.name) / "external-post-terminal", "external.sqlite3")
                backup.unlink(); backup.symlink_to(external)
            elif scenario == "sidecar":
                Path(str(backup) + "-wal").write_bytes(b"late wal")
            elif scenario == "hardlink_then_replace":
                os.link(backup, root / "pre-replacement-link.sqlite3")
                replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "hardlink-replacement", backup.name)
                replacement.replace(backup)
            elif scenario == "directory_swap":
                backup.rename(root / "moved-after-terminal.sqlite3")
                replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "directory-swap-post-terminal", backup.name)
                replacement.replace(backup)
            elif scenario == "append_then_restore_size":
                with backup.open("ab") as handle: handle.write(b"temporary")
                with backup.open("r+b") as handle: handle.truncate(backup.stat().st_size - len(b"temporary"))
            elif scenario == "replace_twice":
                one = self._make_inventory_backup(Path(self.temp_dir.name) / "post-terminal-one", backup.name)
                one.replace(backup)
                two = self._make_inventory_backup(Path(self.temp_dir.name) / "post-terminal-two", backup.name)
                two.replace(backup)
            elif scenario == "nested_add":
                (root / "late-nested-terminal").mkdir()
            calls["mutated"] = True

        @contextmanager
        def connection_with_last_window_mutation(*args, **kwargs):
            calls["connections"] += 1
            with original_get_connection(*args, **kwargs) as connection:
                yield connection
            if calls["connections"] == 5 and not calls["mutated"]:
                mutate()

        with patch.object(database, "get_connection", side_effect=connection_with_last_window_mutation):
            response = self._call_real_inventory_api(root)
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(calls["connections"], 5, calls)
        self.assertTrue(calls["mutated"], calls)
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED", payload)
        self.assertEqual(payload["orphaned_artifacts"], [])

    return test


_REAL_INVENTORY_POST_TERMINAL_METADATA_FS_RACE_CASES = [
    ("test_post_terminal_fs_append", "append"),
    ("test_post_terminal_fs_replace", "replace"),
    ("test_post_terminal_fs_delete", "delete"),
    ("test_post_terminal_fs_touch", "touch"),
    ("test_post_terminal_fs_chmod", "chmod"),
    ("test_post_terminal_fs_symlink", "symlink"),
    ("test_post_terminal_fs_same_bytes_replace", "same_bytes_replace"),
    ("test_post_terminal_fs_rename", "rename"),
    ("test_post_terminal_fs_hardlink", "hardlink"),
    ("test_post_terminal_fs_directory", "directory"),
    ("test_post_terminal_fs_temp_add", "temp_add"),
    ("test_post_terminal_fs_staging_add", "staging_add"),
    ("test_post_terminal_fs_staging_remove", "staging_remove"),
    ("test_post_terminal_fs_external_symlink", "external_symlink"),
    ("test_post_terminal_fs_sidecar", "sidecar"),
    ("test_post_terminal_fs_hardlink_replace", "hardlink_then_replace"),
    ("test_post_terminal_fs_directory_swap", "directory_swap"),
    ("test_post_terminal_fs_append_truncate", "append_then_restore_size"),
    ("test_post_terminal_fs_replace_twice", "replace_twice"),
    ("test_post_terminal_fs_nested_add", "nested_add"),
]
for _test_name, _scenario in _REAL_INVENTORY_POST_TERMINAL_METADATA_FS_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_real_inventory_post_terminal_metadata_fs_race_test(_scenario))





def _make_managed_directory_metadata_race_test(mode, target):
    def test(self):
        root = Path(self.temp_dir.name) / ("directory-metadata-" + target + "-" + oct(mode))
        self._make_inventory_backup(root, "taimobot-directory-metadata.sqlite3")
        staging = root / "recovery-staging"
        staging.mkdir()
        self._make_inventory_backup(staging, "taimobot-staged-directory-metadata.sqlite3")
        self._create_inventory_metadata_tables()
        target_path = root if target == "root" else staging
        original_mode = target_path.stat(follow_symlinks=False).st_mode & 0o7777
        original_get_connection = database.get_connection
        calls = {"connections": 0, "mutated": False}

        @contextmanager
        def mutate_directory_after_terminal_metadata(*args, **kwargs):
            calls["connections"] += 1
            with original_get_connection(*args, **kwargs) as connection:
                yield connection
            if calls["connections"] == 5 and not calls["mutated"]:
                target_path.chmod(mode)
                calls["mutated"] = True

        try:
            with patch.object(database, "get_connection", side_effect=mutate_directory_after_terminal_metadata):
                response = self._call_real_inventory_api(root)
            payload = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(calls["mutated"], calls)
            self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED", payload)
            self.assertEqual(payload["orphaned_artifacts"], [])
        finally:
            target_path.chmod(original_mode)

    return test


_MANAGED_DIRECTORY_METADATA_RACE_CASES = [
    ("test_directory_metadata_root_mode_000", 0o000, "root"),
    ("test_directory_metadata_root_mode_400", 0o400, "root"),
    ("test_directory_metadata_root_mode_500", 0o500, "root"),
    ("test_directory_metadata_root_mode_600", 0o600, "root"),
    ("test_directory_metadata_root_mode_700", 0o700, "root"),
    ("test_directory_metadata_root_mode_711", 0o711, "root"),
    ("test_directory_metadata_root_mode_750", 0o750, "root"),
    ("test_directory_metadata_root_mode_755", 0o755, "root"),
    ("test_directory_metadata_root_mode_770", 0o770, "root"),
    ("test_directory_metadata_root_mode_777", 0o777, "root"),
    ("test_directory_metadata_staging_mode_000", 0o000, "staging"),
    ("test_directory_metadata_staging_mode_400", 0o400, "staging"),
    ("test_directory_metadata_staging_mode_500", 0o500, "staging"),
    ("test_directory_metadata_staging_mode_600", 0o600, "staging"),
    ("test_directory_metadata_staging_mode_700", 0o700, "staging"),
    ("test_directory_metadata_staging_mode_711", 0o711, "staging"),
    ("test_directory_metadata_staging_mode_750", 0o750, "staging"),
    ("test_directory_metadata_staging_mode_755", 0o755, "staging"),
    ("test_directory_metadata_staging_mode_770", 0o770, "staging"),
    ("test_directory_metadata_staging_mode_777", 0o777, "staging"),
]
for _test_name, _mode, _target in _MANAGED_DIRECTORY_METADATA_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_managed_directory_metadata_race_test(_mode, _target))


def _make_post_digest_return_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / ("post-digest-" + scenario)
        backup = self._make_inventory_backup(root, "taimobot-post-digest.sqlite3")
        staging = root / "recovery-staging"
        if scenario == "staging_remove":
            staging.mkdir()
            self._make_inventory_backup(staging, "preexisting-stage.sqlite3")
        self._create_inventory_metadata_tables()
        real_hash = maintenance_module.sha256_file
        calls = {"count": 0, "mutated": False}

        def mutate_after_digest():
            if scenario == "append":
                with backup.open("ab") as handle: handle.write(b"late")
            elif scenario == "rewrite":
                data = backup.read_bytes()
                backup.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            elif scenario == "touch":
                os.utime(backup, None)
            elif scenario == "chmod":
                backup.chmod(0o600)
            elif scenario == "replace":
                replacement = self._make_inventory_backup(Path(self.temp_dir.name) / ("replacement-" + scenario), backup.name)
                replacement.replace(backup)
            elif scenario == "delete":
                backup.unlink()
            elif scenario == "symlink":
                backup.unlink(); backup.symlink_to(root / "missing-target.sqlite3")
            elif scenario == "hardlink":
                os.link(backup, root / "late-hardlink.sqlite3")
            elif scenario == "rename":
                backup.rename(root / "renamed.sqlite3")
            elif scenario == "directory":
                backup.unlink(); backup.mkdir()
            elif scenario == "external_symlink":
                external = self._make_inventory_backup(Path(self.temp_dir.name) / "external-post-digest", "external.sqlite3")
                backup.unlink(); backup.symlink_to(external)
            elif scenario == "sidecar":
                Path(str(backup) + "-wal").write_bytes(b"late wal")
            elif scenario == "temp_add":
                (root / ".taimobot-late.tmp").write_text("partial")
            elif scenario == "staging_add":
                staging.mkdir(); self._make_inventory_backup(staging, "late-stage.sqlite3")
            elif scenario == "staging_remove":
                shutil.rmtree(staging)
            elif scenario == "same_bytes_replace":
                replacement = Path(self.temp_dir.name) / "same-bytes-replacement.sqlite3"
                replacement.write_bytes(backup.read_bytes()); replacement.replace(backup)
            elif scenario == "truncate_restore":
                original = backup.read_bytes()
                with backup.open("r+b") as handle: handle.truncate(1)
                backup.write_bytes(original)
            elif scenario == "replace_twice":
                one = self._make_inventory_backup(Path(self.temp_dir.name) / "replacement-one", backup.name)
                one.replace(backup)
                two = self._make_inventory_backup(Path(self.temp_dir.name) / "replacement-two", backup.name)
                two.replace(backup)
            elif scenario == "directory_swap":
                backup.rename(root / "moved.sqlite3")
                replacement = self._make_inventory_backup(Path(self.temp_dir.name) / "directory-swap", backup.name)
                replacement.replace(backup)
            elif scenario == "add_nested":
                (root / "late-nested").mkdir()
            calls["mutated"] = True

        def hash_then_mutate(path):
            digest = real_hash(path)
            calls["count"] += 1
            # One backup is hashed during inventory and in each of the three
            # final fences. Mutate only after the terminal fence's digest returns.
            if calls["count"] == 4:
                mutate_after_digest()
            return digest

        with patch.object(maintenance_module, "sha256_file", side_effect=hash_then_mutate):
            response = self._call_real_inventory_api(root)
        payload = response.json()
        self.assertTrue(calls["mutated"], calls)
        self.assertEqual(payload["status"], "RECOVERY_ARTIFACT_INVENTORY_BLOCKED", payload)
        self.assertEqual(payload["orphaned_artifacts"], [])

    return test


_POST_DIGEST_RETURN_RACE_CASES = [
    ("test_post_digest_append", "append"),
    ("test_post_digest_rewrite", "rewrite"),
    ("test_post_digest_touch", "touch"),
    ("test_post_digest_chmod", "chmod"),
    ("test_post_digest_replace", "replace"),
    ("test_post_digest_delete", "delete"),
    ("test_post_digest_symlink", "symlink"),
    ("test_post_digest_hardlink", "hardlink"),
    ("test_post_digest_rename", "rename"),
    ("test_post_digest_directory", "directory"),
    ("test_post_digest_external_symlink", "external_symlink"),
    ("test_post_digest_sidecar", "sidecar"),
    ("test_post_digest_temp_add", "temp_add"),
    ("test_post_digest_staging_add", "staging_add"),
    ("test_post_digest_staging_remove", "staging_remove"),
    ("test_post_digest_same_bytes_replace", "same_bytes_replace"),
    ("test_post_digest_truncate_restore", "truncate_restore"),
    ("test_post_digest_replace_twice", "replace_twice"),
    ("test_post_digest_directory_swap", "directory_swap"),
    ("test_post_digest_nested_add", "add_nested"),
]
for _test_name, _scenario in _POST_DIGEST_RETURN_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_post_digest_return_race_test(_scenario))


def _make_restore_candidate_source_race_test(scenario):
    def test(self):
        root = Path(self.temp_dir.name) / "restore-source-races"
        root.mkdir()
        source = self._make_inventory_backup(root, "taimobot-source.sqlite3")
        staging = root / "recovery-staging" / "candidate.sqlite3"
        live_before = maintenance_module.sha256_file(database.DB_PATH)
        real_read = os.read
        mutated = {"done": False}

        def mutate():
            alternate_dir = Path(self.temp_dir.name) / "restore-source-alternates"
            alternate = self._make_inventory_backup(
                alternate_dir, "taimobot-alternate.sqlite3"
            )
            preserved = root / "preserved-source.sqlite3"
            if scenario == "append":
                with source.open("ab") as handle:
                    handle.write(b"late-append")
            elif scenario == "rewrite":
                with source.open("r+b") as handle:
                    handle.seek(0)
                    handle.write(b"X" * 32)
                    handle.flush()
            elif scenario == "touch":
                os.utime(source, None)
            elif scenario == "chmod":
                source.chmod(0o400)
            elif scenario == "replace":
                alternate.replace(source)
            elif scenario == "delete":
                source.unlink()
            elif scenario == "symlink":
                source.rename(preserved)
                source.symlink_to(preserved)
            elif scenario == "hardlink":
                source.rename(preserved)
                os.link(preserved, source)
            elif scenario == "rename":
                source.rename(preserved)
            elif scenario == "directory":
                source.rename(preserved)
                source.mkdir()
            elif scenario == "external_symlink":
                source.unlink()
                source.symlink_to(alternate)
            elif scenario == "truncate_restore":
                original = source.read_bytes()
                with source.open("r+b") as handle:
                    handle.truncate(1)
                source.write_bytes(original)
            elif scenario == "same_bytes_replace":
                replacement = root / "same-bytes.sqlite3"
                replacement.write_bytes(source.read_bytes())
                replacement.replace(source)
            elif scenario == "replace_twice":
                source.rename(preserved)
                alternate.replace(source)
            elif scenario == "append_truncate_restore":
                original = source.read_bytes()
                with source.open("ab") as handle:
                    handle.write(b"temporary")
                with source.open("r+b") as handle:
                    handle.truncate(len(original))
            elif scenario == "rename_roundtrip":
                source.rename(preserved)
                preserved.rename(source)
            elif scenario == "hardlink_roundtrip":
                os.link(source, preserved)
                source.unlink()
                preserved.rename(source)
            elif scenario == "chmod_roundtrip":
                original_mode = source.stat().st_mode & 0o777
                source.chmod(0o400 if original_mode != 0o400 else 0o600)
                source.chmod(original_mode)
            elif scenario == "rewrite_restore":
                original = source.read_bytes()
                with source.open("r+b") as handle:
                    handle.write(b"Z" * min(64, len(original)))
                    handle.flush()
                    handle.seek(0)
                    handle.write(original[:min(64, len(original))])
            elif scenario == "directory_swap":
                moved_root = root.with_name("restore-source-races-moved")
                root.rename(moved_root)
                root.mkdir()
                alternate.replace(root / source.name)
            elif scenario == "utime_restore":
                original = source.stat()
                os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns + 1000000))
                os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
            mutated["done"] = True

        def read_then_mutate(fd, size):
            chunk = real_read(fd, size)
            if chunk and not mutated["done"]:
                mutate()
            return chunk

        real_tempdir = tempfile.TemporaryDirectory
        snapshot_directories = []

        def tracked_tempdir(*args, **kwargs):
            temporary = real_tempdir(*args, **kwargs)
            snapshot_directories.append(Path(temporary.name))
            return temporary

        with patch("app.maintenance.tempfile.TemporaryDirectory", side_effect=tracked_tempdir):
            with patch("app.maintenance.os.read", side_effect=read_then_mutate):
                with self.assertRaises(OSError):
                    verify_restore_candidate(source, staging)

        self.assertTrue(mutated["done"], scenario)
        self.assertTrue(all(not path.exists() for path in snapshot_directories), scenario)
        self.assertFalse(staging.exists(), scenario)
        self.assertEqual(maintenance_module.sha256_file(database.DB_PATH), live_before)
        self.assertEqual(
            list(staging.parent.glob("*.source.tmp")) if staging.parent.exists() else [],
            [],
        )

    return test


_RESTORE_CANDIDATE_SOURCE_RACE_CASES = [
    ("test_restore_source_append_race", "append"),
    ("test_restore_source_rewrite_race", "rewrite"),
    ("test_restore_source_touch_race", "touch"),
    ("test_restore_source_chmod_race", "chmod"),
    ("test_restore_source_replace_race", "replace"),
    ("test_restore_source_delete_race", "delete"),
    ("test_restore_source_symlink_race", "symlink"),
    ("test_restore_source_hardlink_race", "hardlink"),
    ("test_restore_source_rename_race", "rename"),
    ("test_restore_source_directory_race", "directory"),
    ("test_restore_source_external_symlink_race", "external_symlink"),
    ("test_restore_source_truncate_restore_race", "truncate_restore"),
    ("test_restore_source_same_bytes_replace_race", "same_bytes_replace"),
    ("test_restore_source_replace_twice_race", "replace_twice"),
    ("test_restore_source_append_truncate_restore_race", "append_truncate_restore"),
    ("test_restore_source_rename_roundtrip_race", "rename_roundtrip"),
    ("test_restore_source_hardlink_roundtrip_race", "hardlink_roundtrip"),
    ("test_restore_source_chmod_roundtrip_race", "chmod_roundtrip"),
    ("test_restore_source_rewrite_restore_race", "rewrite_restore"),
    ("test_restore_source_directory_swap_race", "directory_swap"),
]
for _test_name, _scenario in _RESTORE_CANDIDATE_SOURCE_RACE_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_restore_candidate_source_race_test(_scenario))


def test_restore_candidate_rejects_identical_source_and_staging_path(self):
    source = self._make_inventory_backup(
        Path(self.temp_dir.name) / "same-path", "taimobot-same.sqlite3"
    )
    original = source.read_bytes()
    with self.assertRaisesRegex(ValueError, "paths must be different"):
        verify_restore_candidate(source, source)
    self.assertEqual(source.read_bytes(), original)


DailyMaintenanceTests.test_restore_candidate_rejects_identical_source_and_staging_path = (
    test_restore_candidate_rejects_identical_source_and_staging_path
)


def _make_live_database_symlink_preflight_test(scenario):
    def test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "backups"
            staging_dir = backup_dir / "recovery-staging"
            staging_dir.mkdir(parents=True)
            source = backup_dir / "taimobot-source.sqlite3"
            rollback = backup_dir / "taimobot-rollback.sqlite3"
            staged = staging_dir / "taimobot-staged.sqlite3"
            valid_live_target = root / "valid-live-target.sqlite3"
            for target in (source, rollback, staged, valid_live_target):
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(target) as copy:
                    live.backup(copy)

            link = root / "live-link.sqlite3"
            if scenario == "absolute":
                link.symlink_to(valid_live_target.resolve())
            elif scenario == "relative":
                link.symlink_to(Path(valid_live_target.name))
            elif scenario == "relative_dot":
                link.symlink_to(Path(".") / valid_live_target.name)
            elif scenario == "relative_parent":
                nested = root / "nested"
                nested.mkdir()
                link = nested / "live.sqlite3"
                link.symlink_to(Path("..") / valid_live_target.name)
            elif scenario == "chain":
                first = root / "first-link.sqlite3"
                first.symlink_to(valid_live_target)
                link.symlink_to(first)
            elif scenario == "dangling":
                link.symlink_to(root / "missing-target.sqlite3")
            elif scenario == "external":
                outside = root / "external-target.sqlite3"
                shutil.copy2(valid_live_target, outside)
                link.symlink_to(outside)
            elif scenario == "unicode":
                target = root / "database-ḓ.sqlite3"
                valid_live_target.rename(target)
                link.symlink_to(target)
            elif scenario == "spaces":
                target = root / "live database copy.sqlite3"
                valid_live_target.rename(target)
                link.symlink_to(target)
            elif scenario == "hidden":
                target = root / ".hidden-live.sqlite3"
                valid_live_target.rename(target)
                link.symlink_to(target)
            elif scenario == "absolute_chain":
                first = root / "absolute-first.sqlite3"
                first.symlink_to(valid_live_target.resolve())
                link.symlink_to(first.resolve(strict=False))
            elif scenario == "directory_target":
                directory_target = root / "database-directory"
                directory_target.mkdir()
                link.symlink_to(directory_target)
            elif scenario == "self_loop":
                link.symlink_to(link.name)
            elif scenario == "two_node_loop":
                other = root / "other-link.sqlite3"
                link.symlink_to(other)
                other.symlink_to(link)
            elif scenario == "target_replaced":
                link.symlink_to(valid_live_target)
                replacement = root / "replacement.sqlite3"
                with sqlite3.connect(database.DB_PATH) as live, sqlite3.connect(replacement) as copy:
                    live.backup(copy)
                replacement.replace(valid_live_target)
            elif scenario == "target_hardlink":
                hard = root / "hard-linked-live.sqlite3"
                os.link(valid_live_target, hard)
                link.symlink_to(hard)
            elif scenario == "nested_target":
                nested = root / "sub" / "dir"
                nested.mkdir(parents=True)
                target = nested / "live.sqlite3"
                valid_live_target.rename(target)
                link.symlink_to(target)
            elif scenario == "uppercase_extension":
                target = root / "LIVE.SQLITE3"
                valid_live_target.rename(target)
                link.symlink_to(target)
            elif scenario == "long_name":
                target = root / ("live-" + "x" * 80 + ".sqlite3")
                valid_live_target.rename(target)
                link.symlink_to(target)
            elif scenario == "same_directory_alias":
                alias = root / "alias.sqlite3"
                alias.symlink_to(valid_live_target)
                link.symlink_to(alias)
            else:
                raise AssertionError("Unknown symlink scenario: " + scenario)

            before = {
                str(path): maintenance_module.sha256_file(path)
                for path in (source, rollback, staged)
            }
            with self.assertRaisesRegex(ValueError, "live database is missing or unsafe"):
                verify_restore_execution_readiness(
                    source, staged, rollback,
                    backup_dir=backup_dir,
                    live_database_path=link,
                )
            self.assertTrue(link.is_symlink(), scenario)
            self.assertEqual(
                {str(path): maintenance_module.sha256_file(path)
                 for path in (source, rollback, staged)},
                before,
            )

    return test


_LIVE_DATABASE_SYMLINK_PREFLIGHT_CASES = [
    ("test_live_db_symlink_absolute_preflight", "absolute"),
    ("test_live_db_symlink_relative_preflight", "relative"),
    ("test_live_db_symlink_relative_dot_preflight", "relative_dot"),
    ("test_live_db_symlink_relative_parent_preflight", "relative_parent"),
    ("test_live_db_symlink_chain_preflight", "chain"),
    ("test_live_db_dangling_symlink_preflight", "dangling"),
    ("test_live_db_external_symlink_preflight", "external"),
    ("test_live_db_unicode_target_symlink_preflight", "unicode"),
    ("test_live_db_spaces_target_symlink_preflight", "spaces"),
    ("test_live_db_hidden_target_symlink_preflight", "hidden"),
    ("test_live_db_absolute_chain_symlink_preflight", "absolute_chain"),
    ("test_live_db_directory_target_symlink_preflight", "directory_target"),
    ("test_live_db_self_loop_symlink_preflight", "self_loop"),
    ("test_live_db_two_node_loop_symlink_preflight", "two_node_loop"),
    ("test_live_db_replaced_target_symlink_preflight", "target_replaced"),
    ("test_live_db_hardlink_target_symlink_preflight", "target_hardlink"),
    ("test_live_db_nested_target_symlink_preflight", "nested_target"),
    ("test_live_db_uppercase_extension_symlink_preflight", "uppercase_extension"),
    ("test_live_db_long_name_target_symlink_preflight", "long_name"),
    ("test_live_db_same_directory_alias_symlink_preflight", "same_directory_alias"),
]
for _test_name, _scenario in _LIVE_DATABASE_SYMLINK_PREFLIGHT_CASES:
    setattr(DailyMaintenanceTests, _test_name, _make_live_database_symlink_preflight_test(_scenario))


def _make_restore_lock_unknown_state_test(exception_type, message):
    def test(self):
        marker = maintenance_module.restore_lock_path()
        with patch.object(Path, "lstat", side_effect=exception_type(message)) as inspect:
            with patch.object(Path, "exists", side_effect=AssertionError("exists() must not decide lock state")):
                self.assertTrue(is_restore_lock_active())
        inspect.assert_called_once_with()
    return test


_RESTORE_LOCK_UNKNOWN_STATE_CASES = [
    ("test_restore_lock_unknown_permission_error", PermissionError, "permission denied"),
    ("test_restore_lock_unknown_access_error", PermissionError, "access denied"),
    ("test_restore_lock_unknown_io_error", OSError, "I/O error"),
    ("test_restore_lock_unknown_stale_mount", OSError, "stale file handle"),
    ("test_restore_lock_unknown_device_error", OSError, "device unavailable"),
    ("test_restore_lock_unknown_network_error", OSError, "network filesystem unavailable"),
    ("test_restore_lock_unknown_resource_error", OSError, "resource temporarily unavailable"),
    ("test_restore_lock_unknown_interrupted_error", OSError, "inspection interrupted"),
    ("test_restore_lock_unknown_bad_descriptor", OSError, "bad descriptor"),
    ("test_restore_lock_unknown_not_directory", NotADirectoryError, "parent is not a directory"),
    ("test_restore_lock_unknown_is_directory", IsADirectoryError, "directory metadata unavailable"),
    ("test_restore_lock_unknown_read_only", PermissionError, "read-only filesystem"),
    ("test_restore_lock_unknown_operation_not_permitted", PermissionError, "operation not permitted"),
    ("test_restore_lock_unknown_input_output", OSError, "input/output failure"),
    ("test_restore_lock_unknown_file_table", OSError, "file table overflow"),
    ("test_restore_lock_unknown_too_many_links", OSError, "too many symbolic links"),
    ("test_restore_lock_unknown_quota", OSError, "filesystem quota exceeded"),
    ("test_restore_lock_unknown_remote_disconnect", OSError, "remote filesystem disconnected"),
    ("test_restore_lock_unknown_metadata_unavailable", OSError, "metadata unavailable"),
    ("test_restore_lock_unknown_unclassified_os_error", OSError, "unclassified filesystem failure"),
]
for _name, _error_type, _message in _RESTORE_LOCK_UNKNOWN_STATE_CASES:
    setattr(
        DailyMaintenanceTests,
        _name,
        _make_restore_lock_unknown_state_test(_error_type, _message),
    )


def test_restore_lock_confirmed_absence_is_the_only_unlocked_state(self):
    marker = maintenance_module.restore_lock_path()
    with patch.object(Path, "lstat", side_effect=FileNotFoundError(marker)):
        self.assertFalse(is_restore_lock_active())


DailyMaintenanceTests.test_restore_lock_confirmed_absence_is_the_only_unlocked_state = (
    test_restore_lock_confirmed_absence_is_the_only_unlocked_state
)


def _make_locked_preflight_final_lock_race_test(scenario, overrides):
    def test(self):
        backup_dir, source, staged, rollback, hashes = (
            self._make_locked_restore_artifacts("final-lock-" + scenario)
        )
        plan_id = "a" * 32
        lock = acquire_restore_lock(plan_id)
        original_inspect = maintenance_module.inspect_restore_lock
        initial_state = original_inspect()
        final_state = {**initial_state, **overrides}
        try:
            with patch(
                "app.maintenance.inspect_restore_lock",
                side_effect=[initial_state, final_state],
            ) as inspect:
                with self.assertRaisesRegex(
                    RuntimeError, "did not remain active through locked preflight"
                ):
                    verify_restore_execution_readiness_under_lock(
                        source, staged, rollback,
                        plan_id=plan_id,
                        lock_token=lock["lock_token"],
                        external_processes_stopped=True,
                        expected_artifact_hashes=hashes,
                        backup_dir=backup_dir,
                        live_database_path=database.DB_PATH,
                    )
                self.assertEqual(inspect.call_count, 2, scenario)
            self.assertTrue(is_restore_lock_active(), scenario)
        finally:
            release_restore_lock(lock["lock_token"])
    return test


_LOCKED_PREFLIGHT_FINAL_LOCK_RACE_CASES = [
    ("test_locked_preflight_final_lock_missing", {"status": "NO_RESTORE_LOCK", "marker_present": False}),
    ("test_locked_preflight_final_lock_abandoned", {"status": "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW"}),
    ("test_locked_preflight_final_lock_uninspectable", {"status": "RESTORE_LOCK_UNINSPECTABLE"}),
    ("test_locked_preflight_final_lock_unreadable", {"status": "RESTORE_LOCK_METADATA_UNREADABLE"}),
    ("test_locked_preflight_final_lock_unsafe_marker", {"status": "RESTORE_LOCK_UNSAFE_MARKER"}),
    ("test_locked_preflight_final_lock_unsafe_guard", {"status": "RESTORE_LOCK_UNSAFE_GUARD"}),
    ("test_locked_preflight_final_lock_changed", {"status": "RESTORE_LOCK_CHANGED_DURING_INSPECTION"}),
    ("test_locked_preflight_final_lock_guard_uninspectable", {"status": "RESTORE_LOCK_GUARD_UNINSPECTABLE"}),
    ("test_locked_preflight_final_lock_wrong_plan", {"plan_id": "b" * 32}),
    ("test_locked_preflight_final_lock_wrong_token", {"lock_token": "c" * 32}),
    ("test_locked_preflight_final_lock_empty_plan", {"plan_id": None}),
    ("test_locked_preflight_final_lock_empty_token", {"lock_token": None}),
    ("test_locked_preflight_final_lock_marker_absent", {"status": "NO_RESTORE_LOCK", "marker_present": False}),
    ("test_locked_preflight_final_lock_stale_process", {"status": "ABANDONED_RESTORE_MARKER_REQUIRES_OPERATOR_REVIEW", "process_id": 999999999}),
    ("test_locked_preflight_final_lock_unknown_status", {"status": "RESTORE_LOCK_STATE_UNKNOWN"}),
    ("test_locked_preflight_final_lock_wrong_plan_and_token", {"plan_id": "d" * 32, "lock_token": "e" * 32}),
    ("test_locked_preflight_final_lock_wrong_token_and_absent_marker", {"lock_token": "f" * 32, "marker_present": False}),
    ("test_locked_preflight_final_lock_wrong_plan_and_active_status", {"plan_id": "0" * 32, "status": "RESTORE_LOCK_ACTIVE"}),
    ("test_locked_preflight_final_lock_missing_owner_pid", {"process_id": None}),
    ("test_locked_preflight_final_lock_missing_created_at", {"created_at": None}),
]
for _name, _overrides in _LOCKED_PREFLIGHT_FINAL_LOCK_RACE_CASES:
    setattr(
        DailyMaintenanceTests,
        _name,
        _make_locked_preflight_final_lock_race_test(_name, _overrides),
    )


def _make_approved_preflight_final_fence_test(scenario, artifact_label=None, mutation=None):
    def test(self):
        zone = ZoneInfo("Africa/Johannesburg")
        now = datetime.now(zone)
        backup_dir, source, staged, rollback, hashes = (
            self._make_locked_restore_artifacts("approved-final-" + scenario)
        )
        plan_id = "b" * 32
        plan = {
            "plan_id": plan_id,
            "backup_file": str(source),
            "staging_file": str(staged),
            "pre_restore_backup_file": str(rollback),
            "status": "APPROVED",
            "created_at": now.isoformat(),
        }
        artifact_paths = {"source": source, "staged": staged, "rollback": rollback}
        acquired = {"lock": None}

        def readiness(*args, **kwargs):
            if mutation == "activate_lock":
                acquired["lock"] = acquire_restore_lock(plan_id)
            else:
                path = artifact_paths[artifact_label]
                original = path.read_bytes()
                if mutation == "append":
                    path.write_bytes(original + b"changed-during-readiness")
                elif mutation == "rewrite":
                    path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
                elif mutation == "truncate":
                    path.write_bytes(original[:-1])
                elif mutation == "replace":
                    replacement = path.with_name(path.name + ".replacement")
                    replacement.write_bytes(original + b"-replacement")
                    replacement.replace(path)
            return {"status": "RESTORE_EXECUTION_PREFLIGHT_PASSED"}

        try:
            with patch(
                "app.maintenance.verify_restore_execution_readiness",
                side_effect=readiness,
            ):
                if mutation == "activate_lock":
                    with self.assertRaisesRegex(ValueError, "lock became active"):
                        verify_approved_restore_plan(
                            plan, hashes, backup_dir=backup_dir,
                            live_database_path=database.DB_PATH, now=now,
                        )
                else:
                    with self.assertRaisesRegex(ValueError, "changed during approved restore preflight"):
                        verify_approved_restore_plan(
                            plan, hashes, backup_dir=backup_dir,
                            live_database_path=database.DB_PATH, now=now,
                        )
        finally:
            if acquired["lock"] is not None:
                release_restore_lock(acquired["lock"]["lock_token"])
        self.assertFalse(is_restore_lock_active(), scenario)
    return test


_APPROVED_PREFLIGHT_FINAL_FENCE_CASES = [
    ("test_approved_preflight_source_append_race", "source", "append"),
    ("test_approved_preflight_source_rewrite_race", "source", "rewrite"),
    ("test_approved_preflight_source_truncate_race", "source", "truncate"),
    ("test_approved_preflight_source_replace_race", "source", "replace"),
    ("test_approved_preflight_staged_append_race", "staged", "append"),
    ("test_approved_preflight_staged_rewrite_race", "staged", "rewrite"),
    ("test_approved_preflight_staged_truncate_race", "staged", "truncate"),
    ("test_approved_preflight_staged_replace_race", "staged", "replace"),
    ("test_approved_preflight_rollback_append_race", "rollback", "append"),
    ("test_approved_preflight_rollback_rewrite_race", "rollback", "rewrite"),
    ("test_approved_preflight_rollback_truncate_race", "rollback", "truncate"),
    ("test_approved_preflight_rollback_replace_race", "rollback", "replace"),
    ("test_approved_preflight_lock_appears_race_01", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_02", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_03", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_04", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_05", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_06", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_07", None, "activate_lock"),
    ("test_approved_preflight_lock_appears_race_08", None, "activate_lock"),
]
for _name, _artifact_label, _mutation in _APPROVED_PREFLIGHT_FINAL_FENCE_CASES:
    setattr(
        DailyMaintenanceTests,
        _name,
        _make_approved_preflight_final_fence_test(
            _name, _artifact_label, _mutation
        ),
    )


def _make_restore_approval_transaction_race_test(scenario):
    def test(self):
        from app import main as main_module
        from app.main import MaintenanceRestoreApproveRequest, approve_maintenance_restore
        from fastapi import HTTPException

        plan_id = "c" * 32
        backup_dir, source, staged, rollback = self._create_restore_approval_fixture(plan_id)
        inventory = self._ready_restore_inventory(backup_dir, source, staged, rollback)
        real_get_connection = main_module.get_connection
        calls = {"count": 0}

        def mutate_after_validation():
            with sqlite3.connect(str(database.DB_PATH)) as connection:
                if scenario == "plan_source_path":
                    connection.execute(
                        "UPDATE maintenance_restore_plans SET backup_file=? WHERE plan_id=?",
                        (str(backup_dir / "changed-source.sqlite3"), plan_id),
                    )
                elif scenario == "plan_staged_path":
                    connection.execute(
                        "UPDATE maintenance_restore_plans SET staging_file=? WHERE plan_id=?",
                        (str(backup_dir / "recovery-staging" / "changed-staged.sqlite3"), plan_id),
                    )
                elif scenario == "plan_rollback_path":
                    connection.execute(
                        "UPDATE maintenance_restore_plans SET pre_restore_backup_file=? WHERE plan_id=?",
                        (str(backup_dir / "changed-rollback.sqlite3"), plan_id),
                    )
                elif scenario == "plan_created_at":
                    connection.execute(
                        "UPDATE maintenance_restore_plans SET created_at=? WHERE plan_id=?",
                        (datetime.now(ZoneInfo("Africa/Johannesburg")).isoformat(), plan_id),
                    )
                elif scenario == "plan_status":
                    connection.execute(
                        "UPDATE maintenance_restore_plans SET status='EXPIRED' WHERE plan_id=?",
                        (plan_id,),
                    )
                elif scenario in ("hash_source", "hash_staged", "hash_rollback"):
                    column = {
                        "hash_source": "source_sha256",
                        "hash_staged": "staged_sha256",
                        "hash_rollback": "rollback_sha256",
                    }[scenario]
                    connection.execute(
                        f"UPDATE maintenance_restore_artifacts SET {column}=? WHERE plan_id=?",
                        ("0" * 64, plan_id),
                    )
                elif scenario == "artifact_row_delete":
                    connection.execute(
                        "DELETE FROM maintenance_restore_artifacts WHERE plan_id=?", (plan_id,)
                    )
                elif scenario == "plan_row_delete":
                    connection.execute(
                        "DELETE FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
                    )

            target = {
                "source_append": source,
                "source_rewrite": source,
                "source_truncate": source,
                "source_replace": source,
                "staged_append": staged,
                "staged_rewrite": staged,
                "staged_truncate": staged,
                "staged_replace": staged,
                "rollback_append": rollback,
                "rollback_rewrite": rollback,
                "rollback_truncate": rollback,
                "rollback_replace": rollback,
            }.get(scenario)
            if target is not None:
                original = target.read_bytes()
                if scenario.endswith("_append"):
                    target.write_bytes(original + b"changed-after-validation")
                elif scenario.endswith("_rewrite"):
                    target.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
                elif scenario.endswith("_truncate"):
                    target.write_bytes(original[:-1])
                elif scenario.endswith("_replace"):
                    replacement = target.with_name(target.name + ".race-replacement")
                    replacement.write_bytes(original + b"-replacement")
                    replacement.replace(target)

        def intercepted_get_connection():
            calls["count"] += 1
            connection = real_get_connection()
            if calls["count"] == 2:
                mutate_after_validation()
            return connection

        with patch("app.main.BACKUP_DIR", backup_dir), patch(
            "app.main.require_admin"
        ), patch(
            "app.main.inspect_recovery_candidates", side_effect=inventory
        ), patch(
            "app.main.get_connection", side_effect=intercepted_get_connection
        ):
            with self.assertRaises(HTTPException) as rejected:
                approve_maintenance_restore(
                    MaintenanceRestoreApproveRequest(plan_id=plan_id, confirm_approval=True),
                    {"email": "approver@example.com"},
                )

        self.assertEqual(rejected.exception.status_code, 409, scenario)
        self.assertEqual(calls["count"], 2, scenario)
        with database.get_connection() as connection:
            row = connection.execute(
                "SELECT status FROM maintenance_restore_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        self.assertTrue(row is None or row["status"] != "APPROVED", scenario)

    return test


_RESTORE_APPROVAL_TRANSACTION_RACE_CASES = [
    ("test_restore_approval_transaction_plan_source_path_race", "plan_source_path"),
    ("test_restore_approval_transaction_plan_staged_path_race", "plan_staged_path"),
    ("test_restore_approval_transaction_plan_rollback_path_race", "plan_rollback_path"),
    ("test_restore_approval_transaction_plan_created_at_race", "plan_created_at"),
    ("test_restore_approval_transaction_plan_status_race", "plan_status"),
    ("test_restore_approval_transaction_source_hash_metadata_race", "hash_source"),
    ("test_restore_approval_transaction_staged_hash_metadata_race", "hash_staged"),
    ("test_restore_approval_transaction_rollback_hash_metadata_race", "hash_rollback"),
    ("test_restore_approval_transaction_artifact_row_delete_race", "artifact_row_delete"),
    ("test_restore_approval_transaction_plan_row_delete_race", "plan_row_delete"),
    ("test_restore_approval_transaction_source_append_race", "source_append"),
    ("test_restore_approval_transaction_source_rewrite_race", "source_rewrite"),
    ("test_restore_approval_transaction_source_truncate_race", "source_truncate"),
    ("test_restore_approval_transaction_source_replace_race", "source_replace"),
    ("test_restore_approval_transaction_staged_append_race", "staged_append"),
    ("test_restore_approval_transaction_staged_rewrite_race", "staged_rewrite"),
    ("test_restore_approval_transaction_staged_truncate_race", "staged_truncate"),
    ("test_restore_approval_transaction_staged_replace_race", "staged_replace"),
    ("test_restore_approval_transaction_rollback_append_race", "rollback_append"),
    ("test_restore_approval_transaction_rollback_rewrite_race", "rollback_rewrite"),
]
for _name, _scenario in _RESTORE_APPROVAL_TRANSACTION_RACE_CASES:
    setattr(
        DailyMaintenanceTests,
        _name,
        _make_restore_approval_transaction_race_test(_scenario),
    )


def _make_restore_lock_timestamp_validation_test(value):
    def test(self):
        lock_path = maintenance_module.restore_lock_path()
        marker = {
            "plan_id": "a" * 32,
            "pid": os.getpid(),
            "created_at": value,
            "lock_token": "b" * 32,
        }
        lock_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        result = inspect_restore_lock()
        self.assertEqual(result["status"], "RESTORE_LOCK_METADATA_UNREADABLE")
        self.assertTrue(result["marker_present"])
        self.assertFalse(result.get("automatic_recovery_performed", False))
    return test


_RESTORE_LOCK_INVALID_TIMESTAMP_CASES = [["empty",""],["whitespace","   "],["plain_text","not-a-timestamp"],["date_only","2026-11-21"],["naive_datetime","2026-11-21T10:00:00"],["invalid_month","2026-13-21T10:00:00+02:00"],["zero_month","2026-00-21T10:00:00+02:00"],["invalid_day","2026-11-32T10:00:00+02:00"],["invalid_hour","2026-11-21T25:00:00+02:00"],["invalid_minute","2026-11-21T10:60:00+02:00"],["invalid_second","2026-11-21T10:00:60+02:00"],["invalid_offset_hour","2026-11-21T10:00:00+25:00"],["invalid_offset_minute","2026-11-21T10:00:00+02:99"],["trailing_garbage","2026-11-21T10:00:00+02:00garbage"],["bad_leap_day","2025-02-29T10:00:00+02:00"],["null",null],["integer",123],["boolean",true],["array",["2026-11-21T10:00:00+02:00"]],["object",{"timestamp":"2026-11-21T10:00:00+02:00"}]]
for _scenario, _value in _RESTORE_LOCK_INVALID_TIMESTAMP_CASES:
    setattr(
        DailyMaintenanceTests,
        "test_restore_lock_invalid_timestamp_" + _scenario,
        _make_restore_lock_timestamp_validation_test(_value),
    )


def _make_restore_lock_marker_metadata_race_test(scenario):
    def test(self):
        import fcntl as system_fcntl
        lock_path = maintenance_module.restore_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        marker = {
            "plan_id": "a" * 32,
            "pid": os.getpid(),
            "created_at": "2026-11-21T10:00:00+02:00",
            "lock_token": "b" * 32,
        }
        original_bytes = json.dumps(marker, sort_keys=True).encode("utf-8")
        lock_path.write_bytes(original_bytes)
        original_mode = lock_path.stat().st_mode
        alias = lock_path.with_name(lock_path.name + ".metadata-race-link")
        moved = lock_path.with_name(lock_path.name + ".metadata-race-moved")
        real_flock = system_fcntl.flock

        def mutate():
            if scenario == "chmod_roundtrip":
                lock_path.chmod(0o400)
                lock_path.chmod(original_mode & 0o777)
            elif scenario == "chmod_change":
                lock_path.chmod(0o400)
            elif scenario == "hardlink_roundtrip":
                os.link(lock_path, alias)
                alias.unlink()
            elif scenario == "hardlink_present":
                os.link(lock_path, alias)
            elif scenario == "touch_restore_mtime":
                old = lock_path.stat()
                os.utime(lock_path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
                os.utime(lock_path, ns=(old.st_atime_ns, old.st_mtime_ns))
            elif scenario == "touch_mtime":
                old = lock_path.stat()
                os.utime(lock_path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
            elif scenario == "rewrite_same_bytes":
                lock_path.write_bytes(original_bytes)
            elif scenario == "append_truncate":
                with lock_path.open("ab") as stream:
                    stream.write(b"x")
                lock_path.write_bytes(original_bytes)
            elif scenario == "truncate_restore":
                lock_path.write_bytes(b"")
                lock_path.write_bytes(original_bytes)
            elif scenario == "rename_roundtrip":
                lock_path.rename(moved)
                moved.rename(lock_path)
            elif scenario == "replace_same_bytes":
                replacement = lock_path.with_name(lock_path.name + ".replacement")
                replacement.write_bytes(original_bytes)
                os.replace(replacement, lock_path)
            elif scenario == "unlink_recreate":
                lock_path.unlink()
                lock_path.write_bytes(original_bytes)
            elif scenario == "symlink_swap":
                target = lock_path.with_name(lock_path.name + ".target")
                target.write_bytes(original_bytes)
                lock_path.unlink()
                lock_path.symlink_to(target)
            elif scenario == "directory_swap":
                lock_path.unlink()
                lock_path.mkdir()
            elif scenario == "append_only":
                with lock_path.open("ab") as stream:
                    stream.write(b" ")
            elif scenario == "truncate_only":
                lock_path.write_bytes(original_bytes[:-1])
            elif scenario == "chmod_executable_roundtrip":
                lock_path.chmod(0o700)
                lock_path.chmod(original_mode & 0o777)
            elif scenario == "hardlink_replace":
                os.link(lock_path, alias)
                os.replace(alias, lock_path)
            elif scenario == "double_replacement":
                for suffix in (".one", ".two"):
                    replacement = lock_path.with_name(lock_path.name + suffix)
                    replacement.write_bytes(original_bytes)
                    os.replace(replacement, lock_path)
            elif scenario == "metadata_then_content_restore":
                lock_path.chmod(0o400)
                lock_path.chmod(original_mode & 0o777)
                lock_path.write_bytes(original_bytes)
                lock_path.chmod(original_mode & 0o777)
            elif scenario == "link_then_rename":
                os.link(lock_path, alias)
                lock_path.rename(moved)
                moved.rename(lock_path)
                alias.unlink()

        def mutate_then_probe(fd, operation):
            mutate()
            return real_flock(fd, operation)

        try:
            with patch("app.maintenance.fcntl.flock", side_effect=mutate_then_probe):
                result = inspect_restore_lock()
            self.assertEqual(result["status"], "RESTORE_LOCK_CHANGED_DURING_INSPECTION", scenario)
        finally:
            for path in (lock_path, alias, moved):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
    return test


_RESTORE_LOCK_MARKER_METADATA_RACE_CASES = [
    "chmod_roundtrip", "chmod_change", "hardlink_roundtrip", "hardlink_present",
    "touch_restore_mtime", "touch_mtime", "rewrite_same_bytes", "append_truncate",
    "truncate_restore", "rename_roundtrip", "replace_same_bytes", "unlink_recreate",
    "symlink_swap", "directory_swap", "append_only", "truncate_only",
    "chmod_executable_roundtrip", "hardlink_replace", "double_replacement",
    "metadata_then_content_restore",
]
for _scenario in _RESTORE_LOCK_MARKER_METADATA_RACE_CASES:
    setattr(
        DailyMaintenanceTests,
        "test_restore_lock_marker_metadata_race_" + _scenario,
        _make_restore_lock_marker_metadata_race_test(_scenario),
    )


def _make_restore_guard_descriptor_race_test(scenario):
    def test(self):
        import fcntl as system_fcntl
        lock_path = maintenance_module.restore_lock_path()
        guard_path = database.restore_guard_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        guard_path.unlink(missing_ok=True)
        marker = {
            "plan_id": "a" * 32,
            "pid": os.getpid(),
            "created_at": "2026-11-21T10:00:00+02:00",
            "lock_token": "b" * 32,
        }
        lock_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
        alias = guard_path.with_name(guard_path.name + ".race-link")
        moved = guard_path.with_name(guard_path.name + ".race-moved")
        target = guard_path.with_name(guard_path.name + ".race-target")
        real_flock = system_fcntl.flock

        def mutate():
            if scenario == "chmod_roundtrip":
                guard_path.chmod(0o400)
                guard_path.chmod(0o600)
            elif scenario == "chmod_change":
                guard_path.chmod(0o400)
            elif scenario == "hardlink_roundtrip":
                os.link(guard_path, alias)
                alias.unlink()
            elif scenario == "hardlink_present":
                os.link(guard_path, alias)
            elif scenario == "touch_restore_mtime":
                old = guard_path.stat()
                os.utime(guard_path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
                os.utime(guard_path, ns=(old.st_atime_ns, old.st_mtime_ns))
            elif scenario == "touch_mtime":
                old = guard_path.stat()
                os.utime(guard_path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
            elif scenario == "rewrite_same_bytes":
                guard_path.write_bytes(guard_path.read_bytes())
            elif scenario == "append_truncate":
                with guard_path.open("ab") as stream:
                    stream.write(b"x")
                guard_path.write_bytes(b"")
            elif scenario == "rename_roundtrip":
                guard_path.rename(moved)
                moved.rename(guard_path)
            elif scenario == "replace_same_bytes":
                target.write_bytes(guard_path.read_bytes())
                os.replace(target, guard_path)
            elif scenario == "unlink_recreate":
                guard_path.unlink()
                guard_path.write_bytes(b"")
            elif scenario == "symlink_swap":
                target.write_bytes(b"")
                guard_path.unlink()
                guard_path.symlink_to(target)
            elif scenario == "directory_swap":
                guard_path.unlink()
                guard_path.mkdir()
            elif scenario == "append_only":
                with guard_path.open("ab") as stream:
                    stream.write(b"x")
            elif scenario == "truncate_only":
                guard_path.write_bytes(b"")
            elif scenario == "chmod_executable_roundtrip":
                guard_path.chmod(0o700)
                guard_path.chmod(0o600)
            elif scenario == "hardlink_replace":
                os.link(guard_path, alias)
                os.replace(alias, guard_path)
            elif scenario == "double_replacement":
                for suffix in (".one", ".two"):
                    replacement = guard_path.with_name(guard_path.name + suffix)
                    replacement.write_bytes(b"")
                    os.replace(replacement, guard_path)
            elif scenario == "metadata_then_content_restore":
                guard_path.chmod(0o400)
                guard_path.chmod(0o600)
                guard_path.write_bytes(b"")
                guard_path.chmod(0o600)
            elif scenario == "link_then_rename":
                os.link(guard_path, alias)
                guard_path.rename(moved)
                moved.rename(guard_path)
                alias.unlink()

        def mutate_then_probe(fd, operation):
            mutate()
            return real_flock(fd, operation)

        try:
            with patch("app.maintenance.fcntl.flock", side_effect=mutate_then_probe):
                result = inspect_restore_lock()
            self.assertEqual(result["status"], "RESTORE_LOCK_GUARD_UNINSPECTABLE", scenario)
        finally:
            for path in (lock_path, guard_path, alias, moved, target):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
    return test


_RESTORE_GUARD_DESCRIPTOR_RACE_CASES = [
    "chmod_roundtrip", "chmod_change", "hardlink_roundtrip", "hardlink_present",
    "touch_restore_mtime", "touch_mtime", "rewrite_same_bytes", "append_truncate",
    "rename_roundtrip", "replace_same_bytes", "unlink_recreate", "symlink_swap",
    "directory_swap", "append_only", "truncate_only", "chmod_executable_roundtrip",
    "hardlink_replace", "double_replacement", "metadata_then_content_restore",
    "link_then_rename",
]
for _scenario in _RESTORE_GUARD_DESCRIPTOR_RACE_CASES:
    setattr(
        DailyMaintenanceTests,
        "test_restore_guard_descriptor_race_" + _scenario,
        _make_restore_guard_descriptor_race_test(_scenario),
    )
