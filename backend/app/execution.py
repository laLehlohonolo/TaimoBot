
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
        raise ValueError("Instrument symbol contains invalid characters.")

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


def open_paper_trade(
    account_id,
    client_order_id,
    side,
    entry_price,
    stop_loss,
    risk_percent,
    take_profit=None,
    symbol=DEFAULT_SYMBOL
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
                opened_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'OPEN', ?, CURRENT_TIMESTAMP
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
                risk["risk_amount"]
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
        "risk_amount": risk["risk_amount"],
        "current_open_risk": risk["current_open_risk"],
        "total_open_risk": risk["projected_total_open_risk"],
        "remaining_risk": risk["remaining_risk_after_trade"],
        "maximum_risk_percent": risk["maximum_risk_percent"],
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
            SELECT *
            FROM trades
            WHERE trade_id = ?
            """,
            (trade_id,)
        ).fetchone()

        if trade is None:
            raise ValueError("Trade not found.")

        if trade["status"] != "OPEN":
            raise ValueError(
                "Only open trades can be closed."
            )

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

        remaining_open_risk = get_open_risk(
            connection, trade["account_id"]
        )

    return {
        "trade_id": trade_id,
        "symbol": trade["symbol"],
        "status": "CLOSED",
        "exit_price": exit_value,
        "realized_pnl": round(pnl, 2),
        "new_balance": round(new_balance, 2),
        "remaining_open_risk": round(
            remaining_open_risk, 2
        ),
        "mode": "paper_trading"
    }
