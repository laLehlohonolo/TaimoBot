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


DEPOSIT_TRANSITIONS = {
    "CREATED": frozenset({"PENDING_PROVIDER", "FAILED", "EXPIRED", "RECONCILIATION_REQUIRED"}),
    "PENDING_PROVIDER": frozenset({"CONFIRMED", "FAILED", "EXPIRED", "RECONCILIATION_REQUIRED"}),
    "CONFIRMED": frozenset({"POSTED", "RECONCILIATION_REQUIRED"}),
    "POSTED": frozenset({"REVERSED", "RECONCILIATION_REQUIRED"}),
    "FAILED": frozenset(),
    "EXPIRED": frozenset(),
    "REVERSED": frozenset(),
    "RECONCILIATION_REQUIRED": frozenset(),
}

WITHDRAWAL_TRANSITIONS = {
    "REQUESTED": frozenset({"IDENTITY_CHECK_PENDING", "REJECTED", "CANCELLED", "RECONCILIATION_REQUIRED"}),
    "IDENTITY_CHECK_PENDING": frozenset({"APPROVED", "REJECTED", "CANCELLED", "RECONCILIATION_REQUIRED"}),
    "APPROVED": frozenset({"PAYOUT_PENDING", "CANCELLED", "RECONCILIATION_REQUIRED"}),
    "PAYOUT_PENDING": frozenset({"PAID", "PAYOUT_FAILED", "RETURNED", "RECONCILIATION_REQUIRED"}),
    "PAID": frozenset({"RETURNED", "RECONCILIATION_REQUIRED"}),
    "REJECTED": frozenset(),
    "CANCELLED": frozenset(),
    "PAYOUT_FAILED": frozenset(),
    "RETURNED": frozenset(),
    "RECONCILIATION_REQUIRED": frozenset(),
}


def validate_transaction_transition(
    transaction_kind: str,
    current_status: str,
    next_status: str,
) -> str:
    """Validate a proposed state transition; this function never persists it."""
    transition_maps = {
        "DEPOSIT": DEPOSIT_TRANSITIONS,
        "WITHDRAWAL": WITHDRAWAL_TRANSITIONS,
    }
    if transaction_kind not in transition_maps:
        raise LedgerValidationError("Unsupported money transaction kind.")

    transitions = transition_maps[transaction_kind]
    if current_status not in transitions:
        raise LedgerValidationError("Unknown current transaction status.")
    if next_status not in transitions:
        raise LedgerValidationError("Unknown next transaction status.")
    if next_status not in transitions[current_status]:
        raise LedgerValidationError(
            f"Invalid {transaction_kind.lower()} transition: "
            f"{current_status} -> {next_status}."
        )
    return next_status


def build_reversal_lines(
    original_lines: Iterable[Mapping[str, object]],
    *,
    currency: str = "ZAR",
) -> tuple[dict[str, object], ...]:
    """Build balanced compensating lines; never edits or deletes the original."""
    original = validate_journal_lines(original_lines, currency=currency)
    reversed_lines = tuple(
        {
            "account_id": line["account_id"],
            "side": "CREDIT" if line["side"] == "DEBIT" else "DEBIT",
            "amount_minor": line["amount_minor"],
            "currency": line["currency"],
        }
        for line in original
    )
    return validate_journal_lines(reversed_lines, currency=currency)
