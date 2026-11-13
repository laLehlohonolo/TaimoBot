

import math


MAX_RISK_PERCENT = 1.0


class RiskError(ValueError):
    """Raised when a trade fails risk validation."""


def validate_positive_number(value, field_name):
    """Ensure a value is a finite positive number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RiskError(f"{field_name} must be a valid number.")

    if not math.isfinite(number) or number <= 0:
        raise RiskError(f"{field_name} must be greater than zero.")

    return number


def calculate_position_size(
    account_balance,
    entry_price,
    stop_loss,
    risk_percent,
    side
):
    """
    Calculate paper-trading position size.

    Quantity represents abstract paper units.
    Broker contract specifications and lot sizes
    must be verified before any broker integration.
    """

    balance = validate_positive_number(
        account_balance, "Account balance"
    )
    entry = validate_positive_number(entry_price, "Entry price")
    stop = validate_positive_number(stop_loss, "Stop loss")
    risk = validate_positive_number(risk_percent, "Risk percentage")

    side = str(side).upper().strip()

    if side not in ("BUY", "SELL"):
        raise RiskError("Side must be BUY or SELL.")

    if risk > MAX_RISK_PERCENT:
        raise RiskError(
            "Trade rejected: maximum permitted risk is 1%."
        )

    if side == "BUY" and stop >= entry:
        raise RiskError(
            "A BUY trade requires stop loss below entry price."
        )

    if side == "SELL" and stop <= entry:
        raise RiskError(
            "A SELL trade requires stop loss above entry price."
        )

    risk_amount = balance * (risk / 100)
    price_distance = abs(entry - stop)

    quantity = risk_amount / price_distance

    return {
        "approved": True,
        "side": side,
        "account_balance": round(balance, 2),
        "risk_percent": risk,
        "risk_amount": round(risk_amount, 2),
        "entry_price": entry,
        "stop_loss": stop,
        "price_distance": price_distance,
        "position_size": round(quantity, 8),
        "maximum_risk_percent": MAX_RISK_PERCENT,
        "mode": "paper_trading"
    }
