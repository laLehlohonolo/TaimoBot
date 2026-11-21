import sqlite3
import unittest

from app.database import ensure_market_observation_schema
from app.market_observation_store import (
    list_market_observations,
    save_market_observation,
)


class MarketObservationStoreTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    def valid_record(self):
        return {
            "symbol": "1HZ100V",
            "timeframe": "M1",
            "timestamp": "2026-11-21T10:15:00+02:00",
            "open": 1000.0,
            "high": 1002.0,
            "low": 999.0,
            "close": 1001.0,
            "volume": 0,
            "source": "deriv-test",
        }

    def test_saves_validated_synthetic_candle_as_non_executable(self):
        result = save_market_observation(self.connection, self.valid_record())
        self.connection.commit()
        self.assertTrue(result["inserted"])
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["symbol"], "1HZ100V")
        rows = list_market_observations(self.connection, symbol="1HZ100V")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], "2026-11-21T08:15:00+00:00")
        self.assertFalse(rows[0]["execution_eligible"])

    def test_duplicate_source_candle_is_idempotent(self):
        first = save_market_observation(self.connection, self.valid_record())
        second = save_market_observation(self.connection, self.valid_record())
        self.connection.commit()
        self.assertTrue(first["inserted"])
        self.assertFalse(second["inserted"])
        self.assertEqual(len(list_market_observations(self.connection)), 1)

    def test_same_candle_from_distinct_sources_is_retained_separately(self):
        first = self.valid_record()
        second = self.valid_record()
        second["source"] = "deriv-replay"
        save_market_observation(self.connection, first)
        save_market_observation(self.connection, second)
        self.connection.commit()
        self.assertEqual(len(list_market_observations(self.connection)), 2)

    def test_query_filters_symbol_and_timeframe(self):
        record = self.valid_record()
        save_market_observation(self.connection, record)
        other = self.valid_record()
        other.update({
            "symbol": "R_50",
            "timeframe": "M5",
            "timestamp": "2026-11-21T10:20:00+02:00",
        })
        save_market_observation(self.connection, other)
        self.connection.commit()
        rows = list_market_observations(
            self.connection, symbol="r_50", timeframe="M5"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "R_50")

    def test_rejects_invalid_records_and_unsafe_query_limits(self):
        record = self.valid_record()
        record["high"] = 900
        with self.assertRaises(ValueError):
            save_market_observation(self.connection, record)
        for limit in (0, -1, 1001, True, 1.5):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    list_market_observations(self.connection, limit=limit)

    def test_rejects_unsupported_timeframe_filter(self):
        with self.assertRaisesRegex(ValueError, "timeframe"):
            list_market_observations(self.connection, timeframe="M2")


if __name__ == "__main__":
    unittest.main()
