"""Authenticated, write-once participant display-currency preference API."""
import sqlite3
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictBool

from .auth_routes import current_user
from .database import get_connection

router = APIRouter(prefix="/participant", tags=["participant preferences"])


class DisplayCurrencySelectionRequest(BaseModel):
    currency: Literal["ZAR", "USD", "EUR", "GBP", "KWD"]
    confirmed: StrictBool


@router.get("/currency-preference")
def get_display_currency_preference(user=Depends(current_user)):
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT currency, selected_at
            FROM participant_display_currency_preferences
            WHERE user_id = ?
            """,
            (user["user_id"],),
        ).fetchone()

    if row is None:
        return {"selected": False, "currency": None, "selected_at": None, "locked": False}

    return {
        "selected": True,
        "currency": row["currency"],
        "selected_at": row["selected_at"],
        "locked": True,
    }


@router.post("/currency-preference", status_code=201)
def select_display_currency(request: DisplayCurrencySelectionRequest, user=Depends(current_user)):
    if request.confirmed is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required.")

    selected_at = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO participant_display_currency_preferences
                    (user_id, currency, selected_at, confirmed)
                VALUES (?, ?, ?, 1)
                """,
                (user["user_id"], request.currency, selected_at),
            )
    except sqlite3.IntegrityError as error:
        # The primary key makes this a single successful selection per participant.
        # Database triggers also prevent later updates or deletion.
        if "participant_display_currency_preferences" in str(error) or "UNIQUE constraint failed" in str(error):
            raise HTTPException(
                status_code=409,
                detail="Display currency has already been selected and cannot be changed.",
            ) from error
        raise

    return {
        "selected": True,
        "currency": request.currency,
        "selected_at": selected_at,
        "locked": True,
    }
