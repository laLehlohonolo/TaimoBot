import unittest

from app.profit_taking import (
    evaluate_group_profit,
    evaluate_position_profit,
)


class AdaptiveProfitTakingTests(unittest.TestCase):
    def test_small_positive_net_profit_is_eligible_without_large_fixed_target(self):
        result = evaluate_position_profit(
            {"side": "BUY", "entry_price": 2000, "quantity": 0.1},
            2000.20,
            spread_cost=0.005,
            commission_cost=0.005,
        )

        self.assertAlmostEqual(result["gross_pnl"], 0.02)
        self.assertAlmostEqual(result["total_costs"], 0.01)
        self.assertAlmostEqual(result["net_pnl"], 0.01)
        self.assertTrue(result["should_take_profit"])

    def test_transaction_costs_can_turn_gross_profit_into_net_loss(self):
        result = evaluate_position_profit(
            {"side": "BUY", "entry_price": 100, "quantity": 2},
            101,
            spread_cost=0.5,
            commission_cost=0.75,
            swap_cost=0.25,
        )

        self.assertAlmostEqual(result["gross_pnl"], 2.0)
        self.assertAlmostEqual(result["total_costs"], 1.5)
        self.assertAlmostEqual(result["net_pnl"], 0.5)
        self.assertTrue(result["should_take_profit"])

        loss_after_costs = evaluate_position_profit(
            {"side": "BUY", "entry_price": 100, "quantity": 2},
            100.5,
            spread_cost=0.5,
            commission_cost=0.75,
            swap_cost=0.25,
        )
        self.assertAlmostEqual(loss_after_costs["net_pnl"], -0.5)
        self.assertFalse(loss_after_costs["should_take_profit"])

    def test_sell_position_uses_inverse_price_movement(self):
        result = evaluate_position_profit(
            {"side": "SELL", "entry_price": 2000, "quantity": 0.5},
            1999,
            commission_cost=0.1,
        )

        self.assertAlmostEqual(result["gross_pnl"], 0.5)
        self.assertAlmostEqual(result["net_pnl"], 0.4)
        self.assertTrue(result["should_take_profit"])

    def test_caller_can_supply_a_small_net_profit_floor(self):
        position = {"side": "BUY", "entry_price": 100, "quantity": 1}
        result = evaluate_position_profit(
            position, 100.2, minimum_net_profit=0.25
        )

        self.assertAlmostEqual(result["net_pnl"], 0.2)
        self.assertFalse(result["should_take_profit"])

    def test_group_uses_aggregate_net_profit_even_if_one_leg_is_negative(self):
        result = evaluate_group_profit(
            [
                {
                    "side": "BUY",
                    "entry_price": 100,
                    "quantity": 1,
                    "current_price": 101,
                    "commission_cost": 0.2,
                },
                {
                    "side": "BUY",
                    "entry_price": 100,
                    "quantity": 1,
                    "current_price": 99,
                    "commission_cost": 0.2,
                },
            ]
        )

        self.assertEqual(result["position_count"], 2)
        self.assertAlmostEqual(result["positions"][0]["net_pnl"], 0.8)
        self.assertAlmostEqual(result["positions"][1]["net_pnl"], -1.2)
        self.assertAlmostEqual(result["net_pnl"], -0.4)
        self.assertFalse(result["should_take_profit"])

    def test_group_can_be_profitable_despite_a_losing_leg(self):
        result = evaluate_group_profit(
            [
                {
                    "side": "BUY",
                    "entry_price": 100,
                    "quantity": 2,
                    "current_price": 102,
                    "commission_cost": 0.2,
                },
                {
                    "side": "BUY",
                    "entry_price": 100,
                    "quantity": 1,
                    "current_price": 99,
                    "commission_cost": 0.1,
                },
            ],
            minimum_net_profit=1,
        )

        self.assertGreater(result["positions"][1]["net_pnl"], -2)
        self.assertAlmostEqual(result["net_pnl"], 2.7)
        self.assertTrue(result["should_take_profit"])

    def test_invalid_side_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Side"):
            evaluate_position_profit(
                {"side": "HOLD", "entry_price": 100, "quantity": 1},
                101,
            )

    def test_non_finite_and_negative_costs_are_rejected(self):
        position = {"side": "BUY", "entry_price": 100, "quantity": 1}
        with self.assertRaisesRegex(ValueError, "finite"):
            evaluate_position_profit(position, float("nan"))
        with self.assertRaisesRegex(ValueError, "zero or greater"):
            evaluate_position_profit(position, 101, swap_cost=-0.1)

    def test_empty_group_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "At least one"):
            evaluate_group_profit([])


    def test_group_aggregate_preserves_precision_beyond_rounded_leg_display(self):
        result = evaluate_group_profit(
            [
                {
                    "side": "BUY",
                    "entry_price": 100,
                    "quantity": 1,
                    "current_price": 100.000000006,
                },
                {
                    "side": "BUY",
                    "entry_price": 100,
                    "quantity": 1,
                    "current_price": 100.000000006,
                },
            ]
        )

        # Each displayed leg rounds to 1e-8, but the true aggregate is
        # 1.2e-8 and must be rounded only after summing full precision.
        self.assertEqual(result["positions"][0]["gross_pnl"], 1e-8)
        self.assertEqual(result["gross_pnl"], 1e-8)
        self.assertAlmostEqual(result["net_pnl"], 1e-8)


if __name__ == "__main__":
    unittest.main()
