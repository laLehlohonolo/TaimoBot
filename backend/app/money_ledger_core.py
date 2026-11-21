"""Pure validation primitives for a future, disabled real-money ledger.

This module has no database, API, payment-provider, bank, or paper-trading
integration. It validates journal shape only; it does not post money.
"""

from collections.abc import Iterable, Mapping

MAX_AMOUNT_MINOR = 9_000_000_000_000_000
VALID_SIDES = frozenset({"DEBIT", "CREDIT"})


class LedgerValidationError(ValueError):
    """Raised when a proposed journal is not safe to post."""


def validate_journal_lines(
    lines: Iterable[Mapping[str, object]],
    *,
    currency: str = "ZAR",
) -> tuple[dict[str, object], ...]:
    """Return a normalized immutable snapshot of balanced, single-currency lines.

    Amounts are integer minor units (cents for ZAR). This function performs
    no persistence and does not create or move funds.
    """
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise LedgerValidationError("Currency must be a three-letter code.")
    normalized_currency = currency.upper()

    try:
        raw_lines = list(lines)
    except TypeError as exc:
        raise LedgerValidationError("Journal lines must be iterable.") from exc

    if len(raw_lines) < 2:
        raise LedgerValidationError("A journal requires at least two lines.")

    normalized: list[dict[str, object]] = []
    debit_total = 0
    credit_total = 0

    for index, line in enumerate(raw_lines):
        if not isinstance(line, Mapping):
            raise LedgerValidationError(f"Journal line {index} must be a mapping.")

        account_id = line.get("account_id")
        side = line.get("side")
        amount = line.get("amount_minor")
        line_currency = line.get("currency", normalized_currency)

        if not isinstance(account_id, str) or not account_id.strip():
            raise LedgerValidationError(f"Journal line {index} requires an account ID.")
        if not isinstance(side, str) or side not in VALID_SIDES:
            raise LedgerValidationError(f"Journal line {index} has an invalid debit/credit side.")
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise LedgerValidationError(f"Journal line {index} amount must be integer minor units.")
        if amount <= 0 or amount > MAX_AMOUNT_MINOR:
            raise LedgerValidationError(f"Journal line {index} amount is outside the allowed range.")
        if not isinstance(line_currency, str) or line_currency.upper() != normalized_currency:
            raise LedgerValidationError("All journal lines must use the journal currency.")

        if side == "DEBIT":
            debit_total += amount
        else:
            credit_total += amount

        normalized.append({
            "account_id": account_id.strip(),
            "side": side,
            "amount_minor": amount,
            "currency": normalized_currency,
        })

    if debit_total != credit_total:
        raise LedgerValidationError("Journal debits must equal journal credits.")

    return tuple(normalized)
