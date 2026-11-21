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


async def run_backfill_batch(
    connection, client, symbol, timeframe, *, batch_size=500, now_epoch=None
):
    """Collect one historical range, then atomically persist candles and its cursor.

    The cursor walks backwards from the latest provider time. Empty ranges before
    epoch zero are not inferred; an empty non-zero range is held for gap review.
    """
    instrument = get_instrument(symbol)
    if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
        raise ValueError("Historical backfill currently supports Deriv synthetic indices only.")
    if timeframe not in TIMEFRAME_GRANULARITY_SECONDS:
        raise ValueError("Unsupported backfill timeframe.")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be an integer between 1 and 500.")
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
        return {**checkpoint, "received_count": 0, "inserted_count": 0, "complete": True}
    if checkpoint["status"] != "RUNNING" or checkpoint["next_end_epoch"] is None:
        raise ValueError("Backfill checkpoint requires operator review before resuming.")

    end_epoch = min(int(checkpoint["next_end_epoch"]), current_epoch)
    start_epoch = max(0, end_epoch - granularity * (batch_size - 1))
    candles = await client.collect_candle_range(
        instrument["symbol"], timeframe, start_epoch, end_epoch, max_candles=batch_size
    )

    connection.execute("SAVEPOINT market_backfill_batch")
    try:
        results = [save_market_observation(connection, item) for item in candles]
        earliest = min((item["timestamp"] for item in candles), default=None)
        latest = max((item["timestamp"] for item in candles), default=None)
        previous_earliest = checkpoint["earliest_observed_at"]
        previous_latest = checkpoint["latest_observed_at"]
        if previous_earliest and earliest:
            earliest = min(previous_earliest, earliest)
        else:
            earliest = previous_earliest or earliest
        if previous_latest and latest:
            latest = max(previous_latest, latest)
        else:
            latest = previous_latest or latest

        if start_epoch == 0:
            # Reaching the Unix epoch is not evidence that the provider has
            # returned its earliest retained history. Never label this complete.
            status = "GAP_REVIEW"
            next_end = end_epoch
            error = "Reached the epoch-zero boundary; provider history coverage requires review."
        elif not candles:
            status = "GAP_REVIEW"
            next_end = end_epoch
            error = "Provider returned no candles for a non-zero historical interval."
        else:
            status = "RUNNING"
            next_end = start_epoch - granularity
            error = None

        connection.execute(
            """
            UPDATE market_backfill_checkpoints
            SET next_end_epoch = ?, status = ?,
                earliest_observed_at = ?, latest_observed_at = ?,
                batches_completed = batches_completed + 1,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE symbol = ? AND timeframe = ?
            """,
            (
                next_end, status, earliest, latest, error,
                instrument["symbol"], timeframe,
            ),
        )
        connection.execute("RELEASE SAVEPOINT market_backfill_batch")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT market_backfill_batch")
        connection.execute("RELEASE SAVEPOINT market_backfill_batch")
        raise

    updated = get_backfill_checkpoint(connection, instrument["symbol"], timeframe)
    return {
        **updated,
        "received_count": len(candles),
        "inserted_count": sum(1 for item in results if item["inserted"]),
        "duplicate_count": sum(1 for item in results if not item["inserted"]),
        "complete": status == "COMPLETE",
        "read_only": True,
        "execution_triggered": False,
        "signals_generated": False,
    }
