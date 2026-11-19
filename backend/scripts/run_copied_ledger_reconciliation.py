"""Run ten copied-position ledger and restart-reconciliation checks sequentially."""
import importlib.util
import sys
from pathlib import Path
import unittest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def load_module(name, relative_path):
    path = BACKEND_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAPER = load_module("paper_ledger_reconciliation_tests", "tests/test_paper_trading.py")
GROUPS = load_module("copied_group_ledger_reconciliation_tests", "tests/test_position_groups.py")

CASES = [
    (PAPER.PaperTradingTests, "test_account_balance_reconciles_with_all_realized_pnl"),
    (PAPER.PaperTradingTests, "test_closed_trade_and_realized_balance_persist_after_reinitialization"),
    (PAPER.PaperTradingTests, "test_account_history_reports_net_realized_pnl_after_close_costs"),
    (PAPER.PaperTradingTests, "test_group_preview_and_close_match_with_uneven_quantity_cost_allocation"),
    (PAPER.PaperTradingTests, "test_group_profit_preview_rejects_closed_group_even_if_open_leg_remains"),
    (GROUPS.PositionGroupTests, "test_group_close_allocates_costs_to_sources_once_not_follower_copies"),
    (GROUPS.PositionGroupTests, "test_group_close_rollback_state_survives_database_reinitialization"),
    (GROUPS.PositionGroupTests, "test_successful_group_close_ledger_and_balances_survive_database_reinitialization"),
    (GROUPS.PositionGroupTests, "test_group_close_rolls_back_source_balance_if_follower_balance_write_fails"),
    (GROUPS.PositionGroupTests, "test_group_close_rolls_back_when_final_group_status_write_fails"),
]


def main():
    suite = unittest.TestSuite()
    for test_class, name in CASES:
        suite.addTest(test_class(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(
            f"COPIED LEDGER RECONCILIATION GROUP: {result.testsRun}/10 executed; "
            f"failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print("TAIMOBOT COPIED LEDGER RECONCILIATION GROUP: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
