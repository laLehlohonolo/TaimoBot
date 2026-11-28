"""Run the focused broker-ingestion HTTP gateway security batch."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
TEST_FILE = BACKEND_ROOT / "tests" / "test_broker_bridge_ingestion_gateway.py"
spec = importlib.util.spec_from_file_location("broker_bridge_ingestion_gateway_tests", TEST_FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

CASES = [
    "test_connection_bucket_blocks_bursts_and_refills",
    "test_participant_bucket_limits_aggregate_connections",
    "test_rate_limit_buckets_are_isolated_between_participants",
    "test_audit_stores_only_redacted_allowlisted_metadata",
    "test_audit_is_immutable_except_for_privacy_anonymisation",
    "test_account_deletion_anonymises_audit_without_erasing_event",
    "test_unknown_audit_classification_is_rejected",
]


def main():
    suite = unittest.TestSuite(
        module.BrokerBridgeIngestionGatewayTests(name) for name in CASES
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != len(CASES):
        print(
            f"BROKER BRIDGE INGESTION GATEWAY: {result.testsRun}/{len(CASES)} "
            f"executed; failures={len(result.failures)}, errors={len(result.errors)}"
        )
        return 1
    print(f"TAIMOBOT BROKER BRIDGE INGESTION GATEWAY: {len(CASES)}/{len(CASES)} PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
