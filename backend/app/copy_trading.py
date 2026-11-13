import uuid

from .database import get_connection
from .execution import open_paper_trade, transaction


def replicate_open_trade(source_trade_id):
    """
    Attempt one independently risk-checked paper copy for every ACTIVE
    follower of an original source trade. Copied trades never cascade.
    """
    source_trade_id = str(source_trade_id).strip()
    if not source_trade_id:
        raise ValueError("Source trade ID is required.")

    with get_connection() as connection:
        source = connection.execute(
            """
            SELECT *
            FROM trades
            WHERE trade_id = ?
            """,
            (source_trade_id,)
        ).fetchone()

        if source is None:
            raise ValueError("Source trade not found.")

        if source["copied_from_trade_id"] is not None:
            return {
                "source_trade_id": source_trade_id,
                "status": "SKIPPED",
                "reason": "Copied trades cannot trigger further copies.",
                "copies": [],
                "mode": "paper_trading"
            }

        if source["status"] != "OPEN":
            raise ValueError("Only open source trades can be copied.")

        relationships = connection.execute(
            """
            SELECT relationship_id, follower_account_id
            FROM copy_relationships
            WHERE source_account_id = ?
              AND status = 'ACTIVE'
            ORDER BY created_at, relationship_id
            """,
            (source["account_id"],)
        ).fetchall()

    results = []
    for relationship in relationships:
        results.append(
            _copy_for_relationship(
                dict(relationship),
                dict(source)
            )
        )

    return {
        "source_trade_id": source_trade_id,
        "eligible_followers": len(relationships),
        "successful_copies": sum(
            1 for item in results if item["status"] == "COPIED"
        ),
        "failed_copies": sum(
            1 for item in results if item["status"] == "FAILED"
        ),
        "copies": results,
        "mode": "paper_trading"
    }


def _copy_for_relationship(relationship, source):
    relationship_id = relationship["relationship_id"]
    follower_id = relationship["follower_account_id"]
    source_trade_id = source["trade_id"]
    order_id = f"COPY-{relationship_id}-{source_trade_id}"

    # Reserve the unique relationship/source pair before attempting a copy.
    with transaction() as connection:
        current = connection.execute(
            """
            SELECT status, copied_trade_id, failure_reason
            FROM copy_executions
            WHERE relationship_id = ? AND source_trade_id = ?
            """,
            (relationship_id, source_trade_id)
        ).fetchone()

        if current and current["status"] in ("COPIED", "FAILED", "CLOSED"):
            return {
                "relationship_id": relationship_id,
                "follower_account_id": follower_id,
                "status": current["status"],
                "copied_trade_id": current["copied_trade_id"],
                "failure_reason": current["failure_reason"],
                "duplicate_prevented": True
            }

        if current is None:
            execution_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO copy_executions (
                    copy_execution_id, relationship_id,
                    source_trade_id, status
                )
                VALUES (?, ?, ?, 'PENDING')
                """,
                (execution_id, relationship_id, source_trade_id)
            )

    try:
        # Recover safely if a previous attempt opened the trade but did not
        # finish updating the copy-execution record.
        with get_connection() as connection:
            existing_trade = connection.execute(
                """
                SELECT trade_id
                FROM trades
                WHERE client_order_id = ?
                """,
                (order_id,)
            ).fetchone()

        if existing_trade:
            copied_trade_id = existing_trade["trade_id"]
        else:
            copied = open_paper_trade(
                account_id=follower_id,
                client_order_id=order_id,
                side=source["side"],
                entry_price=source["entry_price"],
                stop_loss=source["stop_loss"],
                take_profit=source["take_profit"],
                symbol=source["symbol"]
            )
            copied_trade_id = copied["trade_id"]

        with transaction() as connection:
            connection.execute(
                """
                UPDATE trades
                SET copied_from_trade_id = ?
                WHERE trade_id = ?
                  AND account_id = ?
                """,
                (source_trade_id, copied_trade_id, follower_id)
            )
            connection.execute(
                """
                UPDATE copy_executions
                SET copied_trade_id = ?,
                    status = 'COPIED',
                    failure_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE relationship_id = ?
                  AND source_trade_id = ?
                  AND status = 'PENDING'
                """,
                (copied_trade_id, relationship_id, source_trade_id)
            )

        return {
            "relationship_id": relationship_id,
            "follower_account_id": follower_id,
            "status": "COPIED",
            "copied_trade_id": copied_trade_id,
            "duplicate_prevented": False
        }

    except Exception as error:
        reason = str(error)[:500] or "Copy attempt failed."
        with transaction() as connection:
            connection.execute(
                """
                UPDATE copy_executions
                SET status = 'FAILED',
                    failure_reason = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE relationship_id = ?
                  AND source_trade_id = ?
                  AND status = 'PENDING'
                """,
                (reason, relationship_id, source_trade_id)
            )

        return {
            "relationship_id": relationship_id,
            "follower_account_id": follower_id,
            "status": "FAILED",
            "copied_trade_id": None,
            "failure_reason": reason,
            "duplicate_prevented": False
        }
