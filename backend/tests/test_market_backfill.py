import asyncio
import sqlite3
import tempfile
import unittest

from app.database import ensure_market_observation_schema
from app.market_backfill import (
    get_backfill_checkpoint, prepare_backfill_batch,
    persist_backfill_batch, run_backfill_batch,
    run_backfill_batch_with_connection_factory,
)


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

    async def test_prepare_fetch_persist_uses_separate_database_connections(self):
        request = prepare_backfill_batch(
            self.connection, "1HZ100V", "M1", batch_size=3, now_epoch=3600,
        )
        self.connection.close()
        candles = [candle(3480), candle(3540), candle(3600)]
        # The provider phase runs with no database connection held.
        client = FakeRangeClient(candles)
        fetched = await client.collect_candle_range(
            request["symbol"], request["timeframe"],
            request["start_epoch"], request["end_epoch"],
            max_candles=request["batch_size"],
        )
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(self.connection)
        # Simulate the same durable checkpoint created by the preparation phase.
        self.connection.execute(
            """INSERT INTO market_backfill_checkpoints
               (symbol, timeframe, next_end_epoch, status)
               VALUES (?, ?, ?, 'RUNNING')""",
            ("1HZ100V", "M1", request["expected_next_end_epoch"]),
        )
        result = persist_backfill_batch(self.connection, request, fetched)
        self.assertEqual(result["inserted_count"], 3)
        self.assertEqual(result["next_end_epoch"], 3420)

    async def test_stale_prepared_batch_cannot_advance_checkpoint_twice(self):
        first_request = prepare_backfill_batch(
            self.connection, "1HZ100V", "M1", batch_size=3, now_epoch=3600,
        )
        second_request = prepare_backfill_batch(
            self.connection, "1HZ100V", "M1", batch_size=3, now_epoch=3600,
        )
        candles = [candle(3480), candle(3540), candle(3600)]
        first = persist_backfill_batch(self.connection, first_request, candles)
        self.assertEqual(first["batches_completed"], 1)
        with self.assertRaisesRegex(RuntimeError, "changed during collection"):
            persist_backfill_batch(self.connection, second_request, candles)
        checkpoint = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(checkpoint["batches_completed"], 1)
        self.assertEqual(checkpoint["next_end_epoch"], 3420)

    async def test_worker_closes_database_connections_during_provider_fetch(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database_file:
            active_connections = set()

            class TrackedConnection(sqlite3.Connection):
                def close(self):
                    active_connections.discard(id(self))
                    return super().close()

            def connection_factory():
                connection = sqlite3.connect(
                    database_file.name, factory=TrackedConnection
                )
                connection.row_factory = sqlite3.Row
                ensure_market_observation_schema(connection)
                active_connections.add(id(connection))
                return connection

            # Initialize the durable database schema before the worker starts.
            initial = connection_factory()
            initial.close()

            class ConnectionCheckingClient(FakeRangeClient):
                async def collect_candle_range(self, *args, **kwargs):
                    self.assert_no_connections = len(active_connections) == 0
                    return await super().collect_candle_range(*args, **kwargs)

            client = ConnectionCheckingClient(
                [candle(3480), candle(3540), candle(3600)]
            )
            result = await run_backfill_batch_with_connection_factory(
                connection_factory, client, "1HZ100V", "M1",
                batch_size=3, now_epoch=3600,
            )
            self.assertTrue(client.assert_no_connections)
            self.assertEqual(active_connections, set())
            self.assertEqual(result["inserted_count"], 3)
            check = connection_factory()
            try:
                checkpoint = get_backfill_checkpoint(check, "1HZ100V", "M1")
            finally:
                check.close()
            self.assertEqual(checkpoint["next_end_epoch"], 3420)

    async def test_rejects_out_of_range_misaligned_and_mismatched_provider_candles(self):
        request = prepare_backfill_batch(
            self.connection, "1HZ100V", "M1", batch_size=3, now_epoch=3600,
        )
        invalid_batches = [
            [candle(3420)],  # Outside the requested [3480, 3600] range.
            [{**candle(3480), "timeframe": "M5"}],
            [{**candle(3480), "symbol": "1HZ10V"}],
            [{**candle(3480), "timestamp": "1970-01-01T00:58:01+00:00"}],
            [candle(3480), candle(3480)],  # Duplicate timestamps.
            [{**candle(3480), "timestamp": "1970-01-01T00:58:00.500000+00:00"}],
        ]
        for candles in invalid_batches:
            with self.subTest(candles=candles):
                with self.assertRaises(ValueError):
                    persist_backfill_batch(self.connection, request, candles)
        checkpoint = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertEqual(checkpoint["batches_completed"], 0)
        stored = self.connection.execute(
            "SELECT COUNT(*) AS count FROM market_observations"
        ).fetchone()["count"]
        self.assertEqual(stored, 0)

    async def test_prepared_batch_cannot_be_narrowed_to_skip_checkpoint_candles(self):
        request = prepare_backfill_batch(
            self.connection, "1HZ100V", "M1", batch_size=3, now_epoch=3600,
        )
        altered_end = {**request, "end_epoch": 3540}
        altered_start = {**request, "start_epoch": 3540}
        for altered in (altered_end, altered_start):
            with self.subTest(request=altered):
                with self.assertRaisesRegex(ValueError, "range was altered"):
                    persist_backfill_batch(
                        self.connection, altered, [candle(3540)]
                    )
        checkpoint = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertEqual(checkpoint["batches_completed"], 0)
        stored = self.connection.execute(
            "SELECT COUNT(*) AS count FROM market_observations"
        ).fetchone()["count"]
        self.assertEqual(stored, 0)

    async def test_incomplete_nonempty_batch_is_held_for_gap_review(self):
        client = FakeRangeClient([candle(3480), candle(3600)])
        result = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=3600,
        )
        self.assertEqual(result["status"], "GAP_REVIEW")
        self.assertEqual(result["next_end_epoch"], 3600)
        self.assertFalse(result["complete"])
        self.assertIn("1 expected candle(s) are missing", result["last_error"])
        self.assertEqual(result["inserted_count"], 2)
        checkpoint = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(checkpoint["batches_completed"], 1)
        stored = self.connection.execute(
            "SELECT COUNT(*) AS count FROM market_observations"
        ).fetchone()["count"]
        self.assertEqual(stored, 2)

    async def test_incomplete_batch_requires_operator_review_before_any_retry(self):
        client = FakeRangeClient([candle(3480), candle(3600)])
        first = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=3600,
        )
        self.assertEqual(first["status"], "GAP_REVIEW")
        with self.assertRaisesRegex(ValueError, "operator review"):
            await run_backfill_batch(
                self.connection, client, "1HZ100V", "M1",
                batch_size=3, now_epoch=3600,
            )
        checkpoint = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(checkpoint["status"], "GAP_REVIEW")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertEqual(checkpoint["batches_completed"], 1)
        self.assertEqual(len(client.calls), 1)

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

    async def test_backfill_requires_review_at_epoch_zero_instead_of_claiming_complete(self):
        client = FakeRangeClient([candle(0), candle(60), candle(120)])
        result = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=120,
        )
        self.assertEqual(result["status"], "GAP_REVIEW")
        self.assertEqual(result["next_end_epoch"], 120)
        self.assertFalse(result["complete"])
        self.assertIn("coverage requires review", result["last_error"])
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

    async def test_epoch_zero_gap_review_requires_operator_review_and_does_not_fetch_again(self):
        client = FakeRangeClient([candle(0), candle(60), candle(120)])
        first = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=120,
        )
        self.assertEqual(first["status"], "GAP_REVIEW")
        with self.assertRaisesRegex(ValueError, "operator review"):
            await run_backfill_batch(
                self.connection, client, "1HZ100V", "M1",
                batch_size=3, now_epoch=120,
            )
        self.assertEqual(len(client.calls), 1)

    async def test_batch_rolls_back_all_candles_when_one_observation_is_invalid(self):
        invalid = candle(3540)
        invalid["high"] = 1
        invalid["low"] = 2
        client = FakeRangeClient([candle(3480), invalid])
        with self.assertRaises(ValueError):
            await run_backfill_batch(
                self.connection, client, "1HZ100V", "M1",
                batch_size=3, now_epoch=3600,
            )
        stored = self.connection.execute(
            "SELECT COUNT(*) AS count FROM market_observations"
        ).fetchone()["count"]
        self.assertEqual(stored, 0)
        checkpoint = get_backfill_checkpoint(self.connection, "1HZ100V", "M1")
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertEqual(checkpoint["batches_completed"], 0)

    async def test_repeated_candle_batch_is_idempotent(self):
        candles = [candle(3480), candle(3540), candle(3600)]
        client = FakeRangeClient(candles)
        first = await run_backfill_batch(
            self.connection, client, "1HZ100V", "M1",
            batch_size=3, now_epoch=3600,
        )
        # A fresh checkpoint on a separate database connection is not needed
        # here: directly replay the same observation set to validate uniqueness.
        from app.market_observation_store import save_market_observation
        duplicates = [
            save_market_observation(self.connection, item) for item in candles
        ]
        self.assertEqual(first["inserted_count"], 3)
        self.assertTrue(all(not item["inserted"] for item in duplicates))
        stored = self.connection.execute(
            "SELECT COUNT(*) AS count FROM market_observations"
        ).fetchone()["count"]
        self.assertEqual(stored, 3)

    async def test_rejects_boolean_and_non_integer_now_epoch(self):
        for invalid in (True, 120.5, "120", -1):
            with self.subTest(now_epoch=invalid):
                with self.assertRaisesRegex(ValueError, "integer epoch"):
                    await run_backfill_batch(
                        self.connection, FakeRangeClient([]), "1HZ100V", "M1",
                        batch_size=3, now_epoch=invalid,
                    )

    async def test_rejects_unbounded_or_unrepresentable_batch_size(self):
        for invalid in (1, 501, True, 2.5):
            with self.subTest(batch_size=invalid):
                with self.assertRaisesRegex(ValueError, "batch_size"):
                    await run_backfill_batch(
                        self.connection, FakeRangeClient([]), "1HZ100V", "M1",
                        batch_size=invalid, now_epoch=120,
                    )


    async def test_provider_failure_moves_same_cursor_to_operator_review(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database_file:
            def factory():
                connection = sqlite3.connect(database_file.name)
                connection.row_factory = sqlite3.Row
                ensure_market_observation_schema(connection)
                return connection

            class FailingClient(FakeRangeClient):
                async def collect_candle_range(self, *args, **kwargs):
                    raise TimeoutError("provider timeout")

            with self.assertRaises(TimeoutError):
                await run_backfill_batch_with_connection_factory(
                    factory, FailingClient([]), "1HZ100V", "M1",
                    batch_size=3, now_epoch=3600,
                )
            connection = factory()
            try:
                checkpoint = get_backfill_checkpoint(connection, "1HZ100V", "M1")
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM market_observations"
                ).fetchone()["count"]
            finally:
                connection.close()
            self.assertEqual(checkpoint["status"], "GAP_REVIEW")
            self.assertEqual(checkpoint["next_end_epoch"], 3600)
            self.assertEqual(checkpoint["batches_completed"], 0)
            self.assertIn("TimeoutError", checkpoint["last_error"])
            self.assertEqual(count, 0)

    async def test_invalid_provider_batch_moves_same_cursor_to_operator_review(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database_file:
            def factory():
                connection = sqlite3.connect(database_file.name)
                connection.row_factory = sqlite3.Row
                ensure_market_observation_schema(connection)
                return connection

            invalid = candle(3540)
            invalid["high"] = 1
            invalid["low"] = 2
            client = FakeRangeClient([candle(3480), invalid, candle(3600)])
            with self.assertRaises(ValueError):
                await run_backfill_batch_with_connection_factory(
                    factory, client, "1HZ100V", "M1",
                    batch_size=3, now_epoch=3600,
                )
            connection = factory()
            try:
                checkpoint = get_backfill_checkpoint(connection, "1HZ100V", "M1")
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM market_observations"
                ).fetchone()["count"]
            finally:
                connection.close()
            self.assertEqual(checkpoint["status"], "GAP_REVIEW")
            self.assertEqual(checkpoint["next_end_epoch"], 3600)
            self.assertEqual(checkpoint["batches_completed"], 0)
            self.assertIn("ValueError", checkpoint["last_error"])
            self.assertEqual(count, 0)


    async def test_concurrent_workers_cannot_persist_the_same_checkpoint_batch_twice(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite3") as database_file:
            def factory():
                connection = sqlite3.connect(database_file.name, timeout=10)
                connection.row_factory = sqlite3.Row
                ensure_market_observation_schema(connection)
                return connection

            arrivals = 0
            both_fetching = asyncio.Event()
            candles = [candle(3480), candle(3540), candle(3600)]

            class BarrierClient(FakeRangeClient):
                async def collect_candle_range(self, *args, **kwargs):
                    nonlocal arrivals
                    arrivals += 1
                    if arrivals == 2:
                        both_fetching.set()
                    await asyncio.wait_for(both_fetching.wait(), timeout=3)
                    return await super().collect_candle_range(*args, **kwargs)

            client = BarrierClient(candles)
            outcomes = await asyncio.gather(
                *[
                    run_backfill_batch_with_connection_factory(
                        factory, client, "1HZ100V", "M1",
                        batch_size=3, now_epoch=3600,
                    )
                    for _ in range(2)
                ],
                return_exceptions=True,
            )
            successes = [item for item in outcomes if isinstance(item, dict)]
            failures = [item for item in outcomes if isinstance(item, Exception)]
            connection = factory()
            try:
                checkpoint = get_backfill_checkpoint(connection, "1HZ100V", "M1")
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM market_observations"
                ).fetchone()["count"]
            finally:
                connection.close()
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], RuntimeError)
            self.assertEqual(checkpoint["batches_completed"], 1)
            self.assertEqual(checkpoint["next_end_epoch"], 3420)
            self.assertEqual(checkpoint["status"], "RUNNING")
            self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
