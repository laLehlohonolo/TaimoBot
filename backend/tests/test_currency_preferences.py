import unittest
from datetime import datetime, timezone

from app.currency_preferences import (
    CurrencyPreferenceError,
    SUPPORTED_DISPLAY_CURRENCIES,
    create_display_currency_preference,
)


class CurrencyPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.selected_at = datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)

    def test_supports_exact_requested_currency_set(self):
        self.assertEqual(SUPPORTED_DISPLAY_CURRENCIES, {"ZAR", "USD", "EUR", "GBP", "KWD"})

    def test_creates_confirmed_preference_and_normalizes_code(self):
        result = create_display_currency_preference(
            "participant-1", " zar ", confirmed=True, selected_at=self.selected_at
        )
        self.assertEqual(result.participant_id, "participant-1")
        self.assertEqual(result.currency, "ZAR")
        self.assertEqual(result.selected_at, self.selected_at)

    def test_rejects_unsupported_currency(self):
        with self.assertRaisesRegex(CurrencyPreferenceError, "supported"):
            create_display_currency_preference(
                "participant-1", "KES", confirmed=True, selected_at=self.selected_at
            )

    def test_rejects_missing_confirmation(self):
        with self.assertRaisesRegex(CurrencyPreferenceError, "confirmation"):
            create_display_currency_preference(
                "participant-1", "USD", confirmed=False, selected_at=self.selected_at
            )

    def test_rejects_missing_participant_id(self):
        with self.assertRaisesRegex(CurrencyPreferenceError, "participant ID"):
            create_display_currency_preference(
                " ", "USD", confirmed=True, selected_at=self.selected_at
            )

    def test_rejects_naive_selection_timestamp(self):
        with self.assertRaisesRegex(CurrencyPreferenceError, "timezone"):
            create_display_currency_preference(
                "participant-1", "USD", confirmed=True,
                selected_at=datetime(2026, 11, 21, 16, 0)
            )

    def test_repeated_same_selection_is_idempotent(self):
        original = create_display_currency_preference(
            "participant-1", "USD", confirmed=True, selected_at=self.selected_at
        )
        retry = create_display_currency_preference(
            "participant-1", "USD", confirmed=True,
            selected_at=datetime(2026, 11, 22, 16, 0, tzinfo=timezone.utc),
            existing=original,
        )
        self.assertIs(retry, original)
        self.assertEqual(retry.selected_at, self.selected_at)

    def test_rejects_attempt_to_change_currency(self):
        original = create_display_currency_preference(
            "participant-1", "USD", confirmed=True, selected_at=self.selected_at
        )
        with self.assertRaisesRegex(CurrencyPreferenceError, "only be selected once"):
            create_display_currency_preference(
                "participant-1", "KWD", confirmed=True,
                selected_at=self.selected_at, existing=original,
            )

    def test_rejects_preference_belonging_to_another_participant(self):
        original = create_display_currency_preference(
            "participant-1", "USD", confirmed=True, selected_at=self.selected_at
        )
        with self.assertRaisesRegex(CurrencyPreferenceError, "another participant"):
            create_display_currency_preference(
                "participant-2", "USD", confirmed=True,
                selected_at=self.selected_at, existing=original,
            )

    def test_preference_record_is_immutable(self):
        result = create_display_currency_preference(
            "participant-1", "USD", confirmed=True, selected_at=self.selected_at
        )
        with self.assertRaises((AttributeError, TypeError)):
            result.currency = "KWD"


if __name__ == "__main__":
    unittest.main()
