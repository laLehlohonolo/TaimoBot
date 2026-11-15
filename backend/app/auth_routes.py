from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from .auth import (
    AuthenticationError, authenticate_user, create_session,
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
