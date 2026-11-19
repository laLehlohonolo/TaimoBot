import sqlite3
import unittest

from backend.app.database import migrate_trade_cost_ledger


class TradeCostLedgerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE trades (
                trade_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                realized_pnl REAL NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.executemany(
            "INSERT INTO trades (trade_id, status, realized_pnl) VALUES (?, ?, ?)",
            [
                ("legacy-closed", "CLOSED", 12.75),
                ("legacy-open", "OPEN", 0),
            ],
        )

    def tearDown(self):
        self.connection.close()

    def test_migration_preserves_legacy_closed_pnl_and_zero_costs(self):
        migrate_trade_cost_ledger(self.connection)

        closed = self.connection.execute(
            "SELECT * FROM trades WHERE trade_id = 'legacy-closed'"
        ).fetchone()
        self.assertEqual(closed["realized_pnl"], 12.75)
        self.assertEqual(closed["gross_realized_pnl"], 12.75)
        for field in ("spread_cost", "commission_cost", "swap_cost", "other_costs"):
            self.assertEqual(closed[field], 0)

    def test_open_legacy_trade_is_not_misrepresented_as_closed_pnl(self):
        migrate_trade_cost_ledger(self.connection)

        opened = self.connection.execute(
            "SELECT * FROM trades WHERE trade_id = 'legacy-open'"
        ).fetchone()
        self.assertIsNone(opened["gross_realized_pnl"])
        self.assertEqual(opened["realized_pnl"], 0)

    def test_migration_is_idempotent(self):
        migrate_trade_cost_ledger(self.connection)
        migrate_trade_cost_ledger(self.connection)

        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(trades)")
        }
        self.assertTrue({
            "gross_realized_pnl",
            "spread_cost",
            "commission_cost",
            "swap_cost",
            "other_costs",
        }.issubset(columns))
        closed = self.connection.execute(
            "SELECT gross_realized_pnl FROM trades WHERE trade_id = 'legacy-closed'"
        ).fetchone()
        self.assertEqual(closed["gross_realized_pnl"], 12.75)


if __name__ == "__main__":
    unittest.main()
