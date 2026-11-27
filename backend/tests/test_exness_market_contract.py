import unittest
from datetime import datetime, timezone

from app.exness_market_contract import (
    ExnessMarketDataError,
    normalize_symbol_specification,
    normalize_tick,
)


class ExnessMarketContractTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
        self.specification = {
            "name": "XAUUSD",
            "digits": 2,
            "trade_contract_size": 100.0,
            "point": 0.01,
            "trade_tick_size": 0.01,
            "trade_tick_value": 1.0,
            "volume_min": 0.01,
            "volume_max": 200.0,
            "volume_step": 0.01,
            "trade_stops_level": 0,
            "trade_freeze_level": 0,
            "trade_calc_mode": 0,
        }
        self.tick = {"bid": 2650.10, "ask": 2650.25, "time_msc": 1795773600000}

    def test_valid_symbol_spec_is_normalized_but_remains_unverified(self):
        result = normalize_symbol_specification(
            self.specification,
            expected_server_symbol="XAUUSD",
            captured_at=self.now,
        )
        self.assertEqual(result["canonical_symbol"], "XAUUSD")
        self.assertEqual(result["specification"]["contract_size"], 100.0)
        self.assertEqual(result["verification_status"], "UNVERIFIED_PENDING_REVIEW")
        self.assertFalse(result["execution_eligible"])

    def test_symbol_suffix_is_never_guessed_or_silently_mapped(self):
        with self.assertRaisesRegex(ExnessMarketDataError, "exactly match"):
            normalize_symbol_specification(
                dict(self.specification, name="XAUUSDm"),
                expected_server_symbol="XAUUSD",
                captured_at=self.now,
            )

    def test_missing_or_invalid_contract_fields_fail_closed(self):
        for field, value in (
            ("trade_contract_size", 0),
            ("trade_tick_size", float("nan")),
            ("volume_step", True),
            ("trade_stops_level", -1),
            ("digits", 2.5),
        ):
            with self.subTest(field=field):
                raw = dict(self.specification, **{field: value})
                with self.assertRaises(ExnessMarketDataError):
                    normalize_symbol_specification(
                        raw,
                        expected_server_symbol="XAUUSD",
                        captured_at=self.now,
                    )

    def test_inconsistent_volume_limits_are_rejected(self):
        raw = dict(self.specification, volume_min=1.0, volume_max=0.1)
        with self.assertRaisesRegex(ExnessMarketDataError, "volume_max"):
            normalize_symbol_specification(
                raw,
                expected_server_symbol="XAUUSD",
                captured_at=self.now,
            )

    def test_valid_tick_has_bid_ask_provider_and_receive_times(self):
        result = normalize_tick(
            self.tick,
            server_symbol="XAUUSD",
            expected_server_symbol="XAUUSD",
            received_at=self.now,
        )
        self.assertAlmostEqual(result["spread"], 0.15)
        self.assertEqual(result["evidence_id"], "exness:XAUUSD:1795773600000")
        self.assertEqual(result["verification_status"], "UNVERIFIED_PENDING_REVIEW")
        self.assertFalse(result["execution_eligible"])

    def test_tick_symbol_mismatch_and_crossed_prices_are_rejected(self):
        with self.assertRaisesRegex(ExnessMarketDataError, "does not match"):
            normalize_tick(
                self.tick,
                server_symbol="XAUUSDm",
                expected_server_symbol="XAUUSD",
                received_at=self.now,
            )
        with self.assertRaisesRegex(ExnessMarketDataError, "ask cannot be below bid"):
            normalize_tick(
                dict(self.tick, ask=2650.00),
                server_symbol="XAUUSD",
                expected_server_symbol="XAUUSD",
                received_at=self.now,
            )

    def test_invalid_quotes_and_naive_receive_time_are_rejected(self):
        for tick in (
            dict(self.tick, bid=0),
            dict(self.tick, ask=float("inf")),
            dict(self.tick, time_msc=True),
            dict(self.tick, time_msc=0),
        ):
            with self.subTest(tick=tick):
                with self.assertRaises(ExnessMarketDataError):
                    normalize_tick(
                        tick,
                        server_symbol="XAUUSD",
                        expected_server_symbol="XAUUSD",
                        received_at=self.now,
                    )
        with self.assertRaisesRegex(ExnessMarketDataError, "timezone-aware"):
            normalize_tick(
                self.tick,
                server_symbol="XAUUSD",
                expected_server_symbol="XAUUSD",
                received_at=datetime(2026, 11, 27, 10, 0),
            )


if __name__ == "__main__":
    unittest.main()
