"""Instrument catalogue and broker-specification readiness checks.

This module separates catalogue membership from explicit paper-execution
approval. Observation candidates remain non-executable until a broker adapter
supplies verified, account-currency-aware contract specifications and the
execution allowlist is deliberately updated.
"""
import math
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
