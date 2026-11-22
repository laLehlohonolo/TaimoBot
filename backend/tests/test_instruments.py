import math
import unittest

from app.instruments import (
    INSTRUMENTS,
    REQUIRED_BROKER_SPEC_FIELDS,
    get_instrument,
    list_instruments,
    validate_broker_specifications,
    normalize_trade_volume,
)


class InstrumentRegistryTests(unittest.TestCase):
    def test_catalogue_contains_existing_gold_baseline(self):
        self.assertEqual(get_instrument("xauusd")["role"], "existing_default")

    def test_catalogue_contains_initial_forex_observation_candidates(self):
        self.assertEqual(
            {get_instrument(symbol)["role"] for symbol in ("EURUSD", "GBPUSD")},
            {"initial_observation_candidate"},
        )

    def test_catalogue_is_limited_to_deliberately_reviewed_symbols(self):
        self.assertEqual(set(INSTRUMENTS), {"XAUUSD", "EURUSD", "GBPUSD", "R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V", "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V"})

    def test_deriv_volatility_indices_are_separate_observation_only_assets(self):
        symbols = ("R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V")
        for symbol in symbols:
            with self.subTest(symbol=symbol):
                instrument = get_instrument(symbol)
                self.assertEqual(instrument["asset_class"], "synthetic_index")
                self.assertEqual(instrument["provider"], "Deriv")
                self.assertEqual(instrument["market_group"], "volatility_indices")
                self.assertEqual(instrument["execution_model"], "NOT_CONFIGURED")
                self.assertEqual(instrument["role"], "initial_observation_candidate")

    def test_unknown_instrument_is_rejected(self):
        with self.assertRaises(ValueError):
            get_instrument("USDJPY")

    def test_returned_instrument_record_cannot_mutate_catalogue(self):
        record = get_instrument("EURUSD")
        record["role"] = "execution_enabled"
        self.assertEqual(get_instrument("EURUSD")["role"], "initial_observation_candidate")

    def test_list_returns_detached_records(self):
        records = list_instruments()
        records[0]["role"] = "execution_enabled"
        self.assertEqual(get_instrument("XAUUSD")["role"], "existing_default")

    def valid_specifications(self):
        return {
            "tick_size": 0.00001,
            "tick_value": 1.0,
            "contract_size": 100000,
            "minimum_volume": 0.01,
            "maximum_volume": 100.0,
            "volume_step": 0.01,
            "quote_to_account_rate": 18.5,
        }

    def test_complete_positive_broker_specifications_are_normalized(self):
        result = validate_broker_specifications(self.valid_specifications())
        self.assertEqual(set(result), set(REQUIRED_BROKER_SPEC_FIELDS))
        self.assertEqual(result["contract_size"], 100000.0)

    def test_missing_broker_specification_is_rejected(self):
        specifications = self.valid_specifications()
        del specifications["tick_value"]
        with self.assertRaisesRegex(ValueError, "tick_value"):
            validate_broker_specifications(specifications)

    def test_non_finite_or_non_positive_specifications_are_rejected(self):
        invalid_values = (0, -1, float("nan"), float("inf"), -float("inf"), True, "bad")
        for value in invalid_values:
            with self.subTest(value=value):
                specifications = self.valid_specifications()
                specifications["tick_size"] = value
                with self.assertRaises(ValueError):
                    validate_broker_specifications(specifications)

    def test_minimum_volume_cannot_exceed_maximum(self):
        specifications = self.valid_specifications()
        specifications["minimum_volume"] = 2
        specifications["maximum_volume"] = 1
        with self.assertRaisesRegex(ValueError, "Minimum volume"):
            validate_broker_specifications(specifications)

    def test_non_object_specifications_are_rejected(self):
        for value in (None, [], "specs", 5):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_broker_specifications(value)


    def test_volume_is_rounded_down_to_broker_step(self):
        result = normalize_trade_volume(0.037, self.valid_specifications())
        self.assertEqual(result, 0.03)

    def test_volume_step_uses_minimum_as_grid_origin(self):
        specifications = self.valid_specifications()
        specifications["minimum_volume"] = 0.03
        specifications["maximum_volume"] = 0.13
        specifications["volume_step"] = 0.02
        self.assertEqual(
            normalize_trade_volume(0.10, specifications),
            0.09
        )

    def test_exact_minimum_and_maximum_volumes_are_accepted(self):
        specifications = self.valid_specifications()
        self.assertEqual(
            normalize_trade_volume(0.01, specifications),
            0.01
        )
        self.assertEqual(
            normalize_trade_volume(100.0, specifications),
            100.0
        )

    def test_volume_below_broker_minimum_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "below the broker minimum"):
            normalize_trade_volume(0.009, self.valid_specifications())

    def test_volume_above_broker_maximum_is_rejected_not_clipped(self):
        with self.assertRaisesRegex(ValueError, "exceeds the broker maximum"):
            normalize_trade_volume(100.01, self.valid_specifications())

    def test_invalid_requested_volume_is_rejected(self):
        for value in (0, -1, float("nan"), float("inf"), True, "invalid", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_trade_volume(value, self.valid_specifications())

    def test_volume_normalization_requires_complete_broker_specifications(self):
        specifications = self.valid_specifications()
        del specifications["volume_step"]
        with self.assertRaisesRegex(ValueError, "volume_step"):
            normalize_trade_volume(0.05, specifications)



class InstrumentExecutionApprovalTests(unittest.TestCase):
    def test_only_xauusd_is_execution_approved(self):
        from app.instruments import EXECUTION_ENABLED_SYMBOLS
        self.assertEqual(EXECUTION_ENABLED_SYMBOLS, frozenset({"XAUUSD"}))

    def test_observation_candidates_are_not_execution_approved(self):
        from app.instruments import require_execution_approval
        for symbol in ("EURUSD", "GBPUSD"):
            with self.subTest(symbol=symbol):
                with self.assertRaisesRegex(ValueError, "observation-only"):
                    require_execution_approval(symbol)

    def test_unknown_instrument_is_not_execution_approved(self):
        from app.instruments import require_execution_approval
        with self.assertRaisesRegex(ValueError, "observation-only"):
            require_execution_approval("USDJPY")

    def test_xauusd_remains_execution_approved(self):
        from app.instruments import require_execution_approval
        self.assertEqual(require_execution_approval("xauusd"), "XAUUSD")


if __name__ == "__main__":
    unittest.main()
