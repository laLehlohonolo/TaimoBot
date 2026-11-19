"""Run ten restore approval race and preflight checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_approval_race_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_restore_approval_rejects_source_symlink_swap_during_inventory",
    "test_restore_approval_rejects_staging_symlink_swap_during_inventory",
    "test_restore_approval_rejects_rollback_symlink_swap_during_inventory",
    "test_restore_approval_compare_and_set_rejects_concurrent_plan_status_change",
    "test_restore_approval_compare_and_set_rejects_plan_deleted_during_verification",
    "test_restore_preflight_rejects_prepared_plan_without_mutating_it",
    "test_restore_preflight_reports_read_only_non_authorizing_success",
    "test_restore_preflight_active_lock_blocks_without_plan_mutation",
    "test_restore_preflight_fingerprint_failure_does_not_change_approved_plan",
    "test_sha256_file_rejects_symlink_without_following_target",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE APPROVAL RACE GROUP: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE APPROVAL RACE: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
