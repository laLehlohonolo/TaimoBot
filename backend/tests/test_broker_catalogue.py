import unittest

from app.broker_catalogue import BROKER_CATALOGUE_VERSION, list_broker_catalogue


class BrokerCatalogueTests(unittest.TestCase):
    def test_catalogue_contains_multiple_independent_broker_candidates(self):
        result = list_broker_catalogue()
        ids = {item["broker_id"] for item in result["brokers"]}
        self.assertEqual(
            ids,
            {"exness", "avatrade", "vantage", "fp_markets", "tickmill", "hfm"},
        )
        self.assertEqual(result["catalogue_version"], BROKER_CATALOGUE_VERSION)

    def test_candidates_are_not_misrepresented_as_connected_or_supported(self):
        result = list_broker_catalogue()
        for broker in result["brokers"]:
            self.assertEqual(broker["integration_status"], "CANDIDATE_ONLY")
            self.assertEqual(broker["connection_status"], "NOT_AVAILABLE")
            self.assertEqual(broker["regional_entity_review"], "REQUIRED")
            self.assertFalse(broker["execution_enabled"])
            self.assertEqual(broker["supported_instruments"], [])

    def test_catalogue_is_paper_only_and_read_only(self):
        result = list_broker_catalogue()
        self.assertEqual(result["mode"], "paper_trading")
        self.assertTrue(result["read_only"])
        self.assertFalse(result["live_order_routing"])

    def test_catalogue_returns_detached_records(self):
        first = list_broker_catalogue()
        first["brokers"][0]["platform_candidates"].append("INJECTED")
        second = list_broker_catalogue()
        self.assertNotIn("INJECTED", second["brokers"][0]["platform_candidates"])


if __name__ == "__main__":
    unittest.main()
