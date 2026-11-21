import re
import asyncio
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, StrictFloat, StrictInt

from .database import init_db, get_connection
from .maintenance_scheduler import (
    maintenance_scheduler,
    get_maintenance_status,
    run_maintenance_for_date,
)
from .maintenance import (
    BACKUP_DIR, SAST, create_verified_backup, is_maintenance_window,
    inspect_recovery_candidates, inspect_recovery_artifact_inventory,
    verify_restore_candidate, sha256_file,
    verify_approved_restore_plan,
)
from .cors import configure_cors
from .risk import (
    calculate_position_size,
    RiskError,
    MAX_RISK_PERCENT
)
from .execution import (
    create_paper_account,
    create_position_group,
    get_position_group,
    open_paper_trade,
    get_open_risk,
    validate_symbol,
    DEFAULT_SYMBOL,
    create_copy_relationship,
    get_copy_relationships,
    update_copy_relationship_status
)
from .copy_trading import (
    replicate_open_trade,
    close_trade_with_copies,
    close_position_group_with_copies
)
from .history import (
    get_account_history,
    get_trade_history
)
from .currency_preference_routes import router as currency_preference_router
from .auth_routes import (
    router as auth_router,
    current_user,
    require_account_access,
    require_group_access,
    require_trade_access,
    require_relationship_access,
    require_admin,
)
from .strategy_knowledge import list_strategies, create_strategy, get_strategy
from .profit_taking import evaluate_position_profit, evaluate_group_profit
from .safety import (
    activate_emergency_stop,
    deactivate_emergency_stop,
    is_emergency_stop_active
)
from .instruments import list_instruments, EXECUTION_ENABLED_SYMBOLS
from .market_observation_store import list_market_observations, inspect_market_observation_quality
from .deriv_public_client import DerivPublicMarketDataClient
from .market_observations import SUPPORTED_TIMEFRAMES
from .instruments import get_instrument


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    maintenance_task = asyncio.create_task(maintenance_scheduler())
    try:
        yield
    finally:
        maintenance_task.cancel()
        try:
            await maintenance_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="TaimoBot API",
    description="Multi-instrument paper trading backend for TaimoBot",
    version="0.7.0",
    lifespan=lifespan
)

app.include_router(auth_router)
app.include_router(currency_preference_router)

# Cross-origin browser access is disabled unless exact trusted origins are configured.
configure_cors(app, os.getenv("TAIMOBOT_CORS_ORIGINS", ""))


class RiskPreviewRequest(BaseModel):
    account_balance: float = Field(gt=0)
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    risk_percent: float = Field(gt=0, le=1.0)
    side: Literal["BUY", "SELL"]


class AccountRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=100)
    starting_balance: float = Field(gt=0)


class PositionGroupRequest(BaseModel):
    symbol: str = Field(
        default=DEFAULT_SYMBOL,
        min_length=1,
        max_length=30,
        pattern=r"^[A-Za-z0-9._/-]+$"
    )
    side: Literal["BUY", "SELL"]


class TradeRequest(BaseModel):
    account_id: str = Field(min_length=1, max_length=100)
    client_order_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(
        default=DEFAULT_SYMBOL,
        min_length=1,
        max_length=30,
        pattern=r"^[A-Za-z0-9._/-]+$"
    )
    side: Literal["BUY", "SELL"]
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    risk_percent: float = Field(gt=0, le=1.0)
    take_profit: float | None = Field(default=None, gt=0)
    position_group_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100
    )


class CloseTradeRequest(BaseModel):
    exit_price: StrictFloat = Field(gt=0)
    spread_cost: StrictFloat = Field(default=0, ge=0)
    commission_cost: StrictFloat = Field(default=0, ge=0)
    swap_cost: StrictFloat = Field(default=0, ge=0)
    other_costs: StrictFloat = Field(default=0, ge=0)


class CloseGroupRequest(BaseModel):
    exit_price: StrictFloat = Field(gt=0)
    spread_cost: StrictFloat = Field(default=0, ge=0)
    commission_cost: StrictFloat = Field(default=0, ge=0)
    swap_cost: StrictFloat = Field(default=0, ge=0)
    other_costs: StrictFloat = Field(default=0, ge=0)


class ProfitPreviewRequest(BaseModel):
    current_price: StrictFloat = Field(gt=0)
    spread_cost: StrictFloat = Field(default=0, ge=0)
    commission_cost: StrictFloat = Field(default=0, ge=0)
    swap_cost: StrictFloat = Field(default=0, ge=0)
    other_costs: StrictFloat = Field(default=0, ge=0)
    minimum_net_profit: StrictFloat = Field(default=0, ge=0)


class CopyRelationshipRequest(BaseModel):
    source_account_id: str = Field(min_length=1, max_length=100)


class MaintenanceRetryRequest(BaseModel):
    run_date: date
    retry_mode: Literal["FAILED", "STALE_RUNNING"]


class MaintenanceRestorePrepareRequest(BaseModel):
    backup_file_name: str = Field(min_length=1, max_length=255, pattern=r"^taimobot-[A-Za-z0-9._-]+[.]sqlite3$")
    confirm_preparation: bool


class MaintenanceRestoreApproveRequest(BaseModel):
    plan_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    confirm_approval: bool


class MaintenanceRestorePreflightRequest(BaseModel):
    plan_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")


class StrategyKnowledgeRequest(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    category: str = Field(min_length=3, max_length=40, pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str = Field(min_length=10, max_length=1000)
    entry_logic: str = Field(min_length=10, max_length=2000)
    exit_logic: str = Field(min_length=10, max_length=2000)
    risk_notes: str = Field(min_length=10, max_length=2000)


class DerivMarketSyncRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9._/-]+$")
    timeframe: str = Field(min_length=2, max_length=3)
    count: StrictInt = Field(default=100, ge=1, le=100)
    confirm_collection: bool


@app.get("/instruments")
def instrument_readiness(user=Depends(current_user)):
    """Read-only catalogue and paper-execution readiness; no market feed is implied."""
    instruments = []
    for instrument in list_instruments():
        symbol = instrument["symbol"]
        execution_enabled = symbol in EXECUTION_ENABLED_SYMBOLS
        instruments.append({
            **instrument,
            "paper_execution_status": (
                "PAPER_EXECUTION_ENABLED" if execution_enabled else "OBSERVATION_ONLY"
            ),
            "market_data_status": "NOT_CONNECTED",
            "broker_specifications_status": "NOT_CONFIGURED",
            "sizing_mode": "ABSTRACT_PAPER_UNITS" if execution_enabled else "DISABLED",
        })
    return {
        "mode": "paper_trading",
        "read_only": True,
        "execution_triggered": False,
        "instruments": instruments,
    }


@app.get("/")
def home():
    return {
        "application": "TaimoBot",
        "status": "running",
        "mode": "paper_trading",
        "market_coverage": "broker_supported_instruments",
        "default_symbol": DEFAULT_SYMBOL,
        "timezone": "Africa/Johannesburg"
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "api": "TaimoBot",
        "trading_mode": "paper"
    }


@app.post("/maintenance/retry")
def retry_maintenance(request: MaintenanceRetryRequest, user=Depends(current_user)):
    """Admin-only, explicit recovery for a failed or stale maintenance run."""
    require_admin(user)

    # A recovery request must match an existing persisted run. Without this
    # check, retry_failed=True could create a brand-new run for an arbitrary
    # date instead of recovering a failed attempt.
    with get_connection() as connection:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'maintenance_runs'
            """
        ).fetchone()
        run = (
            connection.execute(
                "SELECT status FROM maintenance_runs WHERE run_date = ?",
                (request.run_date.isoformat(),),
            ).fetchone()
            if table is not None
            else None
        )

    if run is None:
        raise HTTPException(
            status_code=404,
            detail="No maintenance run exists for the requested date.",
        )

    expected_status = "FAILED" if request.retry_mode == "FAILED" else "RUNNING"
    if run["status"] != expected_status:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Retry mode {request.retry_mode} requires an existing "
                f"{expected_status} maintenance run; found {run['status']}."
            ),
        )

    result = run_maintenance_for_date(
        request.run_date,
        retry_failed=request.retry_mode == "FAILED",
        retry_stale_running=request.retry_mode == "STALE_RUNNING",
    )

    response = {
        "status": result["status"],
        "run_date": result.get("run_date", request.run_date.isoformat()),
        "operator_triggered": True,
    }
    if result.get("reason"):
        response["reason"] = result["reason"]
    if result.get("error"):
        response["error"] = result["error"]
    if result.get("backup"):
        response["backup"] = {
            "status": result["backup"].get("status"),
            "integrity_check": result["backup"].get("integrity_check"),
            "created_at": result["backup"].get("created_at"),
            "timezone": result["backup"].get("timezone"),
        }
    if result.get("restore_verification"):
        response["restore_verification"] = {
            "status": result["restore_verification"].get("status"),
            "integrity_check": result["restore_verification"].get("integrity_check"),
            "live_database_replaced": False,
        }
    return response


@app.get("/maintenance/recovery-candidates")
def maintenance_recovery_candidates(user=Depends(current_user)):
    """Admin-only, read-only inventory of verified recovery candidates."""
    require_admin(user)
    return inspect_recovery_candidates()


@app.get("/maintenance/recovery-artifact-inventory")
def maintenance_recovery_artifact_inventory(user=Depends(current_user)):
    """Admin-only, read-only cross-check of managed recovery artifacts and references."""
    require_admin(user)
    return inspect_recovery_artifact_inventory()


@app.post("/maintenance/restore/prepare")
def prepare_maintenance_restore(request: MaintenanceRestorePrepareRequest, user=Depends(current_user)):
    """Prepare an admin restore in isolation; never replace the live database."""
    require_admin(user)
    if not request.confirm_preparation:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required.")
    inventory = inspect_recovery_candidates()
    if inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
        raise HTTPException(status_code=503, detail="Recovery inventory is unavailable.")
    candidate = next((item for item in inventory.get("candidates", []) if item.get("file_name") == request.backup_file_name), None)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Recovery backup was not found.")
    if not candidate.get("restore_ready"):
        raise HTTPException(status_code=409, detail="Selected backup failed recovery-readiness checks.")
    backup_directory = Path(BACKUP_DIR).resolve()
    selected_backup = backup_directory / request.backup_file_name
    if selected_backup.is_symlink() or not selected_backup.is_file() or selected_backup.resolve().parent != backup_directory:
        raise HTTPException(status_code=409, detail="Selected backup is not a safe managed file.")
    inventory_fingerprint = candidate.get("sha256")
    if not isinstance(inventory_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", inventory_fingerprint):
        raise HTTPException(status_code=409, detail="Selected backup has no trustworthy inventory fingerprint.")
    try:
        if sha256_file(selected_backup) != inventory_fingerprint:
            raise HTTPException(status_code=409, detail="Selected backup changed after recovery inventory; refresh the inventory and prepare again.")
    except OSError as error:
        raise HTTPException(status_code=409, detail="Selected backup could not be fingerprinted safely.") from error
    plan_id = uuid4().hex
    staging_path = backup_directory / "recovery-staging" / ("taimobot-restore-" + plan_id + ".sqlite3")
    try:
        pre_restore_backup = create_verified_backup()
        if pre_restore_backup.get("status") != "BACKUP_VERIFIED":
            raise RuntimeError("Fresh pre-restore backup was not verified.")
        staged = verify_restore_candidate(selected_backup, staging_path)
        if staged.get("status") != "RESTORE_CANDIDATE_VERIFIED":
            raise RuntimeError("Selected restore candidate did not verify.")
        staged_inventory = inspect_recovery_candidates(staging_path.parent)
        staged_record = next((item for item in staged_inventory.get("candidates", []) if item.get("file_name") == staging_path.name), None)
        if staged_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE" or staged_record is None or not staged_record.get("restore_ready"):
            raise RuntimeError("Staged restore failed schema or foreign-key verification.")
        selected_backup_hash = sha256_file(selected_backup)
        if selected_backup_hash != inventory_fingerprint:
            raise RuntimeError("Selected backup changed while the isolated candidate was being staged.")
        staged_candidate_hash = sha256_file(staging_path)
        rollback_backup_path = Path(pre_restore_backup["backup_file"]).resolve()
        rollback_backup_hash = sha256_file(rollback_backup_path)
        with get_connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS maintenance_restore_plans (plan_id TEXT PRIMARY KEY, backup_file TEXT NOT NULL, staging_file TEXT NOT NULL, pre_restore_backup_file TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('PREPARED','APPROVED','APPLIED','FAILED','EXPIRED')), created_at TEXT NOT NULL, created_by TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS maintenance_restore_artifacts (plan_id TEXT PRIMARY KEY REFERENCES maintenance_restore_plans(plan_id) ON DELETE CASCADE, source_sha256 TEXT NOT NULL, staged_sha256 TEXT NOT NULL, rollback_sha256 TEXT NOT NULL)")
            connection.execute("INSERT INTO maintenance_restore_plans (plan_id, backup_file, staging_file, pre_restore_backup_file, status, created_at, created_by) VALUES (?, ?, ?, ?, 'PREPARED', ?, ?)", (plan_id, str(selected_backup.resolve()), str(staging_path.resolve()), str(rollback_backup_path), datetime.now(SAST).isoformat(), str(user.get('email', ''))))
            connection.execute("INSERT INTO maintenance_restore_artifacts (plan_id, source_sha256, staged_sha256, rollback_sha256) VALUES (?, ?, ?, ?)", (plan_id, selected_backup_hash, staged_candidate_hash, rollback_backup_hash))
    except Exception as error:
        try:
            staging_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail="Restore preparation failed safely: " + str(error)[:200]) from error
    return {"status":"RESTORE_PREPARED", "plan_id":plan_id, "selected_backup":request.backup_file_name, "selected_backup_verified":True, "pre_restore_backup_verified":True, "staged_candidate_verified":True, "approval_required":True, "live_database_replaced":False, "trading_mode":"paper"}



@app.post("/maintenance/restore/approve")
def approve_maintenance_restore(request: MaintenanceRestoreApproveRequest, user=Depends(current_user)):
    """Approve a prepared restore only after revalidating every recovery artifact."""
    require_admin(user)
    if not request.confirm_approval:
        raise HTTPException(status_code=400, detail="Explicit restore approval is required.")

    with get_connection() as connection:
        plan = connection.execute(
            """
            SELECT plan_id, backup_file, staging_file, pre_restore_backup_file,
                   status, created_at
            FROM maintenance_restore_plans WHERE plan_id = ?
            """,
            (request.plan_id,),
        ).fetchone()
        artifact_hashes = connection.execute(
            """
            SELECT source_sha256, staged_sha256, rollback_sha256
            FROM maintenance_restore_artifacts WHERE plan_id = ?
            """,
            (request.plan_id,),
        ).fetchone() if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_restore_artifacts'"
        ).fetchone() else None

    if plan is None:
        raise HTTPException(status_code=404, detail="Restore plan not found.")
    if plan["status"] != "PREPARED":
        raise HTTPException(status_code=409, detail="Only a PREPARED restore plan can be approved.")
    if artifact_hashes is None:
        raise HTTPException(status_code=409, detail="Restore plan has no immutable artifact fingerprints; prepare it again.")

    try:
        created_at = datetime.fromisoformat(plan["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("Restore plan timestamp has no timezone.")
        age_seconds = (datetime.now(SAST) - created_at.astimezone(SAST)).total_seconds()
        if age_seconds < 0 or age_seconds > 30 * 60:
            raise ValueError("Restore plan has expired.")

        backup_directory = Path(BACKUP_DIR).resolve()
        staging_directory = (backup_directory / "recovery-staging").resolve()
        selected_backup = Path(plan["backup_file"])
        staging_file = Path(plan["staging_file"])
        pre_restore_backup = Path(plan["pre_restore_backup_file"])
        for artifact, expected_parent in (
            (selected_backup, backup_directory),
            (pre_restore_backup, backup_directory),
            (staging_file, staging_directory),
        ):
            if artifact.is_symlink() or not artifact.is_file() or artifact.resolve().parent != expected_parent:
                raise ValueError("A required restore artifact is missing or outside its managed directory.")

        source_inventory = inspect_recovery_candidates(backup_directory)
        staged_inventory = inspect_recovery_candidates(staging_directory)
        source_records = {item.get("file_name"): item for item in source_inventory.get("candidates", [])}
        staged_records = {item.get("file_name"): item for item in staged_inventory.get("candidates", [])}
        if source_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
            raise ValueError("Managed backup inventory could not be verified.")
        if staged_inventory.get("status") != "RECOVERY_INVENTORY_COMPLETE":
            raise ValueError("Staged recovery inventory could not be verified.")
        if not source_records.get(selected_backup.name, {}).get("restore_ready"):
            raise ValueError("Selected source backup no longer passes recovery checks.")
        if not source_records.get(pre_restore_backup.name, {}).get("restore_ready"):
            raise ValueError("Pre-restore rollback backup no longer passes recovery checks.")
        if not staged_records.get(staging_file.name, {}).get("restore_ready"):
            raise ValueError("Staged restore candidate no longer passes recovery checks.")

        current_hashes = (
            sha256_file(selected_backup),
            sha256_file(staging_file),
            sha256_file(pre_restore_backup),
        )
        expected_hashes = (
            artifact_hashes["source_sha256"],
            artifact_hashes["staged_sha256"],
            artifact_hashes["rollback_sha256"],
        )
        if current_hashes != expected_hashes:
            raise ValueError("Recovery artifact fingerprint mismatch: an artifact changed after preparation; create a new restore plan.")

        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")

            # Re-read the exact plan and immutable artifact fingerprints while
            # holding the write transaction. This closes the gap between the
            # initial filesystem validation and the status compare-and-set.
            current_plan = connection.execute(
                """
                SELECT backup_file, staging_file, pre_restore_backup_file,
                       status, created_at
                FROM maintenance_restore_plans WHERE plan_id = ?
                """,
                (request.plan_id,),
            ).fetchone()
            expected_plan_state = (
                str(selected_backup), str(staging_file),
                str(pre_restore_backup), "PREPARED", plan["created_at"],
            )
            actual_plan_state = (
                current_plan["backup_file"], current_plan["staging_file"],
                current_plan["pre_restore_backup_file"], current_plan["status"],
                current_plan["created_at"],
            ) if current_plan is not None else None
            if actual_plan_state != expected_plan_state:
                connection.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Restore plan metadata changed during approval; prepare a new plan.",
                )

            current_artifact_hashes = connection.execute(
                """
                SELECT source_sha256, staged_sha256, rollback_sha256
                FROM maintenance_restore_artifacts WHERE plan_id = ?
                """,
                (request.plan_id,),
            ).fetchone()
            actual_hash_state = (
                current_artifact_hashes["source_sha256"],
                current_artifact_hashes["staged_sha256"],
                current_artifact_hashes["rollback_sha256"],
            ) if current_artifact_hashes is not None else None
            if actual_hash_state != expected_hashes:
                connection.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Restore artifact fingerprints changed during approval; prepare a new plan.",
                )

            final_hashes = (
                sha256_file(selected_backup),
                sha256_file(staging_file),
                sha256_file(pre_restore_backup),
            )
            if final_hashes != expected_hashes:
                connection.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="A recovery artifact changed during approval; prepare a new plan.",
                )

            updated = connection.execute(
                """
                UPDATE maintenance_restore_plans SET status = 'APPROVED'
                WHERE plan_id = ? AND status = 'PREPARED'
                """,
                (request.plan_id,),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise HTTPException(status_code=409, detail="Restore plan changed before approval.")
            connection.commit()
    except HTTPException:
        raise
    except (OSError, sqlite3.Error, ValueError) as error:
        raise HTTPException(status_code=409, detail="Restore approval blocked: " + str(error)[:180]) from error

    return {
        "status": "RESTORE_APPROVED",
        "plan_id": request.plan_id,
        "source_backup_verified": True,
        "pre_restore_backup_verified": True,
        "staged_candidate_verified": True,
        "restore_application_authorized": False,
        "live_database_replaced": False,
        "trading_mode": "paper",
    }


@app.post("/maintenance/restore/preflight")
def preflight_maintenance_restore(request: MaintenanceRestorePreflightRequest, user=Depends(current_user)):
    """Admin-only read-only preflight for an approved restore plan."""
    require_admin(user)
    with get_connection() as connection:
        plan = connection.execute(
            """
            SELECT plan_id, backup_file, staging_file, pre_restore_backup_file,
                   status, created_at
            FROM maintenance_restore_plans WHERE plan_id = ?
            """,
            (request.plan_id,),
        ).fetchone()
        artifact_hashes = connection.execute(
            """
            SELECT source_sha256, staged_sha256, rollback_sha256
            FROM maintenance_restore_artifacts WHERE plan_id = ?
            """,
            (request.plan_id,),
        ).fetchone() if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_restore_artifacts'"
        ).fetchone() else None

    if plan is None:
        raise HTTPException(status_code=404, detail="Restore plan not found.")
    if artifact_hashes is None:
        raise HTTPException(status_code=409, detail="Restore plan has no immutable artifact fingerprints.")

    try:
        return verify_approved_restore_plan(plan, artifact_hashes)
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        raise HTTPException(status_code=409, detail="Restore preflight blocked: " + str(error)[:180]) from error


@app.get("/safety/status")
def safety_status():
    emergency_stop = is_emergency_stop_active()
    maintenance_active = is_maintenance_window()
    maintenance = get_maintenance_status()
    new_orders_allowed = not emergency_stop and not maintenance_active

    if emergency_stop:
        block_reason = "EMERGENCY_STOP"
    elif maintenance_active:
        block_reason = "DAILY_MAINTENANCE"
    else:
        block_reason = None

    return {
        "emergency_stop": emergency_stop,
        "maintenance_window_active": maintenance_active,
        "maintenance": maintenance,
        "new_orders_allowed": new_orders_allowed,
        "block_reason": block_reason,
        "trading_mode": "paper",
        "timezone": "Africa/Johannesburg"
    }


@app.post("/safety/emergency-stop")
def emergency_stop(user=Depends(current_user)):
    require_admin(user)
    return activate_emergency_stop()


@app.post("/safety/resume")
def resume_trading(user=Depends(current_user)):
    require_admin(user)
    return deactivate_emergency_stop()


@app.post("/risk/preview")
def risk_preview(request: RiskPreviewRequest):
    try:
        return calculate_position_size(
            account_balance=request.account_balance,
            entry_price=request.entry_price,
            stop_loss=request.stop_loss,
            risk_percent=request.risk_percent,
            side=request.side
        )
    except RiskError as error:
        raise HTTPException(
            status_code=422,
            detail=str(error)
        )


@app.get("/accounts/{account_id}/risk")
def account_risk_status(account_id: str, user=Depends(current_user)):
    require_account_access(account_id, user)
    connection = get_connection()

    try:
        account = connection.execute(
            """
            SELECT balance
            FROM paper_accounts
            WHERE account_id = ?
            """,
            (account_id,)
        ).fetchone()

        if account is None:
            raise HTTPException(
                status_code=404,
                detail="Paper account not found."
            )

        balance = float(account["balance"])
        open_risk = get_open_risk(connection, account_id)

        maximum_risk = balance * (MAX_RISK_PERCENT / 100)
        remaining_risk = max(0.0, maximum_risk - open_risk)

        open_positions = connection.execute(
            """
            SELECT COUNT(*) AS position_count
            FROM trades
            WHERE account_id = ?
              AND status = 'OPEN'
            """,
            (account_id,)
        ).fetchone()

        return {
            "account_id": account_id,
            "account_balance": round(balance, 2),
            "open_positions": open_positions["position_count"],
            "current_open_risk": round(open_risk, 2),
            "maximum_risk_percent": MAX_RISK_PERCENT,
            "maximum_risk_amount": round(maximum_risk, 2),
            "remaining_risk_budget": round(remaining_risk, 2),
            "risk_limit_reached": open_risk >= maximum_risk,
            "mode": "paper_trading"
        }

    finally:
        connection.close()


@app.get("/accounts")
def list_accounts(user=Depends(current_user)):
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT account_id, starting_balance, balance, risk_profile, created_at
            FROM paper_accounts
            WHERE owner_user_id = ?
            ORDER BY created_at DESC, account_id
            """,
            (user["user_id"],)
        ).fetchall()

    return {
        "count": len(rows),
        "accounts": [
            {
                "account_id": row["account_id"],
                "starting_balance": float(row["starting_balance"]),
                "balance": float(row["balance"]),
                "risk_profile": row["risk_profile"],
                "created_at": row["created_at"],
                "mode": "paper_trading"
            }
            for row in rows
        ]
    }


@app.post("/accounts")
def create_account(request: AccountRequest, user=Depends(current_user)):
    try:
        return create_paper_account(
            request.account_id,
            request.starting_balance,
            owner_user_id=user["user_id"]
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )


@app.get("/accounts/{account_id}")
def account_history(account_id: str, user=Depends(current_user)):
    require_account_access(account_id, user)
    try:
        return get_account_history(account_id)
    except ValueError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error)
        )


@app.get("/accounts/{account_id}/trades")
def trade_history(account_id: str, limit: int = 50, user=Depends(current_user)):
    require_account_access(account_id, user)
    try:
        return get_trade_history(account_id, limit)
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )


@app.post("/accounts/{account_id}/groups")
def create_group(
    account_id: str,
    request: PositionGroupRequest,
    user=Depends(current_user)
):
    require_account_access(account_id, user)
    try:
        symbol = validate_symbol(request.symbol)

        return create_position_group(
            account_id=account_id,
            symbol=symbol,
            side=request.side
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )


@app.get("/groups/{group_id}")
def group_details(group_id: str, user=Depends(current_user)):
    require_group_access(group_id, user)
    try:
        return get_position_group(group_id)
    except ValueError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error)
        )


@app.post("/groups/{group_id}/profit-preview")
def group_profit_preview(
    group_id: str,
    request: ProfitPreviewRequest,
    user=Depends(current_user)
):
    """Return a read-only aggregate net profit estimate for an open group."""
    require_group_access(group_id, user)
    with get_connection() as connection:
        group = connection.execute(
            """
            SELECT group_id, account_id, symbol, side, status
            FROM position_groups
            WHERE group_id = ?
            """,
            (group_id,)
        ).fetchone()
        trades = connection.execute(
            """
            SELECT trade_id, side, entry_price, quantity
            FROM trades
            WHERE position_group_id = ?
              AND status = 'OPEN'
            ORDER BY trade_id
            """,
            (group_id,)
        ).fetchall()

    if group is None:
        raise HTTPException(status_code=404, detail="Position group not found.")
    if group["status"] != "OPEN":
        raise HTTPException(
            status_code=409,
            detail="Profit preview is available only for open position groups."
        )
    if not trades:
        raise HTTPException(
            status_code=409,
            detail="Profit preview is available only for groups with open positions."
        )

    # Request costs are group-level estimates. Allocate them by quantity so
    # they are counted once in the aggregate, not once per position.
    total_quantity = sum(float(trade["quantity"]) for trade in trades)
    cost_totals = {
        "spread_cost": request.spread_cost,
        "commission_cost": request.commission_cost,
        "swap_cost": request.swap_cost,
        "other_costs": request.other_costs,
    }
    positions = []
    trade_ids = []
    remaining_costs = dict(cost_totals)
    for index, trade in enumerate(trades):
        weight = float(trade["quantity"]) / total_quantity
        if index == len(trades) - 1:
            allocated_costs = dict(remaining_costs)
        else:
            allocated_costs = {
                name: amount * weight
                for name, amount in cost_totals.items()
            }
            for name, amount in allocated_costs.items():
                remaining_costs[name] -= amount
        positions.append({
            "side": trade["side"],
            "entry_price": trade["entry_price"],
            "quantity": trade["quantity"],
            "current_price": request.current_price,
            **allocated_costs,
        })
        trade_ids.append(trade["trade_id"])

    try:
        result = evaluate_group_profit(
            positions,
            minimum_net_profit=request.minimum_net_profit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    return {
        "group_id": group["group_id"],
        "account_id": group["account_id"],
        "symbol": group["symbol"],
        "trade_ids": trade_ids,
        **result,
        "read_only": True,
        "execution_triggered": False,
        "cost_allocation": "proportional_to_position_quantity",
    }


@app.post("/groups/{group_id}/close")
def close_group(
    group_id: str,
    request: CloseGroupRequest,
    user=Depends(current_user)
):
    require_group_access(group_id, user)
    try:
        return close_position_group_with_copies(
            group_id=group_id,
            exit_price=request.exit_price,
            spread_cost=request.spread_cost,
            commission_cost=request.commission_cost,
            swap_cost=request.swap_cost,
            other_costs=request.other_costs,
        )
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(
            status_code=status_code,
            detail=str(error)
        )


@app.post("/trades")
def create_trade(request: TradeRequest, user=Depends(current_user)):
    require_account_access(request.account_id, user)
    if request.position_group_id:
        require_group_access(request.position_group_id, user)
    try:
        symbol = validate_symbol(request.symbol)

        trade = open_paper_trade(
            account_id=request.account_id,
            client_order_id=request.client_order_id,
            side=request.side,
            entry_price=request.entry_price,
            stop_loss=request.stop_loss,
            risk_percent=request.risk_percent,
            take_profit=request.take_profit,
            symbol=symbol,
            position_group_id=request.position_group_id
        )

        # Copy attempts are isolated per follower and never undo the source trade.
        copy_summary = replicate_open_trade(trade["trade_id"])
        return {**trade, "copy_summary": copy_summary}
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )
    except RuntimeError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )


@app.post("/trades/{trade_id}/profit-preview")
def trade_profit_preview(
    trade_id: str,
    request: ProfitPreviewRequest,
    user=Depends(current_user)
):
    """Return a read-only net profit estimate for an owned open paper trade."""
    require_trade_access(trade_id, user)
    with get_connection() as connection:
        trade = connection.execute(
            """
            SELECT trade_id, account_id, symbol, side, quantity, entry_price, status
            FROM trades
            WHERE trade_id = ?
            """,
            (trade_id,)
        ).fetchone()

    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found.")
    if trade["status"] != "OPEN":
        raise HTTPException(
            status_code=409,
            detail="Profit preview is available only for open trades."
        )

    try:
        result = evaluate_position_profit(
            {
                "side": trade["side"],
                "entry_price": trade["entry_price"],
                "quantity": trade["quantity"],
            },
            request.current_price,
            spread_cost=request.spread_cost,
            commission_cost=request.commission_cost,
            swap_cost=request.swap_cost,
            other_costs=request.other_costs,
            minimum_net_profit=request.minimum_net_profit,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    return {
        "trade_id": trade["trade_id"],
        "account_id": trade["account_id"],
        "symbol": trade["symbol"],
        **result,
        "read_only": True,
        "execution_triggered": False,
    }


@app.post("/trades/{trade_id}/close")
def close_trade(
    trade_id: str,
    request: CloseTradeRequest,
    user=Depends(current_user)
):
    require_trade_access(trade_id, user)
    try:
        return close_trade_with_copies(
            trade_id,
            request.exit_price,
            spread_cost=request.spread_cost,
            commission_cost=request.commission_cost,
            swap_cost=request.swap_cost,
            other_costs=request.other_costs,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
    )


@app.post("/accounts/{follower_account_id}/copy-relationships")
def create_copy_relationship_route(
    follower_account_id: str,
    request: CopyRelationshipRequest,
    user=Depends(current_user)
):
    require_account_access(follower_account_id, user)
    require_account_access(request.source_account_id, user)
    try:
        return create_copy_relationship(
            follower_account_id=follower_account_id,
            source_account_id=request.source_account_id
        )
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))


@app.get("/accounts/{follower_account_id}/copy-relationships")
def list_copy_relationships_route(follower_account_id: str, user=Depends(current_user)):
    require_account_access(follower_account_id, user)
    try:
        return get_copy_relationships(follower_account_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@app.post("/copy-relationships/{relationship_id}/pause")
def pause_copy_relationship_route(relationship_id: str, user=Depends(current_user)):
    require_relationship_access(relationship_id, user)
    try:
        return update_copy_relationship_status(relationship_id, "PAUSED")
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))


@app.post("/copy-relationships/{relationship_id}/resume")
def resume_copy_relationship_route(relationship_id: str, user=Depends(current_user)):
    require_relationship_access(relationship_id, user)
    try:
        return update_copy_relationship_status(relationship_id, "ACTIVE")
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))


@app.post("/copy-relationships/{relationship_id}/stop")
def stop_copy_relationship_route(relationship_id: str, user=Depends(current_user)):
    require_relationship_access(relationship_id, user)
    try:
        return update_copy_relationship_status(relationship_id, "STOPPED")
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))
from .copy_trading import (
    replicate_open_trade,
    close_trade_with_copies,
    close_position_group_with_copies
)



@app.get("/strategies")
def strategy_library(user=Depends(current_user)):
    """List built-in reference techniques and this user's contributions."""
    return list_strategies(user["user_id"])


@app.post("/strategies")
def add_strategy_knowledge(
    request: StrategyKnowledgeRequest,
    user=Depends(current_user)
):
    """Store a strategy idea as unvalidated knowledge, never as an active signal."""
    return create_strategy(
        user["user_id"],
        request.model_dump()
    )


@app.get("/strategies/{strategy_id}")
def strategy_details(strategy_id: str, user=Depends(current_user)):
    strategy = get_strategy(strategy_id, user["user_id"])
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    return strategy


@app.post("/market-observations/deriv-sync")
async def trigger_deriv_market_sync(request: DerivMarketSyncRequest, user=Depends(current_user)):
    """Admin-triggered, bounded Deriv candle collection; never generates signals or orders."""
    require_admin(user)
    if not request.confirm_collection:
        raise HTTPException(status_code=400, detail="Explicit collection confirmation is required.")
    try:
        instrument = get_instrument(request.symbol)
        if instrument.get("provider") != "Deriv" or instrument.get("asset_class") != "synthetic_index":
            raise ValueError("Only catalogued Deriv synthetic indices can be collected.")
        if request.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError("Unsupported observation timeframe.")
        client = DerivPublicMarketDataClient()
        candles = await client.collect_candles(
            instrument["symbol"], request.timeframe, count=request.count
        )
        # Do not hold a SQLite connection while waiting on the external WebSocket.
        with get_connection() as connection:
            observations = client.persist_candles(connection, candles)
        result = {
            "provider": "Deriv",
            "symbol": instrument["symbol"],
            "timeframe": request.timeframe,
            "received_count": len(candles),
            "inserted_count": sum(1 for item in observations if item["inserted"]),
            "duplicate_count": sum(1 for item in observations if not item["inserted"]),
            "read_only": True,
            "execution_triggered": False,
            "signals_generated": False,
            "observations": observations,
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="Deriv market-data request timed out.") from error
    except RuntimeError as error:
        raise HTTPException(status_code=502, detail="Deriv market-data provider rejected the request.") from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="Deriv market-data collection failed safely.") from error
    return {
        **result,
        "operator_triggered": True,
        "read_only": True,
        "execution_triggered": False,
        "signals_generated": False,
    }


@app.get("/market-observations")
def market_observations_route(
    symbol: str | None = None,
    timeframe: str | None = None,
    limit: int = 100,
    user=Depends(current_user),
):
    """List stored, validated market candles without creating signals or orders."""
    del user  # Authentication is required; observations are currently shared research data.
    try:
        with get_connection() as connection:
            observations = list_market_observations(
                connection, symbol=symbol, timeframe=timeframe, limit=limit
            )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {
        "observations": observations,
        "count": len(observations),
        "read_only": True,
        "execution_triggered": False,
        "signals_generated": False,
    }



@app.get("/market-observations/quality")
def market_observation_quality_route(
    symbol: str,
    timeframe: str,
    user=Depends(current_user),
):
    """Authenticated read-only freshness and continuity diagnostics for Deriv synthetics."""
    del user
    try:
        with get_connection() as connection:
            quality = inspect_market_observation_quality(
                connection, symbol=symbol, timeframe=timeframe
            )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {
        **quality,
        "read_only": True,
        "signals_generated": False,
        "execution_triggered": False,
    }
