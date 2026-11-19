"""Run ten position-group close and accounting checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_position_groups.py"
SPEC = importlib.util.spec_from_file_location("position_group_close_tests", TEST_FILE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

CASES = [
    "test_group_reports_combined_position_statistics",
    "test_group_cannot_exceed_account_risk_limit",
    "test_group_close_closes_all_positions_atomically",
    "test_group_close_closes_source_and_follower_copies",
    "test_group_close_allocates_costs_to_sources_once_not_follower_copies",
    "test_group_close_rolls_back_when_follower_close_fails",
    "test_group_close_fails_uncommitted_pending_copy_and_releases_lease",
    "test_group_close_reconciles_pending_execution_for_closed_follower",
    "test_group_close_rolls_back_source_balance_if_follower_balance_write_fails",
    "test_group_close_rolls_back_when_final_group_status_write_fails",
]


def main():
    suite = unittest.TestSuite()
    for name in CASES:
        suite.addTest(MODULE.PositionGroupTests(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"POSITION GROUP ACCOUNTING: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT POSITION GROUP ACCOUNTING: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
