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


def validate_broker_specification_provenance(
    specifications,
    expected_broker,
    expected_symbol,
    expected_account_currency,
    now=None,
    max_age_seconds=300,
):
    """Require attributable, correctly scoped and fresh broker specifications.

    This is a fail-closed metadata gate, not independent proof that a broker
    or source is authentic. The adapter must obtain values from a trusted
    broker source and preserve its provenance fields.
    """
    from datetime import datetime, timezone

    if not isinstance(specifications, dict):
        raise ValueError("Broker specifications must be supplied as an object.")

    required = (
        "broker",
        "symbol",
        "specification_source",
        "source_reference",
        "observed_at",
        "quote_currency",
        "account_currency",
    )
    missing = [field for field in required if field not in specifications]
    if missing:
        raise ValueError(
            "Missing broker specification provenance fields: "
            + ", ".join(missing)
        )

    for field in ("broker", "symbol", "specification_source", "source_reference"):
        value = specifications[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(field + " must be a non-empty string.")

    if specifications["broker"].strip().casefold() != str(expected_broker).strip().casefold():
        raise ValueError("Broker specification does not match the requested broker.")
    if specifications["symbol"].strip().upper() != str(expected_symbol).strip().upper():
        raise ValueError("Broker specification does not match the requested instrument.")
    if specifications["account_currency"].strip().upper() != str(expected_account_currency).strip().upper():
        raise ValueError("Broker specification account currency does not match the account.")

    quote_currency = specifications["quote_currency"]
    if not isinstance(quote_currency, str) or not quote_currency.strip():
        raise ValueError("quote_currency must be a non-empty string.")

    if isinstance(max_age_seconds, bool):
        raise ValueError("max_age_seconds must be a finite positive number.")
    try:
        max_age = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_age_seconds must be a finite positive number.")
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("max_age_seconds must be a finite positive number.")

    timestamp = specifications["observed_at"]
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise ValueError("observed_at must be a timezone-aware ISO-8601 timestamp.")
    try:
        observed_at = datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("observed_at must be a timezone-aware ISO-8601 timestamp.")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must include a timezone offset.")

    current_time = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current_time, datetime) or current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")

    age_seconds = (current_time - observed_at).total_seconds()
    if age_seconds < 0:
        raise ValueError("Broker specifications are dated in the future.")
    if age_seconds > max_age:
        raise ValueError("Broker specifications are stale.")

    return {
        "validated": True,
        "broker": specifications["broker"].strip(),
        "symbol": specifications["symbol"].strip().upper(),
        "specification_source": specifications["specification_source"].strip(),
        "source_reference": specifications["source_reference"].strip(),
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "quote_currency": quote_currency.strip().upper(),
        "account_currency": specifications["account_currency"].strip().upper(),
        "mode": "provenance_gate_only",
    }


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
        "calculated": True,
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



def calculate_provenance_checked_stop_risk(
    entry_price,
    stop_loss,
    requested_volume,
    specifications,
    expected_broker,
    expected_symbol,
    expected_account_currency,
    expected_quote_currency,
    now=None,
    max_age_seconds=300,
):
    """Calculate paper-analysis risk only after provenance/freshness checks.

    This composes the metadata gate and monetary calculator. It does not verify
    source authenticity, fetch live broker data, approve an order, or connect
    the result to execution. The adapter remains responsible for supplying a
    single coherent snapshot of contract and conversion inputs.
    """
    provenance = validate_broker_specification_provenance(
        specifications,
        expected_broker=expected_broker,
        expected_symbol=expected_symbol,
        expected_account_currency=expected_account_currency,
        now=now,
        max_age_seconds=max_age_seconds,
    )
    expected_quote = str(expected_quote_currency).strip().upper()
    if not expected_quote or provenance["quote_currency"] != expected_quote:
        raise ValueError("Broker specification quote currency does not match the requested instrument.")

    calculated = calculate_monetary_stop_risk(
        entry_price, stop_loss, requested_volume, specifications
    )

    if calculated["quote_currency"] != provenance["quote_currency"]:
        raise ValueError("Monetary calculation quote currency does not match provenance.")
    if calculated["account_currency"] != provenance["account_currency"]:
        raise ValueError("Monetary calculation account currency does not match provenance.")

    return {
        **calculated,
        "provenance": provenance,
        "mode": "provenance_checked_calculation_only",
    }


def validate_fx_conversion_snapshot(
    snapshot,
    expected_provider,
    expected_from_currency,
    expected_to_currency,
    now=None,
    max_age_seconds=300,
):
    """Validate a fresh, attributable FX quote for currency conversion.

    The pair is expressed as FROM/TO (for example USD/ZAR), and bid/ask
    represent TO-currency units per one FROM-currency unit. The ask is
    returned as the conservative conversion rate for converting a FROM-
    currency liability into TO currency. This validates metadata and quote
    shape only; it does not authenticate the provider or fetch market data.
    """
    from datetime import datetime, timezone
    import re

    if not isinstance(snapshot, dict):
        raise ValueError("FX conversion snapshot must be supplied as an object.")

    required = (
        "provider", "pair", "base_currency", "quote_currency",
        "bid", "ask", "observed_at", "source_reference",
    )
    missing = [field for field in required if field not in snapshot]
    if missing:
        raise ValueError("Missing FX conversion fields: " + ", ".join(missing))

    for field in ("provider", "pair", "base_currency", "quote_currency", "source_reference"):
        value = snapshot[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string.")

    from_currency = str(expected_from_currency).strip().upper()
    to_currency = str(expected_to_currency).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", from_currency) or not re.fullmatch(r"[A-Z]{3}", to_currency):
        raise ValueError("Expected conversion currencies must be three-letter codes.")
    if from_currency == to_currency:
        raise ValueError("FX conversion currencies must be different.")
    if snapshot["provider"].strip().casefold() != str(expected_provider).strip().casefold():
        raise ValueError("FX quote provider does not match the requested provider.")
    if snapshot["base_currency"].strip().upper() != from_currency:
        raise ValueError("FX quote base currency does not match the requested conversion direction.")
    if snapshot["quote_currency"].strip().upper() != to_currency:
        raise ValueError("FX quote quote currency does not match the requested conversion direction.")
    if snapshot["pair"].strip().replace("/", "").replace("_", "").upper() != from_currency + to_currency:
        raise ValueError("FX quote pair does not match the requested conversion direction.")

    if isinstance(max_age_seconds, bool):
        raise ValueError("max_age_seconds must be a finite positive number.")
    try:
        max_age = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("max_age_seconds must be a finite positive number.")
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("max_age_seconds must be a finite positive number.")

    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")

    observed_raw = snapshot["observed_at"]
    if not isinstance(observed_raw, str) or not observed_raw.strip():
        raise ValueError("observed_at must be a timezone-aware ISO-8601 timestamp.")
    try:
        observed_at = datetime.fromisoformat(observed_raw.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("observed_at must be a timezone-aware ISO-8601 timestamp.") from error
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must include a timezone.")

    age_seconds = (now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0:
        raise ValueError("FX quote timestamp is in the future.")
    if age_seconds > max_age:
        raise ValueError("FX quote is stale.")

    try:
        bid = Decimal(str(snapshot["bid"]))
        ask = Decimal(str(snapshot["ask"]))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("FX bid and ask must be finite positive numbers.")
    if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= 0:
        raise ValueError("FX bid and ask must be finite positive numbers.")
    if ask < bid:
        raise ValueError("FX ask cannot be below FX bid.")

    return {
        "validated": True,
        "provider": snapshot["provider"].strip(),
        "pair": from_currency + "/" + to_currency,
        "from_currency": from_currency,
        "to_currency": to_currency,
        "bid": float(bid),
        "ask": float(ask),
        "conversion_rate": float(ask),
        "conversion_side": "ASK",
        "source_reference": snapshot["source_reference"].strip(),
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age,
        "mode": "fx_conversion_validation_only",
    }


def calculate_fx_verified_stop_risk(
    entry_price,
    stop_loss,
    requested_volume,
    specifications,
    expected_broker,
    expected_symbol,
    expected_account_currency,
    expected_quote_currency,
    fx_snapshot,
    expected_fx_provider,
    now=None,
    max_age_seconds=300,
):
    """Calculate stop risk using an independently validated FX quote.

    The FX quote is validated separately from broker contract metadata. Its
    ask is used as the conservative quote-to-account conversion rate. This
    remains calculation-only: it does not authenticate a provider, fetch data,
    approve an order, or connect the result to execution.
    """
    from datetime import datetime, timezone

    checked_at = now or datetime.now(timezone.utc)
    broker_provenance = validate_broker_specification_provenance(
        specifications,
        expected_broker=expected_broker,
        expected_symbol=expected_symbol,
        expected_account_currency=expected_account_currency,
        now=checked_at,
        max_age_seconds=max_age_seconds,
    )
    expected_quote = str(expected_quote_currency).strip().upper()
    if not expected_quote or broker_provenance["quote_currency"] != expected_quote:
        raise ValueError("Broker specification quote currency does not match the requested instrument.")

    fx = validate_fx_conversion_snapshot(
        fx_snapshot,
        expected_provider=expected_fx_provider,
        expected_from_currency=expected_quote,
        expected_to_currency=expected_account_currency,
        now=checked_at,
        max_age_seconds=max_age_seconds,
    )

    calculation_specs = dict(specifications)
    calculation_specs["quote_to_account_rate"] = fx["conversion_rate"]
    calculated = calculate_monetary_stop_risk(
        entry_price, stop_loss, requested_volume, calculation_specs
    )
    if calculated["quote_currency"] != expected_quote:
        raise ValueError("Monetary calculation quote currency does not match the requested instrument.")
    if calculated["account_currency"] != str(expected_account_currency).strip().upper():
        raise ValueError("Monetary calculation account currency does not match the requested account.")

    return {
        **calculated,
        "broker_provenance": broker_provenance,
        "fx_conversion": fx,
        "mode": "independently_fx_validated_calculation_only",
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
