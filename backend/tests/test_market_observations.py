import unittest

from app.market_observations import normalize_market_observation


class MarketObservationValidationTests(unittest.TestCase):
    def valid_record(self):
        return {
            "symbol": "eurusd",
            "timeframe": "M15",
            "timestamp": "2026-11-21T10:15:00+02:00",
            "open": 1.15,
            "high": 1.16,
            "low": 1.14,
            "close": 1.155,
            "volume": 120.0,
            "source": "test-feed",
        }

    def test_valid_record_normalizes_symbol_and_timestamp_to_utc(self):
        result = normalize_market_observation(self.valid_record())
        self.assertEqual(result["symbol"], "EURUSD")
        self.assertEqual(result["timestamp"], "2026-11-21T08:15:00+00:00")
        self.assertEqual(result["provenance_status"], "UNVERIFIED_EXTERNAL_INPUT")
        self.assertFalse(result["execution_eligible"])

    def test_all_catalogued_symbols_can_be_observed_but_unknown_symbols_cannot(self):
        for symbol in ("XAUUSD", "EURUSD", "GBPUSD", "R_10", "R_25", "R_50", "R_75", "R_100", "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V"):
            with self.subTest(symbol=symbol):
                record = self.valid_record()
                record["symbol"] = symbol
                self.assertEqual(normalize_market_observation(record)["symbol"], symbol)
        record = self.valid_record()
        record["symbol"] = "USDJPY"
        with self.assertRaises(ValueError):
            normalize_market_observation(record)

    def test_rejects_unsupported_timeframe(self):
        record = self.valid_record()
        record["timeframe"] = "M2"
        with self.assertRaisesRegex(ValueError, "timeframe"):
            normalize_market_observation(record)

    def test_rejects_timestamp_without_timezone(self):
        record = self.valid_record()
        record["timestamp"] = "2026-11-21T10:15:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            normalize_market_observation(record)

    def test_rejects_invalid_timestamp(self):
        record = self.valid_record()
        record["timestamp"] = "not-a-date"
        with self.assertRaisesRegex(ValueError, "ISO-8601"):
            normalize_market_observation(record)

    def test_rejects_invalid_ohlc_relationships(self):
        invalid = [
            {"open": 1.17, "high": 1.16, "low": 1.14, "close": 1.15},
            {"open": 1.15, "high": 1.16, "low": 1.14, "close": 1.13},
            {"open": 1.15, "high": 1.16, "low": 1.17, "close": 1.15},
        ]
        for values in invalid:
            with self.subTest(values=values):
                record = self.valid_record()
                record.update(values)
                with self.assertRaisesRegex(ValueError, "OHLC"):
                    normalize_market_observation(record)

    def test_rejects_non_finite_or_non_positive_prices(self):
        for value in (0, -1, float("nan"), float("inf"), -float("inf"), True, "bad"):
            with self.subTest(value=value):
                record = self.valid_record()
                record["open"] = value
                with self.assertRaises(ValueError):
                    normalize_market_observation(record)

    def test_rejects_negative_or_non_finite_volume(self):
        for value in (-1, float("nan"), float("inf"), True, "bad"):
            with self.subTest(value=value):
                record = self.valid_record()
                record["volume"] = value
                with self.assertRaisesRegex(ValueError, "volume"):
                    normalize_market_observation(record)

    def test_rejects_missing_or_oversized_source_identifier(self):
        for value in ("", "  ", "x" * 81, None):
            with self.subTest(value=value):
                record = self.valid_record()
                record["source"] = value
                with self.assertRaisesRegex(ValueError, "source"):
                    normalize_market_observation(record)

    def test_rejects_non_object_records(self):
        for value in (None, [], "candle", 4):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "object"):
                    normalize_market_observation(value)


if __name__ == "__main__":
    unittest.main()
