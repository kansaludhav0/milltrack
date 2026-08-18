from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.storage import presign_put

router = APIRouter(prefix="/gate", tags=["gate"])
GATEMAN = require_role("GATEMAN")


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


class EntryIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    gate_id: UUID
    vehicle_number: str
    party_id: Optional[UUID] = None
    purpose: Literal["UNLOADING", "LOADING"]
    driver_name: Optional[str] = None
    driver_mobile: Optional[str] = None
    material_variety_id: Optional[UUID] = None
    approx_bale_count: Optional[int] = None
    invoice_number: Optional[str] = None
    photo_url: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "5b1c2e2a-6b7b-4a2a-9b7b-1a2b3c4d5e6f",
                "gate_id": "8f14e45f-ceea-4a3d-8f56-3c1e0f1a2b3c",
                "vehicle_number": "PB-10-AB-4521",
                "party_id": "1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                "purpose": "UNLOADING",
                "driver_name": "Suresh",
                "driver_mobile": "9876543210",
                "photo_url": "https://milltrack-gate-photos.s3.ap-south-1.amazonaws.com/gate-photos/2026/07/14/....jpg",
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
    photo_url: str
    recorded_by: UUID
    created_at: datetime


@router.post("/entries", response_model=EntryOut, status_code=201, summary="Log a vehicle entering the gate")
async def create_entry(body: EntryIn, user: dict = Depends(GATEMAN)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO operations.gate_vehicle_entries
                (id, gate_id, vehicle_number, party_id, purpose, driver_name, driver_mobile,
                 material_variety_id, approx_bale_count, invoice_number, photo_url, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING *
            """,
            body.id, body.gate_id, body.vehicle_number, body.party_id, body.purpose,
            body.driver_name, body.driver_mobile, body.material_variety_id,
            body.approx_bale_count, body.invoice_number, body.photo_url, user["id"],
        )
    return rec(row)


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


@router.get("/entries", response_model=List[VehicleStatusOut], summary="Today's vehicles — the All Vehicles tab")
async def list_entries(
    purpose: Optional[Literal["UNLOADING", "LOADING"]] = None,
    status: Optional[Literal["ON_PREMISES", "EXITED"]] = None,
    user: dict = Depends(GATEMAN),
) -> List[dict]:
    query = "SELECT * FROM operations.vw_gate_vehicle_current_status WHERE entered_at::date = current_date"
    params: list = []
    if purpose:
        params.append(purpose)
        query += f" AND purpose = ${len(params)}"
    if status:
        params.append(status)
        query += f" AND current_status = ${len(params)}"
    query += " ORDER BY entered_at DESC"
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(query, *params)
    return recs(rows)


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
