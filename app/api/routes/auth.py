from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from app.core.config import settings
from app.core.db import get_pool
from app.core.security import create_access_token, verify_secret

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    # Shop-floor roles log in with employee_code + PIN; GM/MD admin logs in with
    # email + password. Both are just "an identifier + a shared secret" underneath,
    # so one endpoint handles both rather than duplicating the same lockout logic twice.
    employee_code: Optional[str] = None
    email: Optional[str] = None
    secret: str  # PIN for shop-floor roles, password for GM_MD_ADMIN

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> "LoginIn":
        if bool(self.employee_code) == bool(self.email):
            raise ValueError("Provide exactly one of employee_code or email")
        return self

    model_config = {"json_schema_extra": {"example": {"employee_code": "GATE001", "secret": "4821"}}}


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    full_name: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                "token_type": "bearer",
                "role": "GATEMAN",
                "full_name": "Ramesh Kumar",
            }
        }
    }


@router.post("/login", response_model=LoginOut)
async def login(body: LoginIn) -> dict:
    async with get_pool().acquire() as conn:
        if body.employee_code:
            user = await conn.fetchrow(
                "SELECT id, full_name, role, status, secret_hash, failed_login_attempts, "
                "locked_until, token_version FROM identity.users WHERE employee_code = $1",
                body.employee_code,
            )
        else:
            user = await conn.fetchrow(
                "SELECT id, full_name, role, status, secret_hash, failed_login_attempts, "
                "locked_until, token_version FROM identity.users WHERE email = $1",
                body.email,
            )

        if not user or user["status"] != "ACTIVE" or not user["secret_hash"]:
            raise HTTPException(401, "Invalid credentials")
        if user["locked_until"] and user["locked_until"] > datetime.now(timezone.utc):
            raise HTTPException(423, "Account temporarily locked, try again later")

        if not verify_secret(body.secret, user["secret_hash"]):
            attempts = user["failed_login_attempts"] + 1
            locked_until = (
                datetime.now(timezone.utc) + timedelta(minutes=settings.pin_lock_minutes)
                if attempts >= settings.max_pin_attempts
                else None
            )
            await conn.execute(
                "UPDATE identity.users SET failed_login_attempts=$1, locked_until=$2 WHERE id=$3",
                attempts, locked_until, user["id"],
            )
            raise HTTPException(401, "Invalid credentials")

        await conn.execute(
            "UPDATE identity.users SET failed_login_attempts=0, locked_until=NULL, "
            "last_login_at=now() WHERE id=$1",
            user["id"],
        )

    token = create_access_token(str(user["id"]), user["role"], user["token_version"])
    return {"access_token": token, "token_type": "bearer", "role": user["role"], "full_name": user["full_name"]}
