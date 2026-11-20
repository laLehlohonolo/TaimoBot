"""Run ten backup-retention lifecycle checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location(
    "backup_retention_lifecycle", ROOT / "tests" / "test_maintenance.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TESTS = [
    "test_backup_retention_dry_run_then_deletes_only_expired_verified_files",
    "test_backup_retention_keeps_backup_exactly_at_one_year_boundary",
    "test_backup_replaced_with_recent_file_during_delete_is_preserved",
    "test_backup_retention_reports_blocked_when_expired_backup_cannot_be_deleted",
    "test_backup_retention_fails_safe_without_recent_verified_recovery_point",
    "test_backup_retention_preserves_latest_recorded_and_active_attempt_backups",
    "test_backup_retention_preserves_latest_failed_and_superseded_verified_attempts",
    "test_backup_retention_fails_closed_when_maintenance_metadata_cannot_be_read",
    "test_backup_retention_never_deletes_live_database_or_corrupt_backup",
    "test_backup_retention_rejects_less_than_one_year",
]


def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in TESTS)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(
            f"BACKUP RETENTION LIFECYCLE: {result.testsRun}/10; "
            f"failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print("TAIMOBOT BACKUP RETENTION LIFECYCLE: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
