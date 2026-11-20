"""Run ten operator-facing restore consistency checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("restore_operator_reconciliation", ROOT / "tests" / "test_maintenance.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_consistency_clean_completed_histories_need_no_action",
    "test_consistency_pending_started_with_matching_marker_blocks_retry",
    "test_consistency_pending_started_with_absent_marker_never_auto_completes",
    "test_consistency_replacement_marker_plan_conflict_is_reported",
    "test_consistency_nonterminal_execution_requires_operator_review",
    "test_consistency_terminal_execution_still_requires_reconciliation",
    "test_consistency_same_plan_in_both_journals_is_explicit_conflict",
    "test_consistency_invalid_audit_and_journal_fail_closed",
    "test_consistency_unsafe_marker_fails_closed",
    "test_consistency_is_read_only_and_never_authorizes_repair",
]

def main():
    suite = unittest.TestSuite(module.DailyMaintenanceTests(name) for name in CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"RESTORE OPERATOR RECONCILIATION: {result.testsRun}/10; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT RESTORE OPERATOR RECONCILIATION: 10/10 PASSED SEQUENTIALLY.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
