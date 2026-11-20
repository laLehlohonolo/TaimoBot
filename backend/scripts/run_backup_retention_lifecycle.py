"""Run ten backup-retention lifecycle checks sequentially."""
import sys
import unittest

from tests.test_maintenance import DailyMaintenanceTests


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
    suite = unittest.TestSuite(
        DailyMaintenanceTests(name) for name in TESTS
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"BACKUP RETENTION GROUP: {result.testsRun}/{len(TESTS)} sequential checks passed")


if __name__ == "__main__":
    main()
