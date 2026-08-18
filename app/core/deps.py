from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError

from app.core.db import get_pool
from app.core.security import decode_access_token

_bearer = HTTPBearer()


async def get_current_user(creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    try:
        payload = decode_access_token(creds.credentials)
    except PyJWTError:
        raise HTTPException(401, "Invalid or expired token")

    async with get_pool().acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, role, status, token_version, gate_id, godown_type_scope "
            "FROM identity.users WHERE id = $1",
            payload["sub"],
        )

    # Re-checked against the DB on every request (not just the JWT claims) so that
    # deactivating a user or bumping token_version (an admin "revoke everywhere")
    # takes effect immediately instead of waiting for the token to expire.
    if not user or user["status"] != "ACTIVE" or user["token_version"] != payload["tv"]:
        raise HTTPException(401, "Session no longer valid")
    return dict(user)


def require_role(*roles: str):
    """Dependency factory: restrict a route to one or more identity.user_role values."""

    async def _check(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "Forbidden for this role")
        return user

    return _check
