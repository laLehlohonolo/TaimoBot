from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
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
