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

    def test_data_readiness_reports_real_inventory_without_trusting_source_labels(self):
        from app.market_observation_store import save_market_observation

        base = {
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "open": 2000.0,
            "high": 2002.0,
            "low": 1999.0,
            "close": 2001.0,
            "volume": 0,
        }
        with database.get_connection() as connection:
            save_market_observation(connection, {
                **base, "timestamp": "2026-11-21T10:00:00Z", "source": "provider-labelled-live"
            })
            save_market_observation(connection, {
                **base, "timestamp": "2026-11-21T10:05:00Z", "source": "provider-labelled-replay"
            })

        response = self.admin_client.get(
            "/admin/brain/strategy-candidates/data-readiness",
            params={"symbol": "XAUUSD", "timeframe": "M5"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["data_available"])
        self.assertEqual(body["observation_count"], 2)
        self.assertEqual(body["source_count"], 2)
        self.assertEqual(body["provenance_status_count"], 1)
        self.assertEqual(body["earliest_timestamp"], "2026-11-21T10:00:00+00:00")
        self.assertEqual(body["latest_timestamp"], "2026-11-21T10:05:00+00:00")
        self.assertEqual(
            {item["source"] for item in body["provenance_breakdown"]},
            {"provider-labelled-live", "provider-labelled-replay"},
        )
        self.assertTrue(all(
            item["provenance_status"] == "UNVERIFIED_EXTERNAL_INPUT"
            for item in body["provenance_breakdown"]
        ))
        self.assertFalse(body["provenance_verified"])
        self.assertFalse(body["eligible_for_backtest"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["signals_generated"])
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

    def test_market_data_quality_detects_real_stored_candle_gap_without_granting_eligibility(self):
        from datetime import datetime, timedelta, timezone
        from app.market_observation_store import save_market_observation

        latest_minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        timestamps = [latest_minute - timedelta(minutes=3), latest_minute - timedelta(minutes=1)]
        with database.get_connection() as connection:
            for index, timestamp in enumerate(timestamps):
                save_market_observation(connection, {
                    "symbol": "R_10",
                    "timeframe": "M1",
                    "timestamp": timestamp.isoformat(),
                    "source": "deriv",
                    "open": 100.0 + index,
                    "high": 102.0 + index,
                    "low": 99.0 + index,
                    "close": 101.0 + index,
                    "volume": 0,
                })

        response = self.admin_client.get(
            "/admin/brain/strategy-candidates/market-data-quality",
            params={"symbol": "R_10", "timeframe": "M1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "GAPS_DETECTED")
        self.assertEqual(body["observation_count"], 2)
        self.assertEqual(body["missing_interval_count"], 1)
        self.assertEqual(body["missing_candle_count"], 1)
        self.assertEqual(body["missing_intervals"][0]["missing_candles"], 1)
        self.assertFalse(body["provenance_verified"])
        self.assertFalse(body["eligible_for_backtest"])
        self.assertFalse(body["execution_eligible"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])

    def test_quality_diagnostics_flag_unaligned_and_future_candles(self):
        from datetime import datetime, timezone
        from app.market_observation_store import (
            inspect_market_observation_quality,
            save_market_observation,
        )

        checked_at = datetime(2026, 11, 21, 20, 5, tzinfo=timezone.utc)
        records = [
            ("R_10", "2026-11-21T20:00:30+00:00"),
            ("R_10", "2026-11-21T20:01:30+00:00"),
            ("R_25", "2026-11-21T20:06:00+00:00"),
        ]
        with database.get_connection() as connection:
            for index, (symbol, timestamp) in enumerate(records):
                save_market_observation(connection, {
                    "symbol": symbol,
                    "timeframe": "M1",
                    "timestamp": timestamp,
                    "source": "deriv",
                    "open": 200.0 + index,
                    "high": 202.0 + index,
                    "low": 199.0 + index,
                    "close": 201.0 + index,
                    "volume": 0,
                })

            unaligned = inspect_market_observation_quality(
                connection, "R_10", "M1", now=checked_at
            )
            future = inspect_market_observation_quality(
                connection, "R_25", "M1", now=checked_at
            )

        self.assertEqual(unaligned["status"], "INVALID_CADENCE")
        self.assertEqual(unaligned["unaligned_observation_count"], 2)
        self.assertFalse(unaligned["execution_eligible"])
        self.assertEqual(future["status"], "FUTURE_DATA")
        self.assertTrue(future["future_data"])
        self.assertEqual(future["future_observation_count"], 1)
        self.assertFalse(future["execution_eligible"])

    def test_market_observation_duplicate_is_idempotent_across_timezone_offsets(self):
        from app.market_observation_store import save_market_observation, list_market_observations

        base = {
            "symbol": "R_10",
            "timeframe": "M1",
            "source": "deriv",
            "open": 300.0,
            "high": 302.0,
            "low": 299.0,
            "close": 301.0,
            "volume": 0,
        }
        with database.get_connection() as connection:
            first = save_market_observation(
                connection, {**base, "timestamp": "2026-11-21T20:00:00Z"}
            )
            duplicate = save_market_observation(
                connection, {**base, "timestamp": "2026-11-21T22:00:00+02:00"}
            )
            rows = list_market_observations(
                connection, symbol="R_10", timeframe="M1", limit=10
            )

        self.assertTrue(first["inserted"])
        self.assertFalse(duplicate["inserted"])
        self.assertEqual(first["timestamp"], duplicate["timestamp"])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["execution_eligible"])
        self.assertEqual(rows[0]["provenance_status"], "UNVERIFIED_EXTERNAL_INPUT")

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
