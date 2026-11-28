"""Run the focused broker evidence-ingestion service regression batch."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_broker_bridge_ingestion_service.py"
spec = importlib.util.spec_from_file_location("broker_bridge_ingestion_service_tests", TEST_FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_valid_heartbeat_is_authenticated_and_persisted_read_only",
    "test_participant_identity_cannot_be_supplied_in_payload",
    "test_server_database_ownership_is_persisted",
    "test_oversized_body_is_rejected_before_vault_access",
    "test_non_json_content_type_is_rejected_before_vault_access",
    "test_invalid_utf8_and_malformed_json_are_rejected",
    "test_duplicate_json_fields_are_rejected",
    "test_non_finite_json_numbers_are_rejected",
    "test_stale_and_future_provider_timestamps_are_rejected",
    "test_untrusted_connection_selector_cannot_reuse_another_key",
    "test_vault_failure_is_redacted_and_fails_closed",
    "test_replay_is_rejected_by_persistent_nonce_store",
    "test_identical_evidence_is_idempotent_across_fresh_requests",
]


def main():
    suite = unittest.TestSuite(
        module.BrokerBridgeIngestionServiceTests(name) for name in CASES
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != len(CASES):
        print(
            f"BROKER BRIDGE INGESTION SERVICE: {result.testsRun}/{len(CASES)} "
            f"executed; failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print(f"TAIMOBOT BROKER BRIDGE INGESTION SERVICE: {len(CASES)}/{len(CASES)} PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
