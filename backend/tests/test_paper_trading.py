
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path

from fastapi.testclient import TestClient

from app import database
from app.database import init_db
from app.auth import register_user, create_session
from app.main import app
from app.execution import (
    create_paper_account,
    open_paper_trade,
    close_paper_trade,
    close_position_group,
    validate_symbol
)
from app.risk import (
    calculate_position_size,
    get_risk_profile,
    RiskError
)
from app.safety import (
    activate_emergency_stop,
    deactivate_emergency_stop
)


class PaperTradingTests(unittest.TestCase):

    def setUp(self):
        # Keep ordinary trading tests independent of the wall clock.
        # The maintenance-window block itself is tested with an explicit clock override.
        self.maintenance_clock = patch("app.safety.is_maintenance_window", return_value=False)
        self.maintenance_clock.start()
        self.admin_env = patch.dict(
            "os.environ",
            {"TAIMOBOT_ADMIN_EMAILS": "paper-tests@taimobot.local"}
        )
        self.admin_env.start()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = database.DB_PATH
        database.DB_PATH = Path(self.temp_dir.name) / "test.db"

        init_db()
        deactivate_emergency_stop()
        user = register_user("paper-tests@taimobot.local", "TestPassphrase-2026-Secure!")
        self.user_id = user["user_id"]
        session = create_session(self.user_id)
        create_paper_account("TEST-001", 10000, owner_user_id=self.user_id)

        self.client = TestClient(app)
        self.client.headers.update({"Authorization": "Bearer " + session["access_token"]})

    def tearDown(self):
        self.maintenance_clock.stop()
        self.client.close()
        self.admin_env.stop()
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

    def test_first_successful_paper_trade_records_first_trade_milestone_once(self):
        with database.get_connection() as connection:
            before = connection.execute(
                "SELECT first_trade_at FROM participant_milestones WHERE user_id = ?",
                (self.user_id,)
            ).fetchone()["first_trade_at"]
        self.assertIsNone(before)

        self.open_trade("MILESTONE-FIRST-TRADE")
        with database.get_connection() as connection:
            first = connection.execute(
                "SELECT first_trade_at FROM participant_milestones WHERE user_id = ?",
                (self.user_id,)
            ).fetchone()["first_trade_at"]
        self.assertIsNotNone(first)

        self.open_trade("MILESTONE-SECOND-TRADE")
        with database.get_connection() as connection:
            second = connection.execute(
                "SELECT first_trade_at FROM participant_milestones WHERE user_id = ?",
                (self.user_id,)
            ).fetchone()["first_trade_at"]
        self.assertEqual(first, second)

    def test_migration_recovers_legacy_participant_first_trade_history(self):
        self.open_trade("LEGACY-MILESTONE-FIRST")
        self.open_trade("LEGACY-MILESTONE-SECOND")

        with database.get_connection() as connection:
            expected_first_trade = connection.execute(
                """
                SELECT MIN(COALESCE(opened_at, created_at))
                FROM trades
                WHERE account_id = 'TEST-001'
                  AND status IN ('OPEN', 'CLOSED')
                """
            ).fetchone()[0]
            connection.execute(
                "UPDATE users SET created_at = '2016-11-16 09:00:00' WHERE user_id = ?",
                (self.user_id,)
            )
            connection.execute(
                "DELETE FROM participant_milestones WHERE user_id = ?",
                (self.user_id,)
            )

            database.migrate_participant_milestones(connection)
            migrated = connection.execute(
                """
                SELECT joined_at, first_trade_at
                FROM participant_milestones
                WHERE user_id = ?
                """,
                (self.user_id,)
            ).fetchone()

        self.assertEqual(migrated["joined_at"], "2016-11-16 09:00:00")
        self.assertEqual(migrated["first_trade_at"], expected_first_trade)

        # Re-running the migration must preserve the original milestones.
        with database.get_connection() as connection:
            database.migrate_participant_milestones(connection)
            repeated = connection.execute(
                """
                SELECT joined_at, first_trade_at
                FROM participant_milestones
                WHERE user_id = ?
                """,
                (self.user_id,)
            ).fetchone()

        self.assertEqual(repeated["joined_at"], migrated["joined_at"])
        self.assertEqual(repeated["first_trade_at"], migrated["first_trade_at"])

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

    def test_valid_position_sizes_respect_risk_and_stop_distance(self):
        cases = [
            {
                "name": "quarter_percent_buy",
                "account_balance": 10000,
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.25,
                "side": "BUY",
                "current_open_risk": 0,
                "expected_risk": 25,
                "expected_size": 2.5,
                "expected_projected": 25,
                "expected_remaining": 75,
            },
            {
                "name": "quarter_percent_sell",
                "account_balance": 10000,
                "entry_price": 4000,
                "stop_loss": 4010,
                "risk_percent": 0.25,
                "side": "SELL",
                "current_open_risk": 0,
                "expected_risk": 25,
                "expected_size": 2.5,
                "expected_projected": 25,
                "expected_remaining": 75,
            },
            {
                "name": "wider_stop_reduces_quantity",
                "account_balance": 10000,
                "entry_price": 4000,
                "stop_loss": 3980,
                "risk_percent": 0.5,
                "side": "BUY",
                "current_open_risk": 0,
                "expected_risk": 50,
                "expected_size": 2.5,
                "expected_projected": 50,
                "expected_remaining": 50,
            },
            {
                "name": "partial_risk_budget",
                "account_balance": 10000,
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5,
                "side": "BUY",
                "current_open_risk": 40,
                "expected_risk": 50,
                "expected_size": 5,
                "expected_projected": 90,
                "expected_remaining": 10,
            },
            {
                "name": "exactly_uses_remaining_risk_budget",
                "account_balance": 10000,
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5,
                "side": "BUY",
                "current_open_risk": 50,
                "expected_risk": 50,
                "expected_size": 5,
                "expected_projected": 100,
                "expected_remaining": 0,
            },
        ]

        for case in cases:
            with self.subTest(case=case["name"]):
                values = {
                    key: value
                    for key, value in case.items()
                    if not key.startswith("expected_") and key != "name"
                }
                result = calculate_position_size(**values)
                self.assertTrue(result["approved"])
                self.assertEqual(result["mode"], "paper_trading")
                self.assertAlmostEqual(result["risk_amount"], case["expected_risk"])
                self.assertAlmostEqual(result["position_size"], case["expected_size"])
                self.assertAlmostEqual(
                    result["projected_total_open_risk"],
                    case["expected_projected"],
                )
                self.assertAlmostEqual(
                    result["remaining_risk_after_trade"],
                    case["expected_remaining"],
                )

    def test_risk_inputs_reject_booleans_and_overflowing_integers(self):
        invalid_positive_values = (True, 10 ** 10000)
        for field in ("account_balance", "entry_price", "stop_loss", "risk_percent"):
            for invalid in invalid_positive_values:
                values = {
                    "account_balance": 10000,
                    "entry_price": 4000,
                    "stop_loss": 3990,
                    "risk_percent": 0.5,
                    "side": "BUY",
                }
                values[field] = invalid
                with self.subTest(field=field, value_type=type(invalid).__name__):
                    with self.assertRaises(RiskError):
                        calculate_position_size(**values)

    def test_current_open_risk_rejects_booleans_and_overflowing_integers(self):
        for invalid in (True, 10 ** 10000):
            with self.subTest(value_type=type(invalid).__name__):
                with self.assertRaises(RiskError):
                    calculate_position_size(
                        account_balance=10000,
                        entry_price=4000,
                        stop_loss=3990,
                        risk_percent=0.5,
                        side="BUY",
                        current_open_risk=invalid,
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

    def test_funding_profile_boundaries(self):
        cases = [
            (4999.99, "STARTER", 0.25),
            (5000, "STANDARD", 0.50),
            (24999.99, "STANDARD", 0.50),
            (25000, "PREMIUM", 1.00)
        ]

        for funding, expected_profile, expected_risk in cases:
            with self.subTest(funding=funding):
                result = get_risk_profile(funding)
                self.assertEqual(
                    result["profile"], expected_profile
                )
                self.assertEqual(
                    result["risk_percent"], expected_risk
                )

    def test_standard_profile_uses_half_percent(self):
        result = self.open_trade("ORDER-STANDARD")
        self.assertEqual(result["risk_profile"], "STANDARD")
        self.assertEqual(result["risk_percent"], 0.50)
        self.assertEqual(result["risk_amount"], 50)

    def test_submitted_risk_cannot_increase_standard_profile(self):
        result = self.open_trade(
            "ORDER-OVERRIDE-HIGH",
            risk_percent=1.0
        )

        self.assertEqual(result["risk_percent"], 0.50)
        self.assertEqual(result["risk_amount"], 50)

    def test_submitted_risk_cannot_reduce_standard_profile(self):
        result = self.open_trade(
            "ORDER-OVERRIDE-LOW",
            risk_percent=0.01
        )

        self.assertEqual(result["risk_percent"], 0.50)
        self.assertEqual(result["risk_amount"], 50)

    def test_starter_profile_uses_quarter_percent(self):
        create_paper_account("STARTER-001", 4000)

        result = open_paper_trade(
            account_id="STARTER-001",
            client_order_id="STARTER-ORDER-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1.0,
            symbol="XAUUSD"
        )

        self.assertEqual(result["risk_profile"], "STARTER")
        self.assertEqual(result["risk_percent"], 0.25)
        self.assertEqual(result["risk_amount"], 10)

    def test_premium_profile_uses_one_percent(self):
        create_paper_account("PREMIUM-001", 30000)

        result = open_paper_trade(
            account_id="PREMIUM-001",
            client_order_id="PREMIUM-ORDER-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.25,
            symbol="XAUUSD"
        )

        self.assertEqual(result["risk_profile"], "PREMIUM")
        self.assertEqual(result["risk_percent"], 1.0)
        self.assertEqual(result["risk_amount"], 300)

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

    def test_starter_can_open_four_positions_within_limit(self):
        create_paper_account("STARTER-002", 4000)

        results = []

        for number in range(1, 5):
            results.append(
                open_paper_trade(
                    account_id="STARTER-002",
                    client_order_id=f"STARTER-{number}",
                    side="BUY",
                    entry_price=4000,
                    stop_loss=3990,
                    risk_percent=1.0,
                    symbol="XAUUSD"
                )
            )

        self.assertEqual(
            results[-1]["total_open_risk"], 40
        )
        self.assertEqual(
            results[-1]["maximum_risk_percent"], 1.0
        )

    def test_duplicate_order_is_rejected(self):
        self.open_trade("ORDER-DUPLICATE")

        with self.assertRaises(ValueError):
            self.open_trade("ORDER-DUPLICATE")

    def test_emergency_stop_blocks_new_orders(self):
        activate_emergency_stop()

        with self.assertRaises(RuntimeError):
            self.open_trade("ORDER-STOPPED")

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
        first = self.open_trade("ORDER-001")
        self.open_trade("ORDER-002")

        close_result = close_paper_trade(
            trade_id=first["trade_id"],
            exit_price=4000
        )

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(close_result["realized_pnl"], 0)
        self.assertEqual(close_result["remaining_open_risk"], 50)

        third = self.open_trade("ORDER-003")
        self.assertEqual(third["total_open_risk"], 100)

    def test_trade_closing_updates_account_balance(self):
        trade = self.open_trade("ORDER-CLOSE", 1.0)

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["realized_pnl"], 50)
        self.assertEqual(result["new_balance"], 10050)
        self.assertEqual(result["remaining_open_risk"], 0)

    def test_open_risk_is_account_specific(self):
        self.open_trade("ORDER-001")
        create_paper_account("TEST-002", 10000)

        second_account_trade = open_paper_trade(
            account_id="TEST-002",
            client_order_id="ORDER-ACCOUNT-2",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=1.0,
            symbol="XAUUSD"
        )

        self.assertEqual(
            second_account_trade["total_open_risk"], 50
        )

    def test_account_risk_endpoint_reports_open_risk(self):
        self.open_trade("ORDER-API-001")
        self.open_trade("ORDER-API-002")

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
        self.open_trade("ORDER-API-003")

        response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(response.status_code, 200)

        result = response.json()

        self.assertEqual(result["open_positions"], 1)
        self.assertEqual(result["current_open_risk"], 50)
        self.assertEqual(result["remaining_risk_budget"], 50)
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

    def test_trade_api_returns_consistent_paper_order_contract(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-CONTRACT-001",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["trade_id"])
        self.assertEqual(result["client_order_id"], "API-CONTRACT-001")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["mode"], "paper_trading")
        self.assertEqual(result["risk_amount"], 50)
        self.assertEqual(result["copy_summary"]["eligible_followers"], 0)
        self.assertEqual(result["copy_summary"]["successful_copies"], 0)
        self.assertEqual(result["copy_summary"]["failed_copies"], 0)

    def test_trade_api_duplicate_order_returns_client_error_without_second_trade(self):
        payload = {
            "account_id": "TEST-001",
            "client_order_id": "API-DUPLICATE-001",
            "symbol": "XAUUSD",
            "side": "BUY",
            "entry_price": 4000,
            "stop_loss": 3990,
            "risk_percent": 0.5
        }

        first = self.client.post("/trades", json=payload)
        second = self.client.post("/trades", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertIn("Duplicate order ID", second.json()["detail"])

        with database.get_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'API-DUPLICATE-001'
                """
            ).fetchone()["total"]

        self.assertEqual(count, 1)

    def test_emergency_stop_api_blocks_new_orders_but_allows_existing_close(self):
        existing = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-BEFORE-STOP",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )
        self.assertEqual(existing.status_code, 200)

        stop_response = self.client.post("/safety/emergency-stop")
        self.assertEqual(stop_response.status_code, 200)
        self.assertTrue(self.client.get("/safety/status").json()["emergency_stop"])

        rejected = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-DURING-STOP",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 0.5
            }
        )
        self.assertEqual(rejected.status_code, 400)

        close_response = self.client.post(
            f"/trades/{existing.json()['trade_id']}/close",
            json={"exit_price": 4010}
        )
        self.assertEqual(close_response.status_code, 200)
        self.assertEqual(close_response.json()["status"], "CLOSED")

        resumed = self.client.post("/safety/resume")
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(self.client.get("/safety/status").json()["emergency_stop"])

    def test_api_rejects_observation_only_instrument(self):
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
        self.assertEqual(response.status_code, 400)
        self.assertIn("observation-only", response.json()["detail"])

    def test_api_cannot_override_assigned_risk_profile(self):
        response = self.client.post(
            "/trades",
            json={
                "account_id": "TEST-001",
                "client_order_id": "API-RISK-OVERRIDE",
                "symbol": "XAUUSD",
                "side": "BUY",
                "entry_price": 4000,
                "stop_loss": 3990,
                "risk_percent": 1.0
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["risk_percent"], 0.5)
        self.assertEqual(response.json()["risk_amount"], 50)

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
        trade = self.open_trade(
            "ORDER-CLOSE-SYMBOL", 0.5, "GBPUSD"
        )

        result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4000
        )

        self.assertEqual(result["symbol"], "GBPUSD")
        self.assertEqual(result["status"], "CLOSED")

    def test_legacy_database_migration_preserves_existing_trades(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        database.DB_PATH = legacy_path

        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE paper_accounts (
                    account_id TEXT PRIMARY KEY,
                    starting_balance REAL NOT NULL,
                    balance REAL NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE trades (
                    trade_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    take_profit REAL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    risk_amount REAL NOT NULL,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    opened_at TEXT,
                    closed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id)
                        REFERENCES paper_accounts(account_id)
                );

                INSERT INTO paper_accounts (
                    account_id, starting_balance, balance
                ) VALUES ('LEGACY-001', 10000, 10000);

                INSERT INTO trades (
                    trade_id, account_id, client_order_id,
                    symbol, side, quantity, entry_price,
                    stop_loss, status, risk_amount
                ) VALUES (
                    'OLD-TRADE-001', 'LEGACY-001', 'OLD-ORDER-001',
                    'XAUUSD', 'BUY', 10, 4000,
                    3990, 'OPEN', 100
                );
                """
            )

        init_db()
        init_db()

        with database.get_connection() as connection:
            trade = connection.execute(
                """
                SELECT trade_id, account_id, symbol, status,
                       position_group_id, copied_from_trade_id,
                       copy_group_id
                FROM trades
                WHERE trade_id = ?
                """,
                ("OLD-TRADE-001",)
            ).fetchone()

            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(trades)"
                ).fetchall()
            }

            group_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'position_groups'
                """
            ).fetchone()

            migrated_account = connection.execute(
                """
                SELECT risk_profile
                FROM paper_accounts
                WHERE account_id = 'LEGACY-001'
                """
            ).fetchone()

        self.assertIsNotNone(trade)
        self.assertEqual(trade["account_id"], "LEGACY-001")
        self.assertEqual(trade["symbol"], "XAUUSD")
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNone(trade["position_group_id"])
        self.assertIsNone(trade["copied_from_trade_id"])
        self.assertIsNone(trade["copy_group_id"])

        self.assertIn("position_group_id", columns)
        self.assertIn("copied_from_trade_id", columns)
        self.assertIn("copy_group_id", columns)
        self.assertIsNotNone(group_table)
        self.assertEqual(migrated_account["risk_profile"], "STANDARD")


    def test_invalid_sell_stop_loss_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=3990,
                risk_percent=0.5,
                side="SELL"
            )

    def test_stop_loss_equal_to_entry_is_rejected(self):
        with self.assertRaises(RiskError):
            calculate_position_size(
                account_balance=10000,
                entry_price=4000,
                stop_loss=4000,
                risk_percent=0.5,
                side="BUY"
            )

    def test_non_finite_prices_are_rejected(self):
        for bad_value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=bad_value):
                with self.assertRaises(RiskError):
                    calculate_position_size(
                        account_balance=10000,
                        entry_price=bad_value,
                        stop_loss=3990,
                        risk_percent=0.5,
                        side="BUY"
                    )

    def test_rejected_stop_loss_does_not_create_trade(self):
        with self.assertRaises(RiskError):
            open_paper_trade(
                account_id="TEST-001",
                client_order_id="ORDER-INVALID-STOP",
                side="BUY",
                entry_price=4000,
                stop_loss=4000,
                risk_percent=0.5,
                symbol="XAUUSD"
            )

        with database.get_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'ORDER-INVALID-STOP'
                """
            ).fetchone()["total"]

        self.assertEqual(count, 0)


    def test_maintenance_blocks_service_and_api_entries_but_allows_close(self):
        existing_trade = self.open_trade("ORDER-BEFORE-MAINTENANCE")

        with patch("app.safety.is_maintenance_window", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "maintenance window"):
                self.open_trade("ORDER-DURING-MAINTENANCE")

            response = self.client.post(
                "/trades",
                json={
                    "account_id": "TEST-001",
                    "client_order_id": "API-DURING-MAINTENANCE",
                    "symbol": "XAUUSD",
                    "side": "BUY",
                    "entry_price": 4000,
                    "stop_loss": 3990,
                    "risk_percent": 0.5
                }
            )
            self.assertEqual(response.status_code, 400)

            # Maintenance blocks entries, never the ability to reduce risk.
            close_result = close_paper_trade(
                trade_id=existing_trade["trade_id"],
                exit_price=4010
            )
            self.assertEqual(close_result["status"], "CLOSED")

        with database.get_connection() as connection:
            rejected_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id IN (
                    'ORDER-DURING-MAINTENANCE',
                    'API-DURING-MAINTENANCE'
                )
                """
            ).fetchone()["total"]

        self.assertEqual(rejected_count, 0)

    def test_emergency_stop_blocks_new_orders_but_allows_safe_close(self):
        existing_trade = self.open_trade("ORDER-BEFORE-EMERGENCY-STOP")

        activate_emergency_stop()

        with self.assertRaises(RuntimeError):
            self.open_trade("ORDER-DURING-EMERGENCY-STOP")

        close_result = close_paper_trade(
            trade_id=existing_trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(close_result["status"], "CLOSED")
        self.assertEqual(close_result["realized_pnl"], 50)
        self.assertEqual(close_result["new_balance"], 10050)

        with database.get_connection() as connection:
            account = connection.execute(
                """
                SELECT starting_balance, balance
                FROM paper_accounts
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()
            rejected_order_count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'ORDER-DURING-EMERGENCY-STOP'
                """
            ).fetchone()["total"]

        self.assertEqual(account["balance"], 10050)
        self.assertEqual(rejected_order_count, 0)

    def test_account_balance_reconciles_with_all_realized_pnl(self):
        first = self.open_trade("ORDER-RECONCILE-001")
        second = self.open_trade("ORDER-RECONCILE-002")

        close_paper_trade(first["trade_id"], 3990)
        close_paper_trade(second["trade_id"], 4010)

        with database.get_connection() as connection:
            account = connection.execute(
                """
                SELECT starting_balance, balance
                FROM paper_accounts
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()
            totals = connection.execute(
                """
                SELECT
                    COALESCE(SUM(realized_pnl), 0) AS realized_total,
                    SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END)
                        AS open_positions
                FROM trades
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()

        expected_balance = (
            account["starting_balance"] + totals["realized_total"]
        )
        self.assertEqual(account["balance"], expected_balance)
        self.assertEqual(totals["realized_total"], 0)
        self.assertEqual(totals["open_positions"], 0)


    def test_open_trade_and_account_persist_after_database_reinitialization(self):
        trade = self.open_trade("ORDER-PERSIST-OPEN")

        # Simulate application startup against the same persistent database.
        init_db()

        account_response = self.client.get("/accounts/TEST-001")
        trades_response = self.client.get("/accounts/TEST-001/trades")
        risk_response = self.client.get("/accounts/TEST-001/risk")

        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(trades_response.status_code, 200)
        self.assertEqual(risk_response.status_code, 200)

        account = account_response.json()
        trades = trades_response.json()["trades"]

        self.assertEqual(account["current_balance"], 10000)
        self.assertEqual(account["open_trades"], 1)
        self.assertEqual(account["realized_pnl"], 0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["trade_id"], trade["trade_id"])
        self.assertEqual(trades[0]["status"], "OPEN")
        self.assertEqual(risk_response.json()["current_open_risk"], 50)

    def test_closed_trade_and_realized_balance_persist_after_reinitialization(self):
        trade = self.open_trade("ORDER-PERSIST-CLOSED")
        close_result = close_paper_trade(
            trade_id=trade["trade_id"],
            exit_price=4010
        )

        self.assertEqual(close_result["new_balance"], 10050)

        # Re-running initialization must not reset or duplicate financial data.
        init_db()
        init_db()

        account_response = self.client.get("/accounts/TEST-001")
        trades_response = self.client.get("/accounts/TEST-001/trades")

        self.assertEqual(account_response.status_code, 200)
        self.assertEqual(trades_response.status_code, 200)

        account = account_response.json()
        trades = trades_response.json()["trades"]

        self.assertEqual(account["starting_balance"], 10000)
        self.assertEqual(account["current_balance"], 10050)
        self.assertEqual(account["closed_trades"], 1)
        self.assertEqual(account["realized_pnl"], 50)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "CLOSED")
        self.assertEqual(trades[0]["realized_pnl"], 50)


    def test_concurrent_duplicate_order_requests_create_only_one_trade(self):
        start = Barrier(2)

        def submit_duplicate():
            start.wait()
            try:
                result = self.open_trade("ORDER-CONCURRENT-DUPLICATE")
                return ("created", result["trade_id"])
            except ValueError:
                return ("rejected", None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(
                lambda _: submit_duplicate(), range(2)
            ))

        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "created"), 1
        )
        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "rejected"), 1
        )

        with database.get_connection() as connection:
            count = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM trades
                WHERE client_order_id = 'ORDER-CONCURRENT-DUPLICATE'
                """
            ).fetchone()["total"]

        self.assertEqual(count, 1)

    def test_small_risk_reservations_keep_precision_and_enforce_one_percent_cap(self):
        create_paper_account("SMALL-BALANCE", 1.00)

        for index in range(4):
            result = open_paper_trade(
                account_id="SMALL-BALANCE",
                client_order_id=f"SMALL-RISK-{index}",
                side="BUY",
                entry_price=4000,
                stop_loss=3990,
                symbol="XAUUSD",
            )
            self.assertAlmostEqual(result["risk_amount"], 0.0025, places=10)

        with database.get_connection() as connection:
            reserved = connection.execute(
                """
                SELECT SUM(risk_amount) AS total
                FROM trades
                WHERE account_id = 'SMALL-BALANCE' AND status = 'OPEN'
                """
            ).fetchone()["total"]

        self.assertAlmostEqual(reserved, 0.01, places=10)
        with self.assertRaises(RiskError):
            open_paper_trade(
                account_id="SMALL-BALANCE",
                client_order_id="SMALL-RISK-OVER-CAP",
                side="BUY",
                entry_price=4000,
                stop_loss=3990,
                symbol="XAUUSD",
            )

    def test_concurrent_orders_cannot_exceed_combined_one_percent_risk(self):
        # A PREMIUM account risks 1% per order, so two simultaneous
        # requests must not both consume the same 1% account allowance.
        create_paper_account("CONCURRENT-PREMIUM", 30000)
        start = Barrier(2)

        def submit_order(order_id):
            start.wait()
            try:
                result = open_paper_trade(
                    account_id="CONCURRENT-PREMIUM",
                    client_order_id=order_id,
                    side="BUY",
                    entry_price=4000,
                    stop_loss=3990,
                    risk_percent=0.5,
                    symbol="XAUUSD"
                )
                return ("created", result["risk_amount"])
            except RiskError:
                return ("risk_rejected", None)

        order_ids = (
            "ORDER-CONCURRENT-RISK-A",
            "ORDER-CONCURRENT-RISK-B"
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit_order, order_ids))

        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "created"), 1
        )
        self.assertEqual(
            sum(1 for status, _ in outcomes if status == "risk_rejected"), 1
        )

        with database.get_connection() as connection:
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS open_positions,
                    COALESCE(SUM(risk_amount), 0) AS total_open_risk
                FROM trades
                WHERE account_id = 'CONCURRENT-PREMIUM'
                  AND status = 'OPEN'
                """
            ).fetchone()

        self.assertEqual(totals["open_positions"], 1)
        self.assertEqual(totals["total_open_risk"], 300)
        self.assertLessEqual(totals["total_open_risk"], 300)


    def test_account_history_reports_net_realized_pnl_after_close_costs(self):
        trade = self.open_trade("ORDER-HISTORY-NET-PNL")

        close_response = self.client.post(
            f"/trades/{trade['trade_id']}/close",
            json={
                "exit_price": 4001,
                "spread_cost": 0.25,
                "commission_cost": 0.50,
                "swap_cost": 0.10,
                "other_costs": 0.15,
            },
        )

        self.assertEqual(close_response.status_code, 200)
        close_result = close_response.json()
        self.assertAlmostEqual(close_result["realized_pnl"], 4.0, places=7)

        # Repeated startup migrations must preserve net P&L and balance.
        init_db()
        init_db()

        history_response = self.client.get("/accounts/TEST-001")
        self.assertEqual(history_response.status_code, 200)
        history = history_response.json()

        self.assertAlmostEqual(history["realized_pnl"], 4.0, places=7)
        self.assertAlmostEqual(history["current_balance"], 10004.0, places=7)
        self.assertEqual(history["closed_trades"], 1)
        self.assertEqual(history["open_trades"], 0)
        self.assertAlmostEqual(
            history["current_balance"],
            history["starting_balance"] + history["realized_pnl"],
            places=7,
        )

    def test_profit_preview_api_is_read_only_and_accounts_for_costs(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW")

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={
                "current_price": 4001,
                "spread_cost": 0.2,
                "commission_cost": 0.1,
                "swap_cost": 0.1,
                "other_costs": 0.1,
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertEqual(preview["trade_id"], trade["trade_id"])
        self.assertEqual(preview["symbol"], "XAUUSD")
        self.assertAlmostEqual(preview["gross_pnl"], trade["quantity"])
        self.assertAlmostEqual(preview["total_costs"], 0.5)
        self.assertAlmostEqual(
            preview["net_pnl"], trade["quantity"] - 0.5
        )
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["execution_triggered"])

        with database.get_connection() as connection:
            stored_trade = connection.execute(
                """
                SELECT status, realized_pnl
                FROM trades
                WHERE trade_id = ?
                """,
                (trade["trade_id"],),
            ).fetchone()
            account = connection.execute(
                """
                SELECT balance
                FROM paper_accounts
                WHERE account_id = 'TEST-001'
                """
            ).fetchone()

        self.assertEqual(stored_trade["status"], "OPEN")
        self.assertEqual(stored_trade["realized_pnl"], 0)
        self.assertEqual(account["balance"], 10000)

    def test_profit_preview_rejects_closed_trade(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-CLOSED")
        close_paper_trade(trade["trade_id"], 4010)

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={"current_price": 4011},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("only for open trades", response.json()["detail"])

    def test_profit_preview_rejects_boolean_and_overflow_cost_inputs(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-NONFINITE")
        for field in (
            "current_price",
            "spread_cost",
            "commission_cost",
            "swap_cost",
            "other_costs",
            "minimum_net_profit",
        ):
            for invalid in (True, 10 ** 10000):
                payload = {"current_price": 4001, field: invalid}
                with self.subTest(field=field, invalid_type=type(invalid).__name__):
                    with self.assertRaises(ValueError):
                        from app.profit_taking import evaluate_position_profit
                        evaluate_position_profit(
                            {
                                "side": "BUY",
                                "entry_price": 4000,
                                "quantity": 1,
                            },
                            payload["current_price"],
                            spread_cost=payload.get("spread_cost", 0),
                            commission_cost=payload.get("commission_cost", 0),
                            swap_cost=payload.get("swap_cost", 0),
                            other_costs=payload.get("other_costs", 0),
                            minimum_net_profit=payload.get("minimum_net_profit", 0),
                        )

    def test_profit_preview_validates_positive_current_price(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-INVALID")

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={"current_price": 0},
        )

        self.assertEqual(response.status_code, 422)

    def test_profit_preview_does_not_mark_small_gross_profit_as_net_profit(self):
        trade = self.open_trade("ORDER-PROFIT-PREVIEW-COSTS")

        response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={
                "current_price": 4000.01,
                "spread_cost": 1,
                "commission_cost": 1,
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        self.assertLess(preview["net_pnl"], 0)
        self.assertFalse(preview["should_take_profit"])


    def test_group_profit_preview_aggregates_positions_and_group_costs_once(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        self.assertEqual(group_response.status_code, 200)
        group_id = group_response.json()["group_id"]

        first = open_paper_trade(
            account_id="TEST-001",
            client_order_id="GROUP-PREVIEW-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
            position_group_id=group_id,
        )
        second = open_paper_trade(
            account_id="TEST-001",
            client_order_id="GROUP-PREVIEW-002",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
            position_group_id=group_id,
        )

        response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={
                "current_price": 4001,
                "spread_cost": 0.2,
                "commission_cost": 0.1,
                "swap_cost": 0.1,
                "other_costs": 0.1,
            },
        )

        self.assertEqual(response.status_code, 200)
        preview = response.json()
        expected_gross = first["quantity"] + second["quantity"]
        self.assertEqual(preview["group_id"], group_id)
        self.assertEqual(
            set(preview["trade_ids"]),
            {first["trade_id"], second["trade_id"]},
        )
        self.assertEqual(preview["position_count"], 2)
        self.assertAlmostEqual(preview["gross_pnl"], expected_gross)
        self.assertAlmostEqual(preview["total_costs"], 0.5)
        self.assertAlmostEqual(preview["net_pnl"], expected_gross - 0.5)
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["execution_triggered"])
        self.assertEqual(
            preview["cost_allocation"],
            "proportional_to_position_quantity",
        )

        with database.get_connection() as connection:
            states = connection.execute(
                """
                SELECT status, realized_pnl
                FROM trades
                WHERE position_group_id = ?
                """,
                (group_id,),
            ).fetchall()
            balance = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        self.assertEqual([row["status"] for row in states], ["OPEN", "OPEN"])
        self.assertEqual(sum(row["realized_pnl"] for row in states), 0)
        self.assertEqual(balance, 10000)

    def test_single_profit_preview_matches_actual_close_ledger_and_balance(self):
        trade = self.open_trade("SINGLE-PREVIEW-CLOSE-PARITY")
        costs = {
            "spread_cost": 0.20,
            "commission_cost": 0.10,
            "swap_cost": 0.10,
            "other_costs": 0.10,
        }
        exit_price = 4001.0

        preview_response = self.client.post(
            f"/trades/{trade['trade_id']}/profit-preview",
            json={"current_price": exit_price, **costs},
        )
        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()
        self.assertTrue(preview["read_only"])
        self.assertFalse(preview["execution_triggered"])

        with database.get_connection() as connection:
            balance_before = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        close_response = self.client.post(
            f"/trades/{trade['trade_id']}/close",
            json={"exit_price": exit_price, **costs},
        )
        self.assertEqual(close_response.status_code, 200)

        with database.get_connection() as connection:
            row = connection.execute(
                """
                SELECT status, realized_pnl, gross_realized_pnl,
                       spread_cost, commission_cost, swap_cost, other_costs
                FROM trades
                WHERE trade_id = ?
                """,
                (trade["trade_id"],),
            ).fetchone()
            balance_after = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        persisted_costs = (
            row["spread_cost"] + row["commission_cost"]
            + row["swap_cost"] + row["other_costs"]
        )
        self.assertEqual(row["status"], "CLOSED")
        self.assertAlmostEqual(preview["net_pnl"], row["realized_pnl"], places=7)
        self.assertAlmostEqual(
            preview["gross_pnl"], row["gross_realized_pnl"], places=7
        )
        self.assertAlmostEqual(
            preview["total_costs"], persisted_costs, places=7
        )
        self.assertAlmostEqual(
            balance_after - balance_before, preview["net_pnl"], places=7
        )
        self.assertAlmostEqual(
            close_response.json()["realized_pnl"], preview["net_pnl"], places=7
        )

    def test_group_profit_preview_matches_actual_close_ledger_and_balance(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        self.assertEqual(group_response.status_code, 200)
        group_id = group_response.json()["group_id"]

        first = self.open_trade("GROUP-PREVIEW-CLOSE-PARITY-1")
        second = self.open_trade("GROUP-PREVIEW-CLOSE-PARITY-2")
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE trades SET position_group_id = ? WHERE trade_id IN (?, ?)",
                (group_id, first["trade_id"], second["trade_id"]),
            )
        costs = {
            "spread_cost": 0.20,
            "commission_cost": 0.10,
            "swap_cost": 0.10,
            "other_costs": 0.10,
        }
        exit_price = 4001.0

        preview_response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={"current_price": exit_price, **costs},
        )
        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()

        with database.get_connection() as connection:
            balance_before = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        close_response = self.client.post(
            f"/groups/{group_id}/close",
            json={"exit_price": exit_price, **costs},
        )
        self.assertEqual(close_response.status_code, 200)

        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, status, realized_pnl, gross_realized_pnl,
                       spread_cost, commission_cost, swap_cost, other_costs
                FROM trades
                WHERE position_group_id = ?
                ORDER BY trade_id
                """,
                (group_id,),
            ).fetchall()
            balance_after = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        source_net = sum(row["realized_pnl"] for row in rows)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status"] == "CLOSED" for row in rows))
        self.assertAlmostEqual(preview["net_pnl"], source_net, places=7)
        self.assertAlmostEqual(
            balance_after - balance_before, preview["net_pnl"], places=7
        )
        self.assertAlmostEqual(
            sum(row["gross_realized_pnl"] for row in rows),
            preview["gross_pnl"],
            places=7,
        )
        self.assertAlmostEqual(
            sum(
                row["spread_cost"] + row["commission_cost"]
                + row["swap_cost"] + row["other_costs"]
                for row in rows
            ),
            preview["total_costs"],
            places=7,
        )
        self.assertAlmostEqual(close_response.json()["realized_pnl"], source_net, places=7)

    def test_group_preview_and_close_match_with_uneven_quantity_cost_allocation(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        self.assertEqual(group_response.status_code, 200)
        group_id = group_response.json()["group_id"]

        first = self.open_trade("GROUP-UNEQUAL-QUANTITY-1")
        second = self.open_trade("GROUP-UNEQUAL-QUANTITY-2")
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE trades SET position_group_id = ?, quantity = ? WHERE trade_id = ?",
                (group_id, 0.013, first["trade_id"]),
            )
            connection.execute(
                "UPDATE trades SET position_group_id = ?, quantity = ? WHERE trade_id = ?",
                (group_id, 0.027, second["trade_id"]),
            )

        costs = {
            "spread_cost": 0.123456789,
            "commission_cost": 0.234567891,
            "swap_cost": 0.345678912,
            "other_costs": 0.456789123,
        }
        preview_response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={"current_price": 4001.123456789, **costs},
        )
        self.assertEqual(preview_response.status_code, 200)
        preview = preview_response.json()

        close_response = self.client.post(
            f"/groups/{group_id}/close",
            json={"exit_price": 4001.123456789, **costs},
        )
        self.assertEqual(close_response.status_code, 200)

        with database.get_connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_id, realized_pnl, gross_realized_pnl,
                       spread_cost, commission_cost, swap_cost, other_costs
                FROM trades
                WHERE position_group_id = ?
                ORDER BY trade_id
                """,
                (group_id,),
            ).fetchall()

        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(
            sum(row["realized_pnl"] for row in rows),
            preview["net_pnl"],
            places=7,
        )
        self.assertAlmostEqual(
            sum(row["gross_realized_pnl"] for row in rows),
            preview["gross_pnl"],
            places=7,
        )
        for field, requested in costs.items():
            self.assertAlmostEqual(
                sum(row[field] for row in rows),
                requested,
                places=12,
            )
        # The close API summary is rounded to account-currency cents;
        # durable ledger rows and the preview retain higher precision.
        self.assertEqual(
            close_response.json()["realized_pnl"],
            round(preview["net_pnl"], 2),
        )


    def test_group_profit_preview_rejects_group_without_open_positions(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        group_id = group_response.json()["group_id"]

        response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={"current_price": 4001},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("open positions", response.json()["detail"])


    def test_group_profit_preview_rejects_closed_group_even_if_open_leg_remains(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        group_id = group_response.json()["group_id"]
        self.open_trade("GROUP-PREVIEW-CLOSED-LEG")

        # Simulate an inconsistent persisted state: a closed group with an
        # orphaned open leg. The preview must trust the group lifecycle state.
        with database.get_connection() as connection:
            connection.execute(
                "UPDATE position_groups SET status = 'CLOSED' WHERE group_id = ?",
                (group_id,),
            )
            connection.execute(
                "UPDATE trades SET position_group_id = ? WHERE client_order_id = ?",
                (group_id, "GROUP-PREVIEW-CLOSED-LEG"),
            )

        response = self.client.post(
            f"/groups/{group_id}/profit-preview",
            json={"current_price": 4001},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("open position groups", response.json()["detail"])

    def test_group_profit_preview_requires_authentication(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        group_id = group_response.json()["group_id"]
        unauthenticated = TestClient(app)

        try:
            response = unauthenticated.post(
                f"/groups/{group_id}/profit-preview",
                json={"current_price": 4001},
            )
        finally:
            unauthenticated.close()

        self.assertEqual(response.status_code, 401)

    def test_close_api_rejects_boolean_exit_price_and_costs(self):
        trade = self.open_trade("ORDER-CLOSE-STRICT-NUMBERS")

        invalid_exit = self.client.post(
            f"/trades/{trade['trade_id']}/close",
            json={"exit_price": True},
        )
        invalid_cost = self.client.post(
            f"/trades/{trade['trade_id']}/close",
            json={"exit_price": 4010, "commission_cost": True},
        )

        self.assertEqual(invalid_exit.status_code, 422)
        self.assertEqual(invalid_cost.status_code, 422)

        with database.get_connection() as connection:
            stored = connection.execute(
                "SELECT status, realized_pnl FROM trades WHERE trade_id = ?",
                (trade["trade_id"],)
            ).fetchone()
            balance = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        self.assertEqual(stored["status"], "OPEN")
        self.assertEqual(stored["realized_pnl"], 0)
        self.assertEqual(balance, 10000)


    def test_profit_preview_api_rejects_boolean_numeric_fields(self):
        trade = self.open_trade("ORDER-PREVIEW-STRICT-NUMBERS")
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        self.assertEqual(group_response.status_code, 200)
        group_id = group_response.json()["group_id"]

        endpoints = (
            f"/trades/{trade['trade_id']}/profit-preview",
            f"/groups/{group_id}/profit-preview",
        )
        fields = (
            "current_price",
            "spread_cost",
            "commission_cost",
            "swap_cost",
            "other_costs",
            "minimum_net_profit",
        )

        for endpoint in endpoints:
            for field in fields:
                payload = {"current_price": 4001}
                if field != "current_price":
                    payload[field] = True
                else:
                    payload["current_price"] = True

                with self.subTest(endpoint=endpoint, field=field):
                    response = self.client.post(endpoint, json=payload)
                    self.assertEqual(response.status_code, 422)


    def test_position_size_rejects_malformed_numeric_inputs(self):
        invalid_values = (None, "", "not-a-number", float("nan"))
        valid_values = {
            "account_balance": 10000,
            "entry_price": 4000,
            "stop_loss": 3990,
            "risk_percent": 0.5,
            "side": "BUY",
        }
        for field in ("account_balance", "entry_price", "stop_loss", "risk_percent"):
            for invalid in invalid_values:
                values = dict(valid_values)
                values[field] = invalid
                with self.subTest(field=field, value=repr(invalid)):
                    with self.assertRaises(RiskError):
                        calculate_position_size(**values)

    def test_position_size_rejects_malformed_current_open_risk(self):
        invalid_values = (None, "", "not-a-number", float("nan"))
        for invalid in invalid_values:
            with self.subTest(value=repr(invalid)):
                with self.assertRaises(RiskError):
                    calculate_position_size(
                        account_balance=10000,
                        entry_price=4000,
                        stop_loss=3990,
                        risk_percent=0.5,
                        side="BUY",
                        current_open_risk=invalid,
                    )


    # Take-profit validation matrix: invalid targets must never reserve risk
    # or create paper positions.
    def open_trade_with_target(self, order_id, side, take_profit):
        return open_paper_trade(
            account_id="TEST-001",
            client_order_id=order_id,
            side=side,
            entry_price=4000,
            stop_loss=3990 if side == "BUY" else 4010,
            take_profit=take_profit,
        )

    def test_buy_take_profit_above_entry_is_accepted(self):
        trade = self.open_trade_with_target("TP-BUY-VALID", "BUY", 4010)
        self.assertEqual(trade["status"], "OPEN")

    def test_sell_take_profit_below_entry_is_accepted(self):
        trade = self.open_trade_with_target("TP-SELL-VALID", "SELL", 3990)
        self.assertEqual(trade["status"], "OPEN")

    def test_buy_take_profit_equal_to_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-BUY-EQUAL", "BUY", 4000)

    def test_buy_take_profit_below_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-BUY-BELOW", "BUY", 3999.99)

    def test_sell_take_profit_equal_to_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-SELL-EQUAL", "SELL", 4000)

    def test_sell_take_profit_above_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-SELL-ABOVE", "SELL", 4000.01)

    def test_boolean_true_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-TRUE", "BUY", True)

    def test_boolean_false_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-FALSE", "BUY", False)

    def test_zero_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-ZERO", "BUY", 0)

    def test_negative_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-NEGATIVE", "BUY", -1)

    def test_nan_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-NAN", "BUY", float("nan"))

    def test_positive_infinity_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-POS-INF", "BUY", float("inf"))

    def test_negative_infinity_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-NEG-INF", "BUY", -float("inf"))

    def test_non_numeric_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-TEXT", "BUY", "not-a-price")

    def test_empty_string_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-EMPTY", "BUY", "")

    def test_list_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-LIST", "BUY", [4010])

    def test_dictionary_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-DICT", "BUY", {"price": 4010})

    def test_overflowing_integer_take_profit_is_rejected(self):
        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-OVERFLOW", "BUY", 10 ** 10000)

    def test_missing_take_profit_remains_optional(self):
        trade = self.open_trade_with_target("TP-OPTIONAL-NONE", "BUY", None)
        self.assertEqual(trade["status"], "OPEN")
        self.assertIsNone(trade["take_profit"])

    def test_rejected_take_profit_does_not_create_trade_or_reserve_risk(self):
        with database.get_connection() as connection:
            before_count = connection.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE account_id = 'TEST-001'"
            ).fetchone()["n"]
            before_risk = connection.execute(
                "SELECT COALESCE(SUM(risk_amount), 0) AS n FROM trades "
                "WHERE account_id = 'TEST-001' AND status = 'OPEN'"
            ).fetchone()["n"]

        with self.assertRaises(ValueError):
            self.open_trade_with_target("TP-NO-SIDE-EFFECT", "BUY", 3990)

        with database.get_connection() as connection:
            after_count = connection.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE account_id = 'TEST-001'"
            ).fetchone()["n"]
            after_risk = connection.execute(
                "SELECT COALESCE(SUM(risk_amount), 0) AS n FROM trades "
                "WHERE account_id = 'TEST-001' AND status = 'OPEN'"
            ).fetchone()["n"]

        self.assertEqual(after_count, before_count)
        self.assertEqual(after_risk, before_risk)



    def test_single_execution_close_rolls_back_trade_when_balance_write_fails(self):
        trade = self.open_trade("ROLLBACK-SINGLE-BALANCE")
        with database.get_connection() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_single_close_balance
                BEFORE UPDATE OF balance ON paper_accounts
                WHEN OLD.account_id = 'TEST-001'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated balance write failure');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            close_paper_trade(trade["trade_id"], 4010)

        with database.get_connection() as connection:
            ledger = connection.execute(
                "SELECT status, realized_pnl FROM trades WHERE trade_id = ?",
                (trade["trade_id"],),
            ).fetchone()
            balance = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        self.assertEqual(ledger["status"], "OPEN")
        self.assertEqual(ledger["realized_pnl"], 0)
        self.assertEqual(balance, 10000)

    def test_execution_group_close_rolls_back_all_positions_when_group_finalization_fails(self):
        group_response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "XAUUSD", "side": "BUY"},
        )
        self.assertEqual(group_response.status_code, 200)
        group_id = group_response.json()["group_id"]

        first = open_paper_trade(
            account_id="TEST-001",
            client_order_id="ROLLBACK-GROUP-001",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
            position_group_id=group_id,
        )
        second = open_paper_trade(
            account_id="TEST-001",
            client_order_id="ROLLBACK-GROUP-002",
            side="BUY",
            entry_price=4000,
            stop_loss=3990,
            risk_percent=0.5,
            symbol="XAUUSD",
            position_group_id=group_id,
        )

        with database.get_connection() as connection:
            connection.execute(
                """
                CREATE TRIGGER fail_group_close_finalization
                BEFORE UPDATE OF status ON position_groups
                WHEN OLD.group_id = '""" + group_id + """'
                 AND NEW.status = 'CLOSED'
                BEGIN
                    SELECT RAISE(ABORT, 'simulated group finalization failure');
                END
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            close_position_group(group_id, 4010)

        with database.get_connection() as connection:
            trades = connection.execute(
                """
                SELECT status, realized_pnl
                FROM trades
                WHERE trade_id IN (?, ?)
                ORDER BY trade_id
                """,
                (first["trade_id"], second["trade_id"]),
            ).fetchall()
            group = connection.execute(
                "SELECT status FROM position_groups WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            balance = connection.execute(
                "SELECT balance FROM paper_accounts WHERE account_id = 'TEST-001'"
            ).fetchone()["balance"]

        self.assertEqual(len(trades), 2)
        self.assertTrue(all(row["status"] == "OPEN" for row in trades))
        self.assertTrue(all(row["realized_pnl"] == 0 for row in trades))
        self.assertEqual(group["status"], "OPEN")
        self.assertEqual(balance, 10000)


    def test_position_size_rejects_overflowing_quantities(self):
        # Finite inputs can still overflow during division when the stop
        # distance is extremely small relative to the risk amount.
        cases = (
            (1e308, 1e-308, 5e-309, 0.25),
            (1e308, 1e-308, 1e-309, 0.25),
            (1e308, 1e-307, 1e-308, 0.50),
            (1e308, 1e-306, 1e-307, 1.00),
            (9e307, 1e-308, 5e-309, 0.25),
            (8e307, 1e-308, 1e-309, 0.25),
            (7e307, 1e-307, 1e-308, 0.50),
            (6e307, 1e-306, 1e-307, 1.00),
            (5e307, 1e-308, 5e-309, 0.25),
            (4e307, 1e-308, 1e-309, 0.25),
            (3e307, 1e-307, 1e-308, 0.50),
            (2e307, 1e-306, 1e-307, 1.00),
            (1e308, 2e-308, 1e-308, 0.25),
            (1e308, 2e-308, 2e-309, 0.25),
            (1e308, 2e-307, 2e-308, 0.50),
            (1e308, 2e-306, 2e-307, 1.00),
            (9e307, 3e-308, 1e-308, 0.25),
            (8e307, 3e-308, 3e-309, 0.25),
            (7e307, 3e-307, 3e-308, 0.50),
            (6e307, 3e-306, 3e-307, 1.00),
        )
        for balance, entry, stop, risk_percent in cases:
            with self.subTest(
                balance=balance, entry=entry, stop=stop,
                risk_percent=risk_percent
            ):
                with self.assertRaises(RiskError):
                    calculate_position_size(
                        account_balance=balance,
                        entry_price=entry,
                        stop_loss=stop,
                        risk_percent=risk_percent,
                        side="BUY",
                    )


    def test_position_size_rejects_quantities_below_supported_precision(self):
        cases = (
            (1e-10, 10, 1, 0.25),
            (2e-10, 20, 2, 0.25),
            (3e-10, 30, 3, 0.25),
            (4e-10, 40, 4, 0.25),
            (5e-10, 50, 5, 0.25),
            (6e-10, 60, 6, 0.25),
            (7e-10, 70, 7, 0.25),
            (8e-10, 80, 8, 0.25),
            (9e-10, 90, 9, 0.25),
            (1e-9, 100, 10, 0.25),
            (2e-10, 10, 1, 0.50),
            (3e-10, 20, 2, 0.50),
            (4e-10, 30, 3, 0.50),
            (5e-10, 40, 4, 0.50),
            (6e-10, 50, 5, 0.50),
            (7e-10, 60, 6, 0.50),
            (8e-10, 70, 7, 0.50),
            (9e-10, 80, 8, 0.50),
            (1e-9, 90, 9, 0.50),
            (2e-9, 100, 10, 0.50),
        )
        for balance, entry, stop, risk_percent in cases:
            with self.subTest(
                balance=balance, entry=entry, stop=stop,
                risk_percent=risk_percent
            ):
                with self.assertRaises(RiskError):
                    calculate_position_size(
                        account_balance=balance,
                        entry_price=entry,
                        stop_loss=stop,
                        risk_percent=risk_percent,
                        side="BUY",
                    )


    def test_direct_order_rejects_observation_only_symbol_without_reserving_risk(self):
        with self.assertRaisesRegex(ValueError, "observation-only"):
            self.open_trade("OBSERVATION-ONLY-EUR", symbol="EURUSD")
        self.assertEqual(self.client.get("/accounts/TEST-001/risk").json()["open_positions"], 0)

    def test_position_group_rejects_observation_only_symbol(self):
        response = self.client.post(
            "/accounts/TEST-001/groups",
            json={"symbol": "GBPUSD", "side": "BUY"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("observation-only", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
