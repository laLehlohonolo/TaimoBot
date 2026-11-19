"""Shared paper-trade close accounting primitives.

Costs are expressed in the account currency and are supplied per position.
This module has no database or broker side effects.
"""
import math


COST_FIELDS = (
    "spread_cost",
    "commission_cost",
    "swap_cost",
    "other_costs",
)


def _finite_non_negative(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite non-negative number.")
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number.")
    return number


def calculate_close_accounting(gross_pnl, *, spread_cost=0,
                               commission_cost=0, swap_cost=0,
                               other_costs=0):
    """Return unrounded gross, costs and net values for durable ledger writes."""
    gross = _finite_non_negative(abs(gross_pnl), "Gross P&L") * (
        -1 if float(gross_pnl) < 0 else 1
    )
    costs = {
        "spread_cost": _finite_non_negative(spread_cost, "Spread cost"),
        "commission_cost": _finite_non_negative(commission_cost, "Commission cost"),
        "swap_cost": _finite_non_negative(swap_cost, "Swap cost"),
        "other_costs": _finite_non_negative(other_costs, "Other costs"),
    }
    total = sum(costs.values())
    return {
        "gross_realized_pnl": gross,
        **costs,
        "total_costs": total,
        "net_realized_pnl": gross - total,
    }
