"""Run thirty-six broker-bridge provisioning, lease, vault, and authentication checks sequentially."""
import importlib.util
import sys
import unittest
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def load_module(module_name, relative_path):
    test_file = BACKEND_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, test_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keys_module = load_module("broker_bridge_key_regression_tests", Path("tests") / "test_broker_bridge_keys.py")
store_module = load_module("broker_bridge_lease_regression_tests", Path("tests") / "test_broker_bridge_provisioning_store.py")
ingestion_module = load_module("broker_bridge_ingestion_regression_tests", Path("tests") / "test_broker_bridge_ingestion_primitives.py")
vault_module = load_module("broker_bridge_vault_regression_tests", Path("tests") / "test_broker_bridge_vault.py")

KEY_CASES = [
    "test_activation_returns_only_non_secret_metadata",
    "test_rotation_atomically_revokes_previous_key",
    "test_revocation_is_connection_scoped_and_idempotent",
    "test_paused_or_unknown_connection_cannot_receive_a_key",
    "test_invalid_secret_reference_and_naive_time_fail_closed",
    "test_unknown_or_unverified_non_admin_provisioner_is_rejected",
    "test_auth_context_is_server_resolved_and_never_returns_key_bytes",
    "test_auth_context_rejects_unknown_revoked_and_paused_keys",
    "test_provisioning_generates_256_bit_secret_without_returning_it",
    "test_ambiguous_vault_timeout_recovers_with_same_durable_operation_id",
    "test_operation_id_cannot_be_reused_for_another_connection_or_actor",
    "test_provisioning_requires_valid_operation_id",
    "test_fake_secret_manager_reuses_reference_for_same_idempotency_key",
    "test_provisioning_compensates_if_connection_is_paused_during_vault_write",
    "test_provisioning_rejects_participant_before_vault_write",
    "test_provisioning_rejects_unknown_connection_before_vault_write",
    "test_provisioning_reports_failed_vault_compensation_without_leaking_ref",
    "test_vault_creation_failure_fails_closed",
]

LEASE_CASES = [
    "test_only_one_worker_can_claim_activation",
    "test_expired_activation_lease_can_be_reclaimed",
    "test_terminal_state_cannot_be_claimed",
]

INGESTION_CASES = [
    "test_valid_signature_is_accepted_without_order_capability",
    "test_modified_body_and_wrong_secret_are_rejected",
    "test_stale_timestamp_is_rejected_before_nonce_consumption",
    "test_nonce_replay_is_rejected_persistently",
    "test_concurrent_replay_allows_exactly_one_request",
    "test_connection_identity_and_state_are_enforced",
    "test_key_must_belong_to_the_connection_being_authenticated",
    "test_revoked_or_rotated_key_is_rejected_after_secret_was_loaded",
    "test_evidence_ownership_is_derived_from_connection_record",
]


VAULT_CASES = [
    "test_reads_only_server_resolved_reference_and_returns_no_cache",
    "test_unknown_or_revoked_key_fails_before_vault_read",
    "test_vault_outage_fails_closed_without_leaking_adapter_error",
    "test_short_or_non_bytes_secret_fails_closed",
    "test_rotation_or_revocation_during_vault_read_fails_closed",
    "test_untrusted_or_malformed_vault_reference_fails_before_read",
]


def main():
    suite = unittest.TestSuite()
    suite.addTests(keys_module.BrokerBridgeKeyMetadataTests(name) for name in KEY_CASES)
    suite.addTests(store_module.ProvisioningActivationLeaseTests(name) for name in LEASE_CASES)
    suite.addTests(ingestion_module.BrokerBridgeIngestionPrimitiveTests(name) for name in INGESTION_CASES)
    suite.addTests(vault_module.BrokerBridgeVaultBoundaryTests(name) for name in VAULT_CASES)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful() or result.testsRun != 36:
        print(f"BROKER BRIDGE SECURITY BATCH: {result.testsRun}/36 executed; failures={len(result.failures)}, errors={len(result.errors)}")
        return 1
    print("TAIMOBOT BROKER BRIDGE SECURITY BATCH: 36/36 PASSED SEQUENTIALLY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
