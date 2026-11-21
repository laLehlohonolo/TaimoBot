"""Read-only client for Deriv's public market-data WebSocket.

Only active-symbol discovery and historical candle requests are permitted here.
There are deliberately no authorization, proposal, buy, sell, or trading methods.
"""
import asyncio
import json

import websockets

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
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number.")
        self.timeout = float(timeout)

    async def _request(self, payload, expected_msg_type, request_id):
        """Open one public socket, issue one read-only request, and close it."""
        async with websockets.connect(
            DERIV_PUBLIC_WS_URL,
            open_timeout=self.timeout,
            close_timeout=5,
            ping_interval=20,
        ) as socket:
            await socket.send(json.dumps({**payload, "req_id": request_id}))
            while True:
                raw = await asyncio.wait_for(socket.recv(), timeout=self.timeout)
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
            expected_msg_type="history",
            request_id=2,
        )
        candles = response.get("candles")
        if not isinstance(candles, list):
            raise ValueError("Deriv history response must contain a candle list.")
        return [
            normalize_deriv_candle(instrument["symbol"], candle, timeframe, source="deriv")
            for candle in candles
        ]

    async def discover_and_store_candles(self, connection, symbol, timeframe, count=100):
        """Discover symbol availability, fetch candles, and persist them as research data."""
        normalized_symbol = get_instrument(symbol)["symbol"]
        active = await self.discover_synthetic_symbols()
        if normalized_symbol not in {item["symbol"] for item in active}:
            raise ValueError("Deriv does not currently report this symbol as active.")
        candles = await self.fetch_candles(normalized_symbol, timeframe, count=count)
        results = [save_market_observation(connection, candle) for candle in candles]
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
