"""Read-only live smoke test for Deriv's public market-data WebSocket.

This script performs only active-symbol discovery and historical candle reads.
It does not authenticate, subscribe, persist observations, generate signals,
or submit any trading request.
"""
import asyncio
from datetime import datetime, timezone

from app.deriv_public_client import DerivPublicMarketDataClient


async def main():
    client = DerivPublicMarketDataClient(timeout=12)

    symbols = await client.discover_synthetic_symbols()
    if not symbols:
        raise RuntimeError("Deriv returned no catalogued active synthetic symbols.")

    preferred = next((item for item in symbols if item["symbol"] == "1HZ100V"), None)
    selected = preferred or symbols[0]
    candles = await client.fetch_candles(selected["symbol"], "M1", count=10)

    if len(candles) < 2:
        raise RuntimeError(
            f"Deriv returned only {len(candles)} candles; at least 2 are required."
        )

    now = datetime.now(timezone.utc)
    timestamps = [
        datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
        for item in candles
    ]
    latest_age = (now - timestamps[-1]).total_seconds()

    if latest_age < -60:
        raise RuntimeError("Deriv returned a candle timestamp more than 60 seconds in the future.")
    if latest_age > 300:
        raise RuntimeError(
            f"Latest M1 candle is stale ({latest_age:.0f} seconds old; limit is 300)."
        )

    print("DERIV PUBLIC MARKET-DATA SMOKE: PASSED")
    print(f"Active catalogued synthetic symbols: {len(symbols)}")
    print(f"Selected symbol: {selected['symbol']}")
    print(f"Historical M1 candles received: {len(candles)}")
    print(f"Latest candle UTC: {timestamps[-1].isoformat()}")
    print(f"Latest candle age seconds: {latest_age:.1f}")
    print("Read-only: true | Authenticated: false | Persisted: false | Orders: none")


if __name__ == "__main__":
    asyncio.run(main())
