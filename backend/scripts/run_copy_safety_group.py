"""Run ten copy-safety regression cases sequentially as a named group."""
import importlib.util
import sys
from pathlib import Path
import unittest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_copy_replication.py"
SPEC = importlib.util.spec_from_file_location("copy_replication_safety_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_follower_position_size_is_calculated_from_own_funding",
    "test_concurrent_replication_of_same_source_creates_one_follower_trade",
    "test_paused_relationship_receives_no_copy",
    "test_copied_trade_cannot_trigger_cascading_copy",
    "test_source_trade_must_be_open",
    "test_unavailable_follower_account_records_failure_without_affecting_source",
    "test_follower_risk_rejection_is_recorded_without_affecting_source",
    "test_emergency_stop_blocks_copy_execution_without_affecting_source",
    "test_maintenance_blocks_copy_execution_without_affecting_source",
    "test_concurrent_copy_entries_cannot_exceed_followers_combined_risk",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.CopyTradeReplicationTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"COPY SAFETY GROUP: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT COPY SAFETY GROUP: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
