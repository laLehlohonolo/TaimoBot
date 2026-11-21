"""Persistent, administrator-only instrument session-calendar registry.

Records are immutable versions. Source verification and approval are separate,
audited decisions by different administrators. Approval never enables backtests,
session-adjusted coverage, signals, or execution.
"""
import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth_routes import current_user, require_admin
from .database import get_connection
from .session_calendars import validate_session_calendar_record

router = APIRouter(prefix="/admin/brain/session-calendars", tags=["admin session calendars"])


class CalendarSubmission(BaseModel):
    provider: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=32)
    timezone: str = Field(min_length=1, max_length=80)
    weekly_sessions: list[dict] = Field(min_length=1, max_length=100)
    holidays: list[str] = Field(max_length=1000)
    maintenance_windows: list[dict] = Field(max_length=1000)
    effective_from: str
    effective_to: str
    source_url: str = Field(min_length=8, max_length=2000)
    retrieved_at: str


class CalendarDecision(BaseModel):
    decision: str = Field(pattern=r"^(VERIFY_SOURCE|APPROVE_CALENDAR|REQUEST_CHANGES|REJECT)$")
    rationale: str = Field(min_length=10, max_length=3000)
    evidence_url: str | None = Field(default=None, max_length=2000)


def _is_https_evidence_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
    )


def _calendar_response(row):
    item = dict(row)
    item["record"] = json.loads(item.pop("record_json"))
    item.update({
        "source_verified": item["status"] in ("SOURCE_VERIFIED_PENDING_APPROVAL", "APPROVED"),
        "admin_reviewed": item["status"] == "APPROVED",
        "session_calendar_applied": False,
        "session_adjusted_coverage_available": False,
        "backtest_eligible": False,
        "execution_eligible": False,
    })
    return item


@router.post("", status_code=201)
def submit_calendar(request: CalendarSubmission, user=Depends(current_user)):
    require_admin(user)
    provider = request.provider.strip().casefold()
    symbol = request.symbol.strip().upper()
    with get_connection() as connection:
        # Serialize version allocation so concurrent submissions cannot reuse a version.
        connection.execute("BEGIN IMMEDIATE")
        latest = connection.execute(
            """SELECT MAX(version) AS version FROM brain_session_calendars
               WHERE provider = ? AND symbol = ?""",
            (provider, symbol),
        ).fetchone()
        version = (latest["version"] or 0) + 1
        raw = request.model_dump()
        raw.update({
            "provider": provider,
            "symbol": symbol,
            "version": version,
            "submitted_by": user["user_id"],
        })
        try:
            record = validate_session_calendar_record(raw)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        calendar_id = "calendar_" + uuid.uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """INSERT INTO brain_session_calendars
               (calendar_id, provider, symbol, version, status, record_json,
                submitted_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'PENDING_SOURCE_VERIFICATION', ?, ?, ?, ?)""",
            (calendar_id, record["provider"], record["symbol"], version,
             json.dumps(record, sort_keys=True), user["user_id"], now, now),
        )
        connection.execute(
            """INSERT INTO brain_session_calendar_audit
               (event_id, calendar_id, actor_user_id, event_type, details_json, created_at)
               VALUES (?, ?, ?, 'SUBMITTED', ?, ?)""",
            ("cal_event_" + uuid.uuid4().hex, calendar_id, user["user_id"],
             json.dumps({"version": version, "status": "PENDING_SOURCE_VERIFICATION"}), now),
        )
        row = connection.execute(
            "SELECT * FROM brain_session_calendars WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
    return _calendar_response(row)


@router.get("")
def list_calendars(symbol: str | None = None, user=Depends(current_user)):
    require_admin(user)
    query = "SELECT * FROM brain_session_calendars"
    params = ()
    if symbol:
        query += " WHERE symbol = ?"
        params = (symbol.strip().upper(),)
    query += " ORDER BY symbol, provider, version DESC LIMIT 500"
    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()
    return {"count": len(rows), "calendars": [_calendar_response(row) for row in rows]}


@router.get("/{calendar_id}")
def get_calendar(calendar_id: str, user=Depends(current_user)):
    require_admin(user)
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM brain_session_calendars WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
        decisions = connection.execute(
            """SELECT decision_id, decision, rationale, evidence_url, decided_by, created_at
               FROM brain_session_calendar_decisions
               WHERE calendar_id = ? ORDER BY created_at, decision_id""",
            (calendar_id,),
        ).fetchall()
    if row is None:
        raise HTTPException(status_code=404, detail="Session calendar not found.")
    return {**_calendar_response(row), "decisions": [dict(item) for item in decisions]}


@router.post("/{calendar_id}/decisions", status_code=201)
def decide_calendar(calendar_id: str, request: CalendarDecision, user=Depends(current_user)):
    require_admin(user)
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        # Serialize review transitions so two administrators cannot race approvals.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM brain_session_calendars WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Session calendar not found.")
        status = row["status"]
        actor = user["user_id"]
        if request.decision == "VERIFY_SOURCE":
            if status != "PENDING_SOURCE_VERIFICATION":
                raise HTTPException(status_code=409, detail="Calendar is not awaiting source verification.")
            if not _is_https_evidence_url(request.evidence_url):
                raise HTTPException(
                    status_code=400,
                    detail="A valid HTTPS evidence URL with a hostname is required for source verification.",
                )
            new_status = "SOURCE_VERIFIED_PENDING_APPROVAL"
        elif request.decision == "APPROVE_CALENDAR":
            if status != "SOURCE_VERIFIED_PENDING_APPROVAL":
                raise HTTPException(status_code=409, detail="Source verification must be recorded first.")
            verifier = connection.execute(
                """SELECT decided_by FROM brain_session_calendar_decisions
                   WHERE calendar_id = ? AND decision = 'VERIFY_SOURCE'
                   ORDER BY created_at DESC LIMIT 1""", (calendar_id,)
            ).fetchone()
            if verifier is None or verifier["decided_by"] == actor:
                raise HTTPException(status_code=409, detail="A different administrator must approve the verified calendar.")
            if not _is_https_evidence_url(request.evidence_url):
                raise HTTPException(
                    status_code=400,
                    detail="A valid HTTPS approval evidence URL with a hostname is required.",
                )
            new_status = "APPROVED"
        elif request.decision == "REQUEST_CHANGES":
            if status != "SOURCE_VERIFIED_PENDING_APPROVAL":
                raise HTTPException(
                    status_code=409,
                    detail="Only a source-verified calendar can be returned for changes.",
                )
            new_status = "PENDING_SOURCE_VERIFICATION"
        else:
            if status in ("APPROVED", "REJECTED"):
                raise HTTPException(status_code=409, detail="Final calendar decisions cannot be changed.")
            new_status = "REJECTED"

        decision_id = "cal_decision_" + uuid.uuid4().hex
        connection.execute(
            """INSERT INTO brain_session_calendar_decisions
               (decision_id, calendar_id, decision, rationale, evidence_url, decided_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, calendar_id, request.decision, request.rationale.strip(),
             request.evidence_url, actor, now),
        )
        connection.execute(
            """UPDATE brain_session_calendars SET status = ?, updated_at = ?
               WHERE calendar_id = ?""", (new_status, now, calendar_id)
        )
        connection.execute(
            """INSERT INTO brain_session_calendar_audit
               (event_id, calendar_id, actor_user_id, event_type, details_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("cal_event_" + uuid.uuid4().hex, calendar_id, actor, request.decision,
             json.dumps({"status": new_status, "rationale": request.rationale.strip(),
                         "evidence_url": request.evidence_url}), now),
        )
        updated = connection.execute(
            "SELECT * FROM brain_session_calendars WHERE calendar_id = ?", (calendar_id,)
        ).fetchone()
    return _calendar_response(updated)
