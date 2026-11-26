import unittest

from app.market_provider_registry import (
    PROVIDER_ID_ENV,
    TRUST_ENV_FIELDS,
    load_market_provider_configuration,
)


class MarketProviderRegistryTests(unittest.TestCase):
    def test_empty_environment_has_no_provider_and_no_trust(self):
        result = load_market_provider_configuration({})
        self.assertIsNone(result["provider_id"])
        self.assertEqual(result["configuration_status"], "NOT_CONFIGURED")
        self.assertIsNone(result["trusted_configuration"])
        self.assertEqual(result["evidence_adapter_status"], "NOT_IMPLEMENTED")
        self.assertFalse(result["execution_eligible"])
        self.assertFalse(result["signals_generated"])
        self.assertFalse(result["orders_submitted"])

    def test_provider_identity_alone_does_not_enable_any_verification(self):
        result = load_market_provider_configuration({PROVIDER_ID_ENV: "  xau-feed  "})
        self.assertEqual(result["provider_id"], "xau-feed")
        self.assertEqual(result["configuration_status"], "CONFIGURED")
        self.assertEqual(
            result["trusted_configuration"],
            {
                "provider_id": "xau-feed",
                "source_verified": False,
                "symbol_mapping_verified": False,
                "provenance_verified": False,
                "broker_specification_verified": False,
            },
        )
        self.assertEqual(result["evidence_adapter_status"], "NOT_IMPLEMENTED")

    def test_only_explicit_server_true_values_enable_verification_flags(self):
        environment = {PROVIDER_ID_ENV: "xau-feed"}
        for field, env_key in TRUST_ENV_FIELDS.items():
            environment[env_key] = " true "
        result = load_market_provider_configuration(environment)
        self.assertTrue(all(result["trusted_configuration"][field] is True for field in TRUST_ENV_FIELDS))

    def test_malformed_or_false_flags_fail_closed(self):
        environment = {PROVIDER_ID_ENV: "xau-feed"}
        for index, env_key in enumerate(TRUST_ENV_FIELDS.values()):
            environment[env_key] = ("yes", "1", "false", "")[index]
        result = load_market_provider_configuration(environment)
        self.assertTrue(all(result["trusted_configuration"][field] is False for field in TRUST_ENV_FIELDS))

    def test_blank_provider_id_is_not_registered_even_with_true_flags(self):
        environment = {PROVIDER_ID_ENV: " "}
        environment.update({key: "true" for key in TRUST_ENV_FIELDS.values()})
        result = load_market_provider_configuration(environment)
        self.assertIsNone(result["provider_id"])
        self.assertIsNone(result["trusted_configuration"])


if __name__ == "__main__":
    unittest.main()
