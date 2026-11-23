
import uuid

from .database import get_connection
from .execution import (
    open_paper_trade, transaction, get_open_risk,
    _calculate_trade_pnl
)
from .risk import get_risk_profile, MAX_RISK_PERCENT, validate_positive_number
from .close_accounting import calculate_close_accounting



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
                    lease_token = NULL,
                    lease_expires_at = NULL,
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
                    lease_token = NULL,
                    lease_expires_at = NULL,
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
                    lease_token = NULL,
                    lease_expires_at = NULL,
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
                lease_token = NULL,
                lease_expires_at = NULL,
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
            # Persist recovery before raising: the connection context rolls
            # back on exceptions, even when recovery itself succeeded.
            connection.commit()
            raise ValueError("Only open source trades can be copied.")

        relationships = connection.execute(
            """
            SELECT cr.relationship_id, cr.follower_account_id, cr.status
            FROM copy_relationships AS cr
            WHERE cr.source_account_id = ?
              AND (
                    cr.status = 'ACTIVE'
                    OR EXISTS (
                        SELECT 1
                        FROM copy_executions AS ce
                        WHERE ce.relationship_id = cr.relationship_id
                          AND ce.source_trade_id = ?
                          AND ce.status = 'PENDING'
                    )
              )
            ORDER BY cr.created_at, cr.relationship_id
            """,
            (source["account_id"], source_trade_id)
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
    execution_token = str(uuid.uuid4())

    # Claim a five-minute lease with a unique owner token. Every write that
    # creates or finalizes a copy must prove it still owns this lease.
    with transaction() as connection:
        current = connection.execute(
            """
            SELECT
                ce.status,
                ce.copied_trade_id,
                ce.failure_reason,
                ce.lease_token,
                ce.lease_expires_at,
                cr.status AS relationship_status
            FROM copy_executions AS ce
            JOIN copy_relationships AS cr
              ON cr.relationship_id = ce.relationship_id
            WHERE ce.relationship_id = ? AND ce.source_trade_id = ?
            """,
            (relationship_id, source_trade_id)
        ).fetchone()

        if current is not None:
            stored_status = current["status"]

            if stored_status == "PENDING":
                # A different worker may have committed the follower trade but
                # not yet finalized the execution row. Do not recover/finalize
                # that trade while its lease is still live: the lease owner is
                # responsible for the outcome. Recovery is safe only after the
                # lease expires (or when no lease owner remains).
                if current["relationship_status"] != "ACTIVE":
                    reason = (
                        "Copy relationship is "
                        + current["relationship_status"]
                        + "; pending copy was not opened."
                    )
                    connection.execute(
                        """
                        UPDATE copy_executions
                        SET status = 'FAILED',
                            failure_reason = ?,
                            lease_token = NULL,
                            lease_expires_at = NULL,
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
                        "duplicate_prevented": True
                    }

                live_foreign_lease = (
                    current["lease_token"]
                    and current["lease_token"] != execution_token
                    and current["lease_expires_at"]
                    and connection.execute(
                        """
                        SELECT 1
                        WHERE datetime(?) > CURRENT_TIMESTAMP
                        """,
                        (current["lease_expires_at"],)
                    ).fetchone()
                )
                if live_foreign_lease:
                    return {
                        "relationship_id": relationship_id,
                        "follower_account_id": follower_id,
                        "status": "PENDING",
                        "copied_trade_id": None,
                        "failure_reason": current["failure_reason"],
                        "duplicate_prevented": True,
                        "recovery_deferred": True
                    }

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
                            lease_token = NULL,
                            lease_expires_at = NULL,
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

                # A live lease is not stealable just because a process restarted.
                # Only an expired lease can be reclaimed by a new token owner.
                if current["lease_expires_at"] and connection.execute(
                    """
                    SELECT 1
                    WHERE datetime(?) > CURRENT_TIMESTAMP
                    """,
                    (current["lease_expires_at"],)
                ).fetchone():
                    return {
                        "relationship_id": relationship_id,
                        "follower_account_id": follower_id,
                        "status": "PENDING",
                        "copied_trade_id": None,
                        "failure_reason": current["failure_reason"],
                        "duplicate_prevented": True,
                        "recovery_deferred": True
                    }

                claimed = connection.execute(
                    """
                    UPDATE copy_executions
                    SET lease_token = ?,
                        lease_expires_at = datetime('now', '+5 minutes'),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE relationship_id = ?
                      AND source_trade_id = ?
                      AND status = 'PENDING'
                      AND (
                          lease_expires_at IS NULL
                          OR datetime(lease_expires_at) <= CURRENT_TIMESTAMP
                      )
                    """,
                    (execution_token, relationship_id, source_trade_id)
                )
                if claimed.rowcount != 1:
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
            connection.execute(
                """
                INSERT INTO copy_executions (
                    copy_execution_id, relationship_id,
                    source_trade_id, status, lease_token, lease_expires_at
                )
                VALUES (?, ?, ?, 'PENDING', ?, datetime('now', '+5 minutes'))
                """,
                (
                    str(uuid.uuid4()),
                    relationship_id,
                    source_trade_id,
                    execution_token
                )
            )

    try:
        with get_connection() as connection:
            currency_rows = connection.execute(
                """
                SELECT account_id, settlement_currency
                FROM paper_accounts
                WHERE account_id IN (?, ?)
                """,
                (source["account_id"], follower_id)
            ).fetchall()

            currencies = {
                row["account_id"]: row["settlement_currency"]
                for row in currency_rows
            }
            source_currency = currencies.get(source["account_id"])
            follower_currency = currencies.get(follower_id)

            if not source_currency or not follower_currency:
                raise ValueError(
                    "Copy rejected: source and follower settlement currencies "
                    "must both be verified."
                )

            if source_currency != follower_currency:
                raise ValueError(
                    "Cross-currency copy is disabled until a fresh, independently "
                    "validated FX quote is available."
                )

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
                copy_relationship_id=relationship_id,
                copy_execution_token=execution_token
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
            finalized = connection.execute(
                """
                UPDATE copy_executions
                SET copied_trade_id = ?,
                    status = ?,
                    failure_reason = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE relationship_id = ?
                  AND source_trade_id = ?
                  AND status = 'PENDING'
                  AND lease_token = ?
                """,
                (
                    copied_trade_id,
                    final_status,
                    relationship_id,
                    source_trade_id,
                    execution_token
                )
            )
            if finalized.rowcount != 1:
                persisted = connection.execute(
                    """
                    SELECT status, copied_trade_id, failure_reason
                    FROM copy_executions
                    WHERE relationship_id = ?
                      AND source_trade_id = ?
                    """,
                    (relationship_id, source_trade_id)
                ).fetchone()
                persisted_status = (
                    persisted["status"] if persisted else "PENDING"
                )
                return {
                    "relationship_id": relationship_id,
                    "follower_account_id": follower_id,
                    "status": persisted_status,
                    "copied_trade_id": (
                        persisted["copied_trade_id"]
                        if persisted else copied_trade_id
                    ),
                    "failure_reason": (
                        persisted["failure_reason"] if persisted else None
                    ),
                    "duplicate_prevented": True,
                    "recovery_deferred": persisted_status == "PENDING"
                }

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
            failed = connection.execute(
                """
                UPDATE copy_executions
                SET status = 'FAILED',
                    failure_reason = ?,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE relationship_id = ?
                  AND source_trade_id = ?
                  AND status = 'PENDING'
                  AND lease_token = ?
                """,
                (reason, relationship_id, source_trade_id, execution_token)
            )

        if failed.rowcount != 1:
            with get_connection() as connection:
                persisted = connection.execute(
                    """
                    SELECT status, copied_trade_id, failure_reason
                    FROM copy_executions
                    WHERE relationship_id = ?
                      AND source_trade_id = ?
                    """,
                    (relationship_id, source_trade_id)
                ).fetchone()
            persisted_status = (
                persisted["status"] if persisted else "PENDING"
            )
            return {
                "relationship_id": relationship_id,
                "follower_account_id": follower_id,
                "status": persisted_status,
                "copied_trade_id": (
                    persisted["copied_trade_id"] if persisted else None
                ),
                "failure_reason": (
                    persisted["failure_reason"] if persisted
                    else "Copy lease ownership changed; recovery is deferred."
                ),
                "duplicate_prevented": True,
                "recovery_deferred": persisted_status == "PENDING"
            }

        return {
            "relationship_id": relationship_id,
            "follower_account_id": follower_id,
            "status": "FAILED",
            "copied_trade_id": None,
            "failure_reason": reason,
            "duplicate_prevented": False
        }


def _reconcile_pending_copy_executions_for_closed_source(connection, source_trade_id):
    """Resolve pending copy records once their source can no longer be copied."""
    pending = connection.execute(
        """
        SELECT relationship_id, source_trade_id
        FROM copy_executions
        WHERE source_trade_id = ?
          AND status = 'PENDING'
        """,
        (source_trade_id,)
    ).fetchall()

    for execution in pending:
        follower = connection.execute(
            """
            SELECT trade_id, status
            FROM trades
            WHERE copied_from_trade_id = ?
              AND account_id = (
                  SELECT follower_account_id
                  FROM copy_relationships
                  WHERE relationship_id = ?
              )
            ORDER BY opened_at DESC, trade_id
            LIMIT 1
            """,
            (source_trade_id, execution["relationship_id"])
        ).fetchone()

        if follower is not None and follower["status"] == "CLOSED":
            status = "CLOSED"
            reason = None
            copied_trade_id = follower["trade_id"]
        else:
            status = "FAILED"
            reason = "Source trade closed: copy rejected because source trade must remain open before the follower copy is committed."
            copied_trade_id = follower["trade_id"] if follower else None

        connection.execute(
            """
            UPDATE copy_executions
            SET status = ?,
                copied_trade_id = COALESCE(copied_trade_id, ?),
                failure_reason = ?,
                lease_token = NULL,
                lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE relationship_id = ?
              AND source_trade_id = ?
              AND status = 'PENDING'
            """,
            (
                status,
                copied_trade_id,
                reason,
                execution["relationship_id"],
                source_trade_id
            )
        )


def close_trade_with_copies(
    trade_id, exit_price, *, spread_cost=0, commission_cost=0,
    swap_cost=0, other_costs=0
):
    """
    Close an original source trade and every open direct copy atomically.

    A copied trade closed on its own affects only its follower account.
    Existing copies are closed even if their relationship is paused/stopped.
    """
    trade_id = str(trade_id).strip()
    if not trade_id:
        raise ValueError("Trade ID is required.")

    exit_value = validate_positive_number(
        exit_price, "Exit price"
    )

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
            gross_pnl = _calculate_trade_pnl(trade, exit_value)
            # Costs supplied for this close belong only to the requested
            # position. Do not silently duplicate source costs onto copies
            # held in separate follower accounts.
            costs = (
                {
                    "spread_cost": spread_cost,
                    "commission_cost": commission_cost,
                    "swap_cost": swap_cost,
                    "other_costs": other_costs,
                }
                if trade["trade_id"] == trade_id
                else {}
            )
            accounting = calculate_close_accounting(gross_pnl, **costs)
            pnl = accounting["net_realized_pnl"]
            account_id = trade["account_id"]
            account_pnl[account_id] = account_pnl.get(account_id, 0.0) + pnl
            trade_results.append({
                "trade_id": trade["trade_id"],
                "account_id": account_id,
                "is_copy": trade["copied_from_trade_id"] is not None,
                "realized_pnl": pnl,
                **accounting,
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
                    gross_realized_pnl = ?,
                    spread_cost = ?,
                    commission_cost = ?,
                    swap_cost = ?,
                    other_costs = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE trade_id = ?
                  AND status = 'OPEN'
                """,
                (
                    result["realized_pnl"],
                    result["gross_realized_pnl"],
                    result["spread_cost"],
                    result["commission_cost"],
                    result["swap_cost"],
                    result["other_costs"],
                    result["trade_id"]
                )
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
                SET status = 'CLOSED',
                    failure_reason = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_trade_id = ?
                  AND status = 'COPIED'
                """,
                (trade_id,)
            )
            _reconcile_pending_copy_executions_for_closed_source(
                connection, trade_id
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

def close_position_group_with_copies(
    group_id, exit_price, *, spread_cost=0, commission_cost=0,
    swap_cost=0, other_costs=0
):
    """Atomically close a source group and copies, allocating costs to sources."""
    group_id = str(group_id).strip()

    if not group_id:
        raise ValueError("Group ID is required.")

    exit_value = validate_positive_number(
        exit_price, "Exit price"
    )

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

        # Validate the complete cost input before doing any writes.
        requested_costs = calculate_close_accounting(
            0,
            spread_cost=spread_cost,
            commission_cost=commission_cost,
            swap_cost=swap_cost,
            other_costs=other_costs,
        )
        cost_fields = (
            "spread_cost", "commission_cost", "swap_cost", "other_costs"
        )
        total_quantity = sum(float(trade["quantity"]) for trade in source_trades)
        if not (total_quantity > 0 and total_quantity < float("inf")):
            raise ValueError("Group quantity must be finite and positive.")

        allocated = {trade["trade_id"]: {} for trade in source_trades}
        for field in cost_fields:
            remaining_cost = requested_costs[field]
            for index, trade in enumerate(source_trades):
                if index == len(source_trades) - 1:
                    share = remaining_cost
                else:
                    share = (
                        requested_costs[field]
                        * float(trade["quantity"]) / total_quantity
                    )
                    remaining_cost -= share
                allocated[trade["trade_id"]][field] = share

        trades_to_close = [*source_trades, *copies]
        account_pnl = {}
        results = []

        for trade in trades_to_close:
            gross_pnl = _calculate_trade_pnl(trade, exit_value)
            trade_costs = (
                allocated[trade["trade_id"]]
                if trade["trade_id"] in allocated
                else {}
            )
            accounting = calculate_close_accounting(
                gross_pnl, **trade_costs
            )
            pnl = accounting["net_realized_pnl"]
            account_id = trade["account_id"]

            account_pnl[account_id] = (
                account_pnl.get(account_id, 0.0) + pnl
            )

            results.append({
                "trade_id": trade["trade_id"],
                "account_id": account_id,
                "source_trade_id": trade["copied_from_trade_id"],
                "is_copy": trade["copied_from_trade_id"] is not None,
                "realized_pnl": pnl,
                **accounting
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
                    gross_realized_pnl = ?,
                    spread_cost = ?,
                    commission_cost = ?,
                    swap_cost = ?,
                    other_costs = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE trade_id = ? AND status = 'OPEN'
                """,
                (
                    item["realized_pnl"],
                    item["gross_realized_pnl"],
                    item["spread_cost"],
                    item["commission_cost"],
                    item["swap_cost"],
                    item["other_costs"],
                    item["trade_id"]
                )
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
                    failure_reason = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE source_trade_id = ?
                  AND status = 'COPIED'
                """,
                (source_id,)
            )
            _reconcile_pending_copy_executions_for_closed_source(
                connection, source_id
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

