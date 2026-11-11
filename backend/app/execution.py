
import uuid
import sqlite3
from contextlib import contextmanager

from .database import get_connection
from .risk import calculate_position_size, validate_positive_number


@contextmanager
def transaction():
    """Run database changes as one atomic transaction."""
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


def create_paper_account(account_id, starting_balance):
    account_id = str(account_id).strip()
    balance = validate_positive_number(
        starting_balance, "Starting balance"
    )

    if not account_id:
        raise ValueError("Account ID is required.")

    with transaction() as connection:
        existing = connection.execute(
            "SELECT account_id FROM paper_accounts WHERE account_id = ?",
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


def open_paper_trade(
    account_id,
    client_order_id,
    side,
    entry_price,
    stop_loss,
    risk_percent,
    take_profit=None
):
    account_id = str(account_id).strip()
    client_order_id = str(client_order_id).strip()
    side = str(side).upper().strip()

    if not account_id or not client_order_id:
        raise ValueError("Account ID and order ID are required.")

    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL.")

    entry = validate_positive_number(entry_price, "Entry price")
    stop = validate_positive_number(stop_loss, "Stop loss")

    target = None
    if take_profit is not None:
        target = validate_positive_number(
            take_profit, "Take profit"
        )

        if side == "BUY" and target <= entry:
            raise ValueError("BUY take profit must be above entry.")

        if side == "SELL" and target >= entry:
            raise ValueError("SELL take profit must be below entry.")

    with transaction() as connection:
        duplicate = connection.execute(
            "SELECT trade_id FROM trades WHERE client_order_id = ?",
            (client_order_id,)
        ).fetchone()

        if duplicate:
            raise ValueError("Duplicate order ID. Trade not opened.")

        account = connection.execute(
            """
            SELECT balance FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        risk = calculate_position_size(
            account_balance=account["balance"],
            entry_price=entry,
            stop_loss=stop,
            risk_percent=risk_percent,
            side=side
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
                opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, CURRENT_TIMESTAMP)
            """,
            (
                trade_id,
                account_id,
                client_order_id,
                "XAUUSD",
                side,
                risk["position_size"],
                entry,
                stop,
                target,
                risk["risk_amount"]
            )
        )

    return {
        "trade_id": trade_id,
        "account_id": account_id,
        "client_order_id": client_order_id,
        "symbol": "XAUUSD",
        "side": side,
        "quantity": risk["position_size"],
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": target,
        "risk_amount": risk["risk_amount"],
        "status": "OPEN",
        "mode": "paper_trading"
    }


def close_paper_trade(trade_id, exit_price):
    trade_id = str(trade_id).strip()
    exit_value = validate_positive_number(
        exit_price, "Exit price"
    )

    if not trade_id:
        raise ValueError("Trade ID is required.")

    with transaction() as connection:
        trade = connection.execute(
            """
            SELECT * FROM trades
            WHERE trade_id = ?
            """,
            (trade_id,)
        ).fetchone()

        if trade is None:
            raise ValueError("Trade not found.")

        if trade["status"] != "OPEN":
            raise ValueError("Only open trades can be closed.")

        if trade["side"] == "BUY":
            pnl = (
                exit_value - trade["entry_price"]
            ) * trade["quantity"]
        else:
            pnl = (
                trade["entry_price"] - exit_value
            ) * trade["quantity"]

        account = connection.execute(
            """
            SELECT balance FROM paper_accounts
            WHERE account_id = ?
            """,
            (trade["account_id"],)
        ).fetchone()

        new_balance = account["balance"] + pnl

        if new_balance < 0:
            raise ValueError(
                "Close rejected: result would make balance negative."
            )

        connection.execute(
            """
            UPDATE paper_accounts
            SET balance = ?
            WHERE account_id = ?
            """,
            (new_balance, trade["account_id"])
        )

        connection.execute(
            """
            UPDATE trades
            SET status = 'CLOSED',
                realized_pnl = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE trade_id = ? AND status = 'OPEN'
            """,
            (pnl, trade_id)
        )

    return {
        "trade_id": trade_id,
        "status": "CLOSED",
        "exit_price": exit_value,
        "realized_pnl": round(pnl, 2),
        "new_balance": round(new_balance, 2),
        "mode": "paper_trading"
    }
from .safety import require_trading_enabled
if not account_id or not client_order_id:
    raise ValueError("Account ID and order ID are required.")
        require_trading_enabled()
