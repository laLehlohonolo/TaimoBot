"""Run ten restore recovery crash-durability checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_recovery_crash_durability", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_recovery_syncs_separate_audit_directory_before_marker_unlink",
    "test_recovery_audit_directory_sync_failure_preserves_marker",
    "test_recovery_audit_directory_sync_failure_does_not_write_started_event",
    "test_recovery_started_audit_fsync_failure_preserves_marker",
    "test_recovery_marker_directory_sync_failure_leaves_pending_started_for_review",
    "test_recovery_marker_unlink_failure_leaves_started_audit_and_marker",
    "test_recovery_after_unlink_before_complete_is_not_automatically_resumed",
    "test_recovery_pending_started_blocks_replacement_marker_after_interruption",
    "test_recovery_crash_boundaries_preserve_database_and_sidecars",
    "test_recovery_crash_boundary_preserves_backup_and_restore_artifacts",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE RECOVERY CRASH DURABILITY: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE RECOVERY CRASH DURABILITY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
