from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.storage import presign_get, presign_put

router = APIRouter(prefix="/gate", tags=["gate"])
GATEMAN = require_role("GATEMAN")
# Read-only visibility for GM/MD on top of the gateman: owners can see everything on the
# floor, but only the gateman role can actually log a vehicle in/out. Never used on writes.
GATE_READ = require_role("GATEMAN", "GM_MD_ADMIN")


class PresignOut(BaseModel):
    upload_url: str
    photo_url: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "upload_url": "https://milltrack-gate-photos.s3.ap-south-1.amazonaws.com/gate-photos/...?X-Amz-...",
                "photo_url": "https://milltrack-gate-photos.s3.ap-south-1.amazonaws.com/gate-photos/2026/07/14/....jpg",
            }
        }
    }


@router.post("/uploads/presign", response_model=PresignOut, summary="Get a short-lived S3 upload URL for a gate photo")
async def presign_upload(user: dict = Depends(GATEMAN)) -> dict:
    return presign_put(prefix="gate-photos")


@router.post(
    "/uploads/presign-bill",
    response_model=PresignOut,
    summary="Get a short-lived S3 upload URL for a bill/invoice photo",
)
async def presign_bill_upload(user: dict = Depends(GATEMAN)) -> dict:
    return presign_put(prefix="bill-photos")


class EntryIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    # gate_id is NOT here — it's supplied via the X-Gate-Id header, not the body. A gate
    # terminal sits at one physical gate for its whole session; making the frontend thread
    # that UUID through every entry payload would just be repeating a connection-level fact.
    vehicle_number: str
    party_id: Optional[UUID] = None
    purpose: Literal["UNLOADING", "LOADING"]
    driver_name: Optional[str] = None
    driver_mobile: Optional[str] = None
    material_variety_id: Optional[UUID] = None
    approx_bale_count: Optional[int] = None
    invoice_number: Optional[str] = None
    photo_url: str
    bill_photo_url: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "5b1c2e2a-6b7b-4a2a-9b7b-1a2b3c4d5e6f",
                "vehicle_number": "PB-10-AB-4521",
                "party_id": "1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                "purpose": "UNLOADING",
                "driver_name": "Suresh",
                "driver_mobile": "9876543210",
                "photo_url": "https://milltrack-gate-photos.s3.ap-south-1.amazonaws.com/gate-photos/2026/07/14/....jpg",
                "bill_photo_url": "https://milltrack-gate-photos.s3.ap-south-1.amazonaws.com/bill-photos/2026/07/14/....jpg",
            }
        }
    }


class EntryOut(BaseModel):
    id: UUID
    entry_code: str
    gate_id: UUID
    vehicle_number: str
    party_id: Optional[UUID]
    purpose: str
    driver_name: Optional[str]
    driver_mobile: Optional[str]
    material_variety_id: Optional[UUID]
    approx_bale_count: Optional[int]
    invoice_number: Optional[str]
    photo_url: str  # always a ready-to-load signed URL, never the bare private S3 url
    bill_photo_url: Optional[str]  # same — signed when present, null when no bill was attached
    recorded_by: UUID
    created_at: datetime


@router.post(
    "/entries",
    response_model=EntryOut,
    status_code=201,
    summary="Log a vehicle entering the gate",
    description="Requires an `X-Gate-Id` header identifying which physical gate (see `config.gates`) "
                 "this terminal/session is running at — set once per device, not per request.",
)
async def create_entry(
    body: EntryIn,
    x_gate_id: UUID = Header(..., alias="X-Gate-Id", description="Physical gate this terminal is at"),
    user: dict = Depends(GATEMAN),
) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        # DO NOTHING, not DO UPDATE — gate_vehicle_entries is append-only; a retried
        # submission finds nothing to insert, so fall back to fetching the existing row.
        row = await conn.fetchrow(
            """
            INSERT INTO operations.gate_vehicle_entries
                (id, gate_id, vehicle_number, party_id, purpose, driver_name, driver_mobile,
                 material_variety_id, approx_bale_count, invoice_number, photo_url, bill_photo_url, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            body.id, x_gate_id, body.vehicle_number, body.party_id, body.purpose,
            body.driver_name, body.driver_mobile, body.material_variety_id,
            body.approx_bale_count, body.invoice_number, body.photo_url, body.bill_photo_url, user["id"],
        )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM operations.gate_vehicle_entries WHERE id = $1", body.id)
    result = rec(row)
    if result:
        try:
            result["photo_url"] = presign_get(result["photo_url"])
        except ValueError:
            # The entry itself is already created at this point — a photo_url from an
            # unrecognized bucket shouldn't turn an otherwise-successful write into an error.
            pass
        if result.get("bill_photo_url"):
            try:
                result["bill_photo_url"] = presign_get(result["bill_photo_url"])
            except ValueError:
                pass
    return result


class VehicleStatusOut(BaseModel):
    gate_entry_id: UUID
    entry_code: str
    vehicle_number: str
    party_id: Optional[UUID]
    purpose: str
    gate_id: UUID
    current_status: Literal["ON_PREMISES", "EXITED"]
    entered_at: datetime
    exited_at: Optional[datetime]
    photo_url: str  # always a ready-to-load signed URL, never the bare private S3 url
    bill_photo_url: Optional[str] = None  # same — signed when present, absent/null otherwise
    driver_name: Optional[str] = None
    driver_mobile: Optional[str] = None
    party_name: Optional[str] = None
    material_variety_id: Optional[UUID] = None
    material_variety_name: Optional[str] = None
    approx_bale_count: Optional[int] = None
    invoice_number: Optional[str] = None


def _with_signed_photos(rows) -> List[dict]:
    out = recs(rows)
    for r in out:
        try:
            r["photo_url"] = presign_get(r["photo_url"])
        except ValueError:
            # Older test rows sometimes carry a photo_url from a placeholder bucket that
            # predates the real one — leave it as-is rather than failing the whole list.
            pass
        if r.get("bill_photo_url"):
            try:
                r["bill_photo_url"] = presign_get(r["bill_photo_url"])
            except ValueError:
                pass
    return out


@router.get("/entries", response_model=List[VehicleStatusOut], summary="Today's vehicles — the All Vehicles tab")
async def list_entries(
    purpose: Optional[Literal["UNLOADING", "LOADING"]] = None,
    status: Optional[Literal["ON_PREMISES", "EXITED"]] = None,
    user: dict = Depends(GATE_READ),
) -> List[dict]:
    query = (
        "SELECT v.*, e.photo_url, e.bill_photo_url, e.driver_name, e.driver_mobile, "
        "e.material_variety_id, e.approx_bale_count, e.invoice_number, "
        "p.name AS party_name, mv.name AS material_variety_name "
        "FROM operations.vw_gate_vehicle_current_status v "
        "JOIN operations.gate_vehicle_entries e ON e.id = v.gate_entry_id "
        "LEFT JOIN config.parties p ON p.id = v.party_id "
        "LEFT JOIN config.material_varieties mv ON mv.id = e.material_variety_id "
        "WHERE v.entered_at::date = current_date"
    )
    params: list = []
    if purpose:
        params.append(purpose)
        query += f" AND v.purpose = ${len(params)}"
    if status:
        params.append(status)
        query += f" AND v.current_status = ${len(params)}"
    query += " ORDER BY v.entered_at DESC"
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(query, *params)
    return _with_signed_photos(rows)


class ExitOut(BaseModel):
    id: UUID
    gate_entry_id: UUID
    exited_at: datetime
    recorded_by: UUID
    created_at: datetime


@router.post(
    "/entries/{entry_id}/exit",
    response_model=ExitOut,
    status_code=201,
    summary="One-way, one-tap exit — the only way a vehicle leaves the yard",
    responses={409: {"description": "This vehicle has already exited"}},
)
async def exit_vehicle(entry_id: UUID, user: dict = Depends(GATEMAN)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.gate_vehicle_exits (gate_entry_id, recorded_by) VALUES ($1,$2) RETURNING *",
            entry_id, user["id"],
        )
    return rec(row)
