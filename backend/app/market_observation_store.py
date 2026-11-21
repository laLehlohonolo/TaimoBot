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
