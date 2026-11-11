
from .database import get_connection


def get_account_history(account_id):
    """Retrieve paper account balance and trading statistics."""

    with get_connection() as connection:
        account = connection.execute(
            """
            SELECT account_id, starting_balance, balance, created_at
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        statistics = connection.execute(
            """
            SELECT
                COUNT(*) AS total_trades,
                SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)
                    AS open_trades,
                SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END)
                    AS closed_trades,
                COALESCE(SUM(realized_pnl), 0) AS realized_pnl
            FROM trades
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

    return {
        "account_id": account["account_id"],
        "starting_balance": account["starting_balance"],
        "current_balance": account["balance"],
        "total_trades": statistics["total_trades"],
        "open_trades": statistics["open_trades"],
        "closed_trades": statistics["closed_trades"],
        "realized_pnl": round(statistics["realized_pnl"], 2),
        "mode": "paper_trading"
    }


def get_trade_history(account_id, limit=50):
    """Retrieve recent trades for a paper account."""

    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100.")

    with get_connection() as connection:
        account = connection.execute(
            """
            SELECT account_id FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if account is None:
            raise ValueError("Paper account not found.")

        trades = connection.execute(
            """
            SELECT
                trade_id,
                client_order_id,
                symbol,
                side,
                quantity,
                entry_price,
                stop_loss,
                take_profit,
                status,
                risk_amount,
                realized_pnl,
                opened_at,
                closed_at
            FROM trades
            WHERE account_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (account_id, limit)
        ).fetchall()

    return {
        "account_id": account_id,
        "count": len(trades),
        "trades": [dict(trade) for trade in trades],
        "mode": "paper_trading"
    }
