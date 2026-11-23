import math
import unittest
from datetime import datetime, timedelta, timezone

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

    def test_normalized_volume_never_exceeds_request_and_stays_on_step_grid(self):
        cases = (
            (0.01, 1.00, 0.01, 0.039, 0.03),
            (0.03, 0.13, 0.02, 0.10, 0.09),
            (0.10, 1.00, 0.05, 0.249, 0.20),
            (0.001, 0.010, 0.0005, 0.00249, 0.002),
        )
        from decimal import Decimal

        for minimum, maximum, step, requested, expected in cases:
            with self.subTest(requested=requested, step=step):
                specifications = self.valid_specifications()
                specifications["minimum_volume"] = minimum
                specifications["maximum_volume"] = maximum
                specifications["volume_step"] = step

                normalized = normalize_trade_volume(requested, specifications)

                self.assertEqual(normalized, expected)
                self.assertLessEqual(normalized, requested)
                self.assertGreaterEqual(normalized, minimum)
                grid_steps = (
                    (Decimal(str(normalized)) - Decimal(str(minimum)))
                    / Decimal(str(step))
                )
                self.assertEqual(grid_steps, grid_steps.to_integral_value())

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


    def monetary_specifications(self):
        return {
            "tick_size": 0.00001,
            "tick_value": 1.0,
            "contract_size": 100000,
            "minimum_volume": 0.01,
            "maximum_volume": 100.0,
            "volume_step": 0.01,
            "quote_to_account_rate": 18.5,
            "tick_value_basis": "QUOTE_CURRENCY_PER_TICK_PER_LOT",
            "quote_currency": "USD",
            "account_currency": "ZAR",
        }

    def test_monetary_stop_risk_uses_explicit_tick_and_conversion_conventions(self):
        from app.instruments import calculate_monetary_stop_risk
        result = calculate_monetary_stop_risk(
            1.10000, 1.09990, 0.03, self.monetary_specifications()
        )
        self.assertEqual(result["tick_count"], 10)
        self.assertEqual(result["normalized_volume"], 0.03)
        self.assertAlmostEqual(result["monetary_risk"], 5.55)
        self.assertEqual(result["account_currency"], "ZAR")
        self.assertEqual(result["mode"], "calculation_only")
        self.assertTrue(result["calculated"])
        self.assertNotIn("approved", result)

    def test_monetary_risk_rejects_missing_tick_value_basis(self):
        from app.instruments import calculate_monetary_stop_risk
        specs = self.monetary_specifications()
        del specs["tick_value_basis"]
        with self.assertRaisesRegex(ValueError, "tick_value_basis"):
            calculate_monetary_stop_risk(1.1, 1.0999, 0.03, specs)

    def test_monetary_risk_rejects_unknown_tick_value_basis(self):
        from app.instruments import calculate_monetary_stop_risk
        specs = self.monetary_specifications()
        specs["tick_value_basis"] = "UNSPECIFIED"
        with self.assertRaisesRegex(ValueError, "tick_value_basis"):
            calculate_monetary_stop_risk(1.1, 1.0999, 0.03, specs)

    def test_monetary_risk_requires_both_currencies(self):
        from app.instruments import calculate_monetary_stop_risk
        for field in ("quote_currency", "account_currency"):
            specs = self.monetary_specifications()
            del specs[field]
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    calculate_monetary_stop_risk(1.1, 1.0999, 0.03, specs)

    def test_monetary_risk_rejects_stop_distance_not_aligned_to_tick_size(self):
        from app.instruments import calculate_monetary_stop_risk
        with self.assertRaisesRegex(ValueError, "multiple"):
            calculate_monetary_stop_risk(
                1.10000, 1.099905, 0.03, self.monetary_specifications()
            )

    def test_monetary_risk_rejects_zero_or_invalid_price(self):
        from app.instruments import calculate_monetary_stop_risk
        for entry, stop in ((1.1, 1.1), (0, 1.0999), (float("nan"), 1.0999)):
            with self.subTest(entry=entry, stop=stop):
                with self.assertRaises(ValueError):
                    calculate_monetary_stop_risk(
                        entry, stop, 0.03, self.monetary_specifications()
                    )

    def test_monetary_risk_uses_volume_after_broker_step_normalization(self):
        from app.instruments import calculate_monetary_stop_risk
        result = calculate_monetary_stop_risk(
            1.10000, 1.09990, 0.037, self.monetary_specifications()
        )
        self.assertEqual(result["normalized_volume"], 0.03)
        self.assertAlmostEqual(result["monetary_risk"], 5.55)

    def test_monetary_risk_rejects_volume_outside_broker_bounds(self):
        from app.instruments import calculate_monetary_stop_risk
        with self.assertRaisesRegex(ValueError, "below the broker minimum"):
            calculate_monetary_stop_risk(
                1.1, 1.0999, 0.009, self.monetary_specifications()
            )


    def test_monetary_risk_is_side_symmetric_and_scales_with_conversion_rate(self):
        from app.instruments import calculate_monetary_stop_risk

        specs = self.monetary_specifications()
        buy = calculate_monetary_stop_risk(1.10000, 1.09990, 0.03, specs)
        sell = calculate_monetary_stop_risk(1.09990, 1.10000, 0.03, specs)

        doubled_conversion = dict(specs)
        doubled_conversion["quote_to_account_rate"] = 37.0
        converted = calculate_monetary_stop_risk(
            1.10000, 1.09990, 0.03, doubled_conversion
        )

        self.assertAlmostEqual(buy["monetary_risk"], 5.55)
        self.assertAlmostEqual(sell["monetary_risk"], 5.55)
        self.assertAlmostEqual(converted["monetary_risk"], 11.10)


    def provenance_specifications(self, observed_at=None):
        return {
            **self.monetary_specifications(),
            "broker": "ExampleBroker",
            "symbol": "XAUUSD",
            "specification_source": "official_contract_metadata",
            "source_reference": "instrument-spec-2026-11-23",
            "observed_at": "2026-11-23T10:00:00+02:00" if observed_at is None else observed_at,
        }

    def validate_provenance(self, specifications, now=None, max_age_seconds=300):
        from app.instruments import validate_broker_specification_provenance
        return validate_broker_specification_provenance(
            specifications,
            expected_broker="ExampleBroker",
            expected_symbol="XAUUSD",
            expected_account_currency="ZAR",
            now=now or datetime(2026, 11, 23, 8, 2, tzinfo=timezone.utc),
            max_age_seconds=max_age_seconds,
        )

    def test_broker_provenance_accepts_matching_fresh_timezone_aware_metadata(self):
        result = self.validate_provenance(self.provenance_specifications())
        self.assertTrue(result["validated"])
        self.assertEqual(result["broker"], "ExampleBroker")
        self.assertEqual(result["symbol"], "XAUUSD")
        self.assertEqual(result["account_currency"], "ZAR")
        self.assertEqual(result["mode"], "provenance_gate_only")
        self.assertAlmostEqual(result["age_seconds"], 120)

    def test_broker_provenance_rejects_missing_required_metadata(self):
        required = (
            "broker", "symbol", "specification_source",
            "source_reference", "observed_at", "quote_currency",
            "account_currency",
        )
        for field in required:
            specs = self.provenance_specifications()
            del specs[field]
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    self.validate_provenance(specs)

    def test_broker_provenance_rejects_wrong_broker_instrument_or_account_currency(self):
        cases = (
            ("broker", "OtherBroker", "requested broker"),
            ("symbol", "EURUSD", "requested instrument"),
            ("account_currency", "USD", "account currency"),
        )
        for field, value, message in cases:
            specs = self.provenance_specifications()
            specs[field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, message):
                    self.validate_provenance(specs)

    def test_broker_provenance_rejects_blank_source_identity(self):
        for field in ("specification_source", "source_reference"):
            specs = self.provenance_specifications()
            specs[field] = " "
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    self.validate_provenance(specs)

    def test_broker_provenance_rejects_stale_specifications(self):
        specs = self.provenance_specifications("2026-11-23T07:55:00Z")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.validate_provenance(specs)

    def test_broker_provenance_rejects_future_dated_specifications(self):
        specs = self.provenance_specifications("2026-11-23T08:03:00Z")
        with self.assertRaisesRegex(ValueError, "future"):
            self.validate_provenance(specs)

    def test_broker_provenance_rejects_naive_or_malformed_timestamps(self):
        for timestamp in ("2026-11-23T08:00:00", "not-a-timestamp", ""):
            specs = self.provenance_specifications(timestamp)
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(ValueError, "observed_at"):
                    self.validate_provenance(specs)

    def test_broker_provenance_rejects_invalid_clock_and_freshness_window(self):
        from app.instruments import validate_broker_specification_provenance
        specs = self.provenance_specifications()
        with self.assertRaisesRegex(ValueError, "now"):
            validate_broker_specification_provenance(
                specs, "ExampleBroker", "XAUUSD", "ZAR",
                now=datetime(2026, 11, 23, 8, 2),
            )
        for max_age in (0, -1, float("nan"), float("inf"), True, "bad"):
            with self.subTest(max_age=max_age):
                with self.assertRaisesRegex(ValueError, "max_age_seconds"):
                    self.validate_provenance(specs, max_age_seconds=max_age)

    def test_broker_provenance_honours_configured_freshness_boundary(self):
        specs = self.provenance_specifications("2026-11-23T07:57:00Z")
        result = self.validate_provenance(specs, max_age_seconds=300)
        self.assertAlmostEqual(result["age_seconds"], 300)


    def test_provenance_checked_monetary_risk_calculates_only_with_fresh_matching_metadata(self):
        from app.instruments import calculate_provenance_checked_stop_risk
        specs = self.provenance_specifications()
        result = calculate_provenance_checked_stop_risk(
            1.10000, 1.09990, 0.03, specs,
            expected_broker="ExampleBroker",
            expected_symbol="XAUUSD",
            expected_account_currency="ZAR",
            now=datetime(2026, 11, 23, 8, 2, tzinfo=timezone.utc),
        )
        self.assertAlmostEqual(result["monetary_risk"], 5.55)
        self.assertTrue(result["provenance"]["validated"])
        self.assertEqual(result["mode"], "provenance_checked_calculation_only")
        self.assertNotIn("approved", result)

    def test_provenance_checked_monetary_risk_rejects_stale_metadata(self):
        from app.instruments import calculate_provenance_checked_stop_risk
        specs = self.provenance_specifications("2026-11-23T07:55:00Z")
        with self.assertRaisesRegex(ValueError, "stale"):
            calculate_provenance_checked_stop_risk(
                1.10000, 1.09990, 0.03, specs,
                "ExampleBroker", "XAUUSD", "ZAR",
                now=datetime(2026, 11, 23, 8, 2, tzinfo=timezone.utc),
            )

    def test_provenance_checked_monetary_risk_rejects_wrong_broker_or_symbol(self):
        from app.instruments import calculate_provenance_checked_stop_risk
        specs = self.provenance_specifications()
        for broker, symbol in (("OtherBroker", "XAUUSD"), ("ExampleBroker", "EURUSD")):
            with self.subTest(broker=broker, symbol=symbol):
                with self.assertRaises(ValueError):
                    calculate_provenance_checked_stop_risk(
                        1.10000, 1.09990, 0.03, specs,
                        broker, symbol, "ZAR",
                        now=datetime(2026, 11, 23, 8, 2, tzinfo=timezone.utc),
                    )

    def test_provenance_checked_monetary_risk_rejects_currency_mismatch(self):
        from app.instruments import calculate_provenance_checked_stop_risk
        specs = self.provenance_specifications()
        specs["account_currency"] = "USD"
        with self.assertRaisesRegex(ValueError, "account currency"):
            calculate_provenance_checked_stop_risk(
                1.10000, 1.09990, 0.03, specs,
                "ExampleBroker", "XAUUSD", "ZAR",
                now=datetime(2026, 11, 23, 8, 2, tzinfo=timezone.utc),
            )

    def test_provenance_checked_monetary_risk_rejects_quote_currency_inconsistency(self):
        from app.instruments import calculate_provenance_checked_stop_risk
        specs = self.provenance_specifications()
        specs["quote_currency"] = "EUR"
        with self.assertRaisesRegex(ValueError, "quote currency does not match provenance"):
            calculate_provenance_checked_stop_risk(
                1.10000, 1.09990, 0.03, specs,
                "ExampleBroker", "XAUUSD", "ZAR",
                now=datetime(2026, 11, 23, 8, 2, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
