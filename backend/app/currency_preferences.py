"""Pure validation for one-time participant display-currency selection.

This module does not persist preferences or perform currency conversion.
Database/API enforcement must use an immutable write-once record.
"""
from dataclasses import dataclass
from datetime import datetime

SUPPORTED_DISPLAY_CURRENCIES = frozenset({"ZAR", "USD", "EUR", "GBP", "KWD"})


class CurrencyPreferenceError(ValueError):
    """Raised when a display-currency selection violates product rules."""


@dataclass(frozen=True)
class DisplayCurrencyPreference:
    participant_id: str
    currency: str
    selected_at: datetime


def create_display_currency_preference(
    participant_id: str,
    currency: str,
    *,
    confirmed: bool,
    selected_at: datetime,
    existing: DisplayCurrencyPreference | None = None,
) -> DisplayCurrencyPreference:
    """Validate a first selection; repeat same selection is idempotent, changes fail."""
    if not isinstance(participant_id, str) or not participant_id.strip():
        raise CurrencyPreferenceError("A participant ID is required.")
    if not isinstance(currency, str):
        raise CurrencyPreferenceError("Choose a supported display currency.")
    normalized_currency = currency.strip().upper()
    if normalized_currency not in SUPPORTED_DISPLAY_CURRENCIES:
        raise CurrencyPreferenceError("Choose a supported display currency.")
    if confirmed is not True:
        raise CurrencyPreferenceError("Explicit confirmation is required.")
    if not isinstance(selected_at, datetime) or selected_at.tzinfo is None or selected_at.utcoffset() is None:
        raise CurrencyPreferenceError("Selection timestamp must include a timezone.")

    if existing is not None:
        if existing.participant_id != participant_id:
            raise CurrencyPreferenceError("Existing preference belongs to another participant.")
        if existing.currency != normalized_currency:
            raise CurrencyPreferenceError("Display currency can only be selected once.")
        return existing

    return DisplayCurrencyPreference(
        participant_id=participant_id.strip(),
        currency=normalized_currency,
        selected_at=selected_at,
    )
