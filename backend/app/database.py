
import os
import sqlite3
import threading
import fcntl
from pathlib import Path


_CONNECTION_GUARD = threading.RLock()
_OPEN_CONNECTIONS = set()


def restore_guard_path() -> Path:
    return DB_PATH.with_name(DB_PATH.name + ".restore.guard")


def _acquire_connection_guard_fd() -> int:
    """Hold a shared OS lock for the full lifetime of an app DB connection."""
    guard_path = restore_guard_path()
    descriptor = os.open(str(guard_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        lock_path = DB_PATH.with_name(DB_PATH.name + ".restore.lock")
        if lock_path.exists() or lock_path.is_symlink():
            raise sqlite3.OperationalError(
                "Database access blocked: a controlled restore has quiesced the application."
            )
        return descriptor
    except BlockingIOError as error:
        os.close(descriptor)
        raise sqlite3.OperationalError(
            "Database access blocked: a controlled restore is quiescing the application."
        ) from error
    except Exception:
        os.close(descriptor)
        raise


def open_connection_count() -> int:
    """Return the number of live application-managed SQLite connections in this process."""
    with _CONNECTION_GUARD:
        return len(_OPEN_CONNECTIONS)


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

    def close(self):
        try:
            return super().close()
        finally:
            guard_fd = getattr(self, "_restore_guard_fd", None)
            if guard_fd is not None:
                self._restore_guard_fd = None
                try:
                    os.close(guard_fd)
                except OSError:
                    pass
            with _CONNECTION_GUARD:
                _OPEN_CONNECTIONS.discard(self)


def get_connection():
    """Create a database connection unless a process-shared restore lock is active."""
    lock_path = DB_PATH.with_name(DB_PATH.name + ".restore.lock")
    # Serialize connection creation against restore-lock acquisition inside
    # this process, closing the check-then-connect race.
    with _CONNECTION_GUARD:
        try:
            restore_locked = lock_path.exists() or lock_path.is_symlink()
        except OSError:
            restore_locked = True
        if restore_locked:
            raise sqlite3.OperationalError(
                "Database access blocked: a controlled restore has quiesced the application."
            )

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        guard_fd = _acquire_connection_guard_fd()
        try:
            connection = sqlite3.connect(
                DB_PATH,
                timeout=30,
                factory=ClosingConnection
            )
            connection._restore_guard_fd = guard_fd
            _OPEN_CONNECTIONS.add(connection)
        except Exception:
            os.close(guard_fd)
            raise

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")

    # Connections opened before the restore lock must also stop accepting
    # writes once quiescence begins; checking only at connection creation
    # would leave an idle, previously-open connection as a bypass.
    write_actions = {
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_INDEX, sqlite3.SQLITE_CREATE_TEMP_TABLE,
        sqlite3.SQLITE_CREATE_TEMP_TRIGGER, sqlite3.SQLITE_CREATE_TEMP_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_INDEX, sqlite3.SQLITE_DROP_TEMP_TABLE,
        sqlite3.SQLITE_DROP_TEMP_TRIGGER, sqlite3.SQLITE_DROP_TEMP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER, sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
        sqlite3.SQLITE_ANALYZE, sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
    }

    def restore_quiescence_authorizer(action, arg1, arg2, database_name, trigger_name):
        try:
            locked = lock_path.exists() or lock_path.is_symlink()
        except OSError:
            locked = True
        if not locked:
            return sqlite3.SQLITE_OK

        # Once restore quiescence begins, pre-existing application handles
        # must not start new reads or writes. Transaction finalization remains
        # permitted so an already-open transaction can COMMIT/ROLLBACK and
        # release its SQLite locks; connection close is unaffected.
        blocked_actions = write_actions | {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_RECURSIVE,
        }
        if action in blocked_actions:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(restore_quiescence_authorizer)
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



def migrate_trade_cost_ledger(connection):
    """Add auditable close-cost fields without changing historical balances."""
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(trades)").fetchall()
    }
    new_columns = {
        "gross_realized_pnl": "REAL",
        "spread_cost": "REAL NOT NULL DEFAULT 0",
        "commission_cost": "REAL NOT NULL DEFAULT 0",
        "swap_cost": "REAL NOT NULL DEFAULT 0",
        "other_costs": "REAL NOT NULL DEFAULT 0",
    }
    for column_name, column_type in new_columns.items():
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE trades ADD COLUMN {column_name} {column_type}"
            )

    # Legacy realized_pnl remains authoritative net P&L for old rows.
    # With no historical cost records, preserve it as both gross and net.
    connection.execute(
        """
        UPDATE trades
        SET gross_realized_pnl = realized_pnl
        WHERE status = 'CLOSED'
          AND gross_realized_pnl IS NULL
        """
    )


def migrate_account_ownership(connection):
    """Add nullable ownership without assigning legacy accounts to strangers."""
    existing_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(paper_accounts)"
        ).fetchall()
    }

    if "owner_user_id" not in existing_columns:
        connection.execute(
            "ALTER TABLE paper_accounts ADD COLUMN owner_user_id TEXT "
            "REFERENCES users(user_id)"
        )


def migrate_participant_milestones(connection):
    """Backfill joining dates and recover first successful trade dates for legacy users."""
    connection.execute(
        """
        INSERT OR IGNORE INTO participant_milestones (user_id, joined_at)
        SELECT user_id, created_at
        FROM users
        """
    )

    # Existing participants may already have paper trades from before milestone
    # tracking existed. Preserve that history instead of leaving first_trade_at
    # blank. OPEN and CLOSED are the only successfully opened trade states.
    connection.execute(
        """
        UPDATE participant_milestones
        SET first_trade_at = (
            SELECT MIN(COALESCE(trades.opened_at, trades.created_at))
            FROM paper_accounts
            JOIN trades
              ON trades.account_id = paper_accounts.account_id
            WHERE paper_accounts.owner_user_id = participant_milestones.user_id
              AND trades.status IN ('OPEN', 'CLOSED')
        )
        WHERE first_trade_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM paper_accounts
              JOIN trades
                ON trades.account_id = paper_accounts.account_id
              WHERE paper_accounts.owner_user_id = participant_milestones.user_id
                AND trades.status IN ('OPEN', 'CLOSED')
          )
        """
    )


def migrate_account_settlement_currency(connection):
    """Add nullable settlement denomination without guessing legacy currency."""
    existing_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(paper_accounts)"
        ).fetchall()
    }
    if "settlement_currency" not in existing_columns:
        connection.execute(
            "ALTER TABLE paper_accounts ADD COLUMN settlement_currency TEXT"
        )

    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_paper_account_settlement_currency_immutable
        BEFORE UPDATE OF settlement_currency ON paper_accounts
        FOR EACH ROW
        WHEN OLD.settlement_currency IS NOT NULL
         AND NEW.settlement_currency IS NOT OLD.settlement_currency
        BEGIN
            SELECT RAISE(ABORT, 'Account settlement currency is immutable.');
        END
        """
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



def migrate_copy_execution_leases(connection):
    """Add token-owned expiry leases for safe copy-worker recovery."""
    existing_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(copy_executions)"
        ).fetchall()
    }

    if "lease_token" not in existing_columns:
        connection.execute(
            "ALTER TABLE copy_executions ADD COLUMN lease_token TEXT"
        )

    if "lease_expires_at" not in existing_columns:
        connection.execute(
            "ALTER TABLE copy_executions ADD COLUMN lease_expires_at TEXT"
        )

    # Legacy PENDING rows have no owner token. Expire them at migration so
    # the first new worker can safely claim and reconcile them.
    connection.execute(
        """
        UPDATE copy_executions
        SET lease_expires_at = CURRENT_TIMESTAMP
        WHERE status = 'PENDING'
          AND lease_token IS NULL
        """
    )


def seed_strategy_knowledge(connection):
    """Insert reference techniques as unvalidated knowledge, never as approved signals."""
    techniques = [["builtin-scalping","Scalping","SHORT_TERM","Short-duration trading that seeks small price movements and generally closes positions within a short time window.","Define a short-term market condition, confirm spread and volatility are acceptable, and require a clear entry trigger.","Use a predefined price or signal-based exit; consider costs and close when the setup invalidates.","Small targets can be consumed by spread, commission and slippage. Validate net expectancy and avoid excessive trade frequency."],["builtin-day-trading","Day trading","INTRADAY","Positions are opened and closed within the same trading day.","Use intraday market structure, session context and a documented entry trigger.","Close according to target, stop, invalidation or end-of-session rules.","Intraday volatility and transaction costs can materially affect outcomes."],["builtin-swing-trading","Swing trading","MULTI_DAY","Aims to capture price moves over multiple sessions.","Identify a broader directional or range setup and define invalidation before entry.","Use a planned target, trailing rule or setup invalidation.","Overnight gaps, swaps and wider stops must be included in risk calculations."],["builtin-trend-following","Trend following","TREND","Attempts to participate in sustained directional market movement.","Require objective trend conditions and a separate entry confirmation.","Exit on trend invalidation, protective stop or predefined trailing logic.","Can suffer repeated losses in sideways markets."],["builtin-breakout","Breakout trading","MOMENTUM","Looks for price to move beyond a defined support, resistance or range boundary.","Define the range objectively and require a breakout confirmation or retest.","Exit if the breakout fails, at a planned target or through a trailing rule.","False breakouts and slippage around fast moves are key risks."],["builtin-mean-reversion","Mean reversion","RANGE","Looks for price to return toward a defined average or range after an extension.","Require evidence of range conditions and a measurable deviation from a reference level.","Exit near the reference level or if the range thesis is invalidated.","Can fail severely when a new trend begins; never average down without validated risk."],["builtin-price-action","Price action","STRUCTURE","Uses price structure, candles and key levels to form trade hypotheses.","Define structure, key levels and a reproducible confirmation pattern.","Exit on structural invalidation or a predefined reward/risk plan.","Subjective pattern interpretation must be converted into testable rules."],["builtin-support-resistance","Support and resistance","LEVELS","Uses historically observed price areas as possible decision levels.","Identify levels from a documented lookback and require a reaction or confirmation.","Exit at the next level, target or invalidation point.","Levels are zones, not guarantees; avoid assuming a level must hold."],["builtin-momentum","Momentum trading","MOMENTUM","Seeks to participate in price moves with measurable directional strength.","Require a defined momentum measure and price confirmation.","Exit when momentum weakens, target is reached or stop is triggered.","Momentum can reverse quickly; account for volatility and execution costs."],["builtin-multi-timeframe","Multi-timeframe analysis","CONFIRMATION","Combines context from higher and lower chart timeframes.","Use a higher timeframe for context and a lower timeframe for a precise trigger.","Exit according to the execution timeframe while respecting higher-timeframe invalidation.","Timeframe conflicts and look-ahead bias must be controlled."],["builtin-volatility","Volatility-aware trading","VOLATILITY","Adapts entry filters, stop distances and targets to measured volatility.","Measure current volatility and require a suitable regime before entry.","Use volatility-adjusted exits and close if conditions materially change.","Volatility sizing must not increase monetary risk beyond the account limit."],["builtin-session","Session-based trading","SESSION","Studies recurring market-session behavior and liquidity conditions.","Use explicitly defined session windows and test across many dates.","Apply session-specific exits and avoid assuming historical patterns repeat.","Session effects can change and require ongoing out-of-sample validation."]]
    for (
        strategy_id, name, category, description,
        entry_logic, exit_logic, risk_notes
    ) in techniques:
        connection.execute(
            """
            INSERT OR IGNORE INTO strategy_knowledge (
                strategy_id, owner_user_id, name, category, description,
                entry_logic, exit_logic, risk_notes, provenance, evidence_status
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 'BUILTIN_REFERENCE', 'UNVALIDATED')
            """,
            (
                strategy_id, name, category, description,
                entry_logic, exit_logic, risk_notes
            )
        )




def ensure_market_observation_schema(connection):
    """Create the append-only, deduplicated market candle store."""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_observations (
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            open REAL NOT NULL CHECK (open > 0),
            high REAL NOT NULL CHECK (high > 0),
            low REAL NOT NULL CHECK (low > 0),
            close REAL NOT NULL CHECK (close > 0),
            volume REAL NOT NULL CHECK (volume >= 0),
            provenance_status TEXT NOT NULL DEFAULT 'UNVERIFIED_EXTERNAL_INPUT',
            received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (symbol, timeframe, observed_at, source),
            CHECK (low <= open AND low <= close),
            CHECK (high >= open AND high >= close),
            CHECK (low <= high)
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_observations_lookup
        ON market_observations(symbol, timeframe, observed_at DESC)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS market_backfill_checkpoints (
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            next_end_epoch INTEGER,
            status TEXT NOT NULL CHECK (status IN (
                'NOT_STARTED', 'RUNNING', 'COMPLETE', 'GAP_REVIEW', 'FAILED'
            )),
            earliest_observed_at TEXT,
            latest_observed_at TEXT,
            batches_completed INTEGER NOT NULL DEFAULT 0 CHECK (batches_completed >= 0),
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, timeframe)
        )
        """
    )

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_backfill_review_audit (
            review_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('RETRY_SAME_RANGE')),
            rationale TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (decided_by) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_market_backfill_review_history
            ON market_backfill_review_audit(symbol, timeframe, created_at DESC);

        CREATE TRIGGER IF NOT EXISTS prevent_market_backfill_review_update
        BEFORE UPDATE ON market_backfill_review_audit
        BEGIN
            SELECT RAISE(ABORT, 'Backfill review history is append-only.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_market_backfill_review_delete
        BEFORE DELETE ON market_backfill_review_audit
        BEGIN
            SELECT RAISE(ABORT, 'Backfill review history is append-only.');
        END;
        """
    )


def ensure_strategy_lab_schema(connection):
    """Create private, administrator-governed strategy research records."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS brain_strategy_candidates (
            candidate_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            hypothesis TEXT NOT NULL,
            instruments_json TEXT NOT NULL,
            timeframes_json TEXT NOT NULL,
            market_conditions TEXT NOT NULL,
            entry_logic TEXT NOT NULL,
            exit_logic TEXT NOT NULL,
            invalidation_logic TEXT NOT NULL,
            risk_notes TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'RESEARCH', 'REJECTED', 'APPROVED_FOR_STAGING'
            )),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (created_by) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_brain_strategy_candidates_status
            ON brain_strategy_candidates(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS brain_strategy_candidate_decisions (
            decision_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN (
                'REQUEST_MORE_RESEARCH', 'REJECT', 'APPROVE_FOR_STAGING'
            )),
            rationale TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES brain_strategy_candidates(candidate_id),
            FOREIGN KEY (decided_by) REFERENCES users(user_id)
        );

        CREATE TRIGGER IF NOT EXISTS prevent_brain_strategy_decision_update
        BEFORE UPDATE ON brain_strategy_candidate_decisions
        BEGIN
            SELECT RAISE(ABORT, 'Strategy decision history is append-only.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_brain_strategy_decision_delete
        BEFORE DELETE ON brain_strategy_candidate_decisions
        BEGIN
            SELECT RAISE(ABORT, 'Strategy decision history is append-only.');
        END;

        CREATE TABLE IF NOT EXISTS brain_strategy_lab_audit (
            event_id TEXT PRIMARY KEY,
            candidate_id TEXT NOT NULL,
            actor_user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (candidate_id) REFERENCES brain_strategy_candidates(candidate_id),
            FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
        );

        CREATE TRIGGER IF NOT EXISTS prevent_brain_strategy_lab_audit_update
        BEFORE UPDATE ON brain_strategy_lab_audit
        BEGIN
            SELECT RAISE(ABORT, 'Strategy lab audit history is append-only.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_brain_strategy_lab_audit_delete
        BEFORE DELETE ON brain_strategy_lab_audit
        BEGIN
            SELECT RAISE(ABORT, 'Strategy lab audit history is append-only.');
        END;
        """
    )



def ensure_session_calendar_schema(connection):
    """Create immutable, versioned calendar records and append-only review history."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS brain_session_calendars (
            calendar_id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            symbol TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version > 0),
            status TEXT NOT NULL CHECK (status IN (
                'PENDING_SOURCE_VERIFICATION',
                'SOURCE_VERIFIED_PENDING_APPROVAL',
                'APPROVED', 'REJECTED'
            )),
            record_json TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(provider, symbol, version),
            FOREIGN KEY (submitted_by) REFERENCES users(user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_brain_session_calendars_symbol
            ON brain_session_calendars(symbol, provider, version DESC);

        CREATE TRIGGER IF NOT EXISTS prevent_brain_session_calendar_record_mutation
        BEFORE UPDATE OF provider, symbol, version, record_json, submitted_by, created_at
        ON brain_session_calendars
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar versions are immutable.');
        END;

        DROP TRIGGER IF EXISTS validate_brain_session_calendar_status_transition;
        CREATE TRIGGER validate_brain_session_calendar_status_transition
        BEFORE UPDATE OF status ON brain_session_calendars
        WHEN NOT (
            (
                OLD.status = 'PENDING_SOURCE_VERIFICATION'
                AND NEW.status = 'SOURCE_VERIFIED_PENDING_APPROVAL'
                AND EXISTS (
                    SELECT 1 FROM brain_session_calendar_decisions
                    WHERE calendar_id = NEW.calendar_id AND decision = 'VERIFY_SOURCE'
                )
            ) OR (
                OLD.status = 'PENDING_SOURCE_VERIFICATION'
                AND NEW.status = 'REJECTED'
                AND EXISTS (
                    SELECT 1 FROM brain_session_calendar_decisions
                    WHERE calendar_id = NEW.calendar_id AND decision = 'REJECT'
                )
            ) OR (
                OLD.status = 'SOURCE_VERIFIED_PENDING_APPROVAL'
                AND NEW.status = 'APPROVED'
                AND EXISTS (
                    SELECT 1 FROM brain_session_calendar_decisions
                    WHERE calendar_id = NEW.calendar_id AND decision = 'APPROVE_CALENDAR'
                )
            ) OR (
                OLD.status = 'SOURCE_VERIFIED_PENDING_APPROVAL'
                AND NEW.status = 'PENDING_SOURCE_VERIFICATION'
                AND EXISTS (
                    SELECT 1 FROM brain_session_calendar_decisions
                    WHERE calendar_id = NEW.calendar_id AND decision = 'REQUEST_CHANGES'
                )
            ) OR (
                OLD.status = 'SOURCE_VERIFIED_PENDING_APPROVAL'
                AND NEW.status = 'REJECTED'
                AND EXISTS (
                    SELECT 1 FROM brain_session_calendar_decisions
                    WHERE calendar_id = NEW.calendar_id AND decision = 'REJECT'
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar status changes require a matching recorded decision.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_brain_session_calendar_delete
        BEFORE DELETE ON brain_session_calendars
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar versions cannot be deleted.');
        END;

        CREATE TABLE IF NOT EXISTS brain_session_calendar_decisions (
            decision_id TEXT PRIMARY KEY,
            calendar_id TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN (
                'VERIFY_SOURCE', 'APPROVE_CALENDAR', 'REQUEST_CHANGES', 'REJECT'
            )),
            rationale TEXT NOT NULL,
            evidence_url TEXT,
            decided_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (calendar_id) REFERENCES brain_session_calendars(calendar_id),
            FOREIGN KEY (decided_by) REFERENCES users(user_id)
        );

        CREATE TRIGGER IF NOT EXISTS prevent_brain_session_calendar_decision_update
        BEFORE UPDATE ON brain_session_calendar_decisions
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar decision history is append-only.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_brain_session_calendar_decision_delete
        BEFORE DELETE ON brain_session_calendar_decisions
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar decision history is append-only.');
        END;

        CREATE TABLE IF NOT EXISTS brain_session_calendar_audit (
            event_id TEXT PRIMARY KEY,
            calendar_id TEXT NOT NULL,
            actor_user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (calendar_id) REFERENCES brain_session_calendars(calendar_id),
            FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
        );

        CREATE TRIGGER IF NOT EXISTS prevent_brain_session_calendar_audit_update
        BEFORE UPDATE ON brain_session_calendar_audit
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar audit history is append-only.');
        END;

        CREATE TRIGGER IF NOT EXISTS prevent_brain_session_calendar_audit_delete
        BEFORE DELETE ON brain_session_calendar_audit
        BEGIN
            SELECT RAISE(ABORT, 'Session calendar audit history is append-only.');
        END;
        """
    )


def init_db():
    """Create tables and safely migrate existing paper-trading data."""

    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS participant_display_currency_preferences (
                user_id TEXT PRIMARY KEY,
                currency TEXT NOT NULL CHECK (currency IN ('ZAR', 'USD', 'EUR', 'GBP', 'KWD')),
                selected_at TEXT NOT NULL,
                confirmed INTEGER NOT NULL CHECK (confirmed = 1),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE RESTRICT
            );

            CREATE TRIGGER IF NOT EXISTS prevent_display_currency_preference_update
            BEFORE UPDATE ON participant_display_currency_preferences
            BEGIN
                SELECT RAISE(ABORT, 'Display currency preference is immutable.');
            END;

            CREATE TRIGGER IF NOT EXISTS prevent_display_currency_preference_delete
            BEFORE DELETE ON participant_display_currency_preferences
            BEGIN
                SELECT RAISE(ABORT, 'Display currency preference cannot be deleted.');
            END;

            CREATE TABLE IF NOT EXISTS participant_milestones (
                user_id TEXT PRIMARY KEY,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                first_trade_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS auth_login_attempts (
                email TEXT PRIMARY KEY,
                attempts INTEGER NOT NULL DEFAULT 0,
                window_started_at TEXT NOT NULL,
                locked_until TEXT
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                ON auth_sessions(user_id);

            CREATE TABLE IF NOT EXISTS paper_accounts (
                account_id TEXT PRIMARY KEY,
                owner_user_id TEXT,
                starting_balance REAL NOT NULL
                    CHECK (starting_balance > 0),
                balance REAL NOT NULL
                    CHECK (balance >= 0),
                risk_profile TEXT,
                settlement_currency TEXT,
                created_at TEXT NOT NULL
                    DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
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

            CREATE TABLE IF NOT EXISTS system_safety_state (
                singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                emergency_stop_active INTEGER NOT NULL DEFAULT 0
                    CHECK (emergency_stop_active IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT OR IGNORE INTO system_safety_state (
                singleton_id, emergency_stop_active
            ) VALUES (1, 0);

            CREATE TABLE IF NOT EXISTS strategy_knowledge (
                strategy_id TEXT PRIMARY KEY,
                owner_user_id TEXT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                entry_logic TEXT NOT NULL,
                exit_logic TEXT NOT NULL,
                risk_notes TEXT NOT NULL,
                provenance TEXT NOT NULL DEFAULT 'USER_SUBMITTED',
                evidence_status TEXT NOT NULL DEFAULT 'UNVALIDATED'
                    CHECK (evidence_status IN (
                        'UNVALIDATED', 'BACKTESTED', 'PAPER_TESTED',
                        'VALIDATED', 'RETIRED'
                    )),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_user_id) REFERENCES users(user_id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_strategy_knowledge_owner
                ON strategy_knowledge(owner_user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_strategy_knowledge_category
                ON strategy_knowledge(category, evidence_status);
            """
        )


        seed_strategy_knowledge(connection)
        ensure_market_observation_schema(connection)
        ensure_strategy_lab_schema(connection)
        ensure_session_calendar_schema(connection)

        migrate_trade_columns(connection)
        migrate_trade_cost_ledger(connection)
        migrate_account_ownership(connection)
        migrate_participant_milestones(connection)
        migrate_account_risk_profiles(connection)
        migrate_account_settlement_currency(connection)
        migrate_copy_execution_leases(connection)

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
