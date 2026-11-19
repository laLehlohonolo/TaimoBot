"""Run ten restore interruption journal consistency checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_maintenance.py"
SPEC = importlib.util.spec_from_file_location("restore_interruption_journal_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_recovery_journal_accepts_only_a_completed_matching_pair",
    "test_unmatched_started_blocks_next_recovery_and_preserves_new_marker",
    "test_recovery_journal_rejects_complete_without_started",
    "test_recovery_journal_rejects_duplicate_started_events",
    "test_recovery_journal_rejects_mismatched_operator_incident_or_plan",
    "test_recovery_journal_rejects_marker_fingerprint_replacement",
    "test_active_restore_guard_blocks_recovery_even_with_completed_history",
    "test_recovery_cleanup_preserves_source_staged_and_rollback_artifacts",
    "test_recovery_audit_requires_manual_review_after_process_restart",
    "test_recovery_audit_pair_verification_is_idempotent_and_read_only",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.DailyMaintenanceTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE INTERRUPTION JOURNAL: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE INTERRUPTION JOURNAL: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
