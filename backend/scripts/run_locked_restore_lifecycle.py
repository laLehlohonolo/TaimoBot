"""Run ten locked restore lifecycle checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("locked_restore_lifecycle_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_locked_preflight_requires_all_three_expected_fingerprints",
    "test_locked_preflight_rejects_source_fingerprint_mismatch",
    "test_locked_preflight_rejects_staged_fingerprint_mismatch",
    "test_locked_preflight_rejects_rollback_fingerprint_mismatch",
    "test_locked_preflight_detects_artifact_change_during_inventory",
    "test_locked_preflight_rejects_plan_id_not_bound_to_lock",
    "test_locked_preflight_requires_explicit_external_process_acknowledgement",
    "test_released_lock_token_cannot_release_a_later_lock",
    "test_tampered_lock_marker_cannot_be_released_by_original_owner",
    "test_second_lock_cannot_replace_first_plan_or_owner",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"LOCKED RESTORE LIFECYCLE: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT LOCKED RESTORE LIFECYCLE: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
