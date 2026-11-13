
import re
import uuid
from contextlib import contextmanager

from .database import get_connection
from .risk import (
    calculate_position_size,
    validate_positive_number
)
from .safety import require_trading_enabled


DEFAULT_SYMBOL = "XAUUSD"


@contextmanager
def transaction():
    """Execute database changes atomically."""
    connection = get_connection()

    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def validate_symbol(symbol):
    """Validate and normalize a trading instrument symbol."""
    value = str(symbol).strip().upper()

    if not value:
        raise ValueError("Instrument symbol is required.")

    if len(value) > 30:
        raise ValueError("Instrument symbol is too long.")

    if not re.fullmatch(r"[A-Z0-9._/-]+", value):
        raise ValueError(
            "Instrument symbol contains invalid characters."
        )

    return value


def create_paper_account(account_id, starting_balance):
    account_id = str(account_id).strip()

    balance = validate_positive_number(
        starting_balance, "Starting balance"
    )

    if not account_id:
        raise ValueError("Account ID is required.")

    with transaction() as connection:
        existing = connection.execute(
            """
            SELECT account_id
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if existing:
            raise ValueError("Account already exists.")

        connection.execute(
            """
            INSERT INTO paper_accounts
            (account_id, starting_balance, balance)
            VALUES (?, ?, ?)
            """,
            (account_id, balance, balance)
        )

    return {
        "account_id": account_id,
        "starting_balance": balance,
        "balance": balance,
        "mode": "paper_trading"
    }


def get_open_risk(connection, account_id):
    """Return total reserved risk across all open instruments."""
    result = connection.execute(
        """
        SELECT COALESCE(SUM(risk_amount), 0) AS total_open_risk
        FROM trades
        WHERE account_id = ?
          AND status = 'OPEN'
        """,
        (account_id,)
    ).fetchone()

    return float(result["total_open_risk"])


def create_position_group(account_id, symbol, side):
    """Create a group for related positions in one instrument."""
    account_id = str(account_id).strip()
    symbol = validate_symbol(symbol)
    side = str(side).strip().upper()

    if not account_id:
        raise ValueError("Account ID is required.")

    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL.")

    group_id = str(uuid.uuid4())

    with transaction() as connection:
        account = connection.execute(
            """
            SELECT account_id
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        connection.execute(
            """
            INSERT INTO position_groups (
                group_id,
                account_id,
                symbol,
                side,
                status
            )
            VALUES (?, ?, ?, ?, 'OPEN')
            """,
            (group_id, account_id, symbol, side)
        )

    return {
        "group_id": group_id,
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "status": "OPEN",
        "mode": "paper_trading"
    }


def get_position_group(group_id):
    """Return combined statistics for an open or closed group."""
    group_id = str(group_id).strip()

    if not group_id:
        raise ValueError("Position group ID is required.")

    with get_connection() as connection:
        group = connection.execute(
            """
            SELECT *
            FROM position_groups
            WHERE group_id = ?
            """,
            (group_id,)
        ).fetchone()

        if group is None:
            raise ValueError("Position group not found.")

        statistics = connection.execute(
            """
            SELECT
                COUNT(*) AS total_positions,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)
                    AS open_positions,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END)
                    AS closed_positions,
                COALESCE(
                    SUM(
                        CASE WHEN status = 'OPEN'
                        THEN quantity ELSE 0 END
                    ), 0
                ) AS combined_quantity,
                COALESCE(
                    SUM(
                        CASE WHEN status = 'OPEN'
                        THEN quantity * entry_price ELSE 0 END
                    ), 0
                ) AS weighted_entry_total,
                COALESCE(
                    SUM(
                        CASE WHEN status = 'OPEN'
                        THEN risk_amount ELSE 0 END
                    ), 0
                ) AS combined_open_risk,
                COALESCE(SUM(realized_pnl), 0) AS realized_pnl
            FROM trades
            WHERE position_group_id = ?
            """,
            (group_id,)
        ).fetchone()

    quantity = float(statistics["combined_quantity"])
    weighted_total = float(statistics["weighted_entry_total"])

    average_entry = (
        weighted_total / quantity if quantity > 0 else 0
    )

    return {
        "group_id": group["group_id"],
        "account_id": group["account_id"],
        "symbol": group["symbol"],
        "side": group["side"],
        "status": group["status"],
        "total_positions": statistics["total_positions"],
        "open_positions": statistics["open_positions"],
        "closed_positions": statistics["closed_positions"],
        "combined_quantity": round(quantity, 8),
        "weighted_average_entry": round(average_entry, 8),
        "combined_open_risk": round(
            float(statistics["combined_open_risk"]), 2
        ),
        "realized_pnl": round(
            float(statistics["realized_pnl"]), 2
        ),
        "mode": "paper_trading"
    }


def open_paper_trade(
    account_id,
    client_order_id,
    side,
    entry_price,
    stop_loss,
    risk_percent,
    take_profit=None,
    symbol=DEFAULT_SYMBOL,
    position_group_id=None
):
    account_id = str(account_id).strip()
    client_order_id = str(client_order_id).strip()
    side = str(side).upper().strip()
    symbol = validate_symbol(symbol)

    if not account_id or not client_order_id:
        raise ValueError("Account ID and order ID are required.")

    require_trading_enabled()

    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL.")

    entry = validate_positive_number(
        entry_price, "Entry price"
    )
    stop = validate_positive_number(
        stop_loss, "Stop loss"
    )

    target = None

    if take_profit is not None:
        target = validate_positive_number(
            take_profit, "Take profit"
        )

        if side == "BUY" and target <= entry:
            raise ValueError(
                "BUY take profit must be above entry."
            )

        if side == "SELL" and target >= entry:
            raise ValueError(
                "SELL take profit must be below entry."
            )

    if position_group_id is not None:
        position_group_id = str(position_group_id).strip()

        if not position_group_id:
            raise ValueError("Position group ID cannot be empty.")

    with transaction() as connection:
        duplicate = connection.execute(
            """
            SELECT trade_id
            FROM trades
            WHERE client_order_id = ?
            """,
            (client_order_id,)
        ).fetchone()

        if duplicate:
            raise ValueError(
                "Duplicate order ID. Trade not opened."
            )

        account = connection.execute(
            """
            SELECT balance
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        if position_group_id is not None:
            group = connection.execute(
                """
                SELECT *
                FROM position_groups
                WHERE group_id = ?
                """,
                (position_group_id,)
            ).fetchone()

            if group is None:
                raise ValueError("Position group not found.")

            if group["status"] != "OPEN":
                raise ValueError("Position group is not open.")

            if group["account_id"] != account_id:
                raise ValueError(
                    "Position group belongs to another account."
                )

            if group["symbol"] != symbol:
                raise ValueError(
                    "Position group instrument does not match."
                )

            if group["side"] != side:
                raise ValueError(
                    "Position group direction does not match."
                )

        current_open_risk = get_open_risk(
            connection, account_id
        )

        risk = calculate_position_size(
            account_balance=account["balance"],
            entry_price=entry,
            stop_loss=stop,
            risk_percent=risk_percent,
            side=side,
            current_open_risk=current_open_risk
        )

        trade_id = str(uuid.uuid4())

        connection.execute(
            """
            INSERT INTO trades (
                trade_id,
                account_id,
                client_order_id,
                symbol,
                side,
                quantity,
                entry_price,
                stop_loss,
                take_profit,
                status,
                risk_amount,
                opened_at,
                position_group_id
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'OPEN', ?, CURRENT_TIMESTAMP, ?
            )
            """,
            (
                trade_id,
                account_id,
                client_order_id,
                symbol,
                side,
                risk["position_size"],
                entry,
                stop,
                target,
                risk["risk_amount"],
                position_group_id
            )
        )

    return {
        "trade_id": trade_id,
        "account_id": account_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "quantity": risk["position_size"],
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "position_group_id": position_group_id,
        "risk_amount": risk["risk_amount"],
        "current_open_risk": risk["current_open_risk"],
        "total_open_risk": risk["projected_total_open_risk"],
        "remaining_risk": risk["remaining_risk_after_trade"],
        "maximum_risk_percent": risk["maximum_risk_percent"],
        "status": "OPEN",
        "mode": "paper_trading"
    }


def _calculate_trade_pnl(trade, exit_value):
    if trade["side"] == "BUY":
        return (
            exit_value - trade["entry_price"]
        ) * trade["quantity"]

    return (
        trade["entry_price"] - exit_value
    ) * trade["quantity"]


def close_paper_trade(trade_id, exit_price):
    """Close one position and update its group if it was the last leg."""
    trade_id = str(trade_id).strip()

    exit_value = validate_positive_number(
        exit_price, "Exit price"
    )

    if not trade_id:
        raise ValueError("Trade ID is required.")

    with transaction() as connection:
        trade = connection.execute(
            """
            SELECT *
            FROM trades
            WHERE trade_id = ?
            """,
            (trade_id,)
        ).fetchone()

        if trade is None:
            raise ValueError("Trade not found.")

        if trade["status"] != "OPEN":
            raise ValueError("Only open trades can be closed.")

        pnl = _calculate_trade_pnl(trade, exit_value)

        account = connection.execute(
            """
            SELECT balance
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (trade["account_id"],)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        new_balance = account["balance"] + pnl

        if new_balance < 0:
            raise ValueError(
                "Close rejected: result would make balance negative."
            )

        update_result = connection.execute(
            """
            UPDATE trades
            SET status = 'CLOSED',
                realized_pnl = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE trade_id = ?
              AND status = 'OPEN'
            """,
            (pnl, trade_id)
        )

        if update_result.rowcount != 1:
            raise ValueError(
                "Trade could not be closed. It may already be closed."
            )

        connection.execute(
            """
            UPDATE paper_accounts
            SET balance = ?
            WHERE account_id = ?
            """,
            (new_balance, trade["account_id"])
        )

        if trade["position_group_id"]:
            remaining_group_trades = connection.execute(
                """
                SELECT COUNT(*) AS remaining
                FROM trades
                WHERE position_group_id = ?
                  AND status = 'OPEN'
                """,
                (trade["position_group_id"],)
            ).fetchone()["remaining"]

            if remaining_group_trades == 0:
                connection.execute(
                    """
                    UPDATE position_groups
                    SET status = 'CLOSED',
                        closed_at = CURRENT_TIMESTAMP
                    WHERE group_id = ?
                    """,
                    (trade["position_group_id"],)
                )

        remaining_open_risk = get_open_risk(
            connection, trade["account_id"]
        )

    return {
        "trade_id": trade_id,
        "symbol": trade["symbol"],
        "position_group_id": trade["position_group_id"],
        "status": "CLOSED",
        "exit_price": exit_value,
        "realized_pnl": round(pnl, 2),
        "new_balance": round(new_balance, 2),
        "remaining_open_risk": round(
            remaining_open_risk, 2
        ),
        "mode": "paper_trading"
    }


def close_position_group(group_id, exit_price):
    """Close every open position in a group atomically."""
    group_id = str(group_id).strip()

    exit_value = validate_positive_number(
        exit_price, "Exit price"
    )

    if not group_id:
        raise ValueError("Position group ID is required.")

    with transaction() as connection:
        group = connection.execute(
            """
            SELECT *
            FROM position_groups
            WHERE group_id = ?
            """,
            (group_id,)
        ).fetchone()

        if group is None:
            raise ValueError("Position group not found.")

        if group["status"] != "OPEN":
            raise ValueError("Position group is not open.")

        trades = connection.execute(
            """
            SELECT *
            FROM trades
            WHERE position_group_id = ?
              AND status = 'OPEN'
            """,
            (group_id,)
        ).fetchall()

        if not trades:
            raise ValueError(
                "Position group has no open positions."
            )

        account = connection.execute(
            """
            SELECT balance
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (group["account_id"],)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        total_pnl = sum(
            _calculate_trade_pnl(trade, exit_value)
            for trade in trades
        )

        new_balance = account["balance"] + total_pnl

        if new_balance < 0:
            raise ValueError(
                "Group close rejected: result would make "
                "balance negative."
            )

        closed_ids = []

        for trade in trades:
            pnl = _calculate_trade_pnl(trade, exit_value)

            result = connection.execute(
                """
                UPDATE trades
                SET status = 'CLOSED',
                    realized_pnl = ?,
                    closed_at = CURRENT_TIMESTAMP
                WHERE trade_id = ?
                  AND status = 'OPEN'
                """,
                (pnl, trade["trade_id"])
            )

            if result.rowcount != 1:
                raise ValueError(
                    "A group position could not be closed."
                )

            closed_ids.append(trade["trade_id"])

        connection.execute(
            """
            UPDATE paper_accounts
            SET balance = ?
            WHERE account_id = ?
            """,
            (new_balance, group["account_id"])
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

        remaining_open_risk = get_open_risk(
            connection, group["account_id"]
        )

    return {
        "group_id": group_id,
        "account_id": group["account_id"],
        "symbol": group["symbol"],
        "status": "CLOSED",
        "closed_positions": len(closed_ids),
        "closed_trade_ids": closed_ids,
        "exit_price": exit_value,
        "realized_pnl": round(total_pnl, 2),
        "new_balance": round(new_balance, 2),
        "remaining_open_risk": round(
            remaining_open_risk, 2
        ),
        "mode": "paper_trading"
    }
