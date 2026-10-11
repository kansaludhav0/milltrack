from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.db import get_pool
from app.core.deps import require_role
from app.core.storage import presign_get, upload_fixed

router = APIRouter(prefix="/app", tags=["app-release"])
ADMIN = require_role("GM_MD_ADMIN")

# Always the same key — uploading a new build overwrites this one object, by design
# (no version history is kept; "latest" is whatever was most recently uploaded here).
_APK_KEY = "app-releases/latest.apk"
# Long enough that a slow connection on the shop floor can finish downloading the whole
# file before the link expires — unlike the short-lived links used for viewing a photo.
_DOWNLOAD_EXPIRES_IN = 3600


class AppReleaseOut(BaseModel):
    version_label: str
    release_notes: Optional[str]
    download_url: str
    uploaded_at: datetime


async def _current_release(conn):
    return await conn.fetchrow("SELECT * FROM config.app_release WHERE id = true")


def _release_out(row) -> dict:
    return {
        "version_label": row["version_label"],
        "release_notes": row["release_notes"],
        "download_url": presign_get(row["file_url"], expires_in=_DOWNLOAD_EXPIRES_IN),
        "uploaded_at": row["uploaded_at"],
    }


@router.post(
    "/release",
    response_model=AppReleaseOut,
    status_code=201,
    summary="Upload a new APK build — replaces whatever was there before, Admin only",
)
async def upload_release(
    version_label: str = Form(...),
    release_notes: Optional[str] = Form(None),
    file: UploadFile = File(...),
    admin: dict = Depends(ADMIN),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".apk"):
        raise HTTPException(400, "File must be a .apk")
    data = await file.read()
    file_url = upload_fixed(_APK_KEY, data, "application/vnd.android.package-archive")
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO config.app_release (id, version_label, release_notes, file_url, uploaded_by)
            VALUES (true, $1, $2, $3, $4)
            ON CONFLICT (id) DO UPDATE SET
                version_label = EXCLUDED.version_label,
                release_notes = EXCLUDED.release_notes,
                file_url = EXCLUDED.file_url,
                uploaded_by = EXCLUDED.uploaded_by,
                uploaded_at = now()
            RETURNING *
            """,
            version_label, release_notes, file_url, admin["id"],
        )
    return _release_out(row)


@router.get(
    "/release/latest",
    response_model=AppReleaseOut,
    summary="Current APK build's version info + a download link — public, no login "
            "required (field staff need this before they can log in at all)",
    responses={404: {"description": "No app release has been uploaded yet"}},
)
async def latest_release() -> dict:
    async with get_pool().acquire() as conn:
        row = await _current_release(conn)
    if row is None:
        raise HTTPException(404, "No app release has been uploaded yet")
    return _release_out(row)


@router.get(
    "/release/latest/download",
    summary="Redirects straight to the current APK file — the link to hand out to field staff",
    responses={404: {"description": "No app release has been uploaded yet"}},
)
async def download_latest_release() -> RedirectResponse:
    async with get_pool().acquire() as conn:
        row = await _current_release(conn)
    if row is None:
        raise HTTPException(404, "No app release has been uploaded yet")
    return RedirectResponse(presign_get(row["file_url"], expires_in=_DOWNLOAD_EXPIRES_IN))
