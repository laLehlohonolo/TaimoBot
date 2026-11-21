import unittest

from app.instruments import list_instruments
from app.session_calendars import (
    SESSION_CALENDARS,
    assess_session_calendar_readiness,
    validate_session_calendar_record,
)


class SessionCalendarTests(unittest.TestCase):
    def valid_record(self):
        return {
            "provider": "ExampleBroker",
            "symbol": "XAUUSD",
            "timezone": "Africa/Johannesburg",
            "weekly_sessions": [
                {"weekday": 0, "start": "08:00", "end": "16:00"},
                {"weekday": 1, "start": "08:00", "end": "16:00"},
            ],
            "holidays": ["2026-12-25"],
            "maintenance_windows": [
                {
                    "start": "2026-12-24T22:00:00Z",
                    "end": "2026-12-24T23:00:00Z",
                }
            ],
            "effective_from": "2026-01-01",
            "effective_to": "2027-12-31",
            "source_url": "https://broker.example.test/instrument-specifications",
            "retrieved_at": "2026-11-21T20:00:00+02:00",
            "submitted_by": "admin-review-pending",
            "version": 1,
        }

    def test_valid_record_is_structurally_valid_but_not_verified_or_enabled(self):
        result = validate_session_calendar_record(self.valid_record())
        self.assertEqual(result["status"], "PENDING_SOURCE_VERIFICATION")
        self.assertFalse(result["source_verified"])
        self.assertFalse(result["admin_reviewed"])
        self.assertFalse(result["session_calendar_applied"])
        self.assertFalse(result["session_adjusted_coverage_available"])
        self.assertFalse(result["backtest_eligible"])
        self.assertFalse(result["execution_eligible"])
        self.assertEqual(result["symbol"], "XAUUSD")

    def test_registry_starts_empty_and_every_catalogued_instrument_is_unconfigured(self):
        self.assertEqual(dict(SESSION_CALENDARS), {})
        for instrument in list_instruments():
            result = assess_session_calendar_readiness(instrument["symbol"])
            self.assertEqual(result["status"], "NOT_CONFIGURED")
            self.assertIsNone(result["session_calendar_id"])
            self.assertFalse(result["session_adjusted_coverage_available"])
            self.assertFalse(result["backtest_eligible"])
            self.assertFalse(result["execution_eligible"])

    def test_readiness_rejects_pending_or_unreviewed_calendar(self):
        pending = validate_session_calendar_record(self.valid_record())
        result = assess_session_calendar_readiness("XAUUSD", pending)
        self.assertEqual(result["status"], "PENDING_VERIFICATION")
        self.assertFalse(result["session_adjusted_coverage_available"])
        self.assertFalse(result["backtest_eligible"])
        self.assertFalse(result["execution_eligible"])

    def test_rejects_empty_weekly_schedule(self):
        record = self.valid_record()
        record["weekly_sessions"] = []
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            validate_session_calendar_record(record)

    def test_untrusted_verification_claims_are_discarded(self):
        record = self.valid_record()
        record.update({
            "status": "VERIFIED",
            "source_verified": True,
            "admin_reviewed": True,
            "calendar_id": "forged-calendar-id",
        })
        validated = validate_session_calendar_record(record)
        self.assertEqual(validated["status"], "PENDING_SOURCE_VERIFICATION")
        self.assertFalse(validated["source_verified"])
        self.assertFalse(validated["admin_reviewed"])
        readiness = assess_session_calendar_readiness("XAUUSD", validated)
        self.assertEqual(readiness["status"], "PENDING_VERIFICATION")
        self.assertIsNone(readiness["session_calendar_id"])
        self.assertFalse(readiness["session_adjusted_coverage_available"])
        self.assertFalse(readiness["backtest_eligible"])
        self.assertFalse(readiness["execution_eligible"])

    def test_rejects_unknown_timezone(self):
        record = self.valid_record()
        record["timezone"] = "Mars/Olympus_Mons"
        with self.assertRaisesRegex(ValueError, "IANA timezone"):
            validate_session_calendar_record(record)

    def test_rejects_timezone_naive_retrieval_and_maintenance_timestamps(self):
        record = self.valid_record()
        record["retrieved_at"] = "2026-11-21T20:00:00"
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            validate_session_calendar_record(record)

        record = self.valid_record()
        record["maintenance_windows"][0]["start"] = "2026-12-24T22:00:00"
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            validate_session_calendar_record(record)

    def test_rejects_overlapping_weekly_sessions(self):
        record = self.valid_record()
        record["weekly_sessions"].append(
            {"weekday": 0, "start": "15:00", "end": "18:00"}
        )
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            validate_session_calendar_record(record)

    def test_rejects_invalid_weekdays_and_overnight_intervals(self):
        record = self.valid_record()
        record["weekly_sessions"][0]["weekday"] = 7
        with self.assertRaisesRegex(ValueError, "weekday"):
            validate_session_calendar_record(record)

        record = self.valid_record()
        record["weekly_sessions"][0].update({"start": "22:00", "end": "02:00"})
        with self.assertRaisesRegex(ValueError, "same-day"):
            validate_session_calendar_record(record)

    def test_rejects_invalid_effective_dates_and_versions(self):
        record = self.valid_record()
        record["effective_from"] = "2027-01-01"
        record["effective_to"] = "2026-01-01"
        with self.assertRaisesRegex(ValueError, "cannot precede"):
            validate_session_calendar_record(record)

        record = self.valid_record()
        record["version"] = True
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validate_session_calendar_record(record)

    def test_rejects_non_https_source_references(self):
        record = self.valid_record()
        record["source_url"] = "http://broker.example.test/spec"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_session_calendar_record(record)

    def test_rejects_invalid_maintenance_windows(self):
        record = self.valid_record()
        record["maintenance_windows"][0] = {
            "start": "2026-12-24T23:00:00Z",
            "end": "2026-12-24T22:00:00Z",
        }
        with self.assertRaisesRegex(ValueError, "must follow"):
            validate_session_calendar_record(record)


if __name__ == "__main__":
    unittest.main()
