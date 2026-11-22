"""Instrument catalogue and broker-specification readiness checks.

This module separates catalogue membership from explicit paper-execution
approval. Observation candidates remain non-executable until a broker adapter
supplies verified, account-currency-aware contract specifications and the
execution allowlist is deliberately updated.
"""
import math
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from types import MappingProxyType


INSTRUMENTS = MappingProxyType({
    "XAUUSD": MappingProxyType({
        "symbol": "XAUUSD",
        "asset_class": "metal",
        "role": "existing_default",
        "base_currency": "XAU",
        "quote_currency": "USD",
    }),
    "EURUSD": MappingProxyType({
        "symbol": "EURUSD",
        "asset_class": "forex",
        "role": "initial_observation_candidate",
        "base_currency": "EUR",
        "quote_currency": "USD",
    }),
    "GBPUSD": MappingProxyType({
        "symbol": "GBPUSD",
        "asset_class": "forex",
        "role": "initial_observation_candidate",
        "base_currency": "GBP",
        "quote_currency": "USD",
    }),
    "R_10": MappingProxyType({
        "symbol": "R_10",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "R_25": MappingProxyType({
        "symbol": "R_25",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "R_50": MappingProxyType({
        "symbol": "R_50",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "R_75": MappingProxyType({
        "symbol": "R_75",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "R_100": MappingProxyType({
        "symbol": "R_100",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ10V": MappingProxyType({
        "symbol": "1HZ10V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ25V": MappingProxyType({
        "symbol": "1HZ25V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ50V": MappingProxyType({
        "symbol": "1HZ50V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ75V": MappingProxyType({
        "symbol": "1HZ75V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ100V": MappingProxyType({
        "symbol": "1HZ100V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ15V": MappingProxyType({
        "symbol": "1HZ15V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ30V": MappingProxyType({
        "symbol": "1HZ30V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
    "1HZ90V": MappingProxyType({
        "symbol": "1HZ90V",
        "asset_class": "synthetic_index",
        "provider": "Deriv",
        "role": "initial_observation_candidate",
        "market_group": "volatility_indices",
        "execution_model": "NOT_CONFIGURED",
    }),
})

# Values must be supplied by a verified broker/instrument adapter, never
# guessed from a generic FX or metals convention.
REQUIRED_BROKER_SPEC_FIELDS = (
    "tick_size",
    "tick_value",
    "contract_size",
    "minimum_volume",
    "maximum_volume",
    "volume_step",
    "quote_to_account_rate",
)


def list_instruments():
    """Return detached catalogue records; callers cannot mutate the registry."""
    return [dict(record) for record in INSTRUMENTS.values()]


def get_instrument(symbol):
    """Return a detached instrument record or raise for an unknown symbol."""
    normalized = str(symbol).strip().upper()
    if normalized not in INSTRUMENTS:
        raise ValueError("Instrument is not in the approved observation catalogue.")
    return dict(INSTRUMENTS[normalized])


def validate_broker_specifications(specifications):
    """Validate required positive broker values before sizing can be enabled.

    This is a completeness/shape check, not proof that the values are correct.
    Values must be sourced and verified for the selected broker, account
    currency, and instrument.
    """
    if not isinstance(specifications, dict):
        raise ValueError("Broker specifications must be supplied as an object.")

    missing = [
        field for field in REQUIRED_BROKER_SPEC_FIELDS
        if field not in specifications
    ]
    if missing:
        raise ValueError(
            "Missing broker specification fields: " + ", ".join(missing)
        )

    normalized = {}
    for field in REQUIRED_BROKER_SPEC_FIELDS:
        value = specifications[field]
        if isinstance(value, bool):
            raise ValueError(field + " must be a finite positive number.")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(field + " must be a finite positive number.")
        if not math.isfinite(number) or number <= 0:
            raise ValueError(field + " must be a finite positive number.")
        normalized[field] = number

    if normalized["minimum_volume"] > normalized["maximum_volume"]:
        raise ValueError("Minimum volume cannot exceed maximum volume.")

    return normalized


def normalize_trade_volume(requested_volume, specifications):
    """Round risk-sized volume down to the broker step or reject unsafe bounds.

    The step grid is anchored at minimum_volume. This helper deliberately
    rejects requests above maximum_volume rather than silently clipping them.
    It is a sizing utility only; it does not establish that broker values are
    authentic or connect specifications to the order execution path.
    """
    validated = validate_broker_specifications(specifications)

    try:
        requested = Decimal(str(requested_volume))
        minimum = Decimal(str(validated["minimum_volume"]))
        maximum = Decimal(str(validated["maximum_volume"]))
        step = Decimal(str(validated["volume_step"]))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Requested volume must be a finite positive number.")

    if not requested.is_finite() or requested <= 0:
        raise ValueError("Requested volume must be a finite positive number.")
    if requested < minimum:
        raise ValueError("Requested volume is below the broker minimum.")
    if requested > maximum:
        raise ValueError("Requested volume exceeds the broker maximum.")

    steps = ((requested - minimum) / step).to_integral_value(
        rounding=ROUND_FLOOR
    )
    normalized = minimum + steps * step
    if normalized < minimum or normalized > maximum:
        raise ValueError("No valid broker volume is available for this request.")

    return float(normalized)


def calculate_monetary_stop_risk(
    entry_price, stop_loss, requested_volume, specifications
):
    """Calculate stop-distance risk using an explicit broker tick-value convention.

    tick_value must mean quote-currency value per tick for one standard lot.
    quote_to_account_rate must mean account-currency units per one quote-
    currency unit. This calculation is deliberately not wired into execution.
    """
    validated = validate_broker_specifications(specifications)
    basis = specifications.get("tick_value_basis")
    if basis != "QUOTE_CURRENCY_PER_TICK_PER_LOT":
        raise ValueError(
            "tick_value_basis must explicitly identify quote-currency "
            "value per tick per lot."
        )

    quote_currency = specifications.get("quote_currency")
    account_currency = specifications.get("account_currency")
    if not isinstance(quote_currency, str) or not quote_currency.strip():
        raise ValueError("quote_currency is required.")
    if not isinstance(account_currency, str) or not account_currency.strip():
        raise ValueError("account_currency is required.")

    try:
        entry = Decimal(str(entry_price))
        stop = Decimal(str(stop_loss))
        tick_size = Decimal(str(validated["tick_size"]))
        tick_value = Decimal(str(validated["tick_value"]))
        conversion = Decimal(str(validated["quote_to_account_rate"]))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Prices and broker monetary specifications must be finite numbers.")

    if any(not value.is_finite() or value <= 0 for value in (
        entry, stop, tick_size, tick_value, conversion
    )):
        raise ValueError("Prices and broker monetary specifications must be finite positive numbers.")
    distance = abs(entry - stop)
    if distance <= 0:
        raise ValueError("Entry and stop loss must be different.")
    ticks = distance / tick_size
    if ticks != ticks.to_integral_value():
        raise ValueError("Stop distance must be an exact multiple of broker tick_size.")

    volume = Decimal(str(normalize_trade_volume(
        requested_volume, specifications
    )))
    risk = ticks * tick_value * volume * conversion
    if not risk.is_finite() or risk <= 0:
        raise ValueError("Calculated monetary risk is outside the supported range.")

    return {
        "approved": True,
        "normalized_volume": float(volume),
        "stop_distance": float(distance),
        "tick_count": int(ticks),
        "tick_value_basis": basis,
        "quote_currency": quote_currency.strip().upper(),
        "account_currency": account_currency.strip().upper(),
        "quote_to_account_rate": float(conversion),
        "monetary_risk": float(risk),
        "mode": "calculation_only",
    }


# Execution approval is deliberately narrower than catalogue membership.
EXECUTION_ENABLED_SYMBOLS = frozenset({"XAUUSD"})


def require_execution_approval(symbol):
    """Reject observation-only or unknown instruments at every order boundary."""
    normalized = str(symbol).strip().upper()
    if normalized not in EXECUTION_ENABLED_SYMBOLS:
        raise ValueError(
            "Instrument is observation-only and is not approved for paper execution."
        )
    return normalized
