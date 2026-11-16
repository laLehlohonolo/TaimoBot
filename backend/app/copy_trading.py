
import uuid

from .database import get_connection
from .execution import (
    open_paper_trade, transaction, get_open_risk,
    _calculate_trade_pnl
)
from .risk import get_risk_profile, MAX_RISK_PERCENT



def _reconcile_pending_for_closed_source(source, connection):
    """
    Finalize interrupted copy records after the source has already closed.

    Source closure and linked-copy closure are atomic. Therefore, a committed
    follower trade must already be CLOSED before this recovery runs.
    """
    pending = connection.execute(
        """
        SELECT ce.relationship_id, cr.follower_account_id
        FROM copy_executions ce
        JOIN copy_relationships cr
          ON cr.relationship_id = ce.relationship_id
        WHERE ce.source_trade_id = ?
          AND ce.status = 'PENDING'
        """,
        (source["trade_id"],)
    ).fetchall()

    for item in pending:
        order_id = f"COPY-{item['relationship_id']}-{source['trade_id']}"
        copied = connection.execute(
            """
            SELECT trade_id, account_id, copied_from_trade_id, status
            FROM trades
            WHERE client_order_id = ?
            """,
            (order_id,)
        ).fetchone()

        if copied is None:
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
                (
                    "Source trade closed before the follower copy was committed.",
                    item["relationship_id"],
                    source["trade_id"]
                )
            )
            continue

        if (
            copied["account_id"] != item["follower_account_id"]
            or copied["copied_from_trade_id"] not in (None, source["trade_id"])
        ):
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
                (
                    "Closed-source recovery stopped: follower trade ownership or source link mismatch.",
                    item["relationship_id"],
                    source["trade_id"]
                )
            )
            continue

        if copied["status"] != "CLOSED":
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
                (
                    "Source is closed but the follower position is not; manual reconciliation required.",
                    item["relationship_id"],
                    source["trade_id"]
                )
            )
            continue

        connection.execute(
            """
            UPDATE trades
            SET copied_from_trade_id = ?
            WHERE trade_id = ?
              AND account_id = ?
              AND copied_from_trade_id IS NULL
            """,
            (source["trade_id"], copied["trade_id"], item["follower_account_id"])
        )
        connection.execute(
            """
            UPDATE copy_executions
            SET copied_trade_id = ?,
                status = 'CLOSED',
                failure_reason = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE relationship_id = ?
              AND source_trade_id = ?
              AND status = 'PENDING'
            """,
            (copied["trade_id"], item["relationship_id"], source["trade_id"])
        )


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
            "SELECT * FROM trades WHERE trade_id = ?",
            (source_trade_id,)
        ).fetchone()

        if source is None:
            raise ValueError("Source trade not found.")

        if source["copied_from_trade_id"] is not None:
            return {
                "source_trade_id": source_trade_id,
                "status": "SKIPPED",
                "reason": "Copied trades cannot trigger further copies.",
                "eligible_followers": 0,
                "successful_copies": 0,
                "failed_copies": 0,
                "copies": [],
                "mode": "paper_trading"
            }

        if source["status"] != "OPEN":
            _reconcile_pending_for_closed_source(dict(source), connection)
            raise ValueError("Only open source trades can be copied.")

        relationships = connection.execute(
            """
            SELECT relationship_id, follower_account_id
            FROM copy_relationships
            WHERE source_account_id = ? AND status = 'ACTIVE'
            ORDER BY created_at, relationship_id
            """,
            (source["account_id"],)
        ).fetchall()

    results = [
        _copy_for_relationship(dict(relationship), dict(source))
        for relationship in relationships
    ]

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

    # The unique relationship/source row is the idempotency record.
    with transaction() as connection:
        current = connection.execute(
            """
            SELECT status, copied_trade_id, failure_reason
            FROM copy_executions
            WHERE relationship_id = ? AND source_trade_id = ?
            """,
            (relationship_id, source_trade_id)
        ).fetchone()

        if current is not None:
            stored_status = current["status"]

            if stored_status == "PENDING":
                # Recover a crash that happened after the follower trade
                # committed but before its copy-execution row was finalized.
                existing_trade = connection.execute(
                    """
                    SELECT trade_id, account_id, copied_from_trade_id, status
                    FROM trades
                    WHERE client_order_id = ?
                    """,
                    (order_id,)
                ).fetchone()

                if existing_trade is not None:
                    if (
                        existing_trade["account_id"] != follower_id
                        or existing_trade["copied_from_trade_id"] not in
                            (None, source_trade_id)
                    ):
                        raise ValueError(
                            "Copy recovery stopped: idempotency order belongs "
                            "to a different account or source trade."
                        )

                    connection.execute(
                        """
                        UPDATE trades
                        SET copied_from_trade_id = ?
                        WHERE trade_id = ?
                          AND account_id = ?
                          AND copied_from_trade_id IS NULL
                        """,
                        (source_trade_id, existing_trade["trade_id"], follower_id)
                    )
                    recovered_status = (
                        "CLOSED" if existing_trade["status"] == "CLOSED"
                        else "COPIED"
                    )
                    connection.execute(
                        """
                        UPDATE copy_executions
                        SET copied_trade_id = ?,
                            status = ?,
                            failure_reason = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE relationship_id = ?
                          AND source_trade_id = ?
                          AND status = 'PENDING'
                        """,
                        (
                            existing_trade["trade_id"],
                            recovered_status,
                            relationship_id,
                            source_trade_id
                        )
                    )
                    return {
                        "relationship_id": relationship_id,
                        "follower_account_id": follower_id,
                        "status": recovered_status,
                        "copied_trade_id": existing_trade["trade_id"],
                        "duplicate_prevented": True,
                        "recovered": True
                    }

                # A PENDING row may belong to a live worker. Only reclaim it
                # after five minutes without an update.
                reclaimed = connection.execute(
                    """
                    UPDATE copy_executions
                    SET updated_at = CURRENT_TIMESTAMP
                    WHERE relationship_id = ?
                      AND source_trade_id = ?
                      AND status = 'PENDING'
                      AND datetime(updated_at) <= datetime('now', '-5 minutes')
                    """,
                    (relationship_id, source_trade_id)
                )
                if reclaimed.rowcount != 1:
                    return {
                        "relationship_id": relationship_id,
                        "follower_account_id": follower_id,
                        "status": "PENDING",
                        "copied_trade_id": None,
                        "failure_reason": current["failure_reason"],
                        "duplicate_prevented": True,
                        "recovery_deferred": True
                    }

            else:
                return {
                    "relationship_id": relationship_id,
                    "follower_account_id": follower_id,
                    "status": (
                        "DUPLICATE" if stored_status == "COPIED"
                        else stored_status
                    ),
                    "copied_trade_id": current["copied_trade_id"],
                    "failure_reason": current["failure_reason"],
                    "duplicate_prevented": True
                }

        else:
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
        # Reject before opening if the follower's own combined risk budget
        # cannot accommodate its assigned per-trade allocation.
        with get_connection() as connection:
            account = connection.execute(
                """
                SELECT balance, starting_balance
                FROM paper_accounts
                WHERE account_id = ?
                """,
                (follower_id,)
            ).fetchone()

            if account is None:
                raise ValueError("Follower paper account not found.")

            balance = float(account["balance"])
            profile = get_risk_profile(account["starting_balance"])
            current_open_risk = get_open_risk(connection, follower_id)
            requested_risk = balance * profile["risk_percent"] / 100
            maximum_risk = balance * MAX_RISK_PERCENT / 100

            if current_open_risk + requested_risk > maximum_risk + 1e-9:
                raise ValueError(
                    "Copy rejected: follower's combined open risk "
                    "would exceed the 1% account maximum."
                )

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
                symbol=source["symbol"],
                copy_source_trade_id=source_trade_id,
                copy_relationship_id=relationship_id
            )
            copied_trade_id = copied["trade_id"]

        with transaction() as connection:
            copied_row = connection.execute(
                """
                SELECT status, account_id, copied_from_trade_id
                FROM trades
                WHERE trade_id = ?
                """,
                (copied_trade_id,)
            ).fetchone()
            if (
                copied_row is None
                or copied_row["account_id"] != follower_id
                or copied_row["copied_from_trade_id"] not in (None, source_trade_id)
            ):
                raise ValueError(
                    "Copy finalization stopped: follower trade does not match "
                    "the expected account and source."
                )

            connection.execute(
                """
                UPDATE trades
                SET copied_from_trade_id = ?
                WHERE trade_id = ? AND account_id = ?
                  AND copied_from_trade_id IS NULL
                """,
                (source_trade_id, copied_trade_id, follower_id)
            )
            final_status = (
                "CLOSED" if copied_row["status"] == "CLOSED" else "COPIED"
            )
            connection.execute(
                """
                UPDATE copy_executions
                SET copied_trade_id = ?,
                    status = ?,
                    failure_reason = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE relationship_id = ?
                  AND source_trade_id = ?
                  AND status = 'PENDING'
                """,
                (copied_trade_id, final_status, relationship_id, source_trade_id)
            )

        return {
            "relationship_id": relationship_id,
            "follower_account_id": follower_id,
            "status": final_status,
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



def close_trade_with_copies(trade_id, exit_price):
    """
    Close an original source trade and every open direct copy atomically.

    A copied trade closed on its own affects only its follower account.
    Existing copies are closed even if their relationship is paused/stopped.
    """
    trade_id = str(trade_id).strip()
    if not trade_id:
        raise ValueError("Trade ID is required.")

    try:
        exit_value = float(exit_price)
    except (TypeError, ValueError):
        raise ValueError("Exit price must be a positive number.")

    if not (exit_value > 0 and exit_value < float("inf")):
        raise ValueError("Exit price must be a positive number.")

    with transaction() as connection:
        requested_trade = connection.execute(
            "SELECT * FROM trades WHERE trade_id = ?",
            (trade_id,)
        ).fetchone()

        if requested_trade is None:
            raise ValueError("Trade not found.")

        if requested_trade["status"] != "OPEN":
            raise ValueError("Only open trades can be closed.")

        is_copied_trade = requested_trade["copied_from_trade_id"] is not None

        if is_copied_trade:
            trades_to_close = [requested_trade]
        else:
            copies = connection.execute(
                """
                SELECT * FROM trades
                WHERE copied_from_trade_id = ?
                  AND status = 'OPEN'
                ORDER BY account_id, trade_id
                """,
                (trade_id,)
            ).fetchall()
            trades_to_close = [requested_trade, *copies]

        account_pnl = {}
        trade_results = []

        for trade in trades_to_close:
            pnl = _calculate_trade_pnl(trade, exit_value)
            account_id = trade["account_id"]
            account_pnl[account_id] = account_pnl.get(account_id, 0.0) + pnl
            trade_results.append({
                "trade_id": trade["trade_id"],
                "account_id": account_id,
                "is_copy": trade["copied_from_trade_id"] is not None,
                "realized_pnl": pnl,
                "position_group_id": trade["position_group_id"]
            })

        new_balances = {}
        for account_id, pnl in account_pnl.items():
            account = connection.execute(
                """
                SELECT balance FROM paper_accounts
                WHERE account_id = ?
                """,
                (account_id,)
            ).fetchone()

            if account is None:
                raise ValueError("Paper account not found.")

            new_balance = float(account["balance"]) + pnl
            if new_balance < 0:
                raise ValueError(
                    "Close rejected: result would make an account balance negative. "
                    "No source or copied positions were closed."
                )
            new_balances[account_id] = new_balance

        for result in trade_results:
            update = connection.execute(
                """
                UPDATE trades
                SET status = 'CLOSED',
                    realized_pnl = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE trade_id = ?
                  AND status = 'OPEN'
                """,
                (result["realized_pnl"], result["trade_id"])
            )
            if update.rowcount != 1:
                raise ValueError(
                    "A source or copied trade could not be closed. "
                    "The entire close was rolled back."
                )

        for account_id, new_balance in new_balances.items():
            connection.execute(
                "UPDATE paper_accounts SET balance = ? WHERE account_id = ?",
                (new_balance, account_id)
            )

        if is_copied_trade:
            connection.execute(
                """
                UPDATE copy_executions
                SET status = 'CLOSED', updated_at = CURRENT_TIMESTAMP
                WHERE copied_trade_id = ?
                  AND status = 'COPIED'
                """,
                (trade_id,)
            )
        else:
            connection.execute(
                """
                UPDATE copy_executions
                SET status = 'CLOSED', updated_at = CURRENT_TIMESTAMP
                WHERE source_trade_id = ?
                  AND status = 'COPIED'
                """,
                (trade_id,)
            )

        group_ids = {
            item["position_group_id"]
            for item in trade_results
            if item["position_group_id"]
        }
        for group_id in group_ids:
            remaining = connection.execute(
                """
                SELECT COUNT(*) AS remaining
                FROM trades
                WHERE position_group_id = ? AND status = 'OPEN'
                """,
                (group_id,)
            ).fetchone()["remaining"]
            if remaining == 0:
                connection.execute(
                    """
                    UPDATE position_groups
                    SET status = 'CLOSED', closed_at = CURRENT_TIMESTAMP
                    WHERE group_id = ?
                    """,
                    (group_id,)
                )

        remaining_risk = {
            account_id: round(get_open_risk(connection, account_id), 2)
            for account_id in account_pnl
        }

    source_result = trade_results[0]
    copy_results = [
        {
            "trade_id": item["trade_id"],
            "account_id": item["account_id"],
            "status": "CLOSED",
            "realized_pnl": round(item["realized_pnl"], 2),
            "new_balance": round(new_balances[item["account_id"]], 2),
            "remaining_open_risk": remaining_risk[item["account_id"]]
        }
        for item in trade_results[1:]
    ]

    return {
        "trade_id": trade_id,
        "account_id": requested_trade["account_id"],
        "symbol": requested_trade["symbol"],
        "status": "CLOSED",
        "exit_price": exit_value,
        "realized_pnl": round(source_result["realized_pnl"], 2),
        "new_balance": round(new_balances[requested_trade["account_id"]], 2),
        "remaining_open_risk": remaining_risk[requested_trade["account_id"]],
        "closed_copies": len(copy_results),
        "copy_closures": copy_results,
        "close_scope": "FOLLOWER_ONLY" if is_copied_trade else "SOURCE_AND_OPEN_COPIES",
        "mode": "paper_trading"
    }

def close_position_group_with_copies(group_id, exit_price):
    """Atomically close a source position group and all direct open copies."""
    group_id = str(group_id).strip()

    try:
        exit_value = float(exit_price)
    except (TypeError, ValueError):
        raise ValueError("Exit price must be a positive number.")

    if not group_id or not (0 < exit_value < float("inf")):
        raise ValueError("Group ID and positive exit price are required.")

    with transaction() as connection:
        group = connection.execute(
            "SELECT * FROM position_groups WHERE group_id = ?",
            (group_id,)
        ).fetchone()

        if group is None:
            raise ValueError("Position group not found.")

        if group["status"] != "OPEN":
            raise ValueError("Position group is not open.")

        source_trades = connection.execute(
            """
            SELECT * FROM trades
            WHERE position_group_id = ?
              AND status = 'OPEN'
            ORDER BY trade_id
            """,
            (group_id,)
        ).fetchall()

        if not source_trades:
            raise ValueError("Position group has no open positions.")

        source_ids = [trade["trade_id"] for trade in source_trades]
        placeholders = ",".join("?" for _ in source_ids)

        copies = connection.execute(
            f"""
            SELECT * FROM trades
            WHERE copied_from_trade_id IN ({placeholders})
              AND status = 'OPEN'
            ORDER BY account_id, trade_id
            """,
            source_ids
        ).fetchall()

        trades_to_close = [*source_trades, *copies]
        account_pnl = {}
        results = []

        for trade in trades_to_close:
            pnl = _calculate_trade_pnl(trade, exit_value)
            account_id = trade["account_id"]

            account_pnl[account_id] = (
                account_pnl.get(account_id, 0.0) + pnl
            )

            results.append({
                "trade_id": trade["trade_id"],
                "account_id": account_id,
                "source_trade_id": trade["copied_from_trade_id"],
                "is_copy": trade["copied_from_trade_id"] is not None,
                "realized_pnl": pnl
            })

        new_balances = {}

        for account_id, pnl in account_pnl.items():
            account = connection.execute(
                """
                SELECT balance FROM paper_accounts
                WHERE account_id = ?
                """,
                (account_id,)
            ).fetchone()

            if account is None:
                raise ValueError("Paper account not found.")

            new_balance = float(account["balance"]) + pnl

            if new_balance < 0:
                raise ValueError(
                    "Group close rejected: an account balance "
                    "would become negative. All positions remain open."
                )

            new_balances[account_id] = new_balance

        for item in results:
            update = connection.execute(
                """
                UPDATE trades
                SET status = 'CLOSED',
                    realized_pnl = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE trade_id = ? AND status = 'OPEN'
                """,
                (item["realized_pnl"], item["trade_id"])
            )

            if update.rowcount != 1:
                raise ValueError(
                    "A group or copied position could not be closed. "
                    "The entire operation was rolled back."
                )

        for account_id, balance in new_balances.items():
            connection.execute(
                """
                UPDATE paper_accounts
                SET balance = ?
                WHERE account_id = ?
                """,
                (balance, account_id)
            )

        for source_id in source_ids:
            connection.execute(
                """
                UPDATE copy_executions
                SET status = 'CLOSED',
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_trade_id = ?
                  AND status = 'COPIED'
                """,
                (source_id,)
            )

        connection.execute(
            """
            UPDATE position_groups
            SET status = 'CLOSED',
                closed_at = CURRENT_TIMESTAMP
            WHERE group_id = ?
            """,
            (group_id,)
        )

        remaining_risk = {
            account_id: round(get_open_risk(connection, account_id), 2)
            for account_id in account_pnl
        }

    return {
        "group_id": group_id,
        "account_id": group["account_id"],
        "symbol": group["symbol"],
        "status": "CLOSED",
        "closed_positions": len(source_trades),
        "closed_copies": len(copies),
        "realized_pnl": round(
            sum(item["realized_pnl"] for item in results
                if not item["is_copy"]), 2
        ),
        "new_balance": round(
            new_balances[group["account_id"]], 2
        ),
        "remaining_open_risk": remaining_risk[group["account_id"]],
        "account_results": [
            {
                "account_id": account_id,
                "new_balance": round(balance, 2),
                "remaining_open_risk": remaining_risk[account_id]
            }
            for account_id, balance in new_balances.items()
        ],
        "mode": "paper_trading"
    }

