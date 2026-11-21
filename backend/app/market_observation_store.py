"""Persistent storage for validated, non-executable market observations."""
from .market_observations import normalize_market_observation


def save_market_observation(connection, record):
    """Persist one normalized candle; return False for an idempotent duplicate."""
    observation = normalize_market_observation(record)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO market_observations (
            symbol, timeframe, observed_at, source,
            open, high, low, close, volume, provenance_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation["symbol"],
            observation["timeframe"],
            observation["timestamp"],
            observation["source"],
            observation["open"],
            observation["high"],
            observation["low"],
            observation["close"],
            observation["volume"],
            observation["provenance_status"],
        ),
    )
    return {
        "inserted": cursor.rowcount == 1,
        "symbol": observation["symbol"],
        "timeframe": observation["timeframe"],
        "timestamp": observation["timestamp"],
        "execution_eligible": False,
        "provenance_status": observation["provenance_status"],
    }


def list_market_observations(connection, symbol=None, timeframe=None, limit=100):
    """Return stored candles newest-first with strict bounded query parameters."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000.")
    clauses = []
    params = []
    if symbol is not None:
        from .instruments import get_instrument
        clauses.append("symbol = ?")
        params.append(get_instrument(symbol)["symbol"])
    if timeframe is not None:
        from .market_observations import SUPPORTED_TIMEFRAMES
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError("Unsupported observation timeframe.")
        clauses.append("timeframe = ?")
        params.append(timeframe)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = connection.execute(
        """
        SELECT symbol, timeframe, observed_at AS timestamp, source,
               open, high, low, close, volume, provenance_status, received_at
        FROM market_observations
        """
        + where
        + " ORDER BY observed_at DESC, observation_id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [
        {
            **dict(row),
            "execution_eligible": False,
        }
        for row in rows
    ]


def inspect_market_observation_quality(connection, symbol, timeframe, now=None):
    """Report freshness and missing intervals for a continuous Deriv synthetic series."""
    from datetime import datetime, timezone
    from .instruments import get_instrument
    from .market_observations import SUPPORTED_TIMEFRAMES

    instrument = get_instrument(symbol)
    if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
        raise ValueError("Quality diagnostics currently support Deriv synthetic indices only.")
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("Unsupported observation timeframe.")

    granularity_seconds = {
        "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H4": 14400, "D1": 86400,
    }[timeframe]
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("Diagnostic reference time must include a timezone.")
    checked_at = checked_at.astimezone(timezone.utc)

    rows = connection.execute(
        """
        SELECT observed_at
        FROM market_observations
        WHERE symbol = ? AND timeframe = ? AND source = 'deriv'
        ORDER BY observed_at ASC
        """,
        (instrument["symbol"], timeframe),
    ).fetchall()

    if not rows:
        return {
            "symbol": instrument["symbol"], "timeframe": timeframe,
            "status": "NO_DATA", "observation_count": 0,
            "latest_timestamp": None, "age_seconds": None,
            "stale_after_seconds": granularity_seconds * 2,
            "stale": True, "missing_interval_count": 0,
            "missing_candle_count": 0, "missing_intervals": [],
            "checked_at": checked_at.isoformat(),
            "execution_eligible": False,
        }

    timestamps = [
        datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        .astimezone(timezone.utc)
        for row in rows
    ]
    latest = timestamps[-1]
    age_seconds = max(0.0, (checked_at - latest).total_seconds())
    stale_after_seconds = granularity_seconds * 2
    stale = age_seconds > stale_after_seconds
    missing_intervals = []
    missing_candle_count = 0
    for previous, current in zip(timestamps, timestamps[1:]):
        elapsed = (current - previous).total_seconds()
        if elapsed > granularity_seconds:
            missing = max(0, round(elapsed / granularity_seconds) - 1)
            if missing:
                missing_candle_count += missing
                if len(missing_intervals) < 50:
                    missing_intervals.append({
                        "after": previous.isoformat(),
                        "before": current.isoformat(),
                        "missing_candles": missing,
                    })

    status = "STALE" if stale else ("GAPS_DETECTED" if missing_candle_count else "FRESH")
    return {
        "symbol": instrument["symbol"], "timeframe": timeframe,
        "status": status, "observation_count": len(timestamps),
        "latest_timestamp": latest.isoformat(), "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds, "stale": stale,
        "missing_interval_count": sum(1 for item in missing_intervals),
        "missing_candle_count": missing_candle_count,
        "missing_intervals": missing_intervals,
        "checked_at": checked_at.isoformat(),
        "execution_eligible": False,
    }
