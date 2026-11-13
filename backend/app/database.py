
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


def migrate_trade_columns(connection):
    """Add new trade fields without deleting existing data."""

    existing_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(trades)"
        ).fetchall()
    }

    new_columns = {
        "position_group_id": "TEXT",
        "copied_from_trade_id": "TEXT",
        "copy_group_id": "TEXT"
    }

    for column_name, column_type in new_columns.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE trades ADD COLUMN "
                f"{column_name} {column_type}"
            )


def migrate_account_risk_profiles(connection):
    """Add and assign persistent risk profiles to existing accounts."""

    existing_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(paper_accounts)"
        ).fetchall()
    }

    if "risk_profile" not in existing_columns:
        connection.execute(
            "ALTER TABLE paper_accounts "
            "ADD COLUMN risk_profile TEXT"
        )

    connection.execute(
        """
        UPDATE paper_accounts
        SET risk_profile = CASE
            WHEN starting_balance < 5000 THEN 'STARTER'
            WHEN starting_balance < 25000 THEN 'STANDARD'
            ELSE 'PREMIUM'
        END
        WHERE risk_profile IS NULL
           OR risk_profile = ''
        """
    )


def init_db():
    """Create tables and safely migrate existing paper-trading data."""

    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_accounts (
                account_id TEXT PRIMARY KEY,
                starting_balance REAL NOT NULL
                    CHECK (starting_balance > 0),
                balance REAL NOT NULL
                    CHECK (balance >= 0),
                risk_profile TEXT,
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

            CREATE TABLE IF NOT EXISTS position_groups (
                group_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL
                    CHECK (side IN ('BUY', 'SELL')),
                status TEXT NOT NULL DEFAULT 'OPEN'
                    CHECK (status IN ('OPEN', 'CLOSED')),
                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,

                FOREIGN KEY (account_id)
                    REFERENCES paper_accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS copy_relationships (
                relationship_id TEXT PRIMARY KEY,
                source_account_id TEXT NOT NULL,
                follower_account_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'PAUSED', 'STOPPED')),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK (source_account_id <> follower_account_id),
                UNIQUE (source_account_id, follower_account_id),
                FOREIGN KEY (source_account_id)
                    REFERENCES paper_accounts(account_id),
                FOREIGN KEY (follower_account_id)
                    REFERENCES paper_accounts(account_id)
            );

            CREATE TABLE IF NOT EXISTS copy_executions (
                copy_execution_id TEXT PRIMARY KEY,
                relationship_id TEXT NOT NULL,
                source_trade_id TEXT NOT NULL,
                copied_trade_id TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (
                        status IN ('PENDING', 'COPIED', 'FAILED', 'CLOSED')
                    ),
                failure_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (relationship_id, source_trade_id),
                FOREIGN KEY (relationship_id)
                    REFERENCES copy_relationships(relationship_id),
                FOREIGN KEY (source_trade_id)
                    REFERENCES trades(trade_id),
                FOREIGN KEY (copied_trade_id)
                    REFERENCES trades(trade_id)
            );

            CREATE INDEX IF NOT EXISTS idx_copy_relationships_source
                ON copy_relationships(source_account_id, status);

            CREATE INDEX IF NOT EXISTS idx_copy_relationships_follower
                ON copy_relationships(follower_account_id, status);

            CREATE INDEX IF NOT EXISTS idx_copy_executions_source_trade
                ON copy_executions(source_trade_id);

            CREATE INDEX IF NOT EXISTS idx_copy_executions_copied_trade
                ON copy_executions(copied_trade_id);

            CREATE INDEX IF NOT EXISTS idx_trades_account
                ON trades(account_id);

            CREATE INDEX IF NOT EXISTS idx_trades_status
                ON trades(status);
            """
        )

        migrate_trade_columns(connection)
        migrate_account_risk_profiles(connection)

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_position_group
            ON trades(position_group_id)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_copy_group
            ON trades(copy_group_id)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_position_groups_account
            ON position_groups(account_id)
            """
        )


if __name__ == "__main__":
    init_db()
    print("TaimoBot database initialized.")
