import json
import unittest
from unittest.mock import AsyncMock, patch

from app.deriv_public_client import DerivPublicMarketDataClient


class RetrySocket:
    def __init__(self, response):
        self.response = response

    async def send(self, payload):
        self.sent = json.loads(payload)

    async def recv(self):
        return json.dumps(self.response)


class RetrySocketContext:
    def __init__(self, response):
        self.socket = RetrySocket(response)

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FailingSocketContext:
    async def __aenter__(self):
        raise OSError("temporary connection failure")

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DerivPublicClientRetryTests(unittest.IsolatedAsyncioTestCase):
    def valid_response(self):
        return {
            "msg_type": "candles",
            "req_id": 2,
            "candles": [
                {"epoch": 1795256100, "open": 1000, "high": 1002, "low": 999, "close": 1001}
            ],
        }

    async def test_transient_connection_failure_retries_then_succeeds(self):
        contexts = [
            FailingSocketContext(),
            RetrySocketContext(self.valid_response()),
        ]
        with patch("app.deriv_public_client.websockets.connect", side_effect=contexts) as connect, patch(
            "app.deriv_public_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            candles = await DerivPublicMarketDataClient(timeout=1).fetch_candles(
                "1HZ100V", "M1", count=1
            )
        self.assertEqual(len(candles), 1)
        self.assertEqual(connect.call_count, 2)
        sleep.assert_awaited_once_with(0.25)

    async def test_transient_failures_stop_after_three_total_attempts(self):
        with patch(
            "app.deriv_public_client.websockets.connect",
            side_effect=[FailingSocketContext(), FailingSocketContext(), FailingSocketContext()],
        ) as connect, patch(
            "app.deriv_public_client.asyncio.sleep", new_callable=AsyncMock
        ):
            with self.assertRaisesRegex(OSError, "temporary connection failure"):
                await DerivPublicMarketDataClient(timeout=1).fetch_candles(
                    "1HZ100V", "M1", count=1
                )
        self.assertEqual(connect.call_count, 3)

    async def test_permanent_provider_error_is_not_retried(self):
        response = {
            "req_id": 2,
            "msg_type": "candles",
            "error": {"message": "Unknown symbol"},
        }
        with patch(
            "app.deriv_public_client.websockets.connect",
            return_value=RetrySocketContext(response),
        ) as connect, patch(
            "app.deriv_public_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "Unknown symbol"):
                await DerivPublicMarketDataClient(timeout=1).fetch_candles(
                    "1HZ100V", "M1", count=1
                )
        connect.assert_called_once()
        sleep.assert_not_awaited()

    async def test_malformed_response_is_not_retried(self):
        class MalformedSocket:
            async def send(self, payload):
                pass

            async def recv(self):
                return "{not-json"

        class MalformedContext:
            async def __aenter__(self):
                return MalformedSocket()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        with patch(
            "app.deriv_public_client.websockets.connect",
            return_value=MalformedContext(),
        ) as connect, patch(
            "app.deriv_public_client.asyncio.sleep", new_callable=AsyncMock
        ) as sleep:
            with self.assertRaisesRegex(ValueError, "malformed JSON"):
                await DerivPublicMarketDataClient(timeout=1).fetch_candles(
                    "1HZ100V", "M1", count=1
                )
        connect.assert_called_once()
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
