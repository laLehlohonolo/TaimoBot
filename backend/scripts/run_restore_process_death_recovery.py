"""Run ten real-process restore-lock recovery checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_process_death_recovery", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_dead_child_process_restore_lock_requires_explicit_recovery",
    "test_sigkill_releases_kernel_guard_but_keeps_marker_for_review",
    "test_live_child_guard_blocks_operator_recovery",
    "test_child_process_normal_release_removes_marker_and_unlocks_database",
    "test_crash_after_marker_file_sync_leaves_recoverable_operator_review_state",
    "test_process_death_recovery_writes_verifiable_hash_chained_audit",
    "test_process_death_recovery_preserves_database_and_sqlite_sidecars",
    "test_process_death_recovery_preserves_backup_staged_and_rollback_files",
    "test_two_operator_recovery_processes_cannot_both_recover_same_marker",
    "test_explicit_recovery_after_process_death_allows_new_lock_without_touching_db",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE PROCESS-DEATH RECOVERY: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE PROCESS-DEATH RECOVERY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
