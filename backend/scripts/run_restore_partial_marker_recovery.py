"""Run ten interrupted restore marker recovery checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_partial_marker_recovery", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_partial_restore_marker_empty_file_is_manual_review_only",
    "test_partial_restore_marker_truncated_json_is_not_repaired",
    "test_partial_restore_marker_invalid_utf8_is_preserved",
    "test_partial_restore_marker_duplicate_json_keys_is_not_accepted",
    "test_partial_restore_marker_json_scalar_is_not_accepted",
    "test_partial_restore_marker_oversize_is_not_read_or_removed",
    "test_partial_restore_marker_recovery_requires_valid_abandoned_state",
    "test_partial_restore_marker_recovery_does_not_create_audit",
    "test_partial_restore_marker_blocks_new_restore_lock_attempt",
    "test_partial_restore_marker_keeps_database_and_sidecars_unchanged",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"PARTIAL RESTORE MARKER RECOVERY: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT PARTIAL RESTORE MARKER RECOVERY: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
