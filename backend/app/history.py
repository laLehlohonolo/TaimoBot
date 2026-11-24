
from .database import get_connection


def get_account_history(account_id):
    """Retrieve paper account statistics with explicit P&L cost confidence."""

    with get_connection() as connection:
        account = connection.execute(
            """
            SELECT account_id, starting_balance, balance, settlement_currency, created_at
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
                COALESCE(SUM(CASE WHEN status = 'CLOSED'
                    THEN realized_pnl ELSE 0 END), 0) AS realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'CLOSED'
                    THEN gross_realized_pnl ELSE 0 END), 0) AS gross_realized_pnl,
                COALESCE(SUM(CASE WHEN status = 'CLOSED'
                    THEN spread_cost + commission_cost + swap_cost + other_costs
                    ELSE 0 END), 0) AS recorded_costs_total,
                SUM(CASE WHEN status = 'CLOSED'
                    AND cost_provenance_status = 'ACTUAL'
                    AND gross_realized_pnl IS NOT NULL THEN 1 ELSE 0 END)
                    AS actual_cost_trades,
                SUM(CASE WHEN status = 'CLOSED'
                    AND cost_provenance_status = 'UNKNOWN' THEN 1 ELSE 0 END)
                    AS unknown_cost_trades,
                SUM(CASE WHEN status = 'CLOSED'
                    AND cost_provenance_status = 'MANUAL_UNVERIFIED' THEN 1 ELSE 0 END)
                    AS manual_unverified_cost_trades,
                SUM(CASE WHEN status = 'CLOSED'
                    AND cost_provenance_status = 'ESTIMATED' THEN 1 ELSE 0 END)
                    AS estimated_cost_trades
            FROM trades
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

    closed_trades = statistics["closed_trades"] or 0
    actual_cost_trades = statistics["actual_cost_trades"] or 0
    net_verified = closed_trades > 0 and actual_cost_trades == closed_trades

    return {
        "account_id": account["account_id"],
        "starting_balance": account["starting_balance"],
        "current_balance": account["balance"],
        "settlement_currency": account["settlement_currency"],
        "total_trades": statistics["total_trades"],
        "open_trades": statistics["open_trades"],
        "closed_trades": closed_trades,
        # Retained for compatibility; this ledger field is not necessarily net P&L.
        "realized_pnl": round(statistics["realized_pnl"], 2),
        "gross_realized_pnl": round(statistics["gross_realized_pnl"], 2),
        "recorded_costs_total": round(statistics["recorded_costs_total"], 2),
        "net_realized_pnl": (
            round(statistics["realized_pnl"], 2) if net_verified else None
        ),
        "net_pnl_status": (
            "VERIFIED_ACTUAL_COSTS" if net_verified
            else "NO_CLOSED_TRADES" if closed_trades == 0
            else "UNVERIFIED_COSTS_PRESENT"
        ),
        "cost_provenance_counts": {
            "ACTUAL": actual_cost_trades,
            "UNKNOWN": statistics["unknown_cost_trades"] or 0,
            "MANUAL_UNVERIFIED": statistics["manual_unverified_cost_trades"] or 0,
            "ESTIMATED": statistics["estimated_cost_trades"] or 0,
        },
        "mode": "paper_trading"
    }


def get_trade_history(account_id, limit=50):
    """Retrieve recent trades with gross P&L and cost provenance."""

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
                position_group_id,
                copied_from_trade_id,
                realized_pnl,
                gross_realized_pnl,
                spread_cost,
                commission_cost,
                swap_cost,
                other_costs,
                cost_provenance_status,
                CASE WHEN status = 'CLOSED'
                    AND cost_provenance_status = 'ACTUAL'
                    AND gross_realized_pnl IS NOT NULL
                    THEN realized_pnl ELSE NULL END AS net_realized_pnl,
                CASE WHEN status != 'CLOSED' THEN 'NOT_CLOSED'
                    WHEN cost_provenance_status = 'ACTUAL'
                        AND gross_realized_pnl IS NOT NULL
                        THEN 'VERIFIED_ACTUAL_COSTS'
                    ELSE 'UNVERIFIED_COSTS' END AS net_pnl_status,
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
