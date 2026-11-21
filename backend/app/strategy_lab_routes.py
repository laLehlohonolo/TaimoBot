"""Administrator-only Strategy Intelligence Lab candidate registry.

This first vertical slice stores research candidates and review decisions.
It does not execute strategies, run backtests, or connect candidates to orders.
"""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth_routes import current_user, require_admin
from .database import get_connection

router = APIRouter(
    prefix="/admin/brain/strategy-candidates",
    tags=["admin strategy intelligence"],
)


class CandidateCreateRequest(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    hypothesis: str = Field(min_length=20, max_length=4000)
    instruments: list[str] = Field(min_length=1, max_length=20)
    timeframes: list[str] = Field(min_length=1, max_length=12)
    market_conditions: str = Field(min_length=10, max_length=2000)
    entry_logic: str = Field(min_length=20, max_length=4000)
    exit_logic: str = Field(min_length=20, max_length=4000)
    invalidation_logic: str = Field(min_length=10, max_length=2000)
    risk_notes: str = Field(min_length=10, max_length=2000)


class CandidateDecisionRequest(BaseModel):
    decision: str = Field(pattern=r"^(REQUEST_MORE_RESEARCH|REJECT|APPROVE_FOR_STAGING)$")
    rationale: str = Field(min_length=10, max_length=3000)


def _candidate_dict(row):
    result = dict(row)
    result["instruments"] = json.loads(result.pop("instruments_json"))
    result["timeframes"] = json.loads(result.pop("timeframes_json"))
    return result


@router.post("", status_code=201)
def create_candidate(request: CandidateCreateRequest, user=Depends(current_user)):
    require_admin(user)
    candidate_id = "brainstrat_" + uuid.uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    instruments = sorted({item.strip().upper() for item in request.instruments if item.strip()})
    timeframes = sorted({item.strip().upper() for item in request.timeframes if item.strip()})
    if not instruments or len(instruments) != len(request.instruments):
        raise HTTPException(status_code=400, detail="Instrument entries must be non-empty and unique.")
    if not timeframes or len(timeframes) != len(request.timeframes):
        raise HTTPException(status_code=400, detail="Timeframe entries must be non-empty and unique.")

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO brain_strategy_candidates (
                candidate_id, name, hypothesis, instruments_json, timeframes_json,
                market_conditions, entry_logic, exit_logic, invalidation_logic,
                risk_notes, status, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RESEARCH', ?, ?, ?)
            """,
            (
                candidate_id, request.name.strip(), request.hypothesis.strip(),
                json.dumps(instruments), json.dumps(timeframes),
                request.market_conditions.strip(), request.entry_logic.strip(),
                request.exit_logic.strip(), request.invalidation_logic.strip(),
                request.risk_notes.strip(), user["user_id"], now, now,
            ),
        )
        connection.execute(
            """
            INSERT INTO brain_strategy_lab_audit (
                event_id, candidate_id, actor_user_id, event_type, details_json, created_at
            ) VALUES (?, ?, ?, 'CANDIDATE_CREATED', ?, ?)
            """,
            (uuid.uuid4().hex, candidate_id, user["user_id"],
             json.dumps({"status": "RESEARCH"}), now),
        )
        row = connection.execute(
            "SELECT * FROM brain_strategy_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    return _candidate_dict(row)


@router.get("")
def list_candidates(user=Depends(current_user)):
    require_admin(user)
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT * FROM brain_strategy_candidates
            ORDER BY created_at DESC, candidate_id
            LIMIT 250
            """
        ).fetchall()
    return {"count": len(rows), "candidates": [_candidate_dict(row) for row in rows]}


@router.get("/{candidate_id}")
def get_candidate(candidate_id: str, user=Depends(current_user)):
    require_admin(user)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM brain_strategy_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        decisions = connection.execute(
            """
            SELECT decision_id, decision, rationale, decided_by, created_at
            FROM brain_strategy_candidate_decisions
            WHERE candidate_id = ?
            ORDER BY created_at, decision_id
            """,
            (candidate_id,),
        ).fetchall()
    if row is None:
        raise HTTPException(status_code=404, detail="Strategy candidate not found.")
    return {**_candidate_dict(row), "decisions": [dict(item) for item in decisions]}


@router.get("/data-readiness")
def strategy_data_readiness(
    symbol: str,
    timeframe: str,
    user=Depends(current_user),
):
    """Admin-only, read-only inventory of market data available for research."""
    require_admin(user)
    from .instruments import get_instrument
    from .market_observations import SUPPORTED_TIMEFRAMES

    try:
        instrument = get_instrument(symbol)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise HTTPException(status_code=400, detail="Unsupported observation timeframe.")

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS observation_count,
                   MIN(observed_at) AS earliest_timestamp,
                   MAX(observed_at) AS latest_timestamp,
                   COUNT(DISTINCT source) AS source_count,
                   COUNT(DISTINCT provenance_status) AS provenance_status_count
            FROM market_observations
            WHERE symbol = ? AND timeframe = ?
            """,
            (instrument["symbol"], timeframe),
        ).fetchone()
        provenance = connection.execute(
            """
            SELECT source, provenance_status, COUNT(*) AS count
            FROM market_observations
            WHERE symbol = ? AND timeframe = ?
            GROUP BY source, provenance_status
            ORDER BY source, provenance_status
            """,
            (instrument["symbol"], timeframe),
        ).fetchall()

    count = row["observation_count"]
    return {
        "symbol": instrument["symbol"],
        "timeframe": timeframe,
        "observation_count": count,
        "earliest_timestamp": row["earliest_timestamp"],
        "latest_timestamp": row["latest_timestamp"],
        "source_count": row["source_count"],
        "provenance_status_count": row["provenance_status_count"],
        "provenance_breakdown": [dict(item) for item in provenance],
        "data_available": count > 0,
        "provenance_verified": bool(count) and all(
            item["provenance_status"] == "VERIFIED"
            for item in provenance
        ),
        "eligible_for_backtest": False,
        "eligibility_note": (
            "Backtesting remains disabled until feed provenance, candle continuity, "
            "cost assumptions and deterministic replay are independently validated."
        ),
        "read_only": True,
        "signals_generated": False,
        "execution_triggered": False,
    }


@router.post("/{candidate_id}/decisions", status_code=201)
def decide_candidate(candidate_id: str, request: CandidateDecisionRequest, user=Depends(current_user)):
    require_admin(user)
    now = datetime.now(timezone.utc).isoformat()
    status_by_decision = {
        "REQUEST_MORE_RESEARCH": "RESEARCH",
        "REJECT": "REJECTED",
        "APPROVE_FOR_STAGING": "APPROVED_FOR_STAGING",
    }
    new_status = status_by_decision[request.decision]

    with get_connection() as connection:
        row = connection.execute(
            "SELECT status FROM brain_strategy_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Strategy candidate not found.")
        if row["status"] in ("REJECTED", "APPROVED_FOR_STAGING"):
            raise HTTPException(status_code=409, detail="This candidate has a final research decision.")
        if request.decision == "APPROVE_FOR_STAGING" and row["status"] != "RESEARCH":
            raise HTTPException(status_code=409, detail="Candidate is not eligible for staging review.")

        decision_id = "decision_" + uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO brain_strategy_candidate_decisions (
                decision_id, candidate_id, decision, rationale, decided_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (decision_id, candidate_id, request.decision, request.rationale.strip(),
             user["user_id"], now),
        )
        connection.execute(
            "UPDATE brain_strategy_candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
            (new_status, now, candidate_id),
        )
        connection.execute(
            """
            INSERT INTO brain_strategy_lab_audit (
                event_id, candidate_id, actor_user_id, event_type, details_json, created_at
            ) VALUES (?, ?, ?, 'CANDIDATE_DECISION', ?, ?)
            """,
            (uuid.uuid4().hex, candidate_id, user["user_id"],
             json.dumps({"decision": request.decision, "status": new_status}), now),
        )
    return {
        "candidate_id": candidate_id,
        "decision_id": decision_id,
        "decision": request.decision,
        "status": new_status,
        "production_deployed": False,
        "paper_execution_triggered": False,
    }
