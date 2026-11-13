
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.main import app
from app.execution import create_paper_account
from app.safety import deactivate_emergency_stop


class PositionGroupTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "groups.db"

        deactivate_emergency_stop()
        init_db()
        create_paper_account("GROUP-001", 10000)

        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        deactivate_emergency_stop()
        database.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def create_group(self, symbol="XAUUSD", side="BUY"):
        response = self.client.post(
            "/accounts/GROUP-001/groups",
            json={
                "symbol": symbol,
                "side": side
            }
        )

        self.assertEqual(response.status_code, 200)
        return response.json()["group_id"]

    def open_group_trade(
        self,
        group_id,
        order_id,
        symbol="XAUUSD",
        side="BUY",
        entry=4000,
        stop=3990
    ):
        return self.client.post(
            "/trades",
            json={
                "account_id": "GROUP-001",
                "client_order_id": order_id,
                "symbol": symbol,
                "side": side,
                "entry_price": entry,
                "stop_loss": stop,
                "risk_percent": 0.5,
                "position_group_id": group_id
            }
        )

    def test_api_creates_position_group(self):
        group_id = self.create_group()

        response = self.client.get(
            f"/groups/{group_id}"
        )

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["group_id"], group_id)
        self.assertEqual(result["account_id"], "GROUP-001")
        self.assertEqual(result["symbol"], "XAUUSD")
        self.assertEqual(result["side"], "BUY")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["total_positions"], 0)

    def test_trade_can_be_opened_inside_group(self):
        group_id = self.create_group()

        response = self.open_group_trade(
            group_id,
            "GROUP-ORDER-001"
        )

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(
            result["position_group_id"], group_id
        )
        self.assertEqual(result["risk_amount"], 50)
        self.assertEqual(result["status"], "OPEN")

    def test_group_reports_combined_position_statistics(self):
        group_id = self.create_group()

        first = self.open_group_trade(
            group_id,
            "GROUP-ORDER-001"
        )
        second = self.open_group_trade(
            group_id,
            "GROUP-ORDER-002"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)

        response = self.client.get(
            f"/groups/{group_id}"
        )

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["total_positions"], 2)
        self.assertEqual(result["open_positions"], 2)
        self.assertEqual(result["closed_positions"], 0)
        self.assertEqual(result["combined_open_risk"], 100)
        self.assertEqual(result["combined_quantity"], 10)
        self.assertEqual(result["weighted_average_entry"], 4000)

    def test_group_cannot_exceed_account_risk_limit(self):
        group_id = self.create_group()

        first = self.open_group_trade(
            group_id,
            "GROUP-ORDER-001"
        )
        second = self.open_group_trade(
            group_id,
            "GROUP-ORDER-002"
        )
        third = self.open_group_trade(
            group_id,
            "GROUP-ORDER-003"
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 400)

        response = self.client.get(
            "/accounts/GROUP-001/risk"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["current_open_risk"], 100
        )

    def test_group_rejects_wrong_instrument(self):
        group_id = self.create_group(symbol="XAUUSD")

        response = self.open_group_trade(
            group_id,
            "GROUP-WRONG-SYMBOL",
            symbol="EURUSD"
        )

        self.assertEqual(response.status_code, 400)

    def test_group_rejects_wrong_direction(self):
        group_id = self.create_group(side="BUY")

        response = self.open_group_trade(
            group_id,
            "GROUP-WRONG-SIDE",
            side="SELL"
        )

        self.assertEqual(response.status_code, 400)

    def test_group_cannot_be_used_by_another_account(self):
        group_id = self.create_group()
        create_paper_account("GROUP-002", 10000)

        response = self.client.post(
            "/trades",
            json={
                "account_id": "GROUP-002",
                "client_order_id": "OTHER-ACCOUNT-ORDER",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5,
                "position_group_id": group_id
            }
        )

        self.assertEqual(response.status_code, 400)

    def test_group_close_closes_all_positions_atomically(self):
        group_id = self.create_group()

        self.open_group_trade(
            group_id,
            "GROUP-CLOSE-001"
        )
        self.open_group_trade(
            group_id,
            "GROUP-CLOSE-002"
        )

        response = self.client.post(
            f"/groups/{group_id}/close",
            json={"exit_price": 4010}
        )

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["closed_positions"], 2)
        self.assertEqual(result["realized_pnl"], 100)
        self.assertEqual(result["new_balance"], 10100)
        self.assertEqual(result["remaining_open_risk"], 0)

        group_response = self.client.get(
            f"/groups/{group_id}"
        )

        group = group_response.json()

        self.assertEqual(group["status"], "CLOSED")
        self.assertEqual(group["open_positions"], 0)
        self.assertEqual(group["closed_positions"], 2)
        self.assertEqual(group["realized_pnl"], 100)

    def test_closed_group_cannot_accept_new_positions(self):
        group_id = self.create_group()

        self.open_group_trade(
            group_id,
            "GROUP-CLOSE-003"
        )

        close_response = self.client.post(
            f"/groups/{group_id}/close",
            json={"exit_price": 4000}
        )

        self.assertEqual(close_response.status_code, 200)

        response = self.open_group_trade(
            group_id,
            "GROUP-AFTER-CLOSE"
        )

        self.assertEqual(response.status_code, 400)

    def test_unknown_group_returns_not_found(self):
        response = self.client.get(
            "/groups/UNKNOWN-GROUP"
        )

        self.assertEqual(response.status_code, 404)

    def test_emergency_stop_blocks_group_positions(self):
        group_id = self.create_group()

        self.client.post("/safety/emergency-stop")

        response = self.open_group_trade(
            group_id,
            "GROUP-STOPPED-ORDER"
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
