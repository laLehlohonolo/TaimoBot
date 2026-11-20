"""Run ten restore recovery audit append crash-boundary checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_audit_append_crash_boundaries", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_recovery_audit_short_writes_loop_until_both_records_are_complete",
    "test_started_partial_append_preserves_abandoned_marker",
    "test_started_partial_append_is_detected_as_incomplete_record",
    "test_started_partial_append_blocks_retry_without_truncation",
    "test_zero_length_started_write_fails_closed",
    "test_complete_partial_append_leaves_marker_absent_and_tail_invalid",
    "test_complete_partial_append_blocks_future_recovery_and_preserves_new_marker",
    "test_started_partial_append_preserves_database_and_sqlite_sidecars",
    "test_complete_partial_append_preserves_database_and_sqlite_sidecars",
    "test_partial_audit_tail_is_never_automatically_truncated_or_repaired",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE AUDIT APPEND CRASH BOUNDARIES: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE AUDIT APPEND CRASH BOUNDARIES: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
