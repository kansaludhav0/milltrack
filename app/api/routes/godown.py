import logging
from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes.gate import VehicleStatusOut, _with_signed_photos
from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.sheets import render_mixing_sheet, store_sheet_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/godown", tags=["godown"])
RM = require_role("GODOWN_MANAGER_RM")
# Read-only visibility for GM/MD — see gate.py's GATE_READ for the reasoning. Never used
# on writes; only the RM manager can weigh in a bale, print a QR, or manage a mixing lot.
RM_READ = require_role("GODOWN_MANAGER_RM", "GM_MD_ADMIN")
# RM's job ends once a mixing lot is marked complete (see complete_mixing_lot) — any
# further status change (CONSUMED/CANCELLED/correction) is Admin's call, not RM's.
ADMIN = require_role("GM_MD_ADMIN")


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
    # tare/net come from vw_bale_effective_weight, not the (append-only) bale row directly —
    # see /bales/{id}/tare: a correction never UPDATEs operations.bales, it only appends to
    # bale_tare_corrections, and this view resolves the latest one. gross_weight_kg and
    # everything else is read straight off the immutable bale row as always.
    row = await conn.fetchrow(
        """
        SELECT b.id, b.bale_code, b.gate_entry_id, b.gross_weight_kg,
               ew.tare_weight_kg, ew.net_weight_kg,
               b.quality_grade, b.material_variety_id, b.party_id, b.godown_location_id,
               b.qr_payload, b.recorded_by, b.created_at,
               EXISTS(SELECT 1 FROM operations.bale_qr_prints p WHERE p.bale_id = b.id) AS printed
        FROM operations.bales b
        JOIN operations.vw_bale_effective_weight ew ON ew.bale_id = b.id
        WHERE b.id = $1
        """,
        bale_id,
    )
    return rec(row)


async def _mixing_lot_out(conn, lot_id: UUID) -> Optional[dict]:
    row = await conn.fetchrow(
        """
        SELECT ml.id, ml.lot_code, ml.target_weight_kg, mcs.current_status,
               COALESCE(s.scanned_kg, 0) + COALESCE(w.waste_kg, 0) AS scanned_kg,
               COALESCE(s.bale_count, 0) AS bale_count
        FROM operations.mixing_lots ml
        LEFT JOIN operations.mixing_lot_current_state mcs ON mcs.mixing_lot_id = ml.id
        LEFT JOIN (
            SELECT mixing_lot_id, SUM(net_weight_kg) AS scanned_kg, COUNT(*) AS bale_count
            FROM operations.mixing_bale_scans ms
            WHERE NOT EXISTS (SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = ms.id)
            GROUP BY mixing_lot_id
        ) s ON s.mixing_lot_id = ml.id
        LEFT JOIN (
            SELECT mixing_lot_id, SUM(weight_kg) AS waste_kg
            FROM operations.mixing_waste WHERE removed_at IS NULL GROUP BY mixing_lot_id
        ) w ON w.mixing_lot_id = ml.id
        WHERE ml.id = $1
        """,
        lot_id,
    )
    return rec(row)


# ---- Unloading ----

@router.get("/unloading-queue", response_model=List[VehicleStatusOut], summary="Vehicles awaiting unloading")
async def unloading_queue(user: dict = Depends(RM_READ)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT v.*, e.photo_url FROM operations.vw_gate_vehicle_current_status v
            JOIN operations.gate_vehicle_entries e ON e.id = v.gate_entry_id
            LEFT JOIN operations.unloading_completions uc ON uc.gate_entry_id = v.gate_entry_id
            WHERE v.purpose = 'UNLOADING' AND v.current_status = 'ON_PREMISES' AND uc.gate_entry_id IS NULL
            ORDER BY v.entered_at
            """
        )
    return _with_signed_photos(rows)


@router.post(
    "/entries/{entry_id}/finish-unloading",
    status_code=204,
    summary="Mark a vehicle's unloading as finished — removes it from the Unloading Queue",
)
async def finish_unloading(entry_id: UUID, user: dict = Depends(RM)) -> None:
    async with get_pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO operations.unloading_completions (gate_entry_id, recorded_by)
            VALUES ($1, $2)
            ON CONFLICT (gate_entry_id) DO NOTHING
            """,
            entry_id, user["id"],
        )


async def _bales_for_entry_rows(conn, entry_id: UUID):
    return await conn.fetch(
        """
        SELECT b.id, b.bale_code, b.gate_entry_id, b.gross_weight_kg,
               ew.tare_weight_kg, ew.net_weight_kg,
               b.quality_grade, b.material_variety_id, b.party_id, b.godown_location_id,
               b.qr_payload, b.recorded_by, b.created_at,
               EXISTS(SELECT 1 FROM operations.bale_qr_prints p WHERE p.bale_id = b.id) AS printed
        FROM operations.bales b
        JOIN operations.vw_bale_current_state cs ON cs.bale_id = b.id
        JOIN operations.vw_bale_effective_weight ew ON ew.bale_id = b.id
        WHERE b.gate_entry_id = $1 AND cs.current_status != 'ADJUSTED_OUT'
        ORDER BY b.created_at
        """,
        entry_id,
    )


@router.get("/entries/{entry_id}/bales", response_model=List[BaleOut], summary="Every bale weighed off one vehicle")
async def bales_for_entry(entry_id: UUID, user: dict = Depends(RM_READ)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await _bales_for_entry_rows(conn, entry_id)
    return recs(rows)


class TareIn(BaseModel):
    tare_weight_kg: Decimal = Field(..., gt=0, decimal_places=3)


@router.patch(
    "/entries/{entry_id}/bales/tare",
    response_model=List[BaleOut],
    summary="Correct the tare for every bale weighed off one vehicle (e.g. the scale's tare was wrong)",
    responses={
        404: {"description": "Gate entry not found"},
        400: {"description": "New tare is not less than a bale's gross weight"},
        409: {"description": "One or more bales are already issued to a mixing lot"},
    },
)
async def correct_entry_tare(entry_id: UUID, body: TareIn, user: dict = Depends(RM)) -> List[dict]:
    # operations.bales is append-only — this never UPDATEs a bale row. Each corrected
    # bale instead gets a new row in bale_tare_corrections, and every read path (BaleOut,
    # StockBaleOut, /inventory) resolves the "effective" tare/net via
    # vw_bale_effective_weight, which always prefers the latest correction.
    async with get_pool().acquire() as conn, conn.transaction():
        if not await conn.fetchval("SELECT 1 FROM operations.gate_vehicle_entries WHERE id = $1", entry_id):
            raise HTTPException(404, "Gate entry not found")

        bales = await conn.fetch(
            """
            SELECT b.id, b.bale_code, b.gross_weight_kg, ew.tare_weight_kg AS current_tare,
                   cs.current_status
            FROM operations.bales b
            JOIN operations.vw_bale_current_state cs ON cs.bale_id = b.id
            JOIN operations.vw_bale_effective_weight ew ON ew.bale_id = b.id
            WHERE b.gate_entry_id = $1 AND cs.current_status != 'ADJUSTED_OUT'
            """,
            entry_id,
        )
        if not bales:
            return []

        issued = [b for b in bales if b["current_status"] == "ISSUED_TO_MIXING"]
        if issued:
            raise HTTPException(
                409,
                f"{len(issued)} bale(s) from this vehicle are already issued to a mixing lot, "
                "so their tare can no longer be changed",
            )

        for b in bales:
            if body.tare_weight_kg >= b["gross_weight_kg"]:
                raise HTTPException(400, f"Tare is not less than the gross weight of bale {b['bale_code']}")

        for b in bales:
            if b["current_tare"] == body.tare_weight_kg:
                continue  # already at this tare — idempotent no-op for this bale
            await conn.execute(
                """
                INSERT INTO operations.bale_tare_corrections
                    (bale_id, old_tare_weight_kg, new_tare_weight_kg, old_net_weight_kg, new_net_weight_kg, changed_by)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                b["id"], b["current_tare"], body.tare_weight_kg,
                b["gross_weight_kg"] - b["current_tare"], b["gross_weight_kg"] - body.tare_weight_kg,
                user["id"],
            )

        rows = await _bales_for_entry_rows(conn, entry_id)
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
    # Auto-logs the first print at weigh-in time — no separate manual "print" tap needed.
    # Guarded by an existence check (not unconditional) so a retried/idempotent create
    # (same client-generated id) never logs a second, spurious print.
    async with get_pool().acquire() as conn, conn.transaction():
        # DO NOTHING, not DO UPDATE — bales is append-only; a retried create finds
        # nothing to insert (new_id just stays body.id, which already identifies the
        # existing row) rather than hitting the append-only trigger.
        new_id = await conn.fetchval(
            """
            INSERT INTO operations.bales
                (id, gate_entry_id, gross_weight_kg, tare_weight_kg, quality_grade,
                 material_variety_id, party_id, godown_location_id, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            body.id, body.gate_entry_id, body.gross_weight_kg, body.tare_weight_kg,
            body.quality_grade, body.material_variety_id, body.party_id,
            body.godown_location_id, user["id"],
        )
        new_id = new_id or body.id
        already_printed = await conn.fetchval(
            "SELECT 1 FROM operations.bale_qr_prints WHERE bale_id = $1", new_id
        )
        if not already_printed:
            await conn.execute(
                "INSERT INTO operations.bale_qr_prints (bale_id, printed_by) VALUES ($1, $2)",
                new_id, user["id"],
            )
        return await _bale_out(conn, new_id)


@router.get(
    "/bales/{bale_id}",
    response_model=BaleOut,
    summary="Single bale record + QR",
    responses={404: {"description": "Bale not found"}},
)
async def bale_detail(bale_id: UUID, user: dict = Depends(RM_READ)) -> dict:
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


class VoidIn(BaseModel):
    reason: Optional[str] = Field(None, max_length=200)


@router.post(
    "/bales/{bale_id}/void",
    status_code=204,
    summary="Correct a mistyped bale — reverses it out of stock, never deletes the row",
    responses={
        404: {"description": "Bale not found"},
        409: {"description": "This bale is already in a mixing lot"},
    },
)
async def void_bale(bale_id: UUID, body: VoidIn, user: dict = Depends(RM)) -> None:
    # operations.bales is append-only (DB-enforced) — voiding never UPDATEs the bale row
    # or its QR/bale_code. Instead this appends an ADJUSTED_OUT entry to the same ledger
    # that already drives /inventory, /stock-bales and the scan-eligibility check, so a
    # voided bale disappears from all of them automatically via the existing RECEIVED
    # filter — no second "in stock" definition is introduced.
    async with get_pool().acquire() as conn:
        bale = await conn.fetchrow(
            "SELECT net_weight_kg, godown_location_id FROM operations.bales WHERE id = $1", bale_id
        )
        if not bale:
            raise HTTPException(404, "Bale not found")
        status = await conn.fetchval(
            "SELECT current_status FROM operations.vw_bale_current_state WHERE bale_id = $1", bale_id
        )
        if status == "ADJUSTED_OUT":
            return  # already voided — idempotent 204
        if status != "RECEIVED":
            raise HTTPException(409, "This bale is already in a mixing lot")
        await conn.execute(
            """
            INSERT INTO operations.raw_material_ledger
                (bale_id, tx_type, godown_location_id, quantity_kg, recorded_by, occurred_at, note)
            VALUES ($1, 'ADJUSTED_OUT', $2, $3, $4, now(), $5)
            """,
            bale_id, bale["godown_location_id"], -bale["net_weight_kg"], user["id"], body.reason,
        )


class InventoryRowOut(BaseModel):
    location: str
    variety: str
    quality_grade: Literal["A", "B", "C"]
    bale_count: int
    total_kg: Decimal


@router.get("/inventory", response_model=List[InventoryRowOut], summary="Live raw-material stock by location/variety/grade")
async def inventory(user: dict = Depends(RM_READ)) -> List[dict]:
    # Live SUM over the ledger/current-state view, not the analytics materialized view —
    # this operational screen needs real-time correctness; the GM/MD dashboard is the
    # one place a few-minutes-stale materialized view is acceptable (see admin router).
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT gl.name AS location, mv.name AS variety, b.quality_grade,
                   COUNT(*) AS bale_count, SUM(ew.net_weight_kg) AS total_kg
            FROM operations.vw_bale_current_state cs
            JOIN operations.bales b ON b.id = cs.bale_id
            JOIN operations.vw_bale_effective_weight ew ON ew.bale_id = b.id
            JOIN config.material_varieties mv ON mv.id = b.material_variety_id
            JOIN config.godown_locations gl ON gl.id = cs.godown_location_id
            WHERE cs.current_status = 'RECEIVED'
            GROUP BY gl.name, mv.name, b.quality_grade
            """
        )
    return recs(rows)


class StockBaleOut(BaseModel):
    id: UUID
    bale_code: str
    gate_entry_id: UUID
    vehicle_number: str
    party_id: UUID
    party_name: str
    material_variety_id: UUID
    material_variety_name: str
    default_mixing_category_id: Optional[UUID]
    quality_grade: Literal["A", "B", "C"]
    godown_location_id: UUID
    godown_location_name: str
    gross_weight_kg: Decimal
    tare_weight_kg: Decimal
    net_weight_kg: Decimal
    qr_payload: Optional[str]
    printed: bool
    created_at: datetime


@router.get(
    "/stock-bales",
    response_model=List[StockBaleOut],
    summary="Individual bales currently in godown stock (not yet issued to any mixing lot)",
)
async def stock_bales(
    party_id: Optional[UUID] = None,
    material_variety_id: Optional[UUID] = None,
    godown_location_id: Optional[UUID] = None,
    search: Optional[str] = Query(
        None, description="Matches bale_code, QR payload, party name, material variety name, or vehicle number"
    ),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(RM_READ),
) -> List[dict]:
    # Same "in stock" definition as /inventory: the latest raw_material_ledger entry for
    # a bale is still RECEIVED — i.e. no ISSUED_TO_MIXING scan and no ADJUSTED_OUT void
    # has superseded it. Voided bales (see /bales/{id}/void) are excluded automatically
    # by this same filter, since voiding appends an ADJUSTED_OUT ledger entry.
    conditions = ["cs.current_status = 'RECEIVED'"]
    values: list = []
    if party_id:
        values.append(party_id)
        conditions.append(f"b.party_id = ${len(values)}")
    if material_variety_id:
        values.append(material_variety_id)
        conditions.append(f"b.material_variety_id = ${len(values)}")
    if godown_location_id:
        values.append(godown_location_id)
        conditions.append(f"b.godown_location_id = ${len(values)}")
    if search:
        values.append(f"%{search}%")
        conditions.append(
            f"(b.bale_code ILIKE ${len(values)} OR b.qr_payload ILIKE ${len(values)} "
            f"OR p.name ILIKE ${len(values)} OR mv.name ILIKE ${len(values)} "
            f"OR ge.vehicle_number ILIKE ${len(values)})"
        )
    where = " AND ".join(conditions)
    values.append(limit)
    limit_idx = len(values)
    values.append(offset)
    offset_idx = len(values)

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT b.id, b.bale_code, b.gate_entry_id, ge.vehicle_number,
                   b.party_id, p.name AS party_name,
                   b.material_variety_id, mv.name AS material_variety_name,
                   mv.default_mixing_category_id, b.quality_grade,
                   b.godown_location_id, gl.name AS godown_location_name,
                   b.gross_weight_kg, ew.tare_weight_kg, ew.net_weight_kg,
                   b.qr_payload, EXISTS(
                       SELECT 1 FROM operations.bale_qr_prints pr WHERE pr.bale_id = b.id
                   ) AS printed,
                   b.created_at
            FROM operations.vw_bale_current_state cs
            JOIN operations.bales b ON b.id = cs.bale_id
            JOIN operations.vw_bale_effective_weight ew ON ew.bale_id = b.id
            JOIN operations.gate_vehicle_entries ge ON ge.id = b.gate_entry_id
            JOIN config.parties p ON p.id = b.party_id
            JOIN config.material_varieties mv ON mv.id = b.material_variety_id
            JOIN config.godown_locations gl ON gl.id = b.godown_location_id
            WHERE {where}
            ORDER BY p.name ASC, b.bale_code ASC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *values,
        )
    return recs(rows)


# ---- Mixing / Blow Room ----

class MixingLotIn(BaseModel):
    id: UUID
    count_id: UUID
    target_weight_kg: Decimal

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "44444444-5555-6666-7777-888888888888",
                "count_id": "99999999-8888-7777-6666-555555555555",
                "target_weight_kg": "7154.00",
            }
        }
    }


@router.post(
    "/mixing-lots",
    response_model=MixingLotOut,
    status_code=201,
    summary="Start a new mixing lot — the recipe is applied automatically from the standard "
            "mixing recipe (set by an admin via PUT /admin/mixing-recipe), not supplied per lot",
    responses={409: {"description": "No standard mixing recipe has been configured yet"}},
)
async def create_mixing_lot(body: MixingLotIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        standard_recipe = await conn.fetch(
            "SELECT mixing_category_id, target_percentage FROM config.standard_mixing_recipe"
        )
        if not standard_recipe:
            raise HTTPException(409, "No standard mixing recipe has been configured yet — ask an admin to set one")

        # DO NOTHING, not DO UPDATE — mixing_lots is append-only. A retry finds nothing
        # to insert; return the lot as already created rather than re-running the
        # recipe/status-event inserts below a second time (the status event in
        # particular has no dedupe of its own and would otherwise duplicate).
        lot_id = await conn.fetchval(
            """
            INSERT INTO operations.mixing_lots (id, count_id, target_weight_kg, created_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            body.id, body.count_id, body.target_weight_kg, user["id"],
        )
        if lot_id is None:
            return await _mixing_lot_out(conn, body.id)
        for line in standard_recipe:
            target_weight = (body.target_weight_kg * line["target_percentage"] / 100).quantize(Decimal("0.01"))
            await conn.execute(
                """
                INSERT INTO operations.mixing_lot_recipe_items
                    (mixing_lot_id, mixing_category_id, target_percentage, target_weight_kg)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (mixing_lot_id, mixing_category_id) DO NOTHING
                """,
                lot_id, line["mixing_category_id"], line["target_percentage"], target_weight,
            )
        await conn.execute(
            "INSERT INTO operations.mixing_lot_status_events (mixing_lot_id, status, recorded_by) "
            "VALUES ($1,'BUILDING',$2)",
            lot_id, user["id"],
        )
        return await _mixing_lot_out(conn, lot_id)


@router.get("/mixing-lots", response_model=List[MixingLotOut], summary="Every building/active/consumed mixing lot")
async def list_mixing_lots(user: dict = Depends(RM_READ)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ml.id, ml.lot_code, ml.target_weight_kg, mcs.current_status,
                   COALESCE(s.scanned_kg, 0) + COALESCE(w.waste_kg, 0) AS scanned_kg,
                   COALESCE(s.bale_count, 0) AS bale_count
            FROM operations.mixing_lots ml
            LEFT JOIN operations.mixing_lot_current_state mcs ON mcs.mixing_lot_id = ml.id
            LEFT JOIN (
                SELECT mixing_lot_id, SUM(net_weight_kg) AS scanned_kg, COUNT(*) AS bale_count
                FROM operations.mixing_bale_scans ms
                WHERE NOT EXISTS (SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = ms.id)
                GROUP BY mixing_lot_id
            ) s ON s.mixing_lot_id = ml.id
            LEFT JOIN (
                SELECT mixing_lot_id, SUM(weight_kg) AS waste_kg
                FROM operations.mixing_waste WHERE removed_at IS NULL GROUP BY mixing_lot_id
            ) w ON w.mixing_lot_id = ml.id
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


class WasteOut(BaseModel):
    id: UUID
    mixing_lot_id: UUID
    mixing_category_id: UUID
    weight_kg: Decimal
    note: Optional[str]
    recorded_by: UUID
    created_at: datetime


class MixingLotDetailOut(BaseModel):
    recipe: List[RecipeItemOut]
    bales: List[MixingScanRowOut]
    waste: List[WasteOut]


async def _mixing_lot_detail_data(conn, lot_id: UUID):
    """Shared by the API's own detail view and the frozen Mixing Sheet generator — one
    query, not two copies. The bales query additionally resolves mixing_category by
    name (mixing_category_id alone isn't useful on a printed sheet)."""
    recipe = await conn.fetch(
        """
        SELECT mc.name AS category, ri.target_percentage, ri.target_weight_kg,
               COALESCE(s.scanned_kg, 0) + COALESCE(w.waste_kg, 0) AS scanned_kg
        FROM operations.mixing_lot_recipe_items ri
        JOIN config.mixing_categories mc ON mc.id = ri.mixing_category_id
        LEFT JOIN (
            SELECT mixing_lot_id, mixing_category_id, SUM(net_weight_kg) AS scanned_kg
            FROM operations.mixing_bale_scans ms
            WHERE NOT EXISTS (SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = ms.id)
            GROUP BY mixing_lot_id, mixing_category_id
        ) s ON s.mixing_lot_id = ri.mixing_lot_id AND s.mixing_category_id = ri.mixing_category_id
        LEFT JOIN (
            SELECT mixing_lot_id, mixing_category_id, SUM(weight_kg) AS waste_kg
            FROM operations.mixing_waste WHERE removed_at IS NULL
            GROUP BY mixing_lot_id, mixing_category_id
        ) w ON w.mixing_lot_id = ri.mixing_lot_id AND w.mixing_category_id = ri.mixing_category_id
        WHERE ri.mixing_lot_id = $1
        """,
        lot_id,
    )
    bales = await conn.fetch(
        """
        SELECT s.*, b.bale_code, p.name AS party_name, mc.name AS mixing_category
        FROM operations.mixing_bale_scans s
        JOIN operations.bales b ON b.id = s.bale_id
        JOIN config.parties p ON p.id = b.party_id
        JOIN config.mixing_categories mc ON mc.id = s.mixing_category_id
        WHERE s.mixing_lot_id = $1
          AND NOT EXISTS (SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = s.id)
        ORDER BY s.occurred_at DESC
        """,
        lot_id,
    )
    waste = await conn.fetch(
        """
        SELECT w.*, mc.name AS mixing_category
        FROM operations.mixing_waste w
        JOIN config.mixing_categories mc ON mc.id = w.mixing_category_id
        WHERE w.mixing_lot_id = $1 AND w.removed_at IS NULL
        ORDER BY w.created_at DESC
        """,
        lot_id,
    )
    return recs(recipe), recs(bales), recs(waste)


@router.get(
    "/mixing-lots/{lot_id}",
    response_model=MixingLotDetailOut,
    summary="Recipe progress + every bale scanned into this lot",
    responses={404: {"description": "Mixing lot not found"}},
)
async def mixing_lot_detail(lot_id: UUID, user: dict = Depends(RM_READ)) -> dict:
    async with get_pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM operations.mixing_lots WHERE id = $1", lot_id):
            raise HTTPException(404, "Mixing lot not found")
        recipe, bales, waste = await _mixing_lot_detail_data(conn, lot_id)
    return {"recipe": recipe, "bales": bales, "waste": waste}


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
    summary="Manually set a lot's status — Admin only (CONSUMED/CANCELLED/correction); "
            "the Godown Manager uses POST .../complete instead",
)
async def set_mixing_status(lot_id: UUID, body: StatusIn, admin: dict = Depends(ADMIN)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.mixing_lot_status_events (mixing_lot_id, status, note, recorded_by) "
            "VALUES ($1,$2,$3,$4) RETURNING *",
            lot_id, body.status, body.note, admin["id"],
        )
    return rec(row)


@router.post(
    "/mixing-lots/{lot_id}/complete",
    response_model=StatusEventOut,
    status_code=201,
    summary="Mark a mixing lot complete — the only status action the Godown Manager "
            "needs; the lot becomes Admin-tracked WIP from here",
    responses={
        400: {"description": "Cannot complete an empty mixing lot"},
        404: {"description": "Mixing lot not found"},
        409: {"description": "Lot is not BUILDING"},
    },
)
async def complete_mixing_lot(lot_id: UUID, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        async with conn.transaction():
            status = await conn.fetchval(
                "SELECT current_status FROM operations.mixing_lot_current_state WHERE mixing_lot_id = $1", lot_id
            )
            if status is None:
                if not await conn.fetchval("SELECT 1 FROM operations.mixing_lots WHERE id = $1", lot_id):
                    raise HTTPException(404, "Mixing lot not found")
            if status != "BUILDING":
                raise HTTPException(409, "Lot is not BUILDING")

            has_content = await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM operations.mixing_bale_scans s
                    WHERE s.mixing_lot_id = $1
                      AND NOT EXISTS (SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = s.id)
                ) OR EXISTS(
                    SELECT 1 FROM operations.mixing_waste w WHERE w.mixing_lot_id = $1 AND w.removed_at IS NULL
                )
                """,
                lot_id,
            )
            if not has_content:
                raise HTTPException(400, "Cannot complete an empty mixing lot")

            # No single-ACTIVE-lot restriction — mixing/spinning is continuous, so any
            # number of completed lots can be flowing through production simultaneously.
            row = await conn.fetchrow(
                "INSERT INTO operations.mixing_lot_status_events (mixing_lot_id, status, recorded_by) "
                "VALUES ($1,'ACTIVE',$2) RETURNING *",
                lot_id, user["id"],
            )

        # Freeze today's Mixing Sheet now that the lot is complete. A PDF/S3 hiccup here
        # must never fail the completion itself — it already succeeded and is what
        # matters operationally; the sheet can be missing without blocking the floor.
        try:
            lot = await _mixing_lot_out(conn, lot_id)
            recipe_rows, bale_rows, waste_rows = await _mixing_lot_detail_data(conn, lot_id)
            pdf_bytes = render_mixing_sheet(lot, row["occurred_at"], recipe_rows, bale_rows, waste_rows)
            await store_sheet_report(
                conn, "MIXING", row["occurred_at"].date(), lot_id, None,
                pdf_bytes, user["id"], f"mixing-{lot['lot_code']}.pdf",
            )
        except Exception:
            logger.exception("Failed to generate Mixing Sheet for lot %s", lot_id)
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
    responses={
        404: {"description": "Bale not found"},
        409: {"description": "Lot is not BUILDING, or this bale has already been scanned/voided"},
    },
)
async def scan_bale(lot_id: UUID, body: ScanIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM operations.mixing_bale_scans WHERE id = $1", body.id)
        if existing:
            return rec(existing)  # idempotent retry of an already-successful scan

        lot_status = await conn.fetchval(
            "SELECT current_status FROM operations.mixing_lot_current_state WHERE mixing_lot_id = $1", lot_id
        )
        if lot_status != "BUILDING":
            raise HTTPException(409, "Bales can only be scanned into a lot while it is BUILDING")

        status = await conn.fetchval(
            "SELECT current_status FROM operations.vw_bale_current_state WHERE bale_id = $1", body.bale_id
        )
        if status is None:
            raise HTTPException(404, "Bale not found")
        if status == "ADJUSTED_OUT":
            raise HTTPException(409, "This bale was voided")
        if status != "RECEIVED":
            raise HTTPException(409, "This bale has already been scanned into a lot")

        net_weight = await conn.fetchval("SELECT net_weight_kg FROM operations.bales WHERE id = $1", body.bale_id)
        # DO NOTHING, not DO UPDATE — mixing_bale_scans is append-only. The existence
        # check above already handles the common retry case; this is just the race-
        # window fallback (two concurrent identical requests).
        row = await conn.fetchrow(
            """
            INSERT INTO operations.mixing_bale_scans
                (id, mixing_lot_id, bale_id, mixing_category_id, net_weight_kg, scanned_by)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            body.id, lot_id, body.bale_id, body.mixing_category_id, net_weight, user["id"],
        )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM operations.mixing_bale_scans WHERE id = $1", body.id)
    return rec(row)


@router.delete(
    "/mixing-lots/{lot_id}/scan/{scan_id}",
    status_code=204,
    summary="Return a scanned bale to godown stock — undo a scan",
    responses={
        404: {"description": "Scan not found for this lot"},
        409: {"description": "Bales can only be returned while the lot is BUILDING"},
    },
)
async def unscan_bale(lot_id: UUID, scan_id: UUID, user: dict = Depends(RM)) -> None:
    # mixing_bale_scans is append-only — this never deletes/updates the scan row. It
    # appends a mixing_scan_reversals marker (every read path excludes reversed scans)
    # and appends a RECEIVED ledger entry for the bale, which is exactly what every
    # "in stock" query already looks for — no second definition introduced.
    async with get_pool().acquire() as conn, conn.transaction():
        scan = await conn.fetchrow(
            "SELECT bale_id, net_weight_kg FROM operations.mixing_bale_scans WHERE id = $1 AND mixing_lot_id = $2",
            scan_id, lot_id,
        )
        if not scan:
            raise HTTPException(404, "Scan not found for this lot")

        already_reversed = await conn.fetchval(
            "SELECT 1 FROM operations.mixing_scan_reversals WHERE scan_id = $1", scan_id
        )
        if already_reversed:
            return  # idempotent 204

        lot = await conn.fetchrow(
            """
            SELECT ml.lot_code, mcs.current_status
            FROM operations.mixing_lots ml
            LEFT JOIN operations.mixing_lot_current_state mcs ON mcs.mixing_lot_id = ml.id
            WHERE ml.id = $1
            """,
            lot_id,
        )
        if not lot or lot["current_status"] != "BUILDING":
            raise HTTPException(409, "Bales can only be returned while the lot is BUILDING")

        godown_location_id = await conn.fetchval(
            "SELECT godown_location_id FROM operations.bales WHERE id = $1", scan["bale_id"]
        )

        await conn.execute(
            "INSERT INTO operations.mixing_scan_reversals (scan_id, reversed_by) VALUES ($1, $2)",
            scan_id, user["id"],
        )
        await conn.execute(
            """
            INSERT INTO operations.raw_material_ledger
                (bale_id, tx_type, godown_location_id, quantity_kg, recorded_by, occurred_at, note)
            VALUES ($1, 'RECEIVED', $2, $3, $4, now(), $5)
            """,
            scan["bale_id"], godown_location_id, scan["net_weight_kg"], user["id"],
            f"Returned from mixing lot {lot['lot_code']} (scan reversal)",
        )


class WasteIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    weight_kg: Decimal
    note: Optional[str] = None


@router.post(
    "/mixing-lots/{lot_id}/waste",
    response_model=WasteOut,
    status_code=201,
    summary="Record own waste (Column 5) added to a mixing lot — a typed weight, not a scanned bale",
    responses={409: {"description": "Lot is not BUILDING, or no active WASTE-kind mixing category exists"}},
)
async def add_mixing_waste(lot_id: UUID, body: WasteIn, user: dict = Depends(RM)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id, mixing_lot_id, mixing_category_id, weight_kg, note, recorded_by, created_at "
            "FROM operations.mixing_waste WHERE id = $1",
            body.id,
        )
        if existing:
            return rec(existing)  # idempotent retry of an already-successful add

        lot_status = await conn.fetchval(
            "SELECT current_status FROM operations.mixing_lot_current_state WHERE mixing_lot_id = $1", lot_id
        )
        if lot_status != "BUILDING":
            raise HTTPException(409, "Waste can only be added to a lot while it is BUILDING")

        category_id = await conn.fetchval(
            "SELECT id FROM config.mixing_categories WHERE kind = 'WASTE' AND is_active ORDER BY sort_order LIMIT 1"
        )
        if not category_id:
            raise HTTPException(409, "No active waste-kind mixing category exists")
        row = await conn.fetchrow(
            """
            INSERT INTO operations.mixing_waste
                (id, mixing_lot_id, mixing_category_id, weight_kg, note, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING id, mixing_lot_id, mixing_category_id, weight_kg, note, recorded_by, created_at
            """,
            body.id, lot_id, category_id, body.weight_kg, body.note, user["id"],
        )
    return rec(row)


@router.delete(
    "/mixing-lots/{lot_id}/waste/{waste_id}",
    status_code=204,
    summary="Remove an own-waste entry — only while the lot is BUILDING",
    responses={
        404: {"description": "Waste record not found for this lot"},
        409: {"description": "Lot is not BUILDING"},
    },
)
async def remove_mixing_waste(lot_id: UUID, waste_id: UUID, user: dict = Depends(RM)) -> None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT removed_at FROM operations.mixing_waste WHERE id = $1 AND mixing_lot_id = $2",
            waste_id, lot_id,
        )
        if not row:
            raise HTTPException(404, "Waste record not found for this lot")
        if row["removed_at"] is not None:
            return  # already removed — idempotent 204
        status = await conn.fetchval(
            "SELECT current_status FROM operations.mixing_lot_current_state WHERE mixing_lot_id = $1", lot_id
        )
        if status != "BUILDING":
            raise HTTPException(409, "Waste can only be removed while the lot is BUILDING")
        await conn.execute(
            "UPDATE operations.mixing_waste SET removed_at = now(), removed_by = $1 WHERE id = $2",
            user["id"], waste_id,
        )
