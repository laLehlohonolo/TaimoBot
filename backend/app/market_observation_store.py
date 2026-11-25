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


def inspect_market_observation_coverage(connection, symbol, timeframe, source=None):
    """Measure stored candle coverage per source; never assert source authenticity."""
    from datetime import datetime
    from .instruments import get_instrument
    from .market_observations import SUPPORTED_TIMEFRAMES

    instrument = get_instrument(symbol)
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError("Unsupported observation timeframe.")
    seconds = {
        "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
        "H1": 3600, "H4": 14400, "D1": 86400,
    }[timeframe]
    clauses = ["symbol = ?", "timeframe = ?"]
    params = [instrument["symbol"], timeframe]
    if source is not None:
        if not isinstance(source, str) or not source.strip() or len(source.strip()) > 120:
            raise ValueError("source must contain 1 to 120 non-whitespace characters.")
        clauses.append("source = ?")
        params.append(source.strip())
    rows = connection.execute(
        "SELECT source, observed_at FROM market_observations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY source, observed_at",
        params,
    ).fetchall()

    by_source = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(
            datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        )

    reports = []
    for source_name, timestamps in sorted(by_source.items()):
        unique = sorted(set(timestamps))
        first, last = unique[0], unique[-1]
        unaligned = [
            stamp for stamp in unique
            if abs(stamp.timestamp() % seconds) > 1e-6
            and abs(stamp.timestamp() % seconds - seconds) > 1e-6
        ]
        cadence_valid = not unaligned
        # A single observation has no measurable interval. Reporting 100%
        # coverage for it would turn "one candle exists" into a false quality
        # signal, so require at least two unique timestamps before estimating.
        measurable_span = len(unique) >= 2 and cadence_valid
        expected = (
            int((last - first).total_seconds() // seconds) + 1
            if measurable_span else None
        )
        missing = max(0, expected - len(unique)) if measurable_span else None
        reports.append({
            "source": source_name,
            "observation_count": len(timestamps),
            "unique_timestamp_count": len(unique),
            "duplicate_timestamp_count": len(timestamps) - len(unique),
            "first_timestamp": first.isoformat(),
            "last_timestamp": last.isoformat(),
            "expected_candle_count": expected,
            "missing_candle_count": missing,
            "coverage_percent": round(
                (len(unique) / expected) * 100, 2
            ) if measurable_span and expected else None,
            "coverage_status": (
                "MEASURABLE" if measurable_span
                else ("INVALID_CADENCE" if not cadence_valid else "INSUFFICIENT_SPAN")
            ),
            "unaligned_timestamp_count": len(unaligned),
            "cadence_valid": cadence_valid,
            "provenance_status": "UNVERIFIED_EXTERNAL_INPUT",
            "provenance_verified": False,
            "eligible_for_backtest": False,
            "execution_eligible": False,
        })
    return {
        "symbol": instrument["symbol"],
        "timeframe": timeframe,
        "source_filter": source.strip() if source is not None else None,
        "source_count": len(reports),
        "sources": reports,
        "coverage_basis": "continuous_elapsed_time_between_first_and_last_candles",
        "session_calendar_status": "NOT_CONFIGURED",
        "session_calendar_id": None,
        "session_adjusted_coverage_available": False,
        "session_calendar_policy": "INSTRUMENT_SPECIFIC_CALENDAR_REQUIRED",
        "session_calendar_applied": False,
        "coverage_caveat": (
            "This is a raw elapsed-time coverage estimate. Exchange, OTC, holiday, "
            "maintenance and instrument-specific trading sessions are not excluded."
        ),
        "read_only": True,
        "provenance_verified": False,
        "eligible_for_backtest": False,
        "signals_generated": False,
        "execution_triggered": False,
    }


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
            "stale": True, "future_data": False,
            "future_observation_count": 0, "future_by_seconds": None,
            "unaligned_observation_count": 0, "unaligned_timestamps": [],
            "missing_interval_count": 0,
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
    unaligned_timestamps = [
        timestamp for timestamp in timestamps
        if not abs(timestamp.timestamp() % granularity_seconds) < 1e-6
        and not abs(timestamp.timestamp() % granularity_seconds - granularity_seconds) < 1e-6
    ]
    future_timestamps = [timestamp for timestamp in timestamps if timestamp > checked_at]
    future_data = bool(future_timestamps)
    future_by_seconds = (
        max((timestamp - checked_at).total_seconds() for timestamp in future_timestamps)
        if future_data else None
    )
    age_seconds = max(0.0, (checked_at - latest).total_seconds())
    stale_after_seconds = granularity_seconds * 2
    invalid_cadence = bool(unaligned_timestamps)
    stale = future_data or invalid_cadence or age_seconds > stale_after_seconds
    missing_intervals = []
    missing_interval_count = 0
    missing_candle_count = 0
    unaligned_set = set(unaligned_timestamps)
    for previous, current in zip(timestamps, timestamps[1:]):
        # Do not infer missing candle counts from an interval whose boundary
        # timestamps are themselves invalid for the requested timeframe.
        if previous in unaligned_set or current in unaligned_set:
            continue
        elapsed = (current - previous).total_seconds()
        if elapsed > granularity_seconds:
            missing = max(0, round(elapsed / granularity_seconds) - 1)
            if missing:
                missing_interval_count += 1
                missing_candle_count += missing
                if len(missing_intervals) < 50:
                    missing_intervals.append({
                        "after": previous.isoformat(),
                        "before": current.isoformat(),
                        "missing_candles": missing,
                    })

    status = (
        "INVALID_CADENCE" if invalid_cadence
        else ("FUTURE_DATA" if future_data
        else ("STALE" if stale else ("GAPS_DETECTED" if missing_candle_count else "FRESH")))
    )
    return {
        "symbol": instrument["symbol"], "timeframe": timeframe,
        "status": status, "observation_count": len(timestamps),
        "latest_timestamp": latest.isoformat(), "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds, "stale": stale,
        "future_data": future_data,
        "future_observation_count": len(future_timestamps),
        "future_by_seconds": future_by_seconds,
        "unaligned_observation_count": len(unaligned_timestamps),
        "unaligned_timestamps": [timestamp.isoformat() for timestamp in unaligned_timestamps[:50]],
        "missing_interval_count": missing_interval_count,
        "missing_candle_count": missing_candle_count,
        "missing_intervals": missing_intervals,
        "checked_at": checked_at.isoformat(),
        "execution_eligible": False,
    }
