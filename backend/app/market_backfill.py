"""Resumable, bounded historical candle backfill for read-only market research."""

from datetime import datetime, timezone

from .instruments import get_instrument
from .market_observation_store import save_market_observation
from .deriv_public_client import TIMEFRAME_GRANULARITY_SECONDS


def get_backfill_checkpoint(connection, symbol, timeframe):
    instrument = get_instrument(symbol)
    row = connection.execute(
        """
        SELECT symbol, timeframe, next_end_epoch, status,
               earliest_observed_at, latest_observed_at,
               batches_completed, last_error, updated_at
        FROM market_backfill_checkpoints
        WHERE symbol = ? AND timeframe = ?
        """,
        (instrument["symbol"], timeframe),
    ).fetchone()
    return dict(row) if row else None


def prepare_backfill_batch(
    connection, symbol, timeframe, *, batch_size=500, now_epoch=None
):
    """Read and validate one backfill request without performing network I/O.

    A worker should close its database connection after this call, fetch the
    range, then open a fresh connection for persist_backfill_batch().
    """
    instrument = get_instrument(symbol)
    if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
        raise ValueError("Historical backfill currently supports Deriv synthetic indices only.")
    if timeframe not in TIMEFRAME_GRANULARITY_SECONDS:
        raise ValueError("Unsupported backfill timeframe.")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 2 <= batch_size <= 500:
        raise ValueError("batch_size must be an integer between 2 and 500.")
    granularity = TIMEFRAME_GRANULARITY_SECONDS[timeframe]
    if now_epoch is not None and (
        isinstance(now_epoch, bool) or not isinstance(now_epoch, int) or now_epoch < 0
    ):
        raise ValueError("now_epoch must be a non-negative integer epoch.")
    current_epoch = now_epoch if now_epoch is not None else int(datetime.now(timezone.utc).timestamp())
    current_epoch = current_epoch // granularity * granularity

    connection.execute(
        """
        INSERT OR IGNORE INTO market_backfill_checkpoints (
            symbol, timeframe, next_end_epoch, status
        ) VALUES (?, ?, ?, 'RUNNING')
        """,
        (instrument["symbol"], timeframe, current_epoch),
    )
    checkpoint = get_backfill_checkpoint(connection, instrument["symbol"], timeframe)
    if checkpoint["status"] == "COMPLETE":
        return {
            "symbol": instrument["symbol"], "timeframe": timeframe,
            "status": "COMPLETE", "complete": True,
        }
    if checkpoint["status"] != "RUNNING" or checkpoint["next_end_epoch"] is None:
        raise ValueError("Backfill checkpoint requires operator review before resuming.")

    end_epoch = min(int(checkpoint["next_end_epoch"]), current_epoch)
    start_epoch = max(0, end_epoch - granularity * (batch_size - 1))
    return {
        "symbol": instrument["symbol"],
        "timeframe": timeframe,
        "expected_next_end_epoch": checkpoint["next_end_epoch"],
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "batch_size": batch_size,
        "granularity": granularity,
    }


def persist_backfill_batch(connection, request, candles):
    """Atomically persist a fetched batch only if its checkpoint is still current."""
    if not isinstance(request, dict) or not isinstance(candles, list):
        raise ValueError("A prepared backfill request and candle list are required.")
    symbol = request.get("symbol")
    timeframe = request.get("timeframe")
    instrument = get_instrument(symbol)
    if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
        raise ValueError("Historical backfill currently supports Deriv synthetic indices only.")
    granularity = TIMEFRAME_GRANULARITY_SECONDS.get(timeframe)
    if (
        granularity is None
        or request.get("granularity") != granularity
        or isinstance(request.get("batch_size"), bool)
        or not isinstance(request.get("batch_size"), int)
        or not 2 <= request["batch_size"] <= 500
        or isinstance(request.get("expected_next_end_epoch"), bool)
        or not isinstance(request.get("expected_next_end_epoch"), int)
        or isinstance(request.get("start_epoch"), bool)
        or not isinstance(request.get("start_epoch"), int)
        or isinstance(request.get("end_epoch"), bool)
        or not isinstance(request.get("end_epoch"), int)
        or request["start_epoch"] < 0
        or request["end_epoch"] < request["start_epoch"]
        or request["start_epoch"] % granularity
        or request["end_epoch"] % granularity
        or request["end_epoch"] - request["start_epoch"] > granularity * (request["batch_size"] - 1)
    ):
        raise ValueError("Prepared backfill request is invalid.")
    if len(candles) > request["batch_size"]:
        raise ValueError("Provider returned more candles than the prepared batch permits.")

    checkpoint = get_backfill_checkpoint(connection, instrument["symbol"], timeframe)
    if (
        checkpoint is None
        or checkpoint["status"] != "RUNNING"
        or checkpoint["next_end_epoch"] != request["expected_next_end_epoch"]
    ):
        raise RuntimeError("Backfill checkpoint changed during collection; discard this stale batch.")

    connection.execute("SAVEPOINT market_backfill_batch")
    try:
        results = [save_market_observation(connection, item) for item in candles]
        earliest = min((item["timestamp"] for item in candles), default=None)
        latest = max((item["timestamp"] for item in candles), default=None)
        previous_earliest = checkpoint["earliest_observed_at"]
        previous_latest = checkpoint["latest_observed_at"]
        earliest = min(previous_earliest, earliest) if previous_earliest and earliest else (previous_earliest or earliest)
        latest = max(previous_latest, latest) if previous_latest and latest else (previous_latest or latest)

        if request["start_epoch"] == 0:
            status = "GAP_REVIEW"
            next_end = request["end_epoch"]
            error = "Reached the epoch-zero boundary; provider history coverage requires review."
        elif not candles:
            status = "GAP_REVIEW"
            next_end = request["end_epoch"]
            error = "Provider returned no candles for a non-zero historical interval."
        else:
            status = "RUNNING"
            next_end = request["start_epoch"] - granularity
            error = None

        updated = connection.execute(
            """
            UPDATE market_backfill_checkpoints
            SET next_end_epoch = ?, status = ?,
                earliest_observed_at = ?, latest_observed_at = ?,
                batches_completed = batches_completed + 1,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = ? AND timeframe = ?
              AND status = 'RUNNING' AND next_end_epoch = ?
            """,
            (
                next_end, status, earliest, latest, error,
                instrument["symbol"], timeframe, request["expected_next_end_epoch"],
            ),
        )
        if updated.rowcount != 1:
            raise RuntimeError("Backfill checkpoint changed before the batch could be committed.")
        connection.execute("RELEASE SAVEPOINT market_backfill_batch")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT market_backfill_batch")
        connection.execute("RELEASE SAVEPOINT market_backfill_batch")
        raise

    updated_checkpoint = get_backfill_checkpoint(connection, instrument["symbol"], timeframe)
    return {
        **updated_checkpoint,
        "received_count": len(candles),
        "inserted_count": sum(1 for item in results if item["inserted"]),
        "duplicate_count": sum(1 for item in results if not item["inserted"]),
        "complete": status == "COMPLETE",
        "read_only": True,
        "execution_triggered": False,
        "signals_generated": False,
    }


async def run_backfill_batch_with_connection_factory(
    connection_factory, client, symbol, timeframe, *, batch_size=500, now_epoch=None
):
    """Run one batch while holding DB connections only for short local work.

    connection_factory must return a fresh SQLite connection. The first
    connection reads/prepares the cursor and is closed before provider I/O;
    a second connection atomically persists observations and cursor progress.
    """
    connection = connection_factory()
    try:
        request = prepare_backfill_batch(
            connection, symbol, timeframe,
            batch_size=batch_size, now_epoch=now_epoch,
        )
        connection.commit()
    finally:
        connection.close()

    if request.get("complete"):
        connection = connection_factory()
        try:
            checkpoint = get_backfill_checkpoint(
                connection, request["symbol"], request["timeframe"]
            )
            return {
                **checkpoint, "received_count": 0,
                "inserted_count": 0, "complete": True,
            }
        finally:
            connection.close()

    candles = await client.collect_candle_range(
        request["symbol"], request["timeframe"],
        request["start_epoch"], request["end_epoch"],
        max_candles=request["batch_size"],
    )
    connection = connection_factory()
    try:
        result = persist_backfill_batch(connection, request, candles)
        connection.commit()
        return result
    finally:
        connection.close()


async def run_backfill_batch(
    connection, client, symbol, timeframe, *, batch_size=500, now_epoch=None
):
    """Compatibility helper; workers should use prepare/fetch/persist separately."""
    request = prepare_backfill_batch(
        connection, symbol, timeframe, batch_size=batch_size, now_epoch=now_epoch
    )
    if request.get("complete"):
        checkpoint = get_backfill_checkpoint(connection, request["symbol"], timeframe)
        return {**checkpoint, "received_count": 0, "inserted_count": 0, "complete": True}
    candles = await client.collect_candle_range(
        request["symbol"], timeframe,
        request["start_epoch"], request["end_epoch"],
        max_candles=request["batch_size"],
    )
    return persist_backfill_batch(connection, request, candles)
