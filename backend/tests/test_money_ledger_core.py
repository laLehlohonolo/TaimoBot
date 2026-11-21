import unittest

from app.money_ledger_core import (
    MAX_AMOUNT_MINOR,
    LedgerValidationError,
    validate_journal_lines,
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


if __name__ == "__main__":
    unittest.main()
