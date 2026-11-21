import os
import tempfile
import unittest
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.auth import create_session, register_user
from app.database import init_db
from app.main import app


class SessionCalendarRegistryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "session-calendars.db"
        init_db()
        self.original_admin_emails = os.environ.get("TAIMOBOT_ADMIN_EMAILS")
        os.environ["TAIMOBOT_ADMIN_EMAILS"] = (
            "calendar-admin@taimobot.local,calendar-reviewer@taimobot.local"
        )
        admin = register_user("calendar-admin@taimobot.local", "CalendarAdminPass-2026!")
        reviewer = register_user("calendar-reviewer@taimobot.local", "CalendarReviewerPass-2026!")
        participant = register_user("calendar-user@taimobot.local", "CalendarParticipantPass-2026!")
        self.admin = TestClient(app)
        self.reviewer = TestClient(app)
        self.participant = TestClient(app)
        for client, user in ((self.admin, admin), (self.reviewer, reviewer), (self.participant, participant)):
            client.headers.update({
                "Authorization": "Bearer " + create_session(user["user_id"])["access_token"]
            })

    def tearDown(self):
        self.admin.close()
        self.reviewer.close()
        self.participant.close()
        if self.original_admin_emails is None:
            os.environ.pop("TAIMOBOT_ADMIN_EMAILS", None)
        else:
            os.environ["TAIMOBOT_ADMIN_EMAILS"] = self.original_admin_emails
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def payload(self):
        return {
            "provider": "test-broker",
            "symbol": "XAUUSD",
            "timezone": "Africa/Johannesburg",
            "weekly_sessions": [{"weekday": 0, "start": "08:00", "end": "17:00"}],
            "holidays": [],
            "maintenance_windows": [],
            "effective_from": "2026-01-01",
            "effective_to": "2027-12-31",
            "source_url": "https://broker.example.test/trading-hours",
            "retrieved_at": "2026-11-21T20:00:00Z",
        }

    def test_participant_cannot_access_registry_and_admin_submission_stays_pending(self):
        path = "/admin/brain/session-calendars"
        self.assertEqual(self.participant.get(path).status_code, 403)
        self.assertEqual(self.participant.post(path, json=self.payload()).status_code, 403)
        response = self.admin.post(path, json=self.payload())
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(body["status"], "PENDING_SOURCE_VERIFICATION")
        self.assertTrue(body["record"]["submitted_by"])
        self.assertEqual(body["record"]["submitted_by"], body["submitted_by"])
        self.assertFalse(body["source_verified"])
        self.assertFalse(body["admin_reviewed"])
        self.assertFalse(body["session_calendar_applied"])
        self.assertFalse(body["session_adjusted_coverage_available"])
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["execution_eligible"])

    def test_versions_increase_and_two_person_review_never_enables_trading(self):
        path = "/admin/brain/session-calendars"
        first = self.admin.post(path, json=self.payload()).json()
        second = self.admin.post(path, json=self.payload()).json()
        self.assertEqual((first["version"], second["version"]), (1, 2))
        decision_path = f"{path}/{first['calendar_id']}/decisions"
        verified = self.admin.post(decision_path, json={
            "decision": "VERIFY_SOURCE",
            "rationale": "The source document was independently checked against the provider publication.",
            "evidence_url": "https://broker.example.test/trading-hours",
        })
        self.assertEqual(verified.status_code, 201)
        same_admin = self.admin.post(decision_path, json={
            "decision": "APPROVE_CALENDAR",
            "rationale": "Attempted self-approval is not permitted.",
            "evidence_url": "https://broker.example.test/review-record",
        })
        self.assertEqual(same_admin.status_code, 409)
        approved = self.reviewer.post(decision_path, json={
            "decision": "APPROVE_CALENDAR",
            "rationale": "Second administrator reviewed the source verification record.",
            "evidence_url": "https://broker.example.test/review-record",
        })
        self.assertEqual(approved.status_code, 201)
        body = approved.json()
        self.assertEqual(body["status"], "APPROVED")
        self.assertTrue(body["source_verified"])
        self.assertTrue(body["admin_reviewed"])
        self.assertFalse(body["session_calendar_applied"])
        self.assertFalse(body["session_adjusted_coverage_available"])
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["execution_eligible"])
        detail = self.admin.get(f"{path}/{first['calendar_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["decisions"]), 2)
        self.assertEqual(
            self.participant.get(f"{path}/{first['calendar_id']}").status_code, 403
        )

    def test_calendar_records_decisions_and_audit_are_immutable(self):
        body = self.admin.post("/admin/brain/session-calendars", json=self.payload()).json()
        decision_path = f"/admin/brain/session-calendars/{body['calendar_id']}/decisions"
        verified = self.admin.post(decision_path, json={
            "decision": "VERIFY_SOURCE",
            "rationale": "Source verification recorded for append-only history.",
            "evidence_url": "https://broker.example.test/trading-hours",
        })
        self.assertEqual(verified.status_code, 201)
        with database.get_connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brain_session_calendar_audit SET event_type = 'ALTERED'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM brain_session_calendar_audit")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brain_session_calendar_decisions SET rationale = 'ALTERED'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM brain_session_calendar_decisions")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brain_session_calendars SET record_json = '{}' WHERE calendar_id = ?",
                    (body["calendar_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brain_session_calendars SET status = 'APPROVED' WHERE calendar_id = ?",
                    (body["calendar_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM brain_session_calendars WHERE calendar_id = ?",
                    (body["calendar_id"],),
                )

    def test_source_verification_rejects_malformed_or_non_https_evidence_urls(self):
        path = "/admin/brain/session-calendars"
        body = self.admin.post(path, json=self.payload()).json()
        decision_path = f"{path}/{body['calendar_id']}/decisions"
        for evidence_url in ("http://broker.example.test/source", "https://", "https:///missing-host"):
            response = self.admin.post(decision_path, json={
                "decision": "VERIFY_SOURCE",
                "rationale": "The evidence URL must be a valid HTTPS location.",
                "evidence_url": evidence_url,
            })
            self.assertEqual(response.status_code, 400)
        detail = self.admin.get(f"{path}/{body['calendar_id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["status"], "PENDING_SOURCE_VERIFICATION")
        self.assertEqual(detail.json()["decisions"], [])

    def test_change_request_requires_source_verified_state_and_restarts_review(self):
        path = "/admin/brain/session-calendars"
        body = self.admin.post(path, json=self.payload()).json()
        decision_path = f"{path}/{body['calendar_id']}/decisions"
        invalid = self.admin.post(decision_path, json={
            "decision": "REQUEST_CHANGES",
            "rationale": "This request must not be accepted before source verification.",
        })
        self.assertEqual(invalid.status_code, 409)
        verified = self.admin.post(decision_path, json={
            "decision": "VERIFY_SOURCE",
            "rationale": "The source publication was checked and requires a correction.",
            "evidence_url": "https://broker.example.test/trading-hours",
        })
        self.assertEqual(verified.status_code, 201)
        changed = self.admin.post(decision_path, json={
            "decision": "REQUEST_CHANGES",
            "rationale": "The provider's maintenance schedule requires clarification.",
        })
        self.assertEqual(changed.status_code, 201)
        self.assertEqual(changed.json()["status"], "PENDING_SOURCE_VERIFICATION")
        self.assertFalse(changed.json()["source_verified"])
        self.assertFalse(changed.json()["admin_reviewed"])
        self.assertFalse(changed.json()["backtest_eligible"])
        self.assertFalse(changed.json()["execution_eligible"])


if __name__ == "__main__":
    unittest.main()
