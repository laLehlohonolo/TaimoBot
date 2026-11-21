import unittest

from app.money_ledger_core import (
    MAX_AMOUNT_MINOR,
    LedgerValidationError,
    validate_journal_lines,
    validate_transaction_transition,
    build_reversal_lines,
)


class MoneyLedgerCoreTests(unittest.TestCase):
    def balanced_lines(self):
        return [
            {"account_id": "participant-liability-1", "side": "DEBIT", "amount_minor": 12550},
            {"account_id": "provider-clearing", "side": "CREDIT", "amount_minor": 12550},
        ]

    def test_accepts_balanced_zar_journal_and_normalizes_currency(self):
        result = validate_journal_lines(self.balanced_lines(), currency="zar")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["currency"], "ZAR")
        self.assertEqual(result[0]["amount_minor"], 12550)

    def test_returns_immutable_tuple_snapshot(self):
        lines = self.balanced_lines()
        result = validate_journal_lines(lines)
        lines[0]["amount_minor"] = 1
        self.assertIsInstance(result, tuple)
        self.assertEqual(result[0]["amount_minor"], 12550)

    def test_rejects_unbalanced_journal(self):
        lines = self.balanced_lines()
        lines[0]["amount_minor"] = 12549
        with self.assertRaisesRegex(LedgerValidationError, "debits must equal"):
            validate_journal_lines(lines)

    def test_rejects_single_line_journal(self):
        with self.assertRaisesRegex(LedgerValidationError, "at least two"):
            validate_journal_lines(self.balanced_lines()[:1])

    def test_rejects_empty_account_identifier(self):
        lines = self.balanced_lines()
        lines[0]["account_id"] = "  "
        with self.assertRaisesRegex(LedgerValidationError, "account ID"):
            validate_journal_lines(lines)

    def test_rejects_float_amount_even_when_integral(self):
        lines = self.balanced_lines()
        lines[0]["amount_minor"] = 12550.0
        with self.assertRaisesRegex(LedgerValidationError, "integer minor units"):
            validate_journal_lines(lines)

    def test_rejects_boolean_amount(self):
        lines = self.balanced_lines()
        lines[0]["amount_minor"] = True
        with self.assertRaisesRegex(LedgerValidationError, "integer minor units"):
            validate_journal_lines(lines)

    def test_rejects_zero_and_negative_amounts(self):
        for amount in (0, -1):
            lines = self.balanced_lines()
            lines[0]["amount_minor"] = amount
            lines[1]["amount_minor"] = amount
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(LedgerValidationError, "allowed range"):
                    validate_journal_lines(lines)

    def test_rejects_mixed_currency_lines(self):
        lines = self.balanced_lines()
        lines[1]["currency"] = "USD"
        with self.assertRaisesRegex(LedgerValidationError, "journal currency"):
            validate_journal_lines(lines)

    def test_rejects_invalid_currency_code(self):
        with self.assertRaisesRegex(LedgerValidationError, "three-letter"):
            validate_journal_lines(self.balanced_lines(), currency="ZAR1")

    def test_accepts_maximum_supported_integer_amount(self):
        lines = [
            {"account_id": "a", "side": "DEBIT", "amount_minor": MAX_AMOUNT_MINOR},
            {"account_id": "b", "side": "CREDIT", "amount_minor": MAX_AMOUNT_MINOR},
        ]
        self.assertEqual(
            validate_journal_lines(lines)[0]["amount_minor"],
            MAX_AMOUNT_MINOR,
        )

    def test_rejects_amount_above_supported_bound(self):
        amount = MAX_AMOUNT_MINOR + 1
        lines = [
            {"account_id": "a", "side": "DEBIT", "amount_minor": amount},
            {"account_id": "b", "side": "CREDIT", "amount_minor": amount},
        ]
        with self.assertRaisesRegex(LedgerValidationError, "allowed range"):
            validate_journal_lines(lines)

    def test_builds_balanced_compensating_reversal_without_mutating_original(self):
        original = self.balanced_lines()
        before = [dict(line) for line in original]
        reversal = build_reversal_lines(original)
        self.assertEqual(original, before)
        self.assertEqual(reversal[0]["side"], "CREDIT")
        self.assertEqual(reversal[1]["side"], "DEBIT")
        self.assertEqual(reversal[0]["amount_minor"], 12550)
        self.assertEqual(reversal[1]["amount_minor"], 12550)

    def test_reversal_rejects_unbalanced_original(self):
        lines = self.balanced_lines()
        lines[0]["amount_minor"] -= 1
        with self.assertRaisesRegex(LedgerValidationError, "debits must equal"):
            build_reversal_lines(lines)

    def test_deposit_requires_provider_confirmation_before_posting(self):
        self.assertEqual(
            validate_transaction_transition("DEPOSIT", "CREATED", "PENDING_PROVIDER"),
            "PENDING_PROVIDER",
        )
        self.assertEqual(
            validate_transaction_transition("DEPOSIT", "PENDING_PROVIDER", "CONFIRMED"),
            "CONFIRMED",
        )
        self.assertEqual(
            validate_transaction_transition("DEPOSIT", "CONFIRMED", "POSTED"),
            "POSTED",
        )

    def test_deposit_cannot_skip_confirmation(self):
        with self.assertRaisesRegex(LedgerValidationError, "Invalid deposit transition"):
            validate_transaction_transition("DEPOSIT", "PENDING_PROVIDER", "POSTED")

    def test_posted_deposit_can_only_be_reversed_as_compensation_or_reviewed(self):
        self.assertEqual(
            validate_transaction_transition("DEPOSIT", "POSTED", "REVERSED"),
            "REVERSED",
        )
        with self.assertRaises(LedgerValidationError):
            validate_transaction_transition("DEPOSIT", "REVERSED", "POSTED")

    def test_withdrawal_requires_identity_review_and_approval_before_payout(self):
        self.assertEqual(
            validate_transaction_transition("WITHDRAWAL", "REQUESTED", "IDENTITY_CHECK_PENDING"),
            "IDENTITY_CHECK_PENDING",
        )
        self.assertEqual(
            validate_transaction_transition("WITHDRAWAL", "IDENTITY_CHECK_PENDING", "APPROVED"),
            "APPROVED",
        )
        self.assertEqual(
            validate_transaction_transition("WITHDRAWAL", "APPROVED", "PAYOUT_PENDING"),
            "PAYOUT_PENDING",
        )

    def test_withdrawal_cannot_be_marked_paid_before_payout_pending(self):
        with self.assertRaisesRegex(LedgerValidationError, "Invalid withdrawal transition"):
            validate_transaction_transition("WITHDRAWAL", "APPROVED", "PAID")

    def test_terminal_withdrawal_states_reject_further_transitions(self):
        for status in ("REJECTED", "CANCELLED", "PAYOUT_FAILED"):
            with self.subTest(status=status):
                with self.assertRaises(LedgerValidationError):
                    validate_transaction_transition("WITHDRAWAL", status, "PAID")

    def test_unknown_transaction_kind_and_states_are_rejected(self):
        for args in (
            ("TRANSFER", "CREATED", "POSTED"),
            ("DEPOSIT", "UNKNOWN", "POSTED"),
            ("WITHDRAWAL", "REQUESTED", "UNKNOWN"),
        ):
            with self.subTest(args=args):
                with self.assertRaises(LedgerValidationError):
                    validate_transaction_transition(*args)


if __name__ == "__main__":
    unittest.main()
