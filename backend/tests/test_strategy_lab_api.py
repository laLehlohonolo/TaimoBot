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
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET role = 'ADMIN', email_verified = 1 WHERE user_id = ?",
                (admin["user_id"],),
            )
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

    def test_historical_data_coverage_is_admin_only_and_keeps_sources_separate(self):
        from app.market_observation_store import save_market_observation

        with database.get_connection() as connection:
            for source, timestamp in (
                ("deriv", "2026-11-21T12:00:00Z"),
                ("deriv", "2026-11-21T12:01:00Z"),
                ("deriv", "2026-11-21T12:03:00Z"),
                ("replay", "2026-11-21T12:00:30Z"),
            ):
                save_market_observation(connection, {
                    "symbol": "1HZ100V",
                    "timeframe": "M1",
                    "timestamp": timestamp,
                    "source": source,
                    "open": 1000.0,
                    "high": 1002.0,
                    "low": 999.0,
                    "close": 1001.0,
                    "volume": 0,
                })

        params = {"symbol": "1HZ100V", "timeframe": "M1"}
        self.assertEqual(
            self.participant_client.get(
                "/admin/brain/strategy-candidates/data-coverage", params=params
            ).status_code,
            403,
        )
        response = self.admin_client.get(
            "/admin/brain/strategy-candidates/data-coverage", params=params
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        reports = {item["source"]: item for item in body["sources"]}
        self.assertEqual(body["source_count"], 2)
        self.assertEqual(reports["deriv"]["expected_candle_count"], 4)
        self.assertEqual(reports["deriv"]["missing_candle_count"], 1)
        self.assertEqual(reports["deriv"]["coverage_percent"], 75.0)
        self.assertFalse(reports["replay"]["cadence_valid"])
        self.assertIsNone(reports["replay"]["coverage_percent"])
        self.assertFalse(body["provenance_verified"])
        self.assertFalse(body["eligible_for_backtest"])
        self.assertEqual(body["session_calendar_status"], "NOT_CONFIGURED")
        self.assertIsNone(body["session_calendar_id"])
        self.assertFalse(body["session_adjusted_coverage_available"])
        self.assertEqual(
            body["session_calendar_policy"],
            "INSTRUMENT_SPECIFIC_CALENDAR_REQUIRED",
        )
        self.assertFalse(body["session_calendar_applied"])
        self.assertTrue(body["read_only"])
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])

        # Metals use the same explicit NOT_CONFIGURED state until a
        # broker-specific session calendar is sourced and reviewed.
        metal_response = self.admin_client.get(
            "/admin/brain/strategy-candidates/data-coverage",
            params={"symbol": "XAUUSD", "timeframe": "M5"},
        )
        self.assertEqual(metal_response.status_code, 200)
        metal_body = metal_response.json()
        self.assertEqual(metal_body["session_calendar_status"], "NOT_CONFIGURED")
        self.assertIsNone(metal_body["session_calendar_id"])
        self.assertFalse(metal_body["session_adjusted_coverage_available"])
        self.assertFalse(metal_body["session_calendar_applied"])
        self.assertFalse(metal_body["eligible_for_backtest"])
        self.assertFalse(metal_body["execution_triggered"])

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
        self.original_admin_emails = os.environ.get("TAIMOBOT_ADMIN_EMAILS")
        os.environ["TAIMOBOT_ADMIN_EMAILS"] = "strategy-admin@taimobot.local"
        admin = register_user("strategy-admin@taimobot.local", "AdminPassphrase-2026-Secure!")
        participant = register_user("strategy-participant@taimobot.local", "ParticipantPassphrase-2026!")
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE users SET role = 'ADMIN', email_verified = 1 WHERE user_id = ?",
                (admin["user_id"],),
            )
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


    def test_backfill_checkpoint_review_is_admin_only_and_retries_same_cursor_with_audit(self):
        self.assertEqual(
            self.participant_client.get("/admin/market-backfills").status_code, 403
        )
        self.assertEqual(
            self.participant_client.post(
                "/admin/market-backfills/1HZ100V/M1/review",
                json={
                    "decision": "RETRY_SAME_RANGE",
                    "confirm_retry": True,
                    "rationale": "Provider endpoint recovered; retry the exact held interval.",
                },
            ).status_code,
            403,
        )

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_backfill_checkpoints (
                    symbol, timeframe, next_end_epoch, status, batches_completed,
                    last_error
                ) VALUES ('1HZ100V', 'M1', 3600, 'GAP_REVIEW', 2, 'One candle missing.')
                """
            )

        listed = self.admin_client.get("/admin/market-backfills")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["checkpoints"][0]["status"], "GAP_REVIEW")
        self.assertFalse(listed.json()["backtest_eligible"])
        self.assertFalse(listed.json()["execution_triggered"])

        response = self.admin_client.post(
            "/admin/market-backfills/1HZ100V/M1/review",
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "Provider endpoint recovered; retry the exact held interval.",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "RUNNING")
        self.assertEqual(body["next_end_epoch"], 3600)
        self.assertFalse(body["cursor_advanced"])
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["execution_triggered"])
        self.assertFalse(body["signals_generated"])

        with database.get_connection() as connection:
            checkpoint = connection.execute(
                """
                SELECT next_end_epoch, status, batches_completed
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()
            audit = connection.execute(
                """
                SELECT decision, rationale, decided_by
                FROM market_backfill_review_audit
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchall()
        self.assertEqual(checkpoint["next_end_epoch"], 3600)
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["batches_completed"], 2)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["decision"], "RETRY_SAME_RANGE")
        self.assertTrue(audit[0]["decided_by"])

    def test_backfill_review_rejects_whitespace_only_rationale(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_backfill_checkpoints (
                    symbol, timeframe, next_end_epoch, status
                ) VALUES ('1HZ100V', 'M5', 3600, 'GAP_REVIEW')
                """
            )
        response = self.admin_client.post(
            "/admin/market-backfills/1HZ100V/M5/review",
            json={
                "decision": "RETRY_SAME_RANGE",
                "confirm_retry": True,
                "rationale": "          ",
            },
        )
        self.assertEqual(response.status_code, 422)
        with database.get_connection() as connection:
            checkpoint = connection.execute(
                """
                SELECT status FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M5'
                """
            ).fetchone()
            audit_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM market_backfill_review_audit
                WHERE symbol = '1HZ100V' AND timeframe = 'M5'
                """
            ).fetchone()["count"]
        self.assertEqual(checkpoint["status"], "GAP_REVIEW")
        self.assertEqual(audit_count, 0)

    def test_backfill_review_requires_strict_confirmation_and_gap_state(self):
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_backfill_checkpoints (
                    symbol, timeframe, next_end_epoch, status
                ) VALUES ('1HZ100V', 'M5', 3600, 'RUNNING')
                """
            )
        base = {
            "decision": "RETRY_SAME_RANGE",
            "rationale": "Provider endpoint recovered; retry the exact held interval.",
        }
        for invalid in ("true", 1, None):
            response = self.admin_client.post(
                "/admin/market-backfills/1HZ100V/M5/review",
                json={**base, "confirm_retry": invalid},
            )
            self.assertEqual(response.status_code, 422)
        response = self.admin_client.post(
            "/admin/market-backfills/1HZ100V/M5/review",
            json={**base, "confirm_retry": True},
        )
        self.assertEqual(response.status_code, 409)

    def test_backfill_review_audit_is_append_only(self):
        import sqlite3
        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO users (user_id, email, password_hash)
                VALUES ('backfill-audit-admin', 'backfill-audit-admin@taimobot.local', 'test-hash')
                """
            )
            connection.execute(
                """
                INSERT INTO market_backfill_review_audit (
                    review_id, symbol, timeframe, decision, rationale, decided_by
                ) VALUES (
                    'review_test', '1HZ100V', 'M1', 'RETRY_SAME_RANGE',
                    'An auditable test review rationale.', 'backfill-audit-admin'
                )
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE market_backfill_review_audit SET rationale = 'changed' WHERE review_id = 'review_test'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM market_backfill_review_audit WHERE review_id = 'review_test'"
                )


    def test_backfill_gap_review_retry_run_and_persistence_complete_as_one_admin_workflow(self):
        from datetime import datetime, timezone
        from unittest.mock import patch

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_backfill_checkpoints (
                    symbol, timeframe, next_end_epoch, status,
                    batches_completed
                ) VALUES ('1HZ100V', 'M1', 3600, 'RUNNING', 2)
                """
            )

        class DeterministicDerivClient:
            def __init__(self, epochs):
                self.epochs = epochs
                self.assertions = None

            async def collect_candle_range(
                self, symbol, timeframe, start_epoch, end_epoch, max_candles
            ):
                self.assertions = (symbol, timeframe, start_epoch, end_epoch, max_candles)
                return [
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": datetime.fromtimestamp(
                            epoch, timezone.utc
                        ).isoformat(),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 0,
                        "source": "deriv",
                    }
                    for epoch in self.epochs
                ]

        incomplete_client = DeterministicDerivClient((3540, 3600))
        complete_client = DeterministicDerivClient((3480, 3540, 3600))
        with patch(
            "app.main.DerivPublicMarketDataClient",
            side_effect=[incomplete_client, complete_client],
        ):
            incomplete_run = self.admin_client.post(
                "/admin/market-backfills/1HZ100V/M1/run",
                json={"confirm_collection": True, "batch_size": 3},
            )
            self.assertEqual(incomplete_run.status_code, 200, incomplete_run.text)
            self.assertEqual(incomplete_run.json()["status"], "GAP_REVIEW")
            self.assertEqual(incomplete_run.json()["next_end_epoch"], 3600)
            self.assertFalse(incomplete_run.json()["backtest_eligible"])
            self.assertFalse(incomplete_run.json()["execution_triggered"])

            review = self.admin_client.post(
                "/admin/market-backfills/1HZ100V/M1/review",
                json={
                    "decision": "RETRY_SAME_RANGE",
                    "confirm_retry": True,
                    "rationale": "Reviewed source gap; retry this exact historical range.",
                },
            )
            self.assertEqual(review.status_code, 200)
            self.assertEqual(review.json()["next_end_epoch"], 3600)
            self.assertFalse(review.json()["cursor_advanced"])

            run = self.admin_client.post(
                "/admin/market-backfills/1HZ100V/M1/run",
                json={"confirm_collection": True, "batch_size": 3},
            )

        self.assertEqual(run.status_code, 200, run.text)
        body = run.json()
        self.assertEqual(body["inserted_count"], 1)
        self.assertEqual(body["duplicate_count"], 2)
        self.assertEqual(body["batches_completed"], 4)
        self.assertEqual(body["next_end_epoch"], 3420)
        self.assertTrue(body["read_only"])
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])
        expected_request = ("1HZ100V", "M1", 3480, 3600, 3)
        self.assertEqual(incomplete_client.assertions, expected_request)
        self.assertEqual(complete_client.assertions, expected_request)

        with database.get_connection() as connection:
            checkpoint = connection.execute(
                """
                SELECT next_end_epoch, status, batches_completed
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()
            observations = connection.execute(
                """
                SELECT COUNT(*) AS count FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()["count"]
            reviews = connection.execute(
                """
                SELECT COUNT(*) AS count FROM market_backfill_review_audit
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()["count"]
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["next_end_epoch"], 3420)
        self.assertEqual(checkpoint["batches_completed"], 4)
        self.assertEqual(observations, 3)
        self.assertEqual(reviews, 1)

    def test_exhausted_deriv_retries_recover_through_audited_same_range_admin_flow(self):
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock, patch
        from app.deriv_public_client import DerivPublicMarketDataClient

        class FailingSocketContext:
            async def __aenter__(self):
                raise OSError("temporary Deriv connection failure")

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class CompleteRangeClient:
            def __init__(self):
                self.request = None

            async def collect_candle_range(
                self, symbol, timeframe, start_epoch, end_epoch, max_candles
            ):
                self.request = (symbol, timeframe, start_epoch, end_epoch, max_candles)
                return [
                    {
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "timestamp": datetime.fromtimestamp(
                            epoch, timezone.utc
                        ).isoformat(),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 99.0,
                        "close": 100.5,
                        "volume": 0,
                        "source": "deriv",
                    }
                    for epoch in (3480, 3540, 3600)
                ]

        with database.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO market_backfill_checkpoints (
                    symbol, timeframe, next_end_epoch, status, batches_completed
                ) VALUES ('1HZ100V', 'M1', 3600, 'RUNNING', 0)
                """
            )

        real_client = DerivPublicMarketDataClient(timeout=1)
        recovered_client = CompleteRangeClient()
        run_path = "/admin/market-backfills/1HZ100V/M1/run"
        review_path = "/admin/market-backfills/1HZ100V/M1/review"

        with patch(
            "app.main.DerivPublicMarketDataClient",
            side_effect=[real_client, recovered_client],
        ), patch(
            "app.deriv_public_client.websockets.connect",
            side_effect=[
                FailingSocketContext(),
                FailingSocketContext(),
                FailingSocketContext(),
            ],
        ) as connect, patch(
            "app.deriv_public_client.asyncio.sleep", new_callable=AsyncMock
        ):
            failed = self.admin_client.post(
                run_path,
                json={"confirm_collection": True, "batch_size": 3},
            )

            self.assertEqual(failed.status_code, 502, failed.text)
            self.assertEqual(connect.call_count, 3)
            with database.get_connection() as connection:
                held = connection.execute(
                    """
                    SELECT next_end_epoch, status, batches_completed
                    FROM market_backfill_checkpoints
                    WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                    """
                ).fetchone()
                held_observations = connection.execute(
                    "SELECT COUNT(*) AS count FROM market_observations"
                ).fetchone()["count"]
            self.assertEqual(held["status"], "GAP_REVIEW")
            self.assertEqual(held["next_end_epoch"], 3600)
            self.assertEqual(held["batches_completed"], 0)
            self.assertEqual(held_observations, 0)

            review = self.admin_client.post(
                review_path,
                json={
                    "decision": "RETRY_SAME_RANGE",
                    "confirm_retry": True,
                    "rationale": "Transient provider outage reviewed; retry exact held range.",
                },
            )
            self.assertEqual(review.status_code, 200, review.text)
            self.assertEqual(review.json()["next_end_epoch"], 3600)
            self.assertFalse(review.json()["cursor_advanced"])

            recovered = self.admin_client.post(
                run_path,
                json={"confirm_collection": True, "batch_size": 3},
            )

        self.assertEqual(recovered.status_code, 200, recovered.text)
        body = recovered.json()
        self.assertEqual(
            recovered_client.request, ("1HZ100V", "M1", 3480, 3600, 3)
        )
        self.assertEqual(body["inserted_count"], 3)
        self.assertEqual(body["duplicate_count"], 0)
        self.assertEqual(body["batches_completed"], 1)
        self.assertEqual(body["next_end_epoch"], 3420)
        self.assertTrue(body["read_only"])
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["signals_generated"])
        self.assertFalse(body["execution_triggered"])

        with database.get_connection() as connection:
            checkpoint = connection.execute(
                """
                SELECT next_end_epoch, status, batches_completed
                FROM market_backfill_checkpoints
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()
            observation_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM market_observations
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()["count"]
            review_count = connection.execute(
                """
                SELECT COUNT(*) AS count FROM market_backfill_review_audit
                WHERE symbol = '1HZ100V' AND timeframe = 'M1'
                """
            ).fetchone()["count"]
        self.assertEqual(checkpoint["status"], "RUNNING")
        self.assertEqual(checkpoint["next_end_epoch"], 3420)
        self.assertEqual(checkpoint["batches_completed"], 1)
        self.assertEqual(observation_count, 3)
        self.assertEqual(review_count, 1)

    def test_admin_backfill_run_is_confirmed_bounded_and_read_only(self):
        from unittest.mock import AsyncMock, patch

        path = "/admin/market-backfills/1HZ100V/M1/run"
        payload = {"confirm_collection": True, "batch_size": 25}
        self.assertEqual(
            self.participant_client.post(path, json=payload).status_code, 403
        )
        for invalid in ("true", 1, None):
            response = self.admin_client.post(
                path, json={"confirm_collection": invalid, "batch_size": 25}
            )
            self.assertEqual(response.status_code, 422)
        for invalid_size in (1, 501, True, 2.5):
            response = self.admin_client.post(
                path, json={"confirm_collection": True, "batch_size": invalid_size}
            )
            self.assertEqual(response.status_code, 422)

        expected = {
            "symbol": "1HZ100V",
            "timeframe": "M1",
            "status": "RUNNING",
            "next_end_epoch": 3540,
            "received_count": 25,
            "inserted_count": 25,
            "duplicate_count": 0,
            "complete": False,
            "read_only": True,
            "execution_triggered": False,
            "signals_generated": False,
        }
        with patch(
            "app.main.run_backfill_batch_with_connection_factory",
            new_callable=AsyncMock,
            return_value=expected,
        ) as worker:
            response = self.admin_client.post(path, json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["received_count"], 25)
        self.assertTrue(body["operator_triggered"])
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["execution_triggered"])
        self.assertFalse(body["signals_generated"])
        worker.assert_awaited_once()
        args, kwargs = worker.await_args
        self.assertEqual(args[2:4], ("1HZ100V", "M1"))
        self.assertEqual(kwargs["batch_size"], 25)

    def test_admin_backfill_run_rejects_non_deriv_and_unsupported_timeframe(self):
        non_deriv = self.admin_client.post(
            "/admin/market-backfills/XAUUSD/M1/run",
            json={"confirm_collection": True, "batch_size": 10},
        )
        self.assertEqual(non_deriv.status_code, 400)
        unsupported = self.admin_client.post(
            "/admin/market-backfills/1HZ100V/M2/run",
            json={"confirm_collection": True, "batch_size": 10},
        )
        self.assertEqual(unsupported.status_code, 400)


    def test_backfill_review_history_is_admin_only_and_read_only(self):
        with database.get_connection() as connection:
            admin = connection.execute(
                "SELECT user_id FROM users WHERE email = ?",
                ("strategy-admin@taimobot.local",),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO market_backfill_review_audit (
                    review_id, symbol, timeframe, decision, rationale, decided_by
                ) VALUES (
                    'review_history_test', '1HZ100V', 'M1', 'RETRY_SAME_RANGE',
                    'Provider recovery was checked; retry the same interval.', ?
                )
                """,
                (admin["user_id"],),
            )
        path = "/admin/market-backfills/1HZ100V/M1/reviews"
        self.assertEqual(self.participant_client.get(path).status_code, 403)
        response = self.admin_client.get(path)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["reviews"][0]["decision"], "RETRY_SAME_RANGE")
        self.assertEqual(
            body["reviews"][0]["rationale"],
            "Provider recovery was checked; retry the same interval.",
        )
        self.assertFalse(body["backtest_eligible"])
        self.assertFalse(body["execution_triggered"])
