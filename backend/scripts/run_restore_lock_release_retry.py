"""Run ten restore-lock release retry checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_lock_release_retry", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_restore_release_retry_finishes_directory_sync_after_prior_unlink",
    "test_restore_release_retry_keeps_guard_when_directory_sync_fails_again",
    "test_restore_release_retry_never_removes_replacement_marker",
    "test_restore_release_retry_rejects_symlink_replacement",
    "test_restore_release_retry_requires_original_in_process_guard_token",
    "test_restore_release_without_guard_never_releases_missing_marker",
    "test_restore_release_rejects_duplicate_json_keys_without_unlinking_marker",
    "test_restore_release_rejects_non_object_json_without_unlinking_marker",
    "test_restore_release_retry_syncs_parent_directory_before_guard_release",
    "test_restore_release_retry_preserves_database_and_sqlite_sidecars",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE LOCK RELEASE RETRY: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE LOCK RELEASE RETRY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
