"""Run ten restore marker crash-boundary checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_marker_crash_boundaries", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_restore_lock_fsyncs_parent_directory_before_reporting_acquired",
    "test_restore_lock_directory_publish_sync_failure_removes_exact_marker",
    "test_restore_lock_marker_file_sync_failure_never_reports_success",
    "test_restore_lock_preexisting_marker_is_never_overwritten",
    "test_restore_lock_marker_symlink_collision_preserves_target",
    "test_restore_lock_process_guard_is_held_during_publish_directory_sync",
    "test_restore_lock_failed_publication_does_not_change_database",
    "test_restore_lock_failed_publication_preserves_sqlite_sidecars",
    "test_restore_lock_failed_publication_releases_guard_for_new_attempt",
    "test_restore_lock_publish_directory_sync_precedes_sidecar_inventory",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE MARKER CRASH BOUNDARIES: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE MARKER CRASH BOUNDARIES: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
