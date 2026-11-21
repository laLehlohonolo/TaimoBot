import json
import sqlite3
import unittest
from unittest.mock import patch

from app.database import ensure_market_observation_schema
from app.deriv_public_client import (
    DERIV_PUBLIC_WS_URL,
    DerivPublicMarketDataClient,
)
from app.deriv_market_data import normalize_deriv_candle
from app.market_observation_store import list_market_observations


class FakeSocket:
    def __init__(self, response):
        self.responses = list(response) if isinstance(response, list) else [response]
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        if len(self.responses) > 1:
            response = self.responses.pop(0)
        else:
            response = self.responses[0]
        return json.dumps(response)


class FakeSocketContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DerivPublicClientTests(unittest.IsolatedAsyncioTestCase):
    def active_record(self, symbol="1HZ100V"):
        return {
            "underlying_symbol": symbol,
            "underlying_symbol_name": "Volatility 100 (1s) Index",
            "market": "synthetic_index",
            "subgroup": "synthetics",
            "submarket": "random_index",
            "pip_size": 2,
            "exchange_is_open": 1,
            "is_trading_suspended": 0,
            "trade_count": 0,
            "underlying_symbol_type": "stockindex",
        }

    def history_response(self):
        return {
            "msg_type": "candles",
            "req_id": 2,
            "candles": [
                {"epoch": 1795256100, "open": 1000, "high": 1002, "low": 999, "close": 1001},
                {"epoch": 1795256160, "open": 1001, "high": 1003, "low": 1000, "close": 1002},
            ],
        }

    async def test_discovery_uses_public_endpoint_and_only_returns_known_synthetics(self):
        socket = FakeSocket({
            "msg_type": "active_symbols",
            "req_id": 1,
            "active_symbols": [self.active_record(), self.active_record("EURUSD")],
        })
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)) as connect:
            result = await DerivPublicMarketDataClient().discover_synthetic_symbols()
        self.assertEqual([item["symbol"] for item in result], ["1HZ100V"])
        self.assertEqual(connect.call_args.args[0], DERIV_PUBLIC_WS_URL)
        self.assertEqual(socket.sent, [{"active_symbols": "full", "req_id": 1}])
        self.assertTrue(all(item["execution_eligible"] is False for item in result))

    async def test_fetch_candles_uses_history_only_and_normalizes_data(self):
        socket = FakeSocket(self.history_response())
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            candles = await DerivPublicMarketDataClient().fetch_candles("1HZ100V", "M1", count=2)
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]["source"], "deriv")
        self.assertFalse(candles[0]["execution_eligible"])
        self.assertEqual(socket.sent, [{
            "ticks_history": "1HZ100V", "count": 2, "end": "latest",
            "style": "candles", "granularity": 60, "req_id": 2,
        }])

    async def test_rejects_provider_candle_batch_larger_than_requested(self):
        response = self.history_response()
        response["candles"] = response["candles"] * 2
        socket = FakeSocket(response)
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "more candles than requested"):
                await DerivPublicMarketDataClient().fetch_candles("1HZ100V", "M1", count=2)

    async def test_rejects_deriv_candles_out_of_order_or_with_duplicate_timestamps(self):
        response = self.history_response()
        response["candles"] = list(reversed(response["candles"]))
        socket = FakeSocket(response)
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "strictly ordered and unique"):
                await DerivPublicMarketDataClient().fetch_candles("1HZ100V", "M1", count=2)

        response = self.history_response()
        response["candles"][1]["epoch"] = response["candles"][0]["epoch"]
        socket = FakeSocket(response)
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "strictly ordered and unique"):
                await DerivPublicMarketDataClient().fetch_candles("1HZ100V", "M1", count=2)

    async def test_rejects_deriv_candle_timestamp_not_aligned_to_timeframe(self):
        response = self.history_response()
        response["candles"][0]["epoch"] += 1
        socket = FakeSocket(response)
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "not aligned to its timeframe"):
                await DerivPublicMarketDataClient().fetch_candles("1HZ100V", "M1", count=2)

    async def test_deriv_candle_normalizer_rejects_epoch_outside_datetime_range(self):
        candle = {"epoch": 1e300, "open": 1000, "high": 1002, "low": 999, "close": 1001}
        with self.assertRaisesRegex(ValueError, "outside the supported timestamp range"):
            normalize_deriv_candle("1HZ100V", candle, "M1")

    async def test_deriv_candle_normalizer_rejects_non_deriv_instruments(self):
        candle = {"epoch": 1795256100, "open": 1000, "high": 1002, "low": 999, "close": 1001}
        with self.assertRaisesRegex(ValueError, "Deriv synthetic"):
            normalize_deriv_candle("XAUUSD", candle, "M1")

    async def test_active_symbol_discovery_rejects_duplicate_synthetic_records(self):
        record = self.active_record()
        duplicate = {**record, "underlying_symbol_name": "Conflicting duplicate name"}
        socket = FakeSocket({
            "msg_type": "active_symbols",
            "req_id": 1,
            "active_symbols": [record, duplicate],
        })
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "duplicate synthetic symbol"):
                await DerivPublicMarketDataClient().discover_synthetic_symbols()

    async def test_active_symbol_metadata_must_match_catalogued_asset_class(self):
        record = self.active_record()
        record["market"] = "forex"
        socket = FakeSocket({
            "msg_type": "active_symbols",
            "req_id": 1,
            "active_symbols": [record],
        })
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "market metadata does not match"):
                await DerivPublicMarketDataClient().discover_synthetic_symbols()

    async def test_rejects_non_deriv_symbols_invalid_timeframes_and_counts_before_network(self):
        client = DerivPublicMarketDataClient()
        for args in (("EURUSD", "M1", 10), ("1HZ100V", "M2", 10),
                     ("1HZ100V", "M1", 0), ("1HZ100V", "M1", 501),
                     ("1HZ100V", "M1", True)):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    await client.fetch_candles(*args)

    async def test_provider_error_and_unexpected_response_are_rejected(self):
        client = DerivPublicMarketDataClient()
        error_socket = FakeSocket({
            "req_id": 2, "msg_type": "candles",
            "error": {"message": "Unknown symbol"},
        })
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(error_socket)):
            with self.assertRaisesRegex(RuntimeError, "Unknown symbol"):
                await client.fetch_candles("1HZ100V", "M1", 1)
        bad_socket = FakeSocket({"req_id": 2, "msg_type": "tick"})
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(bad_socket)):
            with self.assertRaisesRegex(ValueError, "unexpected response"):
                await client.fetch_candles("1HZ100V", "M1", 1)

    async def test_collect_candles_finishes_without_database_connection(self):
        contexts = [
            FakeSocketContext(FakeSocket({
                "msg_type": "active_symbols", "req_id": 1,
                "active_symbols": [self.active_record()],
            })),
            FakeSocketContext(FakeSocket(self.history_response())),
        ]
        with patch("app.deriv_public_client.websockets.connect", side_effect=contexts):
            candles = await DerivPublicMarketDataClient().collect_candles("1HZ100V", "M1", 2)
        self.assertEqual(len(candles), 2)
        self.assertTrue(all(candle["execution_eligible"] is False for candle in candles))

    async def test_persist_candles_is_separate_and_atomic(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(connection)
        contexts = [
            FakeSocketContext(FakeSocket({
                "msg_type": "active_symbols", "req_id": 1,
                "active_symbols": [self.active_record()],
            })),
            FakeSocketContext(FakeSocket(self.history_response())),
        ]
        with patch("app.deriv_public_client.websockets.connect", side_effect=contexts):
            candles = await DerivPublicMarketDataClient().collect_candles("1HZ100V", "M1", 2)
        results = DerivPublicMarketDataClient().persist_candles(connection, candles)
        self.assertEqual(sum(1 for item in results if item["inserted"]), 2)
        self.assertEqual(len(list_market_observations(connection, symbol="1HZ100V", timeframe="M1")), 2)
        connection.close()

    async def test_discover_and_store_is_idempotent_and_never_execution_eligible(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(connection)
        active_socket = FakeSocket({
            "msg_type": "active_symbols", "req_id": 1,
            "active_symbols": [self.active_record()],
        })
        history_socket = FakeSocket(self.history_response())
        contexts = [FakeSocketContext(active_socket), FakeSocketContext(history_socket)]
        with patch("app.deriv_public_client.websockets.connect", side_effect=contexts):
            client = DerivPublicMarketDataClient()
            first = await client.discover_and_store_candles(connection, "1HZ100V", "M1", 2)
        self.assertEqual(first["received_count"], 2)
        self.assertEqual(first["inserted_count"], 2)
        self.assertTrue(first["read_only"])
        self.assertFalse(first["execution_triggered"])
        self.assertFalse(first["signals_generated"])
        rows = list_market_observations(connection, symbol="1HZ100V", timeframe="M1")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["execution_eligible"] is False for row in rows))
        connection.close()

    async def test_repeat_sync_counts_existing_candles_as_duplicates(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(connection)
        contexts = []
        for _ in range(2):
            contexts.extend([
                FakeSocketContext(FakeSocket({
                    "msg_type": "active_symbols", "req_id": 1,
                    "active_symbols": [self.active_record()],
                })),
                FakeSocketContext(FakeSocket(self.history_response())),
            ])
        with patch("app.deriv_public_client.websockets.connect", side_effect=contexts):
            client = DerivPublicMarketDataClient()
            await client.discover_and_store_candles(connection, "1HZ100V", "M1", 2)
            second = await client.discover_and_store_candles(connection, "1HZ100V", "M1", 2)
        self.assertEqual(second["inserted_count"], 0)
        self.assertEqual(second["duplicate_count"], 2)
        self.assertEqual(len(list_market_observations(connection)), 2)
        connection.close()

    async def test_database_failure_rolls_back_entire_candle_batch(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(connection)
        connection.execute("""
            CREATE TRIGGER reject_second_deriv_candle
            BEFORE INSERT ON market_observations
            WHEN NEW.observed_at = '2026-11-21T10:16:00+00:00'
            BEGIN SELECT RAISE(ABORT, 'test batch failure'); END;
        """)
        contexts = [
            FakeSocketContext(FakeSocket({
                "msg_type": "active_symbols", "req_id": 1,
                "active_symbols": [self.active_record()],
            })),
            FakeSocketContext(FakeSocket(self.history_response())),
        ]
        with patch("app.deriv_public_client.websockets.connect", side_effect=contexts):
            with self.assertRaises(sqlite3.IntegrityError):
                await DerivPublicMarketDataClient().discover_and_store_candles(
                    connection, "1HZ100V", "M1", 2
                )
        self.assertEqual(list_market_observations(connection), [])
        connection.close()

    async def test_rejects_catalogued_symbol_not_active_on_deriv(self):
        socket = FakeSocket({
            "msg_type": "active_symbols", "req_id": 1,
            "active_symbols": [self.active_record("R_50")],
        })
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        ensure_market_observation_schema(connection)
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaisesRegex(ValueError, "does not currently report this symbol as active"):
                await DerivPublicMarketDataClient().discover_and_store_candles(
                    connection, "1HZ100V", "M1", 1
                )
        connection.close()


    async def test_unmatched_response_is_ignored_before_matching_response(self):
        matching = self.history_response()
        socket = FakeSocket([
            {"msg_type": "candles", "req_id": 999, "candles": []},
            matching,
        ])
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            candles = await DerivPublicMarketDataClient().fetch_candles(
                "1HZ100V", "M1", count=2
            )
        self.assertEqual(len(candles), 2)
        self.assertEqual(socket.sent[0]["req_id"], 2)

    async def test_unmatched_request_ids_do_not_extend_request_deadline(self):
        socket = FakeSocket({"msg_type": "candles", "req_id": 999, "candles": []})
        with patch("app.deriv_public_client.websockets.connect", return_value=FakeSocketContext(socket)):
            with self.assertRaises(TimeoutError):
                await DerivPublicMarketDataClient(timeout=0.01).fetch_candles(
                    "1HZ100V", "M1", count=1
                )


if __name__ == "__main__":
    unittest.main()
