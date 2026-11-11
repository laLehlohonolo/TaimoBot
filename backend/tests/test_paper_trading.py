
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
from app.risk import calculate_position_size, RiskError
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

    def test_risk_above_one_percent_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=3990,
                risk_percent=1.1,
                side="BUY"
            )

    def test_invalid_buy_stop_loss_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=4010,
                risk_percent=1,
                side="BUY"
            )

    def test_duplicate_order_is_rejected(self):
        arguments = {
            "account_id": "TEST-001",
            "client_order_id": "ORDER-001",
            "side": "BUY",
            "entry_price": 4000,
            "stop_loss": 3990,
            "risk_percent": 1
        }

        open_paper_trade(**arguments)

        with self.assertRaises(ValueError):
            open_paper_trade(**arguments)

    def test_emergency_stop_blocks_new_orders(self):
        activate_emergency_stop()

        with self.assertRaises(RuntimeError):
            open_paper_trade(
                account_id="TEST-001",
                client_order_id="ORDER-002",
                side="BUY",
                entry_price=4000,
                stop_loss=3990,
                risk_percent=1
            )

    def test_trade_closing_updates_balance(self):
        trade = open_paper_trade(
            account_id="TEST-001",
            client_order_id="ORDER-003",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1
        )

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["realized_pnl"], 100)
        self.assertEqual(result["new_balance"], 10100)


if __name__ == "__main__":
    unittest.main()
