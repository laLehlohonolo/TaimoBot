
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.main import app
from app.execution import (
    create_paper_account,
    open_paper_trade,
    close_paper_trade,
    validate_symbol
)
from app.risk import (
    calculate_position_size,
    RiskError
)
from app.safety import (
    activate_emergency_stop,
    deactivate_emergency_stop
)


class PaperTradingTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"

        deactivate_emergency_stop()
        init_db()
        create_paper_account("TEST-001", 10000)

        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def open_trade(self, order_id, risk_percent=0.5, symbol="XAUUSD"):
        return open_paper_trade(
            account_id="TEST-001",
            client_order_id=order_id,
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=risk_percent,
            symbol=symbol
        )

    def test_one_percent_risk_calculation(self):
        result = calculate_position_size(
            account_balance=10000,
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1,
            side="BUY"
        )

        self.assertEqual(result["risk_amount"], 100)
        self.assertEqual(result["position_size"], 10)
        self.assertEqual(
            result["projected_total_open_risk"], 100
        )

    def test_risk_above_one_percent_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=3990,
                risk_percent=1.1,
                side="BUY"
            )

    def test_two_positions_can_share_one_percent_budget(self):
        first = self.open_trade("ORDER-001", 0.5)
        second = self.open_trade("ORDER-002", 0.5)

        self.assertEqual(first["risk_amount"], 50)
        self.assertEqual(second["risk_amount"], 50)
        self.assertEqual(second["total_open_risk"], 100)
        self.assertEqual(second["remaining_risk"], 0)

    def test_third_position_is_rejected_when_budget_is_full(self):
        self.open_trade("ORDER-001", 0.5)
        self.open_trade("ORDER-002", 0.5)

        with self.assertRaises(RiskError):
            self.open_trade("ORDER-003", 0.1)

    def test_intentional_additional_position_is_allowed(self):
        first = self.open_trade("ORDER-001", 0.25)
        second = self.open_trade("ORDER-002", 0.25)
        third = self.open_trade("ORDER-003", 0.25)
        fourth = self.open_trade("ORDER-004", 0.25)

        self.assertNotEqual(first["trade_id"], second["trade_id"])
        self.assertNotEqual(second["trade_id"], third["trade_id"])
        self.assertNotEqual(third["trade_id"], fourth["trade_id"])
        self.assertEqual(fourth["total_open_risk"], 100)

    def test_duplicate_order_is_rejected(self):
        self.open_trade("ORDER-DUPLICATE", 0.5)

        with self.assertRaises(ValueError):
            self.open_trade("ORDER-DUPLICATE", 0.5)

    def test_emergency_stop_blocks_new_orders(self):
        activate_emergency_stop()

        with self.assertRaises(RuntimeError):
            self.open_trade("ORDER-STOPPED", 0.5)

    def test_invalid_buy_stop_loss_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=4010,
                risk_percent=0.5,
                side="BUY"
            )

    def test_closing_trade_releases_risk_budget(self):
        first = self.open_trade("ORDER-001", 0.5)
        self.open_trade("ORDER-002", 0.5)

        close_result = close_paper_trade(
            trade_id=first["trade_id"],
            exit_price=4000
        )

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(close_result["realized_pnl"], 0)
        self.assertEqual(close_result["remaining_open_risk"], 50)

        third = self.open_trade("ORDER-003", 0.5)
        self.assertEqual(third["total_open_risk"], 100)

    def test_trade_closing_updates_account_balance(self):
        trade = self.open_trade("ORDER-CLOSE", 1.0)

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["realized_pnl"], 100)
        self.assertEqual(result["new_balance"], 10100)
        self.assertEqual(result["remaining_open_risk"], 0)

    def test_open_risk_is_account_specific(self):
        self.open_trade("ORDER-001", 0.5)
        create_paper_account("TEST-002", 10000)

        second_account_trade = open_paper_trade(
            account_id="TEST-002",
            client_order_id="ORDER-ACCOUNT-2",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1.0,
            symbol="EURUSD"
        )

        self.assertEqual(
            second_account_trade["total_open_risk"], 100
        )

    def test_account_risk_endpoint_reports_open_risk(self):
        self.open_trade("ORDER-API-001", 0.5)
        self.open_trade("ORDER-API-002", 0.5)

        response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["account_id"], "TEST-001")
        self.assertEqual(result["account_balance"], 10000)
        self.assertEqual(result["open_positions"], 2)
        self.assertEqual(result["current_open_risk"], 100)
        self.assertEqual(result["maximum_risk_percent"], 1.0)
        self.assertEqual(result["maximum_risk_amount"], 100)
        self.assertEqual(result["remaining_risk_budget"], 0)
        self.assertTrue(result["risk_limit_reached"])

    def test_account_risk_endpoint_reports_unused_budget(self):
        self.open_trade("ORDER-API-003", 0.25)

        response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["open_positions"], 1)
        self.assertEqual(result["current_open_risk"], 25)
        self.assertEqual(result["remaining_risk_budget"], 75)
        self.assertFalse(result["risk_limit_reached"])

    def test_account_risk_endpoint_rejects_unknown_account(self):
        response = self.client.get("/accounts/UNKNOWN/risk")
        self.assertEqual(response.status_code, 404)

    def test_symbol_is_normalized_to_uppercase(self):
        self.assertEqual(validate_symbol("eurusd"), "EURUSD")

    def test_symbol_with_supported_separator_is_valid(self):
        self.assertEqual(validate_symbol("EUR/USD"), "EUR/USD")

    def test_empty_symbol_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_symbol("")

    def test_invalid_symbol_characters_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_symbol("EUR USD!")

    def test_symbol_length_is_limited(self):
        with self.assertRaises(ValueError):
            validate_symbol("A" * 31)

    def test_different_instruments_can_open_separate_positions(self):
        eur = self.open_trade("ORDER-EUR", 0.5, "EURUSD")
        gold = self.open_trade("ORDER-GOLD", 0.5, "XAUUSD")

        self.assertEqual(eur["symbol"], "EURUSD")
        self.assertEqual(gold["symbol"], "XAUUSD")
        self.assertNotEqual(eur["trade_id"], gold["trade_id"])
        self.assertEqual(gold["total_open_risk"], 100)

    def test_different_instruments_share_one_risk_limit(self):
        self.open_trade("ORDER-EUR", 0.5, "EURUSD")
        self.open_trade("ORDER-GOLD", 0.5, "XAUUSD")

        with self.assertRaises(RiskError):
            self.open_trade("ORDER-BTC", 0.1, "BTCUSD")

    def test_api_accepts_a_valid_instrument_symbol(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-EUR-001",
                "symbol": "EURUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["symbol"], "EURUSD")

    def test_api_rejects_invalid_symbol_format(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-BAD-001",
                "symbol": "EUR USD!",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )

        self.assertEqual(response.status_code, 422)

    def test_closing_trade_returns_its_instrument_symbol(self):
        trade = self.open_trade("ORDER-CLOSE-SYMBOL", 0.5, "GBPUSD")

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4000
        )

        self.assertEqual(result["symbol"], "GBPUSD")
        self.assertEqual(result["status"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
