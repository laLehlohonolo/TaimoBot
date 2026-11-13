
import os
import sqlite3
from pathlib import Path


DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "taimobot.db"
DB_PATH = Path(os.getenv("TAIMOBOT_DB_PATH", str(DEFAULT_DB_PATH)))


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that closes after a context-manager block."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(
                exc_type, exc_value, traceback
            )
        finally:
            self.close()


def get_connection():
    """Create a database connection with safe defaults."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
        factory=ClosingConnection
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")

    return connection


def init_db():
    """Create the TaimoBot paper-trading tables."""
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_accounts (
                account_id TEXT PRIMARY KEY,
                starting_balance REAL NOT NULL
                    CHECK (starting_balance > 0),
                balance REAL NOT NULL
                    CHECK (balance >= 0),
                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL
                    CHECK (side IN ('BUY', 'SELL')),
                quantity REAL NOT NULL
                    CHECK (quantity > 0),
                entry_price REAL NOT NULL
                    CHECK (entry_price > 0),
                stop_loss REAL NOT NULL
                    CHECK (stop_loss > 0),
                take_profit REAL,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (
                        status IN (
                            'PENDING',
                            'OPEN',
                            'CLOSED',
                            'REJECTED'
                        )
                    ),
                risk_amount REAL NOT NULL
                    CHECK (risk_amount >= 0),
                realized_pnl REAL NOT NULL DEFAULT 0,
                opened_at TEXT,
                closed_at TEXT,
                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (account_id)
                    REFERENCES paper_accounts(account_id)
            );

            CREATE INDEX IF NOT EXISTS idx_trades_account
                ON trades(account_id);

            CREATE INDEX IF NOT EXISTS idx_trades_status
                ON trades(status);
            """
        )


if __name__ == "__main__":
    init_db()
    print("TaimoBot database initialized.")
