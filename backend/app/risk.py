
import math


MAX_RISK_PERCENT = 1.0

RISK_PROFILES = {
    "STARTER": {
        "minimum_funding": 0,
        "maximum_funding": 4999.99,
        "risk_percent": 0.25
    },
    "STANDARD": {
        "minimum_funding": 5000,
        "maximum_funding": 24999.99,
        "risk_percent": 0.50
    },
    "PREMIUM": {
        "minimum_funding": 25000,
        "maximum_funding": None,
        "risk_percent": 1.00
    }
}


class RiskError(ValueError):
    """Raised when a trade fails risk validation."""


def validate_positive_number(value, field_name):
    """Validate a finite positive number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RiskError(f"{field_name} must be a valid number.")

    if not math.isfinite(number) or number <= 0:
        raise RiskError(f"{field_name} must be greater than zero.")

    return number


def validate_non_negative_number(value, field_name):
    """Validate a finite number that may be zero."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RiskError(f"{field_name} must be a valid number.")

    if not math.isfinite(number) or number < 0:
        raise RiskError(f"{field_name} cannot be negative.")

    return number


def get_risk_profile(starting_balance):
    """
    Assign a participant risk profile using original funding.

    Subsequent trading profits or losses do not change the profile.
    """

    funding = validate_positive_number(
        starting_balance, "Starting balance"
    )

    if funding < 5000:
        profile_name = "STARTER"
    elif funding < 25000:
        profile_name = "STANDARD"
    else:
        profile_name = "PREMIUM"

    profile = RISK_PROFILES[profile_name]

    return {
        "profile": profile_name,
        "starting_balance": round(funding, 2),
        "risk_percent": profile["risk_percent"],
        "maximum_combined_risk_percent": MAX_RISK_PERCENT
    }


def calculate_position_size(
    account_balance,
    entry_price,
    stop_loss,
    risk_percent,
    side,
    current_open_risk=0
):
    """
    Calculate paper position size under the combined account risk limit.

    Quantity represents abstract paper units, not verified broker lots.
    """

    balance = validate_positive_number(
        account_balance, "Account balance"
    )
    entry = validate_positive_number(entry_price, "Entry price")
    stop = validate_positive_number(stop_loss, "Stop loss")
    requested_risk = validate_positive_number(
        risk_percent, "Risk percentage"
    )
    open_risk = validate_non_negative_number(
        current_open_risk, "Current open risk"
    )

    side = str(side).upper().strip()

    if side not in ("BUY", "SELL"):
        raise RiskError("Side must be BUY or SELL.")

    if requested_risk > MAX_RISK_PERCENT:
        raise RiskError(
            "Requested risk exceeds the 1% account maximum."
        )

    if side == "BUY" and stop >= entry:
        raise RiskError(
            "A BUY trade requires stop loss below entry price."
        )

    if side == "SELL" and stop <= entry:
        raise RiskError(
            "A SELL trade requires stop loss above entry price."
        )

    maximum_risk_amount = balance * (
        MAX_RISK_PERCENT / 100
    )

    requested_risk_amount = balance * (
        requested_risk / 100
    )

    projected_total_risk = (
        open_risk + requested_risk_amount
    )

    if projected_total_risk > maximum_risk_amount + 1e-9:
        remaining_risk = max(
            0.0, maximum_risk_amount - open_risk
        )

        raise RiskError(
            "Trade rejected: combined open account risk would "
            "exceed the 1% maximum. "
            f"Current open risk: {open_risk:.2f}. "
            f"Requested risk: {requested_risk_amount:.2f}. "
            f"Remaining risk budget: {remaining_risk:.2f}."
        )

    price_distance = abs(entry - stop)

    if price_distance <= 0:
        raise RiskError(
            "Entry and stop loss must be different."
        )

    quantity = requested_risk_amount / price_distance

    return {
        "approved": True,
        "side": side,
        "account_balance": round(balance, 2),
        "risk_percent": requested_risk,
        "risk_amount": round(requested_risk_amount, 2),
        "current_open_risk": round(open_risk, 2),
        "projected_total_open_risk": round(
            projected_total_risk, 2
        ),
        "maximum_risk_amount": round(
            maximum_risk_amount, 2
        ),
        "remaining_risk_after_trade": round(
            maximum_risk_amount - projected_total_risk, 2
        ),
        "maximum_risk_percent": MAX_RISK_PERCENT,
        "entry_price": entry,
        "stop_loss": stop,
        "price_distance": price_distance,
        "position_size": round(quantity, 8),
        "mode": "paper_trading"
    }
