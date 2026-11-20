"""Run ten restore process-identity and PID-reuse checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_pid_identity", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_current_pid_without_kernel_guard_is_not_treated_as_active",
    "test_large_reused_or_unrelated_pid_does_not_override_unlocked_guard",
    "test_live_kernel_guard_overrides_dead_pid_metadata",
    "test_live_kernel_guard_overrides_current_pid_metadata",
    "test_live_kernel_guard_overrides_different_positive_pid_metadata",
    "test_zero_pid_is_rejected_as_unreadable_marker_metadata",
    "test_negative_pid_is_rejected_as_unreadable_marker_metadata",
    "test_boolean_pid_is_rejected_as_unreadable_marker_metadata",
    "test_dead_pid_marker_requires_explicit_recovery_not_automatic_cleanup",
    "test_recovery_refuses_pid_metadata_change_after_inspection",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE PID IDENTITY: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE PID IDENTITY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
