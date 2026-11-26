import unittest
from datetime import datetime, timezone

from app.market_health import evaluate_market_health


class MarketHealthEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
        self.evidence = {
            "provider_id": "trusted-xau-feed",
            "provider_version": "adapter-1.0.0",
            "instrument": "XAUUSD",
            "canonical_symbol": "XAUUSD",
            "checked_at": "2026-11-26T12:00:00+00:00",
            "connectivity_status": "CONNECTED",
            "latest_observation_at": "2026-11-26T11:59:00+00:00",
            "expected_interval_seconds": 60,
            "stale_after_seconds": 120,
            "heartbeat_stale_after_seconds": 60,
            "missing_candle_count": 0,
            "invalid_candle_count": 0,
            "future_candle_count": 0,
            "source_verified": True,
            "symbol_mapping_verified": True,
            "provenance_verified": True,
            "broker_specification_verified": True,
            "coverage_complete": True,
            "source_reference": "broker-feed-contract:v1",
            "evidence_id": "evidence-20261126-1200",
        }

    def evaluate(self, evidence=None, trusted="trusted-xau-feed"):
        return evaluate_market_health(
            self.evidence if evidence is None else evidence,
            trusted_provider_id=trusted,
            now=self.now,
        )

    def test_no_provider_is_not_implemented(self):
        result = evaluate_market_health(now=self.now)
        self.assertEqual(result["status"], "NOT_IMPLEMENTED")
        self.assertIsNone(result["observed_at"])
        self.assertFalse(result["execution_eligible"])

    def test_trusted_complete_fresh_xauusd_evidence_is_healthy_but_not_executable(self):
        result = self.evaluate()
        self.assertEqual(result["status"], "HEALTHY")
        self.assertFalse(result["execution_eligible"])
        self.assertFalse(result["signals_generated"])
        self.assertFalse(result["orders_submitted"])

    def test_deriv_synthetic_observation_cannot_satisfy_xauusd_gate(self):
        evidence = dict(self.evidence, provider_id="deriv", instrument="R_100", canonical_symbol="R_100")
        self.assertEqual(self.evaluate(evidence, trusted="deriv")["status"], "BLOCKED")

    def test_untrusted_provider_is_blocked(self):
        self.assertEqual(self.evaluate(trusted="different-provider")["status"], "BLOCKED")

    def test_unverified_source_mapping_provenance_or_broker_specs_are_blocked(self):
        for field in ("source_verified", "symbol_mapping_verified", "provenance_verified", "broker_specification_verified"):
            with self.subTest(field=field):
                evidence = dict(self.evidence, **{field: False})
                self.assertEqual(self.evaluate(evidence)["status"], "BLOCKED")

    def test_unreachable_provider_and_empty_data_are_offline(self):
        unreachable = dict(self.evidence, connectivity_status="DISCONNECTED")
        empty = dict(self.evidence, latest_observation_at=None)
        self.assertEqual(self.evaluate(unreachable)["status"], "OFFLINE")
        self.assertEqual(self.evaluate(empty)["status"], "OFFLINE")

    def test_expired_heartbeat_is_offline(self):
        evidence = dict(self.evidence, checked_at="2026-11-26T11:58:00+00:00", latest_observation_at="2026-11-26T11:57:00+00:00")
        self.assertEqual(self.evaluate(evidence)["status"], "OFFLINE")

    def test_stale_gaps_malformed_and_future_candles_are_degraded(self):
        cases = [
            dict(self.evidence, latest_observation_at="2026-11-26T11:55:00+00:00"),
            dict(self.evidence, missing_candle_count=1),
            dict(self.evidence, invalid_candle_count=1),
            dict(self.evidence, future_candle_count=1),
        ]
        for evidence in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(self.evaluate(evidence)["status"], "DEGRADED")

    def test_future_provider_timestamps_are_degraded(self):
        evidence = dict(self.evidence, latest_observation_at="2026-11-26T12:01:00+00:00")
        self.assertEqual(self.evaluate(evidence)["status"], "DEGRADED")

    def test_incomplete_coverage_is_degraded(self):
        evidence = dict(self.evidence, coverage_complete=False)
        self.assertEqual(self.evaluate(evidence)["status"], "DEGRADED")

    def test_missing_stable_evidence_identity_is_blocked(self):
        for field in ("provider_version", "evidence_id", "source_reference"):
            evidence = dict(self.evidence)
            evidence.pop(field)
            with self.subTest(field=field):
                self.assertEqual(self.evaluate(evidence)["status"], "BLOCKED")

    def test_naive_clock_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            evaluate_market_health(now=datetime(2026, 11, 26, 12, 0))

    def test_boolean_counters_and_zero_intervals_are_not_accepted(self):
        evidence = dict(self.evidence, missing_candle_count=True)
        self.assertEqual(self.evaluate(evidence)["status"], "DEGRADED")
        evidence = dict(self.evidence, expected_interval_seconds=0)
        self.assertEqual(self.evaluate(evidence)["status"], "DEGRADED")


if __name__ == "__main__":
    unittest.main()
