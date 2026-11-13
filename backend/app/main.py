from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .database import init_db, get_connection
from .risk import (
    calculate_position_size,
    RiskError,
    MAX_RISK_PERCENT
)
from .execution import (
    create_paper_account,
    create_position_group,
    get_position_group,
    close_position_group,
    open_paper_trade,
    close_paper_trade,
    get_open_risk,
    validate_symbol,
    DEFAULT_SYMBOL,
    create_copy_relationship,
    get_copy_relationships,
    update_copy_relationship_status
)
from .copy_trading import replicate_open_trade
from .history import (
    get_account_history,
    get_trade_history
)
from .safety import (
    activate_emergency_stop,
    deactivate_emergency_stop,
    is_emergency_stop_active
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="TaimoBot API",
    description="Multi-instrument paper trading backend for TaimoBot",
    version="0.7.0",
    lifespan=lifespan
)


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
    exit_price: float = Field(gt=0)


class CloseGroupRequest(BaseModel):
    exit_price: float = Field(gt=0)


class CopyRelationshipRequest(BaseModel):
    source_account_id: str = Field(min_length=1, max_length=100)


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


@app.get("/safety/status")
def safety_status():
    active = is_emergency_stop_active()

    return {
        "emergency_stop": active,
        "new_orders_allowed": not active
    }


@app.post("/safety/emergency-stop")
def emergency_stop():
    return activate_emergency_stop()


@app.post("/safety/resume")
def resume_trading():
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
def account_risk_status(account_id: str):
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


@app.post("/accounts")
def create_account(request: AccountRequest):
    try:
        return create_paper_account(
            request.account_id,
            request.starting_balance
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
        )


@app.get("/accounts/{account_id}")
def account_history(account_id: str):
    try:
        return get_account_history(account_id)
    except ValueError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error)
        )


@app.get("/accounts/{account_id}/trades")
def trade_history(account_id: str, limit: int = 50):
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
    request: PositionGroupRequest
):
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
def group_details(group_id: str):
    try:
        return get_position_group(group_id)
    except ValueError as error:
        raise HTTPException(
            status_code=404,
            detail=str(error)
        )


@app.post("/groups/{group_id}/close")
def close_group(
    group_id: str,
    request: CloseGroupRequest
):
    try:
        return close_position_group(
            group_id=group_id,
            exit_price=request.exit_price
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
def create_trade(request: TradeRequest):
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
        trade["copy_summary"] = replicate_open_trade(trade["trade_id"])
        return trade
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


@app.post("/trades/{trade_id}/close")
def close_trade(
    trade_id: str,
    request: CloseTradeRequest
):
    try:
        return close_paper_trade(
            trade_id,
            request.exit_price
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error)
    )


@app.post("/accounts/{follower_account_id}/copy-relationships")
def create_copy_relationship_route(
    follower_account_id: str,
    request: CopyRelationshipRequest
):
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
def list_copy_relationships_route(follower_account_id: str):
    try:
        return get_copy_relationships(follower_account_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error))


@app.post("/copy-relationships/{relationship_id}/pause")
def pause_copy_relationship_route(relationship_id: str):
    try:
        return update_copy_relationship_status(relationship_id, "PAUSED")
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))


@app.post("/copy-relationships/{relationship_id}/resume")
def resume_copy_relationship_route(relationship_id: str):
    try:
        return update_copy_relationship_status(relationship_id, "ACTIVE")
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))


@app.post("/copy-relationships/{relationship_id}/stop")
def stop_copy_relationship_route(relationship_id: str):
    try:
        return update_copy_relationship_status(relationship_id, "STOPPED")
    except ValueError as error:
        status_code = (
            404 if "not found" in str(error).lower()
            else 400
        )
        raise HTTPException(status_code=status_code, detail=str(error))

