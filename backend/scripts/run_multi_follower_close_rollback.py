"""Run ten multi-follower close rollback and retry checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_copy_replication.py"
SPEC = importlib.util.spec_from_file_location("multi_follower_close_rollback_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_multi_follower_close_rolls_back_if_first_follower_balance_write_fails",
    "test_multi_follower_close_rolls_back_if_last_follower_balance_write_fails",
    "test_multi_follower_close_rolls_back_if_a_copy_trade_write_fails",
    "test_multi_follower_close_rolls_back_if_copy_execution_finalization_fails",
    "test_multi_follower_close_rolls_back_if_source_trade_write_fails",
    "test_multi_follower_close_retry_succeeds_after_balance_failure_is_removed",
    "test_multi_follower_close_rejects_duplicate_retry_without_double_credit",
    "test_multi_follower_close_still_closes_existing_copies_when_relationships_paused",
    "test_multi_follower_close_rejects_negative_follower_balance_without_partial_close",
    "test_multi_follower_close_preserves_source_costs_once_after_retry",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.CopyTradeReplicationTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"MULTI-FOLLOWER CLOSE ROLLBACK: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT MULTI-FOLLOWER CLOSE ROLLBACK: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
