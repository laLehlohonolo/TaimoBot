import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from typing import Literal

from pydantic import BaseModel, Field

from .auth import (
    AuthenticationError, AuthenticationRateLimitError,
    authenticate_user, create_session,
    get_session_user, register_user, revoke_session
)

router = APIRouter(prefix="/auth", tags=["authentication"])
bearer_scheme = HTTPBearer(auto_error=False)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"}
        )
    try:
        return get_session_user(credentials.credentials)
    except AuthenticationError:
        raise HTTPException(
            status_code=401, detail="Invalid or expired session.",
            headers={"WWW-Authenticate": "Bearer"}
        )


@router.post("/register")
def auth_register(request: RegisterRequest):
    try:
        return register_user(request.email, request.password)
    except AuthenticationError as error:
        raise HTTPException(status_code=400, detail=str(error))


@router.post("/login")
def auth_login(request: LoginRequest):
    try:
        user = authenticate_user(request.email, request.password)
        return {**create_session(user["user_id"]), "user": user}
    except AuthenticationRateLimitError:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")
    except AuthenticationError:
        raise HTTPException(status_code=401, detail="Invalid email or password.")


@router.get("/me")
def auth_me(user=Depends(current_user)):
    return user


@router.post("/logout")
def auth_logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    user=Depends(current_user)
):
    revoke_session(credentials.credentials)
    return {"authenticated": False, "message": "Session revoked."}

def require_account_access(account_id, user):
    from .database import get_connection

    with get_connection() as connection:
        row = connection.execute(
            "SELECT owner_user_id FROM paper_accounts WHERE account_id = ?",
            (account_id,)
        ).fetchone()

    if row is None or row["owner_user_id"] != user["user_id"]:
        raise HTTPException(status_code=404, detail="Paper account not found.")
    return True


def require_group_access(group_id, user):
    from .database import get_connection

    with get_connection() as connection:
        row = connection.execute(
            "SELECT account_id FROM position_groups WHERE group_id = ?",
            (group_id,)
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Position group not found.")
    require_account_access(row["account_id"], user)
    return True


def require_trade_access(trade_id, user):
    from .database import get_connection

    with get_connection() as connection:
        row = connection.execute(
            "SELECT account_id FROM trades WHERE trade_id = ?",
            (trade_id,)
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Trade not found.")
    require_account_access(row["account_id"], user)
    return True


def require_relationship_access(relationship_id, user):
    from .database import get_connection

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT source_account_id, follower_account_id
            FROM copy_relationships
            WHERE relationship_id = ?
            """,
            (relationship_id,)
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Copy relationship not found.")
    require_account_access(row["source_account_id"], user)
    require_account_access(row["follower_account_id"], user)
    return True


def require_admin(user):
    """Allow global safety controls only to explicitly configured administrators."""
    configured = os.getenv("TAIMOBOT_ADMIN_EMAILS", "")
    admin_emails = {
        email.strip().lower()
        for email in configured.split(",")
        if email.strip()
    }
    if user.get("email", "").strip().lower() not in admin_emails:
        raise HTTPException(
            status_code=403,
            detail="Administrator privileges required."
        )
    return user



class SubscriptionTierRequest(BaseModel):
    tier: Literal["STANDARD", "ADVANCED", "PREMIUM", "EXCLUSIVE"]
    reason: str = Field(min_length=3, max_length=500)


@router.get("/subscription/entitlement")
def subscription_entitlement(user=Depends(current_user)):
    """Return the authenticated participant's persisted account entitlement."""
    from .database import get_connection

    limits = {"STANDARD": 1, "ADVANCED": 3, "PREMIUM": 5, "EXCLUSIVE": 10}
    with get_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO participant_subscription_entitlements (user_id, tier)
            VALUES (?, 'STANDARD')
            """,
            (user["user_id"],),
        )
        row = connection.execute(
            """
            SELECT tier, updated_at FROM participant_subscription_entitlements
            WHERE user_id = ?
            """,
            (user["user_id"],),
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) AS account_count FROM paper_accounts WHERE owner_user_id = ?",
            (user["user_id"],),
        ).fetchone()["account_count"]

    limit = limits[row["tier"]]
    return {
        "user_id": user["user_id"],
        "tier": row["tier"],
        "maximum_accounts": limit,
        "accounts_used": count,
        "accounts_remaining": max(0, limit - count),
        "updated_at": row["updated_at"],
        "entitlement_source": "BACKEND",
    }


@router.put("/admin/participants/{participant_user_id}/subscription")
def admin_update_subscription(
    participant_user_id: str,
    request: SubscriptionTierRequest,
    admin=Depends(current_user),
):
    """Admin-only tier assignment with immutable audit history; no payment is implied."""
    from uuid import uuid4
    from .database import get_connection

    require_admin(admin)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        participant = connection.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (participant_user_id,),
        ).fetchone()
        if participant is None:
            raise HTTPException(status_code=404, detail="Participant not found.")

        connection.execute(
            """
            INSERT OR IGNORE INTO participant_subscription_entitlements (user_id, tier)
            VALUES (?, 'STANDARD')
            """,
            (participant_user_id,),
        )
        previous = connection.execute(
            """
            SELECT tier FROM participant_subscription_entitlements WHERE user_id = ?
            """,
            (participant_user_id,),
        ).fetchone()["tier"]
        connection.execute(
            """
            UPDATE participant_subscription_entitlements
            SET tier = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (request.tier, participant_user_id),
        )
        connection.execute(
            """
            INSERT INTO subscription_entitlement_audit
                (audit_id, actor_user_id, participant_user_id,
                 previous_tier, new_tier, reason)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid4()), admin["user_id"], participant_user_id,
             previous, request.tier, request.reason.strip()),
        )
    return {
        "participant_user_id": participant_user_id,
        "previous_tier": previous,
        "new_tier": request.tier,
        "payment_verified": False,
        "message": "Administrator-assigned entitlement recorded; no payment was processed.",
    }



@router.get("/participant/timeline")
def participant_timeline(user=Depends(current_user)):
    """Return the authenticated participant's immutable joining and first-trade milestones."""
    from .database import get_connection

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT users.created_at AS joined_at,
                   participant_milestones.first_trade_at
            FROM users
            LEFT JOIN participant_milestones
              ON participant_milestones.user_id = users.user_id
            WHERE users.user_id = ?
            """,
            (user["user_id"],)
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Participant record not found.")

    return {
        "joined_at": row["joined_at"],
        "first_trade_at": row["first_trade_at"],
        "loyalty_milestone_years": 10,
        "loyalty_reward_status": "TO_BE_DETERMINED",
    }
