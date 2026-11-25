import unittest

from app.deriv_market_data import (
    DERIV_SYNTHETIC_SYMBOLS,
    normalize_active_symbols_response,
    normalize_deriv_candle,
)


class DerivMarketDataAdapterTests(unittest.TestCase):
    def active_record(self, symbol="1HZ100V"):
        return {
            "underlying_symbol": symbol,
            "underlying_symbol_name": "Volatility 100 (1s) Index",
            "underlying_symbol_type": "synthetic_index",
            "market": "synthetic_index",
            "subgroup": "synthetics",
            "submarket": "random_index",
            "pip_size": 2,
            "exchange_is_open": 1,
            "is_trading_suspended": 0,
            "trade_count": 0,
        }

    def test_active_symbols_returns_only_known_deriv_synthetics(self):
        payload = {
            "msg_type": "active_symbols",
            "active_symbols": [
                self.active_record("1HZ100V"),
                self.active_record("R_100"),
                self.active_record("EURUSD"),
            ],
        }
        result = normalize_active_symbols_response(payload)
        self.assertEqual({item["symbol"] for item in result}, {"1HZ100V", "R_100"})
        self.assertTrue(all(item["execution_eligible"] is False for item in result))
        self.assertTrue(all(item["execution_model"] == "NOT_CONFIGURED" for item in result))

    def test_all_catalogued_synthetic_symbols_are_in_provider_filter(self):
        self.assertEqual(DERIV_SYNTHETIC_SYMBOLS, {
            "R_10", "R_25", "R_50", "R_75", "R_100",
            "1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V",
            "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V",
        })

    def test_rejects_wrong_response_type_or_missing_symbol_list(self):
        for payload in ({}, {"msg_type": "tick", "active_symbols": []},
                        {"msg_type": "active_symbols"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    normalize_active_symbols_response(payload)

    def test_rejects_incomplete_or_invalid_provider_metadata(self):
        for field, value in (("pip_size", 0), ("market", ""), ("exchange_is_open", 2)):
            record = self.active_record()
            record[field] = value
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    normalize_active_symbols_response({
                        "msg_type": "active_symbols", "active_symbols": [record]
                    })

    def test_active_symbols_requires_non_negative_trade_count(self):
        for field, value in (("trade_count", None), ("trade_count", -1),
                             ("trade_count", True)):
            record = self.active_record()
            if value is None:
                record.pop(field)
            else:
                record[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    normalize_active_symbols_response({
                        "msg_type": "active_symbols", "active_symbols": [record]
                    })

    def test_missing_symbol_type_is_preserved_as_incomplete_not_inferred(self):
        for missing_value in (None, ""):
            record = self.active_record()
            if missing_value is None:
                record.pop("underlying_symbol_type")
            else:
                record["underlying_symbol_type"] = missing_value
            result = normalize_active_symbols_response({
                "msg_type": "active_symbols", "active_symbols": [record]
            })[0]
            self.assertIsNone(result["underlying_symbol_type"])
            self.assertEqual(
                result["provider_metadata_status"], "INCOMPLETE_PROVIDER_RESPONSE"
            )
            self.assertEqual(
                result["missing_provider_metadata"], ["underlying_symbol_type"]
            )
            self.assertFalse(result["execution_eligible"])

    def test_incomplete_required_text_metadata_reports_symbol_and_missing_fields(self):
        record = self.active_record("1HZ100V")
        record.pop("subgroup")
        record["underlying_symbol_name"] = "  "
        with self.assertRaisesRegex(
            ValueError,
            r"1HZ100V: missing or blank fields: subgroup, underlying_symbol_name",
        ):
            normalize_active_symbols_response({
                "msg_type": "active_symbols", "active_symbols": [record]
            })

    def test_deriv_candle_converts_epoch_and_marks_unverified(self):
        result = normalize_deriv_candle("1HZ100V", {
            "epoch": 1795256100, "open": 1000, "high": 1002,
            "low": 999, "close": 1001,
        }, "M1")
        self.assertEqual(result["symbol"], "1HZ100V")
        self.assertTrue(result["timestamp"].endswith("+00:00"))
        self.assertEqual(result["provenance_status"], "UNVERIFIED_EXTERNAL_INPUT")
        self.assertFalse(result["execution_eligible"])

    def test_rejects_invalid_candle_epoch_and_unknown_symbol(self):
        with self.assertRaises(ValueError):
            normalize_deriv_candle("1HZ100V", {"epoch": float("nan")}, "M1")
        with self.assertRaises(ValueError):
            normalize_deriv_candle("USDJPY", {"epoch": 1}, "M1")


if __name__ == "__main__":
    unittest.main()
