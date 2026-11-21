"""Read-only Deriv market-data response adapters.

These helpers validate provider responses. They do not open a socket, subscribe,
persist data, generate signals, or submit any trade/order request.
"""
from datetime import datetime, timezone
import math

from .instruments import get_instrument
from .market_observations import normalize_market_observation


DERIV_SYNTHETIC_SYMBOLS = frozenset({
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V", "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V",
})


def _finite_positive(value, field):
    if isinstance(value, bool):
        raise ValueError(field + " must be a finite positive number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(field + " must be a finite positive number.")
    if not math.isfinite(number) or number <= 0:
        raise ValueError(field + " must be a finite positive number.")
    return number


def normalize_active_symbols_response(payload):
    """Return known Deriv synthetic symbols present in an active_symbols reply."""
    if not isinstance(payload, dict) or payload.get("msg_type") != "active_symbols":
        raise ValueError("Expected a Deriv active_symbols response.")
    records = payload.get("active_symbols")
    if not isinstance(records, list):
        raise ValueError("Deriv active_symbols response must contain a list.")

    discovered = []
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        symbol = record.get("underlying_symbol")
        if not isinstance(symbol, str):
            continue
        symbol = symbol.strip().upper()
        if symbol not in DERIV_SYNTHETIC_SYMBOLS or symbol in seen:
            continue
        instrument = get_instrument(symbol)
        pip_size = _finite_positive(record.get("pip_size"), "pip_size")
        market = record.get("market")
        subgroup = record.get("subgroup")
        submarket = record.get("submarket")
        display_name = record.get("underlying_symbol_name")
        symbol_type = record.get("underlying_symbol_type")
        trade_count = record.get("trade_count")
        if not all(isinstance(value, str) and value.strip()
                   for value in (market, subgroup, submarket, display_name, symbol_type)):
            raise ValueError("Deriv symbol metadata is incomplete.")
        if isinstance(trade_count, bool) or not isinstance(trade_count, int) or trade_count < 0:
            raise ValueError("trade_count must be a non-negative integer.")
        for flag in ("exchange_is_open", "is_trading_suspended"):
            if record.get(flag) not in (0, 1) or isinstance(record.get(flag), bool):
                raise ValueError(flag + " must be 0 or 1.")
        discovered.append({
            **instrument,
            "display_name": display_name.strip(),
            "underlying_symbol_type": symbol_type.strip(),
            "trade_count": trade_count,
            "market": market.strip(),
            "subgroup": subgroup.strip(),
            "submarket": submarket.strip(),
            "pip_size": pip_size,
            "exchange_is_open": bool(record["exchange_is_open"]),
            "is_trading_suspended": bool(record["is_trading_suspended"]),
            "provider_metadata_status": "UNVERIFIED_PROVIDER_RESPONSE",
            "execution_eligible": False,
        })
        seen.add(symbol)
    return discovered


def normalize_deriv_candle(symbol, candle, timeframe, source="deriv"):
    """Convert one Deriv candle response object into the common OHLCV format."""
    instrument = get_instrument(symbol)
    if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
        raise ValueError("Only catalogued Deriv synthetic indices are supported.")
    if not isinstance(candle, dict):
        raise ValueError("Deriv candle must be an object.")
    epoch = candle.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        raise ValueError("Deriv candle epoch must be numeric.")
    if not math.isfinite(float(epoch)) or epoch < 0:
        raise ValueError("Deriv candle epoch must be a finite non-negative number.")
    timestamp = datetime.fromtimestamp(float(epoch), tz=timezone.utc).isoformat()
    return normalize_market_observation({
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp,
        "open": candle.get("open"),
        "high": candle.get("high"),
        "low": candle.get("low"),
        "close": candle.get("close"),
        "volume": 0,
        "source": source,
    })
