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



    def test_quality_diagnostics_detect_missing_synthetic_candles(self):
        from datetime import datetime, timezone
        from app.market_observation_store import inspect_market_observation_quality

        for timestamp in (
            "2026-11-21T12:00:00+00:00",
            "2026-11-21T12:01:00+00:00",
            "2026-11-21T12:04:00+00:00",
        ):
            record = self.valid_record()
            record["timestamp"] = timestamp
            save_market_observation(self.connection, record)
        result = inspect_market_observation_quality(
            self.connection, "1HZ100V", "M1",
            now=datetime(2026, 11, 21, 12, 5, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "GAPS_DETECTED")
        self.assertEqual(result["missing_candle_count"], 2)
        self.assertEqual(result["missing_interval_count"], 1)
        self.assertFalse(result["execution_eligible"])

    def test_quality_diagnostics_mark_old_series_stale(self):
        from datetime import datetime, timezone
        from app.market_observation_store import inspect_market_observation_quality

        record = self.valid_record()
        record["timestamp"] = "2026-11-21T12:00:00+00:00"
        save_market_observation(self.connection, record)
        result = inspect_market_observation_quality(
            self.connection, "1HZ100V", "M1",
            now=datetime(2026, 11, 21, 12, 5, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "STALE")
        self.assertTrue(result["stale"])

    def test_quality_diagnostics_report_no_data_and_reject_non_synthetics(self):
        from datetime import datetime, timezone
        from app.market_observation_store import inspect_market_observation_quality

        now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
        result = inspect_market_observation_quality(
            self.connection, "1HZ100V", "M1", now=now
        )
        self.assertEqual(result["status"], "NO_DATA")
        self.assertTrue(result["stale"])
        with self.assertRaisesRegex(ValueError, "Deriv synthetic"):
            inspect_market_observation_quality(self.connection, "XAUUSD", "M1", now=now)

    def test_quality_diagnostics_require_timezone_aware_reference_time(self):
        from datetime import datetime
        from app.market_observation_store import inspect_market_observation_quality

        with self.assertRaisesRegex(ValueError, "include a timezone"):
            inspect_market_observation_quality(
                self.connection, "1HZ100V", "M1", now=datetime(2026, 11, 21, 12, 0)
            )

if __name__ == "__main__":
    unittest.main()
