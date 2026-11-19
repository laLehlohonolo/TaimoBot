"""Run ten multi-follower close and lifecycle reconciliation checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_copy_replication.py"
SPEC = importlib.util.spec_from_file_location("multi_follower_close_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_source_close_reconciles_multiple_followers_independently",
    "test_individual_follower_close_does_not_close_source_or_other_followers",
    "test_api_trade_creation_triggers_copy",
    "test_follower_position_size_is_calculated_from_own_funding",
    "test_concurrent_replication_of_same_source_creates_one_follower_trade",
    "test_source_close_during_copy_execution_keeps_follower_and_execution_closed",
    "test_source_closed_before_atomic_copy_insert_prevents_follower_entry",
    "test_concurrent_source_close_wins_before_copy_insert",
    "test_concurrent_source_close_after_copy_insert_closes_follower_before_finalization",
    "test_stale_pending_copy_without_trade_is_retried",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.CopyTradeReplicationTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"MULTI-FOLLOWER CLOSE RECONCILIATION: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT MULTI-FOLLOWER CLOSE RECONCILIATION: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
