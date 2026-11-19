"""Cost-aware adaptive profit evaluation for paper-trading positions.

This module evaluates exits only. It does not fetch market prices, place orders,
or close positions; callers must supply a fresh executable-side price and
cost estimates in the account's currency.
"""
import math


_COST_FIELDS = (
    "spread_cost",
    "commission_cost",
    "swap_cost",
    "other_costs",
)


def _finite_number(value, name, *, allow_zero=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a valid number.")

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a valid number.")

    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite.")

    if number < 0 or (number == 0 and not allow_zero):
        qualifier = "zero or greater" if allow_zero else "greater than zero"
        raise ValueError(f"{name} must be {qualifier}.")

    return number


def evaluate_position_profit(
    position,
    current_price,
    *,
    spread_cost=0,
    commission_cost=0,
    swap_cost=0,
    other_costs=0,
    minimum_net_profit=0,
):
    """Evaluate one position's unrealized P&L after all supplied costs.

    A positive net result can qualify even when it is small. There is no
    hard-coded dollar profit target; the optional minimum is caller supplied
    in the account's currency. BUY positions are marked at the supplied
    executable exit price, as are SELL positions.
    """
    if not isinstance(position, dict):
        raise ValueError("Position must be a mapping.")

    side = str(position.get("side", "")).strip().upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL.")

    entry = _finite_number(position.get("entry_price"), "Entry price")
    quantity = _finite_number(position.get("quantity"), "Quantity")
    mark = _finite_number(current_price, "Current price")

    costs = {
        "spread_cost": _finite_number(spread_cost, "Spread cost", allow_zero=True),
        "commission_cost": _finite_number(commission_cost, "Commission cost", allow_zero=True),
        "swap_cost": _finite_number(swap_cost, "Swap cost", allow_zero=True),
        "other_costs": _finite_number(other_costs, "Other costs", allow_zero=True),
    }
    minimum = _finite_number(
        minimum_net_profit, "Minimum net profit", allow_zero=True
    )

    price_move = mark - entry if side == "BUY" else entry - mark
    gross = price_move * quantity
    total_costs = sum(costs.values())
    net = gross - total_costs

    return {
        "side": side,
        "entry_price": entry,
        "current_price": mark,
        "quantity": quantity,
        "gross_pnl": round(gross, 8),
        **{name: round(value, 8) for name, value in costs.items()},
        "total_costs": round(total_costs, 8),
        "net_pnl": round(net, 8),
        "minimum_net_profit": round(minimum, 8),
        "should_take_profit": net > 0 and net >= minimum,
        "mode": "paper_trading",
    }


def evaluate_group_profit(
    positions,
    *,
    minimum_net_profit=0,
):
    """Evaluate a group by aggregate net P&L, while retaining leg details.

    Each position is a mapping with side, entry_price, quantity, current_price,
    and optional spread_cost, commission_cost, swap_cost, and other_costs.
    Individual legs may be negative while the group as a whole is profitable.
    """
    if not isinstance(positions, (list, tuple)) or not positions:
        raise ValueError("At least one position is required.")

    minimum = _finite_number(
        minimum_net_profit, "Minimum net profit", allow_zero=True
    )
    results = []
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise ValueError(f"Position {index} must be a mapping.")
        results.append(
            evaluate_position_profit(
                position,
                position.get("current_price"),
                spread_cost=position.get("spread_cost", 0),
                commission_cost=position.get("commission_cost", 0),
                swap_cost=position.get("swap_cost", 0),
                other_costs=position.get("other_costs", 0),
            )
        )

    gross = sum(item["gross_pnl"] for item in results)
    total_costs = sum(item["total_costs"] for item in results)
    net = gross - total_costs

    return {
        "positions": results,
        "position_count": len(results),
        "gross_pnl": round(gross, 8),
        "total_costs": round(total_costs, 8),
        "net_pnl": round(net, 8),
        "minimum_net_profit": round(minimum, 8),
        "should_take_profit": net > 0 and net >= minimum,
        "mode": "paper_trading",
    }
