import sqlite3
import unittest

from app.database import ensure_market_observation_schema
from app.market_backfill import get_backfill_checkpoint, run_backfill_batch


class FakeRangeClient:
    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    async def collect_candle_range(self, symbol, timeframe, start_epoch, end_epoch, max_candles):
        self.calls.append((symbol, timeframe, start_epoch, end_epoch, max_candles))
        return self.candles


def candle(epoch, price=100):
    from datetime import datetime, timezone
    return {
        "symbol": "1HZ100V",
        "timeframe": "M1",
        "timestamp": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price,
        "volume": 0,
        "source": "deriv",
    }


class MarketBackfillTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(self.connection)

    def tearDown(self):
        self.connection.close()

    async def test_backfill_persists_candles_and_moves_durable_cursor_backwards(self):
        client = FakeRangeClient([candle(3480), candle(3540), candle(3600)])
        result = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=3600,
        )
        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(result["received_count"], 3)
        self.assertEqual(result["inserted_count"], 3)
        self.assertEqual(result["next_end_epoch"], 3420)
        self.assertEqual(result["batches_completed"], 1)
        self.assertEqual(client.calls, [("1HZ100V", "M1", 3480, 3600, 3)])
        persisted = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(persisted["next_end_epoch"], 3420)

    async def test_backfill_marks_complete_only_after_reaching_epoch_zero(self):
        client = FakeRangeClient([candle(0), candle(60), candle(120)])
        result = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=120,
        )
        self.assertEqual(result["status"], "COMPLETE")
        self.assertIsNone(result["next_end_epoch"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["earliest_observed_at"], candle(0)["timestamp"])

    async def test_empty_nonzero_range_is_held_for_gap_review_not_assumed_inception(self):
        client = FakeRangeClient([])
        result = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=3600,
        )
        self.assertEqual(result["status"], "GAP_REVIEW")
        self.assertEqual(result["next_end_epoch"], 3600)
        self.assertIn("no candles", result["last_error"])

    async def test_completed_checkpoint_is_idempotent_and_does_not_fetch_again(self):
        client = FakeRangeClient([candle(0), candle(60), candle(120)])
        await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=120,
        )
        result = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=120,
        )
        self.assertTrue(result["complete"])
        self.assertEqual(len(client.calls), 1)

    async def test_rejects_unbounded_or_invalid_batch_size(self):
        with self.assertRaisesRegex(ValueError, "batch_size"):
            await run_backfill_batch(
                self.connection, FakeRangeClient([]), "1HZ100V", "M1",
                batch_size=501, now_epoch=120,
            )


if __name__ == "__main__":
    unittest.main()
