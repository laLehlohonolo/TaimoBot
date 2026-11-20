"""Run ten restore lock cleanup and crash-boundary checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_lock_cleanup_group", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_restore_lock_preserves_wal_and_shm_and_does_not_claim_external_quiescence",
    "test_restore_lock_quiesces_application_connections_and_requires_owner_token",
    "test_restore_lock_guard_remains_held_until_marker_directory_is_synced",
    "test_restore_lock_directory_sync_failure_keeps_process_guard_held",
    "test_restore_lock_unlink_failure_keeps_guard_and_allows_safe_retry",
    "test_restore_lock_marker_close_failure_keeps_process_guard_held",
    "test_restore_lock_guard_close_failure_retains_ownership_and_blocks_database",
    "test_restore_lock_is_exclusive_and_never_reclaims_an_existing_lock",
    "test_restore_lock_fails_closed_when_existing_writer_cannot_quiesce",
    "test_restore_lock_rejects_symlinked_guard_without_touching_target",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE LOCK CLEANUP: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE LOCK CLEANUP: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
