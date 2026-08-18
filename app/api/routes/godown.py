from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.routes.gate import VehicleStatusOut
from app.core.db import get_pool, rec, recs
from app.core.deps import require_role

router = APIRouter(prefix="/godown", tags=["godown"])
RM = require_role("GODOWN_MANAGER_RM")


# ---- Shared shapes (also reused by admin.py's traceability search, so a bale or a
# mixing lot looks identical wherever it appears in the API) ----

class BaleOut(BaseModel):
    id: UUID
    bale_code: str
    gate_entry_id: UUID
    gross_weight_kg: Decimal
    tare_weight_kg: Decimal
    net_weight_kg: Decimal
    quality_grade: Literal["A", "B", "C"]
    material_variety_id: UUID
    party_id: UUID
    godown_location_id: UUID
    qr_payload: Optional[str]
    recorded_by: UUID
    created_at: datetime
    printed: bool

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "3f1b6c1a-1111-4a2a-9b7b-1a2b3c4d5e6f",
                "bale_code": "BL-1842",
                "gate_entry_id": "5b1c2e2a-6b7b-4a2a-9b7b-1a2b3c4d5e6f",
                "gross_weight_kg": "186.40",
                "tare_weight_kg": "3.80",
                "net_weight_kg": "182.60",
                "quality_grade": "A",
                "material_variety_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "party_id": "1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                "godown_location_id": "11111111-2222-3333-4444-555555555555",
                "qr_payload": "BL-1842",
                "recorded_by": "22222222-3333-4444-5555-666666666666",
                "created_at": "2026-07-14T09:39:00Z",
                "printed": True,
            }
        }
    }


class MixingLotOut(BaseModel):
    id: UUID
    lot_code: str
    target_weight_kg: Decimal
    current_status: Optional[Literal["BUILDING", "ACTIVE", "CONSUMED", "CANCELLED"]]
    scanned_kg: Decimal
    bale_count: int

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "44444444-5555-6666-7777-888888888888",
                "lot_code": "MX-041",
                "target_weight_kg": "7154.00",
                "current_status": "ACTIVE",
                "scanned_kg": "6584.00",
                "bale_count": 42,
            }
        }
    }


async def _bale_out(conn, bale_id: UUID) -> Optional[dict]:
    row = await conn.fetchrow(
        """
        SELECT b.*, EXISTS(SELECT 1 FROM operations.bale_qr_prints p WHERE p.bale_id = b.id) AS printed
        FROM operations.bales b WHERE b.id = $1
        """,
        bale_id,
    )
    return rec(row)


async def _mixing_lot_out(conn, lot_id: UUID) -> Optional[dict]:
    row = await conn.fetchrow(
        """
        SELECT ml.id, ml.lot_code, ml.target_weight_kg, mcs.current_status,
               COALESCE(SUM(s.net_weight_kg), 0) AS scanned_kg, COUNT(s.id) AS bale_count
        FROM operations.mixing_lots ml
        LEFT JOIN operations.mixing_lot_current_state mcs ON mcs.mixing_lot_id = ml.id
        LEFT JOIN operations.mixing_bale_scans s ON s.mixing_lot_id = ml.id
        WHERE ml.id = $1
        GROUP BY ml.id, ml.lot_code, ml.target_weight_kg, mcs.current_status
        """,
        lot_id,
    )
    return rec(row)


# ---- Unloading ----

@router.get("/unloading-queue", response_model=List[VehicleStatusOut], summary="Vehicles awaiting unloading")
async def unloading_queue(user: dict = Depends(RM)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM operations.vw_gate_vehicle_current_status "
            "WHERE purpose = 'UNLOADING' AND current_status = 'ON_PREMISES' ORDER BY entered_at"
        )
    return recs(rows)


@router.get("/entries/{entry_id}/bales", response_model=List[BaleOut], summary="Every bale weighed off one vehicle")
async def bales_for_entry(entry_id: UUID, user: dict = Depends(RM)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT b.*, EXISTS(SELECT 1 FROM operations.bale_qr_prints p WHERE p.bale_id = b.id) AS printed
            FROM operations.bales b WHERE gate_entry_id = $1 ORDER BY created_at
            """,
            entry_id,
        )
    return recs(rows)


class BaleIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    gate_entry_id: UUID
    gross_weight_kg: Decimal
    tare_weight_kg: Decimal
    quality_grade: Literal["A", "B", "C"]
    material_variety_id: UUID
    party_id: UUID
    godown_location_id: UUID

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "3f1b6c1a-1111-4a2a-9b7b-1a2b3c4d5e6f",
                "gate_entry_id": "5b1c2e2a-6b7b-4a2a-9b7b-1a2b3c4d5e6f",
                "gross_weight_kg": "186.40",
                "tare_weight_kg": "3.80",
                "quality_grade": "A",
                "material_variety_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "party_id": "1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                "godown_location_id": "11111111-2222-3333-4444-555555555555",
            }
        }
    }


@router.post("/bales", response_model=BaleOut, status_code=201, summary="Weigh in one bale (auto QR, auto ledger entry)")
async def create_bale(body: BaleIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        new_id = await conn.fetchval(
            """
            INSERT INTO operations.bales
                (id, gate_entry_id, gross_weight_kg, tare_weight_kg, quality_grade,
                 material_variety_id, party_id, godown_location_id, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING id
            """,
            body.id, body.gate_entry_id, body.gross_weight_kg, body.tare_weight_kg,
            body.quality_grade, body.material_variety_id, body.party_id,
            body.godown_location_id, user["id"],
        )
        return await _bale_out(conn, new_id)


@router.get(
    "/bales/{bale_id}",
    response_model=BaleOut,
    summary="Single bale record + QR",
    responses={404: {"description": "Bale not found"}},
)
async def bale_detail(bale_id: UUID, user: dict = Depends(RM)) -> dict:
    async with get_pool().acquire() as conn:
        bale = await _bale_out(conn, bale_id)
    if not bale:
        raise HTTPException(404, "Bale not found")
    return bale


class PrintOut(BaseModel):
    id: UUID
    bale_id: UUID
    printed_by: UUID
    printed_at: datetime


@router.post("/bales/{bale_id}/print", response_model=PrintOut, status_code=201, summary="Log a (re)print — never regenerates the QR")
async def print_bale(bale_id: UUID, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.bale_qr_prints (bale_id, printed_by) VALUES ($1,$2) RETURNING *",
            bale_id, user["id"],
        )
    return rec(row)


class InventoryRowOut(BaseModel):
    location: str
    variety: str
    quality_grade: Literal["A", "B", "C"]
    bale_count: int
    total_kg: Decimal


@router.get("/inventory", response_model=List[InventoryRowOut], summary="Live raw-material stock by location/variety/grade")
async def inventory(user: dict = Depends(RM)) -> List[dict]:
    # Live SUM over the ledger/current-state view, not the analytics materialized view —
    # this operational screen needs real-time correctness; the GM/MD dashboard is the
    # one place a few-minutes-stale materialized view is acceptable (see admin router).
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT gl.name AS location, mv.name AS variety, b.quality_grade,
                   COUNT(*) AS bale_count, SUM(b.net_weight_kg) AS total_kg
            FROM operations.vw_bale_current_state cs
            JOIN operations.bales b ON b.id = cs.bale_id
            JOIN config.material_varieties mv ON mv.id = b.material_variety_id
            JOIN config.godown_locations gl ON gl.id = cs.godown_location_id
            WHERE cs.current_status = 'RECEIVED'
            GROUP BY gl.name, mv.name, b.quality_grade
            """
        )
    return recs(rows)


# ---- Mixing / Blow Room ----

class RecipeItem(BaseModel):
    mixing_category_id: UUID
    target_percentage: Decimal
    target_weight_kg: Decimal


class MixingLotIn(BaseModel):
    id: UUID
    count_id: UUID
    target_weight_kg: Decimal
    recipe: List[RecipeItem]

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "44444444-5555-6666-7777-888888888888",
                "count_id": "99999999-8888-7777-6666-555555555555",
                "target_weight_kg": "7154.00",
                "recipe": [
                    {"mixing_category_id": "cat-pc-uuid", "target_percentage": "44.00", "target_weight_kg": "3147.76"},
                    {"mixing_category_id": "cat-break-uuid", "target_percentage": "33.00", "target_weight_kg": "2360.82"},
                    {"mixing_category_id": "cat-sf-uuid", "target_percentage": "12.00", "target_weight_kg": "858.48"},
                    {"mixing_category_id": "cat-waste-uuid", "target_percentage": "11.00", "target_weight_kg": "786.94"},
                ],
            }
        }
    }


@router.post("/mixing-lots", response_model=MixingLotOut, status_code=201, summary="Start a new mixing lot with its target recipe")
async def create_mixing_lot(body: MixingLotIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        lot_id = await conn.fetchval(
            """
            INSERT INTO operations.mixing_lots (id, count_id, target_weight_kg, created_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING id
            """,
            body.id, body.count_id, body.target_weight_kg, user["id"],
        )
        for item in body.recipe:
            await conn.execute(
                """
                INSERT INTO operations.mixing_lot_recipe_items
                    (mixing_lot_id, mixing_category_id, target_percentage, target_weight_kg)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (mixing_lot_id, mixing_category_id) DO NOTHING
                """,
                lot_id, item.mixing_category_id, item.target_percentage, item.target_weight_kg,
            )
        await conn.execute(
            "INSERT INTO operations.mixing_lot_status_events (mixing_lot_id, status, recorded_by) "
            "VALUES ($1,'BUILDING',$2)",
            lot_id, user["id"],
        )
        return await _mixing_lot_out(conn, lot_id)


@router.get("/mixing-lots", response_model=List[MixingLotOut], summary="Every building/active/consumed mixing lot")
async def list_mixing_lots(user: dict = Depends(RM)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ml.id, ml.lot_code, ml.target_weight_kg, mcs.current_status,
                   COALESCE(SUM(s.net_weight_kg), 0) AS scanned_kg, COUNT(s.id) AS bale_count
            FROM operations.mixing_lots ml
            LEFT JOIN operations.mixing_lot_current_state mcs ON mcs.mixing_lot_id = ml.id
            LEFT JOIN operations.mixing_bale_scans s ON s.mixing_lot_id = ml.id
            GROUP BY ml.id, ml.lot_code, ml.target_weight_kg, mcs.current_status
            ORDER BY ml.created_at DESC
            """
        )
    return recs(rows)


class RecipeItemOut(BaseModel):
    category: str
    target_percentage: Decimal
    target_weight_kg: Decimal
    scanned_kg: Decimal


class MixingScanRowOut(BaseModel):
    id: UUID
    mixing_lot_id: UUID
    bale_id: UUID
    mixing_category_id: UUID
    net_weight_kg: Decimal
    scanned_by: UUID
    occurred_at: datetime
    created_at: datetime
    bale_code: str
    party_name: str


class MixingLotDetailOut(BaseModel):
    recipe: List[RecipeItemOut]
    bales: List[MixingScanRowOut]


@router.get(
    "/mixing-lots/{lot_id}",
    response_model=MixingLotDetailOut,
    summary="Recipe progress + every bale scanned into this lot",
    responses={404: {"description": "Mixing lot not found"}},
)
async def mixing_lot_detail(lot_id: UUID, user: dict = Depends(RM)) -> dict:
    async with get_pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM operations.mixing_lots WHERE id = $1", lot_id):
            raise HTTPException(404, "Mixing lot not found")
        recipe = await conn.fetch(
            """
            SELECT mc.name AS category, ri.target_percentage, ri.target_weight_kg,
                   COALESCE(SUM(s.net_weight_kg), 0) AS scanned_kg
            FROM operations.mixing_lot_recipe_items ri
            JOIN config.mixing_categories mc ON mc.id = ri.mixing_category_id
            LEFT JOIN operations.mixing_bale_scans s
                   ON s.mixing_lot_id = ri.mixing_lot_id AND s.mixing_category_id = ri.mixing_category_id
            WHERE ri.mixing_lot_id = $1
            GROUP BY mc.name, ri.target_percentage, ri.target_weight_kg
            """,
            lot_id,
        )
        bales = await conn.fetch(
            """
            SELECT s.*, b.bale_code, p.name AS party_name
            FROM operations.mixing_bale_scans s
            JOIN operations.bales b ON b.id = s.bale_id
            JOIN config.parties p ON p.id = b.party_id
            WHERE s.mixing_lot_id = $1
            ORDER BY s.occurred_at DESC
            """,
            lot_id,
        )
    return {"recipe": recs(recipe), "bales": recs(bales)}


class StatusIn(BaseModel):
    status: Literal["BUILDING", "ACTIVE", "CONSUMED", "CANCELLED"]
    note: Optional[str] = None


class StatusEventOut(BaseModel):
    id: UUID
    mixing_lot_id: UUID
    status: Literal["BUILDING", "ACTIVE", "CONSUMED", "CANCELLED"]
    note: Optional[str]
    recorded_by: UUID
    occurred_at: datetime
    created_at: datetime


@router.post(
    "/mixing-lots/{lot_id}/status",
    response_model=StatusEventOut,
    status_code=201,
    summary="Transition a lot's status (only one lot system-wide may be ACTIVE)",
    responses={409: {"description": "Another lot is already ACTIVE"}},
)
async def set_mixing_status(lot_id: UUID, body: StatusIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.mixing_lot_status_events (mixing_lot_id, status, note, recorded_by) "
            "VALUES ($1,$2,$3,$4) RETURNING *",
            lot_id, body.status, body.note, user["id"],
        )
    return rec(row)


class ScanIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    bale_id: UUID
    mixing_category_id: UUID


class ScanOut(BaseModel):
    id: UUID
    mixing_lot_id: UUID
    bale_id: UUID
    mixing_category_id: UUID
    net_weight_kg: Decimal
    scanned_by: UUID
    occurred_at: datetime
    created_at: datetime


@router.post(
    "/mixing-lots/{lot_id}/scan",
    response_model=ScanOut,
    status_code=201,
    summary="Scan a bale's QR to issue it from godown stock into this lot",
    responses={409: {"description": "This bale has already been scanned into a lot"}},
)
async def scan_bale(lot_id: UUID, body: ScanIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        net_weight = await conn.fetchval("SELECT net_weight_kg FROM operations.bales WHERE id = $1", body.bale_id)
        row = await conn.fetchrow(
            """
            INSERT INTO operations.mixing_bale_scans
                (id, mixing_lot_id, bale_id, mixing_category_id, net_weight_kg, scanned_by)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING *
            """,
            body.id, lot_id, body.bale_id, body.mixing_category_id, net_weight, user["id"],
        )
    return rec(row)
