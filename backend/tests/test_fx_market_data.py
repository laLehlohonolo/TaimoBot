"""Tests for the provider-neutral FX quote adapter boundary."""
import unittest
from datetime import datetime, timezone

from app.fx_market_data import load_validated_fx_quote


class FakeFXAdapter:
    provider_name = "TestFX"

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.requested_pair = None

    def get_quote(self, pair):
        self.requested_pair = pair
        return self.snapshot


class FXMarketDataAdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
        self.snapshot = {
            "provider": "TestFX",
            "pair": "USD/ZAR",
            "base_currency": "USD",
            "quote_currency": "ZAR",
            "bid": "18.49",
            "ask": "18.51",
            "observed_at": "2026-11-23T09:59:30+00:00",
            "source_reference": "fixture://usd-zar/quote-001",
        }

    def test_adapter_receives_normalized_pair_and_returns_validated_ask(self):
        adapter = FakeFXAdapter(self.snapshot)
        result = load_validated_fx_quote(
            adapter, "TestFX", "usd", "zar", now=self.now
        )
        self.assertEqual(adapter.requested_pair, "USD/ZAR")
        self.assertEqual(result["conversion_rate"], 18.51)
        self.assertEqual(result["conversion_side"], "ASK")
        self.assertEqual(result["mode"], "fx_conversion_validation_only")

    def test_wrong_adapter_identity_fails_before_quote_request(self):
        adapter = FakeFXAdapter(self.snapshot)
        with self.assertRaisesRegex(ValueError, "adapter identity"):
            load_validated_fx_quote(
                adapter, "OtherFX", "USD", "ZAR", now=self.now
            )
        self.assertIsNone(adapter.requested_pair)

    def test_stale_quote_fails_closed(self):
        stale = dict(self.snapshot, observed_at="2026-11-23T09:50:00+00:00")
        with self.assertRaisesRegex(ValueError, "stale"):
            load_validated_fx_quote(
                FakeFXAdapter(stale), "TestFX", "USD", "ZAR",
                now=self.now, max_age_seconds=300
            )

    def test_reversed_pair_fails_closed(self):
        reversed_quote = dict(
            self.snapshot,
            pair="ZAR/USD",
            base_currency="ZAR",
            quote_currency="USD",
        )
        with self.assertRaisesRegex(ValueError, "base currency"):
            load_validated_fx_quote(
                FakeFXAdapter(reversed_quote), "TestFX", "USD", "ZAR",
                now=self.now
            )

    def test_missing_timestamp_or_source_reference_fails_closed(self):
        for field in ("observed_at", "source_reference"):
            malformed = dict(self.snapshot)
            malformed.pop(field)
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    load_validated_fx_quote(
                        FakeFXAdapter(malformed), "TestFX", "USD", "ZAR",
                        now=self.now
                    )

    def test_adapter_without_required_interface_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "provider_name"):
            load_validated_fx_quote(
                object(), "TestFX", "USD", "ZAR", now=self.now
            )

    def test_non_mapping_provider_response_is_rejected(self):
        class InvalidAdapter:
            provider_name = "TestFX"

            def get_quote(self, pair):
                return "18.51"

        with self.assertRaisesRegex(ValueError, "snapshot object"):
            load_validated_fx_quote(
                InvalidAdapter(), "TestFX", "USD", "ZAR", now=self.now
            )


if __name__ == "__main__":
    unittest.main()
