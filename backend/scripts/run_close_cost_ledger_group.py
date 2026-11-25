"""Run ten close-cost and ledger reconciliation checks sequentially."""
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


CLOSE = load_module("close_accounting_group_tests", "tests/test_close_accounting.py")
MIGRATION = load_module("cost_ledger_migration_group_tests", "tests/test_cost_ledger_migration.py")
PAPER = load_module("paper_ledger_reconciliation_group_tests", "tests/test_paper_trading.py")

CASES = [
    (CLOSE.CloseAccountingTests, "test_calculates_net_after_all_costs_without_rounding_ledger_values"),
    (CLOSE.CloseAccountingTests, "test_negative_gross_pnl_remains_negative_after_costs"),
    (CLOSE.CloseAccountingTests, "test_zero_cost_defaults_preserve_gross_pnl"),
    (CLOSE.CloseAccountingTests, "test_rejects_boolean_and_non_finite_costs_or_gross"),
    (CLOSE.CloseAccountingTests, "test_rejects_negative_costs"),
    (MIGRATION.TradeCostLedgerMigrationTests, "test_migration_preserves_legacy_pnl_and_marks_costs_unknown"),
    (MIGRATION.TradeCostLedgerMigrationTests, "test_open_legacy_trade_is_not_misrepresented_as_closed_pnl"),
    (MIGRATION.TradeCostLedgerMigrationTests, "test_migration_is_idempotent"),
    (PAPER.PaperTradingTests, "test_single_profit_preview_matches_actual_close_ledger_and_balance"),
    (PAPER.PaperTradingTests, "test_group_profit_preview_matches_actual_close_ledger_and_balance"),
]


def main():
    suite = unittest.TestSuite()
    for test_class, name in CASES:
        suite.addTest(test_class(name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 10:
        print(f"CLOSE COST LEDGER GROUP: {result.testsRun}/10 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT CLOSE COST LEDGER GROUP: 10/10 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
