"""Provider-neutral FX quote adapter contract.

This module defines the boundary for a future FX data integration. It ships
no network client, credentials, provider SDK, or execution capability.
"""
from typing import Mapping, Protocol, runtime_checkable

from app.instruments import validate_fx_conversion_snapshot


@runtime_checkable
class FXQuoteAdapter(Protocol):
    """Minimal synchronous contract required from a future FX data adapter."""

    provider_name: str

    def get_quote(self, pair: str) -> Mapping[str, object]:
        """Return one provider-attributed quote snapshot for BASE/QUOTE."""
        ...


def load_validated_fx_quote(
    adapter,
    expected_provider,
    from_currency,
    to_currency,
    now=None,
    max_age_seconds=300,
):
    """Fetch one quote through an adapter and validate it fail-closed.

    The adapter is responsible for obtaining the quote from its real provider
    and preserving the provider timestamp and source reference. This function
    checks the adapter identity and quote shape/freshness; it cannot
    independently authenticate the provider or the upstream response.
    """
    provider_name = getattr(adapter, "provider_name", None)
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ValueError("FX adapter must declare a non-empty provider_name.")
    if provider_name.strip().casefold() != str(expected_provider).strip().casefold():
        raise ValueError("FX adapter identity does not match the configured provider.")

    from_code = str(from_currency).strip().upper()
    to_code = str(to_currency).strip().upper()
    pair = from_code + "/" + to_code

    get_quote = getattr(adapter, "get_quote", None)
    if not callable(get_quote):
        raise ValueError("FX adapter must implement get_quote(pair).")

    snapshot = get_quote(pair)
    if not isinstance(snapshot, Mapping):
        raise ValueError("FX adapter must return a quote snapshot object.")
    # Copy to a plain dictionary so validation has a stable, detached payload.
    return validate_fx_conversion_snapshot(
        dict(snapshot),
        expected_provider=expected_provider,
        expected_from_currency=from_code,
        expected_to_currency=to_code,
        now=now,
        max_age_seconds=max_age_seconds,
    )
