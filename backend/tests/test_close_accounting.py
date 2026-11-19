import unittest

from app.close_accounting import calculate_close_accounting


class CloseAccountingTests(unittest.TestCase):
    def test_calculates_net_after_all_costs_without_rounding_ledger_values(self):
        result = calculate_close_accounting(
            1.23456789,
            spread_cost=0.01,
            commission_cost=0.02,
            swap_cost=0.003,
            other_costs=0.0004,
        )
        self.assertAlmostEqual(result["gross_realized_pnl"], 1.23456789)
        self.assertAlmostEqual(result["total_costs"], 0.0334)
        self.assertAlmostEqual(result["net_realized_pnl"], 1.20116789)

    def test_negative_gross_pnl_remains_negative_after_costs(self):
        result = calculate_close_accounting(-2.5, commission_cost=0.1)
        self.assertEqual(result["gross_realized_pnl"], -2.5)
        self.assertEqual(result["net_realized_pnl"], -2.6)

    def test_zero_cost_defaults_preserve_gross_pnl(self):
        result = calculate_close_accounting(3.25)
        self.assertEqual(result["net_realized_pnl"], 3.25)
        self.assertEqual(result["total_costs"], 0)

    def test_rejects_boolean_and_non_finite_costs_or_gross(self):
        for value in (True, float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    calculate_close_accounting(value)
                with self.assertRaises(ValueError):
                    calculate_close_accounting(1, commission_cost=value)

    def test_rejects_negative_costs(self):
        with self.assertRaises(ValueError):
            calculate_close_accounting(1, spread_cost=-0.01)


if __name__ == "__main__":
    unittest.main()
