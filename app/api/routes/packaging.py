from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.routes.gate import VehicleStatusOut
from app.core.db import get_pool, rec, recs
from app.core.deps import require_role

router = APIRouter(prefix="/packaging", tags=["packaging"])
PKG = require_role("GODOWN_MANAGER_PKG")


class LooseStockRowOut(BaseModel):
    count_id: UUID
    cone_type_id: UUID
    bobbin_colour_id: UUID
    available_kg: Decimal


@router.get("/loose-stock", response_model=List[LooseStockRowOut], summary="Live loose stock by count/cone/colour")
async def loose_stock(user: dict = Depends(PKG)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT count_id, cone_type_id, bobbin_colour_id, SUM(quantity_kg) AS available_kg
            FROM operations.loose_stock_ledger
            GROUP BY count_id, cone_type_id, bobbin_colour_id
            HAVING SUM(quantity_kg) > 0
            """
        )
    return recs(rows)


class PackIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    count_id: UUID
    palli_id: UUID
    bobbin_colour_id: UUID
    cone_type_id: UUID
    net_weight_kg: Decimal
    cone_weights: List[Decimal] = []

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "66666666-7777-8888-9999-000000000000",
                "count_id": "77777777-8888-9999-0000-111111111111",
                "palli_id": "palli-vardhman-red-uuid",
                "bobbin_colour_id": "colour-red-uuid",
                "cone_type_id": "cone-24-uuid",
                "net_weight_kg": "49.80",
                "cone_weights": ["27.90", "21.90"],
            }
        }
    }


class PackagingRecordOut(BaseModel):
    id: UUID
    bag_code: str
    count_id: UUID
    palli_id: UUID
    bobbin_colour_id: UUID
    cone_type_id: UUID
    target_weight_kg: Decimal
    net_weight_kg: Decimal
    packed_by: UUID
    packed_at: datetime
    created_at: datetime

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "66666666-7777-8888-9999-000000000000",
                "bag_code": "PK-118",
                "count_id": "77777777-8888-9999-0000-111111111111",
                "palli_id": "palli-vardhman-red-uuid",
                "bobbin_colour_id": "colour-red-uuid",
                "cone_type_id": "cone-24-uuid",
                "target_weight_kg": "50.00",
                "net_weight_kg": "49.80",
                "packed_by": "22222222-3333-4444-5555-666666666666",
                "packed_at": "2026-07-14T10:02:00Z",
                "created_at": "2026-07-14T10:02:00Z",
            }
        }
    }


@router.post(
    "/records",
    response_model=PackagingRecordOut,
    status_code=201,
    summary="Mark a bag packed — debits loose stock, credits production stock",
)
async def pack_bag(body: PackIn, user: dict = Depends(PKG)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """
            INSERT INTO operations.packaging_records
                (id, count_id, palli_id, bobbin_colour_id, cone_type_id, net_weight_kg, packed_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING *
            """,
            body.id, body.count_id, body.palli_id, body.bobbin_colour_id,
            body.cone_type_id, body.net_weight_kg, user["id"],
        )
        for i, w in enumerate(body.cone_weights, start=1):
            await conn.execute(
                "INSERT INTO operations.packaging_record_cones (packaging_record_id, sequence_no, actual_weight_kg) "
                "VALUES ($1,$2,$3) ON CONFLICT (packaging_record_id, sequence_no) DO NOTHING",
                row["id"], i, w,
            )
    return rec(row)


@router.get("/records", response_model=List[PackagingRecordOut], summary="Bags packed today")
async def packed_today(user: dict = Depends(PKG)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM operations.packaging_records WHERE packed_at::date = current_date ORDER BY packed_at DESC"
        )
    return recs(rows)


@router.get(
    "/loading-vehicles",
    response_model=List[VehicleStatusOut],
    summary="Vehicles available to dispatch against (LOADING-purpose only, still on premises)",
)
async def loading_vehicles(user: dict = Depends(PKG)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM operations.vw_gate_vehicle_current_status "
            "WHERE purpose = 'LOADING' AND current_status = 'ON_PREMISES' ORDER BY entered_at"
        )
    return recs(rows)


class DispatchIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    gate_entry_id: UUID  # trigger rejects this unless the entry's purpose is LOADING
    party_id: UUID


class DispatchEntryOut(BaseModel):
    id: UUID
    dispatch_code: str
    gate_entry_id: UUID
    party_id: UUID
    dispatch_qr: Optional[str]
    generated_by: UUID
    generated_at: datetime
    created_at: datetime


@router.post(
    "/dispatch-entries",
    response_model=DispatchEntryOut,
    status_code=201,
    summary="Open a dispatch against a LOADING-purpose vehicle",
    responses={400: {"description": "The referenced gate entry is not a LOADING-purpose vehicle"}},
)
async def create_dispatch(body: DispatchIn, user: dict = Depends(PKG)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO operations.dispatch_entries (id, gate_entry_id, party_id, generated_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING *
            """,
            body.id, body.gate_entry_id, body.party_id, user["id"],
        )
    return rec(row)


class DispatchLineIn(BaseModel):
    count_id: UUID
    palli_id: UUID
    bag_count: int
    approx_weight_kg: Optional[Decimal] = None


class DispatchLineOut(BaseModel):
    id: UUID
    dispatch_entry_id: UUID
    count_id: UUID
    palli_id: UUID
    bag_count: int
    approx_weight_kg: Optional[Decimal]
    created_at: datetime


@router.post(
    "/dispatch-entries/{dispatch_id}/lines",
    response_model=DispatchLineOut,
    status_code=201,
    summary="Add one Count+Palli+bag-count line to a dispatch",
)
async def add_dispatch_line(dispatch_id: UUID, body: DispatchLineIn, user: dict = Depends(PKG)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.dispatch_lines "
            "(dispatch_entry_id, count_id, palli_id, bag_count, approx_weight_kg) "
            "VALUES ($1,$2,$3,$4,$5) RETURNING *",
            dispatch_id, body.count_id, body.palli_id, body.bag_count, body.approx_weight_kg,
        )
    return rec(row)


class BagAssignIn(BaseModel):
    packaging_record_id: UUID  # unique across all dispatch lines — a bag ships exactly once


class BagAssignOut(BaseModel):
    id: UUID
    dispatch_line_id: UUID
    packaging_record_id: UUID
    created_at: datetime


@router.post(
    "/dispatch-lines/{line_id}/bags",
    response_model=BagAssignOut,
    status_code=201,
    summary="Assign a specific packed bag to a dispatch line — debits production stock",
    responses={409: {"description": "This bag has already been dispatched"}},
)
async def assign_bag(line_id: UUID, body: BagAssignIn, user: dict = Depends(PKG)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.dispatch_line_bags (dispatch_line_id, packaging_record_id) "
            "VALUES ($1,$2) RETURNING *",
            line_id, body.packaging_record_id,
        )
    return rec(row)
