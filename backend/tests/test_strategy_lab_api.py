import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import database
from app.auth import create_session, register_user
from app.database import init_db
from app.main import app


class StrategyLabAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "strategy-lab.db"
        init_db()
        self.original_admin_emails = os.environ.get("TAIMOBOT_ADMIN_EMAILS")
        os.environ["TAIMOBOT_ADMIN_EMAILS"] = "strategy-admin@taimobot.local"
        admin = register_user("strategy-admin@taimobot.local", "AdminPassphrase-2026-Secure!")
        participant = register_user("strategy-participant@taimobot.local", "ParticipantPassphrase-2026!")
        self.admin_client = TestClient(app)
        self.participant_client = TestClient(app)
        self.admin_client.headers.update({
            "Authorization": "Bearer " + create_session(admin["user_id"])["access_token"]
        })
        self.participant_client.headers.update({
            "Authorization": "Bearer " + create_session(participant["user_id"])["access_token"]
        })

    def tearDown(self):
        self.admin_client.close()
        self.participant_client.close()
        if self.original_admin_emails is None:
            os.environ.pop("TAIMOBOT_ADMIN_EMAILS", None)
        else:
            os.environ["TAIMOBOT_ADMIN_EMAILS"] = self.original_admin_emails
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def candidate_payload(self):
        return {
            "name": "Volatility Regime Transition Hypothesis",
            "hypothesis": "A volatility compression and expansion transition may identify directional opportunities.",
            "instruments": ["XAUUSD"],
            "timeframes": ["M5", "H1"],
            "market_conditions": "Only evaluate when source data is fresh and volatility regime is measurable.",
            "entry_logic": "Enter only after a confirmed transition and a reproducible directional trigger.",
            "exit_logic": "Exit at predefined invalidation, protective stop, or evidence-based target.",
            "invalidation_logic": "Invalidate when the regime classification reverses or data quality fails.",
            "risk_notes": "Candidate research only; paper evaluation and the backend 1% risk cap remain mandatory.",
        }

    def test_participant_cannot_list_create_or_read_candidates(self):
        self.assertEqual(self.participant_client.get("/admin/brain/strategy-candidates").status_code, 403)
        self.assertEqual(
            self.participant_client.post(
                "/admin/brain/strategy-candidates", json=self.candidate_payload()
            ).status_code,
            403,
        )
        self.assertEqual(
            self.admin_client.post(
                "/admin/brain/strategy-candidates", json=self.candidate_payload()
            ).status_code,
            201,
        )
        candidate_id = self.admin_client.get(
            "/admin/brain/strategy-candidates"
        ).json()["candidates"][0]["candidate_id"]
        self.assertEqual(
            self.participant_client.get(
                f"/admin/brain/strategy-candidates/{candidate_id}"
            ).status_code,
            403,
        )

    def test_research_data_readiness_is_admin_only_and_never_enables_backtesting(self):
        self.assertEqual(
            self.participant_client.get(
                "/admin/brain/strategy-candidates/data-readiness",
                params={"symbol": "XAUUSD", "timeframe": "M5"},
            ).status_code,
            403,
        )
        response = self.admin_client.get(
            "/admin/brain/strategy-candidates/data-readiness",
            params={"symbol": "XAUUSD", "timeframe": "M5"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["data_available"])
        self.assertFalse(body["provenance_verified"])
        self.assertFalse(body["eligible_for_backtest"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["execution_triggered"])

    def test_market_data_quality_is_admin_only_and_never_enables_research_or_execution(self):
        params = {"symbol": "R_10", "timeframe": "M1"}
        self.assertEqual(
            self.participant_client.get(
                "/admin/brain/strategy-candidates/market-data-quality",
                params=params,
            ).status_code,
            403,
        )
        response = self.admin_client.get(
            "/admin/brain/strategy-candidates/market-data-quality",
            params=params,
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "NO_DATA")
        self.assertTrue(body["stale"])
        self.assertFalse(body["provenance_verified"])
        self.assertFalse(body["eligible_for_backtest"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])

        unsupported = self.admin_client.get(
            "/admin/brain/strategy-candidates/market-data-quality",
            params={"symbol": "XAUUSD", "timeframe": "M1"},
        )
        self.assertEqual(unsupported.status_code, 400)

    def test_admin_can_create_review_and_reject_candidate(self):
        created = self.admin_client.post(
            "/admin/brain/strategy-candidates", json=self.candidate_payload()
        )
        self.assertEqual(created.status_code, 201)
        candidate = created.json()
        self.assertEqual(candidate["status"], "RESEARCH")
        self.assertEqual(candidate["instruments"], ["XAUUSD"])

        decision = self.admin_client.post(
            f"/admin/brain/strategy-candidates/{candidate['candidate_id']}/decisions",
            json={
                "decision": "REJECT",
                "rationale": "Insufficient evidence and no demonstrated out-of-sample advantage.",
            },
        )
        self.assertEqual(decision.status_code, 201)
        self.assertEqual(decision.json()["status"], "REJECTED")
        self.assertFalse(decision.json()["production_deployed"])
        self.assertFalse(decision.json()["paper_execution_triggered"])

    def test_candidate_cannot_be_approved_for_staging_after_final_rejection(self):
        candidate = self.admin_client.post(
            "/admin/brain/strategy-candidates", json=self.candidate_payload()
        ).json()
        self.admin_client.post(
            f"/admin/brain/strategy-candidates/{candidate['candidate_id']}/decisions",
            json={"decision": "REJECT", "rationale": "Evidence did not support the hypothesis."},
        )
        response = self.admin_client.post(
            f"/admin/brain/strategy-candidates/{candidate['candidate_id']}/decisions",
            json={"decision": "APPROVE_FOR_STAGING", "rationale": "Attempted reversal after final decision."},
        )
        self.assertEqual(response.status_code, 409)

    def test_decision_history_is_recorded_and_readable_to_admin_only(self):
        candidate = self.admin_client.post(
            "/admin/brain/strategy-candidates", json=self.candidate_payload()
        ).json()
        self.admin_client.post(
            f"/admin/brain/strategy-candidates/{candidate['candidate_id']}/decisions",
            json={"decision": "REQUEST_MORE_RESEARCH", "rationale": "Collect more independent market regimes."},
        )
        detail = self.admin_client.get(
            f"/admin/brain/strategy-candidates/{candidate['candidate_id']}"
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(len(detail.json()["decisions"]), 1)
        self.assertEqual(
            self.participant_client.get(
                f"/admin/brain/strategy-candidates/{candidate['candidate_id']}"
            ).status_code,
            403,
        )


class StrategyLabSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "strategy-lab-schema.db"
        init_db()

    def tearDown(self):
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_decision_history_is_append_only(self):
        import sqlite3
        import uuid
        with database.get_connection() as connection:
            candidate_id = "candidate_" + uuid.uuid4().hex
            # Use a valid actor row so these failures prove the immutability triggers,
            # rather than a foreign-key constraint.
            connection.execute(
                """
                INSERT INTO users (user_id, email, password_hash)
                VALUES ('system-test-admin', 'system-test-admin@taimobot.local', 'test-hash')
                """
            )
            connection.execute(
                """
                INSERT INTO brain_strategy_candidates (
                    candidate_id, name, hypothesis, instruments_json, timeframes_json,
                    market_conditions, entry_logic, exit_logic, invalidation_logic,
                    risk_notes, status, created_by
                ) VALUES (?, 'Test Candidate', 'A sufficiently detailed test hypothesis.',
                    '["XAUUSD"]', '["M5"]', 'Test conditions are documented.',
                    'Entry logic is sufficiently detailed for validation.',
                    'Exit logic is sufficiently detailed for validation.',
                    'Invalidation conditions are sufficiently detailed.',
                    'Risk notes preserve all existing hard limits.', 'RESEARCH', ?)
                """,
                (candidate_id, "system-test-admin"),
            )
            connection.execute(
                """
                INSERT INTO brain_strategy_candidate_decisions (
                    decision_id, candidate_id, decision, rationale, decided_by
                ) VALUES ('decision_test', ?, 'REJECT', 'A valid rejection rationale.', 'system-test-admin')
                """,
                (candidate_id,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brain_strategy_candidate_decisions SET rationale = 'changed' WHERE decision_id = 'decision_test'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM brain_strategy_candidate_decisions WHERE decision_id = 'decision_test'"
                )
