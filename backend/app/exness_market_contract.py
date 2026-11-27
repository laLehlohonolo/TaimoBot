"""Read-only normalization contract for Exness MT5 XAUUSD observations.

This module does not connect to MT5, store credentials, or expose order APIs.
It only validates and normalizes data already collected by a separately
controlled read-only MT5 bridge. Exact server-symbol mapping is mandatory.
"""
from datetime import datetime, timezone
import math


class ExnessMarketDataError(ValueError):
    """Raised when an MT5 quote or symbol specification is unsafe to accept."""


def _number(value, field, *, positive=False, non_negative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ExnessMarketDataError(f"{field} must be a numeric value.")
    if not math.isfinite(value):
        raise ExnessMarketDataError(f"{field} must be finite.")
    if positive and value <= 0:
        raise ExnessMarketDataError(f"{field} must be greater than zero.")
    if non_negative and value < 0:
        raise ExnessMarketDataError(f"{field} cannot be negative.")
    return value


def _integer(value, field, *, positive=False, non_negative=False):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExnessMarketDataError(f"{field} must be an integer.")
    if positive and value <= 0:
        raise ExnessMarketDataError(f"{field} must be greater than zero.")
    if non_negative and value < 0:
        raise ExnessMarketDataError(f"{field} cannot be negative.")
    return value


def _aware_utc(value, field):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExnessMarketDataError(f"{field} must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat()


def normalize_symbol_specification(raw, *, expected_server_symbol, captured_at):
    """Validate MT5 symbol_info fields; never guess suffixes or contract values."""
    if not isinstance(raw, dict):
        raise ExnessMarketDataError("Symbol specification must be an object.")
    if not isinstance(expected_server_symbol, str) or not expected_server_symbol.strip():
        raise ExnessMarketDataError("An explicit expected server symbol is required.")
    if raw.get("name") != expected_server_symbol:
        raise ExnessMarketDataError("MT5 server symbol does not exactly match the configured mapping.")

    digits = _integer(raw.get("digits"), "digits", non_negative=True)
    if digits > 12:
        raise ExnessMarketDataError("digits exceeds the supported validation range.")
    specification = {
        "contract_size": _number(raw.get("trade_contract_size"), "trade_contract_size", positive=True),
        "point": _number(raw.get("point"), "point", positive=True),
        "tick_size": _number(raw.get("trade_tick_size"), "trade_tick_size", positive=True),
        "tick_value": _number(raw.get("trade_tick_value"), "trade_tick_value", positive=True),
        "volume_min": _number(raw.get("volume_min"), "volume_min", positive=True),
        "volume_max": _number(raw.get("volume_max"), "volume_max", positive=True),
        "volume_step": _number(raw.get("volume_step"), "volume_step", positive=True),
        "stops_level_points": _integer(raw.get("trade_stops_level"), "trade_stops_level", non_negative=True),
        "freeze_level_points": _integer(raw.get("trade_freeze_level"), "trade_freeze_level", non_negative=True),
        "calculation_mode": _integer(raw.get("trade_calc_mode"), "trade_calc_mode", non_negative=True),
        "digits": digits,
    }
    if specification["volume_max"] < specification["volume_min"]:
        raise ExnessMarketDataError("volume_max cannot be below volume_min.")
    if specification["volume_step"] > specification["volume_max"]:
        raise ExnessMarketDataError("volume_step cannot exceed volume_max.")
    return {
        "provider_id": "exness",
        "instrument": "XAUUSD",
        "canonical_symbol": "XAUUSD",
        "server_symbol": expected_server_symbol,
        "specification": specification,
        "captured_at": _aware_utc(captured_at, "captured_at"),
        "verification_status": "UNVERIFIED_PENDING_REVIEW",
        "execution_eligible": False,
    }


def normalize_tick(raw, *, server_symbol, expected_server_symbol, received_at):
    """Normalize one MT5 tick after an explicit symbol mapping has been chosen."""
    if not isinstance(raw, dict):
        raise ExnessMarketDataError("MT5 tick must be an object.")
    if not isinstance(expected_server_symbol, str) or not expected_server_symbol.strip():
        raise ExnessMarketDataError("An explicit expected server symbol is required.")
    if server_symbol != expected_server_symbol:
        raise ExnessMarketDataError("MT5 tick symbol does not match the configured mapping.")

    bid = _number(raw.get("bid"), "bid", positive=True)
    ask = _number(raw.get("ask"), "ask", positive=True)
    if ask < bid:
        raise ExnessMarketDataError("ask cannot be below bid.")
    time_msc = _integer(raw.get("time_msc"), "time_msc", positive=True)
    provider_time = datetime.fromtimestamp(time_msc / 1000, tz=timezone.utc)
    received_time = _aware_utc(received_at, "received_at")
    return {
        "provider_id": "exness",
        "instrument": "XAUUSD",
        "canonical_symbol": "XAUUSD",
        "server_symbol": server_symbol,
        "bid": bid,
        "ask": ask,
        "spread": ask - bid,
        "provider_time": provider_time.isoformat(),
        "received_at": received_time,
        "evidence_id": f"exness:{server_symbol}:{time_msc}",
        "verification_status": "UNVERIFIED_PENDING_REVIEW",
        "execution_eligible": False,
    }
