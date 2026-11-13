
import tempfile
import unittest
from pathlib import Path

from app import database
from app.database import init_db
from app.execution import (
    create_paper_account,
    open_paper_trade,
    close_paper_trade
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

    def tearDown(self):
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def open_trade(self, order_id, risk_percent=0.5):
        return open_paper_trade(
            account_id="TEST-001",
            client_order_id=order_id,
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=risk_percent
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
            risk_percent=1.0
        )

        self.assertEqual(
            second_account_trade["total_open_risk"], 100
        )


if __name__ == "__main__":
    unittest.main()
