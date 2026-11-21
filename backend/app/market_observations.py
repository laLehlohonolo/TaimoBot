"""Validation boundary for provider-neutral market observations.

Incoming market data is untrusted until its provider, symbol mapping, timestamp,
and broker/feed contract have been independently verified. This module only
normalizes OHLCV records; it never creates signals or paper orders.
"""
from datetime import datetime, timezone
import math

from .instruments import get_instrument


SUPPORTED_TIMEFRAMES = frozenset({"M1", "M5", "M15", "M30", "H1", "H4", "D1"})
MAX_SOURCE_LENGTH = 80


def _positive_finite_number(value, field):
    if isinstance(value, bool):
        raise ValueError(field + " must be a finite positive number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(field + " must be a finite positive number.")
    if not math.isfinite(number) or number <= 0:
        raise ValueError(field + " must be a finite positive number.")
    return number


def normalize_market_observation(record):
    """Validate and normalize one OHLCV candle without trusting its provenance."""
    if not isinstance(record, dict):
        raise ValueError("Market observation must be an object.")

    symbol = get_instrument(record.get("symbol"))["symbol"]
    timeframe = record.get("timeframe")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("Unsupported observation timeframe.")

    source = record.get("source")
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > MAX_SOURCE_LENGTH:
        raise ValueError("Observation source must be a non-empty identifier of at most 80 characters.")

    raw_timestamp = record.get("timestamp")
    if not isinstance(raw_timestamp, str):
        raise ValueError("Observation timestamp must be an ISO-8601 string with timezone.")
    try:
        parsed_timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Observation timestamp must be a valid ISO-8601 timestamp.")
    if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
        raise ValueError("Observation timestamp must include a timezone.")
    timestamp = parsed_timestamp.astimezone(timezone.utc).isoformat()

    opening = _positive_finite_number(record.get("open"), "open")
    high = _positive_finite_number(record.get("high"), "high")
    low = _positive_finite_number(record.get("low"), "low")
    close = _positive_finite_number(record.get("close"), "close")

    if low > min(opening, close) or high < max(opening, close) or low > high:
        raise ValueError("OHLC values do not form a valid candle.")

    raw_volume = record.get("volume", 0)
    if isinstance(raw_volume, bool):
        raise ValueError("volume must be a finite non-negative number.")
    try:
        volume = float(raw_volume)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("volume must be a finite non-negative number.")
    if not math.isfinite(volume) or volume < 0:
        raise ValueError("volume must be a finite non-negative number.")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp,
        "open": opening,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
        "source": source.strip(),
        "provenance_status": "UNVERIFIED_EXTERNAL_INPUT",
        "execution_eligible": False,
    }
