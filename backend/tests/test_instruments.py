import math
import unittest

from app.instruments import (
    INSTRUMENTS,
    REQUIRED_BROKER_SPEC_FIELDS,
    get_instrument,
    list_instruments,
    validate_broker_specifications,
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
        self.assertEqual(set(INSTRUMENTS), {"XAUUSD", "EURUSD", "GBPUSD"})

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
