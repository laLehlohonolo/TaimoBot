"""Read-only client for Deriv's public market-data WebSocket.

Only active-symbol discovery and historical candle requests are permitted here.
There are deliberately no authorization, proposal, buy, sell, or trading methods.
"""
import asyncio
import json
import math
from datetime import datetime

import websockets
from websockets.exceptions import ConnectionClosed

from .deriv_market_data import normalize_active_symbols_response, normalize_deriv_candle
from .instruments import get_instrument
from .market_observation_store import save_market_observation


DERIV_PUBLIC_WS_URL = "wss://api.derivws.com/trading/v1/options/ws/public"
TIMEFRAME_GRANULARITY_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}
DEFAULT_TIMEOUT_SECONDS = 12


class DerivPublicMarketDataClient:
    """Small request/response client restricted to public, read-only data."""

    def __init__(self, timeout=DEFAULT_TIMEOUT_SECONDS):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout must be a positive number.")
        self.timeout = float(timeout)

    async def _request(self, payload, expected_msg_type, request_id):
        """Retry only transient transport failures within a finite total deadline."""
        loop = asyncio.get_running_loop()
        max_attempts = 3
        backoffs = (0.25, 0.5)
        # Bound WebSocket close-handshake cleanup after a cancelled attempt.
        # asyncio.wait_for waits for cancellation cleanup, so this allowance
        # must be included in the overall retry budget.
        close_timeout = min(1.0, self.timeout)
        total_deadline = (
            loop.time()
            + (self.timeout * max_attempts)
            + (close_timeout * max_attempts)
            + sum(backoffs)
        )
        transient_errors = (
            TimeoutError,
            OSError,
            ConnectionClosed,
        )

        for attempt in range(max_attempts):
            remaining = total_deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Deriv request exceeded its total retry deadline.")
            try:
                return await asyncio.wait_for(
                    self._request_once(payload, expected_msg_type, request_id),
                    timeout=min(self.timeout, remaining),
                )
            except transient_errors:
                if attempt == max_attempts - 1:
                    raise
                delay = backoffs[attempt]
                remaining = total_deadline - loop.time()
                if remaining <= delay:
                    raise TimeoutError("Deriv request exceeded its total retry deadline.")
                await asyncio.sleep(delay)

        raise TimeoutError("Deriv request exhausted its retry attempts.")

    async def _request_once(self, payload, expected_msg_type, request_id):
        """Open one public socket, issue one read-only request, and close it."""
        async with websockets.connect(
            DERIV_PUBLIC_WS_URL,
            open_timeout=self.timeout,
            close_timeout=min(1.0, self.timeout),
            ping_interval=20,
        ) as socket:
            await socket.send(json.dumps({**payload, "req_id": request_id}))
            deadline = asyncio.get_running_loop().time() + self.timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("Deriv response timed out.")
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                try:
                    response = json.loads(raw)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValueError("Deriv returned a malformed JSON response.") from exc
                if not isinstance(response, dict):
                    raise ValueError("Deriv response must be a JSON object.")
                if response.get("req_id") != request_id:
                    continue
                if response.get("error"):
                    error = response["error"]
                    message = error.get("message", "Deriv request failed.") if isinstance(error, dict) else "Deriv request failed."
                    raise RuntimeError(message)
                if response.get("msg_type") != expected_msg_type:
                    raise ValueError("Deriv returned an unexpected response type.")
                return response

    async def discover_synthetic_symbols(self):
        """Fetch active provider symbols and retain only Taimo's known synthetics."""
        response = await self._request(
            {"active_symbols": "full"},
            expected_msg_type="active_symbols",
            request_id=1,
        )
        return normalize_active_symbols_response(response)

    async def fetch_candles(self, symbol, timeframe, count=100):
        """Fetch and normalize historical candles; this never subscribes or trades."""
        instrument = get_instrument(symbol)
        if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
            raise ValueError("Only catalogued Deriv synthetic indices are supported.")
        if timeframe not in TIMEFRAME_GRANULARITY_SECONDS:
            raise ValueError("Unsupported Deriv candle timeframe.")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
            raise ValueError("count must be an integer between 1 and 500.")
        response = await self._request(
            {
                "ticks_history": instrument["symbol"],
                "count": count,
                "end": "latest",
                "style": "candles",
                "granularity": TIMEFRAME_GRANULARITY_SECONDS[timeframe],
            },
            expected_msg_type="candles",
            request_id=2,
        )
        candles = response.get("candles")
        if not isinstance(candles, list):
            raise ValueError("Deriv history response must contain a candle list.")
        if len(candles) > count:
            raise ValueError("Deriv returned more candles than requested.")
        normalized = [
            normalize_deriv_candle(instrument["symbol"], candle, timeframe, source="deriv")
            for candle in candles
        ]
        granularity = TIMEFRAME_GRANULARITY_SECONDS[timeframe]
        previous_epoch = None
        for observation in normalized:
            observed_at = datetime.fromisoformat(observation["timestamp"].replace("Z", "+00:00"))
            epoch = observed_at.timestamp()
            if not math.isclose(epoch % granularity, 0.0, abs_tol=1e-6):
                raise ValueError("Deriv candle timestamp is not aligned to its timeframe.")
            if previous_epoch is not None and epoch <= previous_epoch:
                raise ValueError("Deriv candles must be strictly ordered and unique.")
            previous_epoch = epoch
        return normalized

    async def fetch_candle_range(self, symbol, timeframe, start_epoch, end_epoch, max_candles=500):
        """Fetch one bounded historical candle interval using explicit UTC epoch bounds."""
        instrument = get_instrument(symbol)
        if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
            raise ValueError("Only catalogued Deriv synthetic indices are supported.")
        if timeframe not in TIMEFRAME_GRANULARITY_SECONDS:
            raise ValueError("Unsupported Deriv candle timeframe.")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in (start_epoch, end_epoch)):
            raise ValueError("Historical range bounds must be non-negative integer epochs.")
        if end_epoch <= start_epoch:
            raise ValueError("Historical range end must be after its start.")
        if isinstance(max_candles, bool) or not isinstance(max_candles, int) or not 2 <= max_candles <= 500:
            raise ValueError("max_candles must be an integer between 2 and 500 for an explicit range.")
        granularity = TIMEFRAME_GRANULARITY_SECONDS[timeframe]
        if start_epoch % granularity or end_epoch % granularity:
            raise ValueError("Historical range bounds must align to the candle timeframe.")
        # Both endpoints are inclusive candle timestamps: N candles span
        # exactly N - 1 intervals. Reject a range that could return N + 1.
        if end_epoch - start_epoch > granularity * (max_candles - 1):
            raise ValueError("Historical range exceeds the bounded candle batch size.")

        response = await self._request(
            {
                "ticks_history": instrument["symbol"],
                "start": start_epoch,
                "end": str(end_epoch),
                "style": "candles",
                "granularity": granularity,
            },
            expected_msg_type="candles",
            request_id=3,
        )
        candles = response.get("candles")
        if not isinstance(candles, list):
            raise ValueError("Deriv history response must contain a candle list.")
        if len(candles) > max_candles:
            raise ValueError("Deriv returned more candles than the bounded range permits.")
        normalized = [
            normalize_deriv_candle(instrument["symbol"], candle, timeframe, source="deriv")
            for candle in candles
        ]
        previous_epoch = None
        for observation in normalized:
            observed_at = datetime.fromisoformat(observation["timestamp"].replace("Z", "+00:00"))
            epoch = int(observed_at.timestamp())
            if epoch % granularity:
                raise ValueError("Deriv candle timestamp is not aligned to its timeframe.")
            if epoch < start_epoch or epoch > end_epoch:
                raise ValueError("Deriv returned a candle outside the requested historical range.")
            if previous_epoch is not None and epoch <= previous_epoch:
                raise ValueError("Deriv candles must be strictly ordered and unique.")
            previous_epoch = epoch
        return normalized

    async def collect_candle_range(self, symbol, timeframe, start_epoch, end_epoch, max_candles=500):
        """Verify provider symbol activity before collecting a bounded historical range."""
        normalized_symbol = get_instrument(symbol)["symbol"]
        active = await self.discover_synthetic_symbols()
        if normalized_symbol not in {item["symbol"] for item in active}:
            raise ValueError("Deriv does not currently report this symbol as active.")
        return await self.fetch_candle_range(
            normalized_symbol, timeframe, start_epoch, end_epoch, max_candles=max_candles
        )

    async def collect_candles(self, symbol, timeframe, count=100):
        """Fetch and validate provider data without holding or opening a database connection."""
        normalized_symbol = get_instrument(symbol)["symbol"]
        active = await self.discover_synthetic_symbols()
        if normalized_symbol not in {item["symbol"] for item in active}:
            raise ValueError("Deriv does not currently report this symbol as active.")
        return await self.fetch_candles(normalized_symbol, timeframe, count=count)

    def persist_candles(self, connection, candles):
        """Persist a fully collected candle batch atomically using a short DB transaction."""
        if not isinstance(candles, list):
            raise ValueError("candles must be a list of normalized observations.")
        connection.execute("SAVEPOINT deriv_market_sync")
        try:
            results = [save_market_observation(connection, candle) for candle in candles]
            connection.execute("RELEASE SAVEPOINT deriv_market_sync")
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT deriv_market_sync")
            connection.execute("RELEASE SAVEPOINT deriv_market_sync")
            raise
        return results

    async def discover_and_store_candles(self, connection, symbol, timeframe, count=100):
        """Compatibility helper; production routes should collect before opening the DB."""
        normalized_symbol = get_instrument(symbol)["symbol"]
        candles = await self.collect_candles(normalized_symbol, timeframe, count=count)
        results = self.persist_candles(connection, candles)
        return {
            "provider": "Deriv",
            "symbol": normalized_symbol,
            "timeframe": timeframe,
            "received_count": len(candles),
            "inserted_count": sum(1 for item in results if item["inserted"]),
            "duplicate_count": sum(1 for item in results if not item["inserted"]),
            "read_only": True,
            "execution_triggered": False,
            "signals_generated": False,
            "observations": results,
        }
