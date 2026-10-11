import logging
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes.gate import VehicleStatusOut, _with_signed_photos
from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.sheets import render_packaging_sheet, store_sheet_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/packaging", tags=["packaging"])
PKG = require_role("GODOWN_MANAGER_PKG")
# Read-only visibility for GM/MD — see gate.py's GATE_READ for the reasoning. Never used
# on writes; only packaging can pack a bag, open a dispatch, or assign a bag to it.
PKG_READ = require_role("GODOWN_MANAGER_PKG", "GM_MD_ADMIN")


class LooseStockRowOut(BaseModel):
    count_id: UUID
    cone_type_id: UUID
    bobbin_colour_id: UUID
    available_kg: Decimal


@router.get("/loose-stock", response_model=List[LooseStockRowOut], summary="Live loose stock by count/cone/colour")
async def loose_stock(user: dict = Depends(PKG_READ)) -> List[dict]:
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


class PaperLogIn(BaseModel):
    log_date: date
    count_id: UUID
    palli_id: UUID
    cone_type_id: UUID
    bobbin_colour_id: UUID  # independent of the Palli's own colour, same as on a packed bag
    loose_cones: int = Field(ge=0)
    packed_cones: int = Field(ge=0)
    dispatched_cones: int = Field(ge=0)
    closing_cones: int = Field(ge=0)


class PaperLogOut(BaseModel):
    id: UUID
    log_date: date
    count_id: UUID
    palli_id: UUID
    cone_type_id: UUID
    bobbin_colour_id: UUID
    cone_bundle_count: int  # "Cone" column on the paper sheet
    cone_package_weight_kg: Decimal  # "Kg" column
    loose_cones: int
    packed_cones: int
    dispatched_cones: int
    closing_cones: int
    # Derived, not stored — kg per cone comes from this cone type's own bundle spec
    # (net_weight_kg / bundle_count), so these are only as precise as that average.
    # The packaging manager always enters whole cones; kg is for comparing against
    # the computed (weighed) reconciliation report, never the other way round.
    kg_per_cone: Decimal
    loose_kg_equivalent: Decimal
    packed_kg_equivalent: Decimal
    dispatched_kg_equivalent: Decimal
    closing_kg_equivalent: Decimal
    recorded_by: UUID
    created_at: datetime
    updated_at: datetime


@router.put(
    "/paper-log",
    response_model=PaperLogOut,
    summary="Record what the paper register says for one day/count/client+colour/cone type, "
            "in whole cones — for comparison only, never overrides the computed reconciliation",
)
async def upsert_paper_log(body: PaperLogIn, user: dict = Depends(PKG)) -> Optional[dict]:
    # Plain upsert, not an append-only correction chain — this is a transparency/
    # comparison tool during the paper-to-digital transition, not the system of record.
    # Retyping the same day's entry to fix a mistake is expected and simply overwrites.
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO operations.packaging_paper_log
                (log_date, count_id, palli_id, cone_type_id, bobbin_colour_id, loose_cones,
                 packed_cones, dispatched_cones, closing_cones, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (log_date, count_id, palli_id, cone_type_id, bobbin_colour_id) DO UPDATE SET
                loose_cones = EXCLUDED.loose_cones,
                packed_cones = EXCLUDED.packed_cones,
                dispatched_cones = EXCLUDED.dispatched_cones,
                closing_cones = EXCLUDED.closing_cones,
                recorded_by = EXCLUDED.recorded_by,
                updated_at = now()
            RETURNING *
            """,
            body.log_date, body.count_id, body.palli_id, body.cone_type_id, body.bobbin_colour_id,
            body.loose_cones, body.packed_cones, body.dispatched_cones, body.closing_cones,
            user["id"],
        )
        return await _paper_log_out(conn, row)


@router.get(
    "/paper-log",
    response_model=List[PaperLogOut],
    summary="The paper register's entries for one day, in cones (plus a derived kg equivalent)",
)
async def get_paper_log(log_date: date = Query(default_factory=date.today), user: dict = Depends(PKG_READ)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM operations.packaging_paper_log WHERE log_date = $1", log_date
        )
        return [await _paper_log_out(conn, row) for row in rows]


async def _paper_log_out(conn, row) -> dict:
    cone_type = await conn.fetchrow(
        "SELECT net_weight_kg, bundle_count FROM config.cone_types WHERE id = $1", row["cone_type_id"]
    )
    # net_weight_kg is the weight of ONE cone, not the package — the package total is
    # bundle_count * net_weight_kg (e.g. 24 cones x 2.1kg = 50.4kg).
    kg_per_cone = cone_type["net_weight_kg"]
    data = dict(row)
    data["cone_bundle_count"] = cone_type["bundle_count"]
    data["cone_package_weight_kg"] = cone_type["bundle_count"] * cone_type["net_weight_kg"]
    data["kg_per_cone"] = kg_per_cone
    data["loose_kg_equivalent"] = row["loose_cones"] * kg_per_cone
    data["packed_kg_equivalent"] = row["packed_cones"] * kg_per_cone
    data["dispatched_kg_equivalent"] = row["dispatched_cones"] * kg_per_cone
    data["closing_kg_equivalent"] = row["closing_cones"] * kg_per_cone
    return data


async def _paper_log_rows_for_sheet(conn, log_date: date) -> List[dict]:
    """Display-name-resolved paper log rows for the frozen Packaging Sheet PDF — the
    API's own PaperLogOut leaves name resolution to the frontend (via /config/lookups),
    but a server-rendered PDF has no frontend to do that, so this resolves it directly."""
    rows = await conn.fetch(
        """
        SELECT pl.loose_cones, pl.packed_cones, pl.dispatched_cones, pl.closing_cones,
               p.display_name AS palli, bc.name AS colour, yc.label AS count,
               ct.bundle_count, ct.net_weight_kg
        FROM operations.packaging_paper_log pl
        JOIN config.pallis p ON p.id = pl.palli_id
        JOIN config.bobbin_colours bc ON bc.id = pl.bobbin_colour_id
        JOIN config.yarn_counts yc ON yc.id = pl.count_id
        JOIN config.cone_types ct ON ct.id = pl.cone_type_id
        WHERE pl.log_date = $1
        ORDER BY p.display_name
        """,
        log_date,
    )
    out = []
    for r in rows:
        kg_per_cone = r["net_weight_kg"]
        out.append({
            "palli": r["palli"],
            "colour": r["colour"],
            "count": r["count"],
            "cone_type": f"{r['bundle_count']} x {kg_per_cone:.2f}kg",
            "loose_display": f"{r['loose_cones']} / {r['loose_cones'] * kg_per_cone:.2f}kg",
            "packed_display": f"{r['packed_cones']} / {r['packed_cones'] * kg_per_cone:.2f}kg",
            "dispatched_display": f"{r['dispatched_cones']} / {r['dispatched_cones'] * kg_per_cone:.2f}kg",
            "closing_display": f"{r['closing_cones']} / {r['closing_cones'] * kg_per_cone:.2f}kg",
        })
    return out


class FinalizeDayIn(BaseModel):
    finalized_date: date


class FinalizeDayOut(BaseModel):
    id: UUID
    finalized_date: date
    finalized_by: UUID
    finalized_at: datetime


@router.post(
    "/finalize-day",
    response_model=FinalizeDayOut,
    status_code=201,
    summary="Freeze that day's paper-log sheet for Admin — Packaging's equivalent of a "
            "shift lock or a mixing-lot completion",
)
async def finalize_day(body: FinalizeDayIn, user: dict = Depends(PKG)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        # DO NOTHING, not DO UPDATE — append-only, and finalized_date is already unique,
        # so calling this twice for the same day is a safe no-op (same pattern as every
        # other idempotent write this session).
        row = await conn.fetchrow(
            "INSERT INTO operations.packaging_day_finalizations (finalized_date, finalized_by) "
            "VALUES ($1,$2) ON CONFLICT (finalized_date) DO NOTHING RETURNING *",
            body.finalized_date, user["id"],
        )
        if row is None:
            row = await conn.fetchrow(
                "SELECT * FROM operations.packaging_day_finalizations WHERE finalized_date = $1",
                body.finalized_date,
            )

        # Freeze today's Packaging Sheet. A PDF/S3 hiccup here must never fail the
        # finalization itself — it already succeeded and is what matters operationally.
        try:
            sheet_rows = await _paper_log_rows_for_sheet(conn, body.finalized_date)
            pdf_bytes = render_packaging_sheet(body.finalized_date, sheet_rows)
            await store_sheet_report(
                conn, "PACKAGING", body.finalized_date, row["id"], None,
                pdf_bytes, user["id"], f"packaging-{body.finalized_date}.pdf",
            )
        except Exception:
            logger.exception("Failed to generate Packaging Sheet for %s", body.finalized_date)
    return rec(row)


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
        # DO NOTHING, not DO UPDATE — packaging_records is append-only; a retry finds
        # nothing to insert, so fall back to fetching the row already there. The
        # cone-weights loop below is safe to re-run regardless (it has its own
        # per-row DO NOTHING), so no need to skip it on a detected retry.
        row = await conn.fetchrow(
            """
            INSERT INTO operations.packaging_records
                (id, count_id, palli_id, bobbin_colour_id, cone_type_id, net_weight_kg, packed_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            body.id, body.count_id, body.palli_id, body.bobbin_colour_id,
            body.cone_type_id, body.net_weight_kg, user["id"],
        )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM operations.packaging_records WHERE id = $1", body.id)
        for i, w in enumerate(body.cone_weights, start=1):
            await conn.execute(
                "INSERT INTO operations.packaging_record_cones (packaging_record_id, sequence_no, actual_weight_kg) "
                "VALUES ($1,$2,$3) ON CONFLICT (packaging_record_id, sequence_no) DO NOTHING",
                row["id"], i, w,
            )
    return rec(row)


@router.get("/records", response_model=List[PackagingRecordOut], summary="Bags packed today")
async def packed_today(user: dict = Depends(PKG_READ)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM operations.packaging_records WHERE packed_at::date = current_date ORDER BY packed_at DESC"
        )
    return recs(rows)


@router.get(
    "/records/available",
    response_model=List[PackagingRecordOut],
    summary="Packed bags not yet assigned to any dispatch, from any day — for the dispatch bag picker",
)
async def available_records(
    count_id: Optional[UUID] = None,
    palli_id: Optional[UUID] = None,
    cone_type_id: Optional[UUID] = None,
    user: dict = Depends(PKG_READ),
) -> List[dict]:
    conditions = [
        "NOT EXISTS (SELECT 1 FROM operations.dispatch_line_bags b WHERE b.packaging_record_id = pr.id)"
    ]
    values: list = []
    if count_id:
        values.append(count_id)
        conditions.append(f"pr.count_id = ${len(values)}")
    if palli_id:
        values.append(palli_id)
        conditions.append(f"pr.palli_id = ${len(values)}")
    if cone_type_id:
        values.append(cone_type_id)
        conditions.append(f"pr.cone_type_id = ${len(values)}")
    where = " AND ".join(conditions)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT pr.* FROM operations.packaging_records pr WHERE {where} ORDER BY pr.packed_at", *values
        )
    return recs(rows)


@router.get(
    "/loading-vehicles",
    response_model=List[VehicleStatusOut],
    summary="Vehicles available to dispatch against (LOADING-purpose only, still on premises)",
)
async def loading_vehicles(user: dict = Depends(PKG_READ)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT v.*, e.photo_url FROM operations.vw_gate_vehicle_current_status v "
            "JOIN operations.gate_vehicle_entries e ON e.id = v.gate_entry_id "
            "WHERE v.purpose = 'LOADING' AND v.current_status = 'ON_PREMISES' ORDER BY v.entered_at"
        )
    return _with_signed_photos(rows)


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
        # DO NOTHING, not DO UPDATE — dispatch_entries is append-only; a retry finds
        # nothing to insert, so fall back to fetching the row already there.
        row = await conn.fetchrow(
            """
            INSERT INTO operations.dispatch_entries (id, gate_entry_id, party_id, generated_by)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            body.id, body.gate_entry_id, body.party_id, user["id"],
        )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM operations.dispatch_entries WHERE id = $1", body.id)
    return rec(row)


@router.get(
    "/dispatch-entries",
    response_model=List[DispatchEntryOut],
    summary="Dispatches opened on a given day (defaults to today) — to resume an in-progress load",
)
async def list_dispatch_entries(
    dispatch_date: date = Query(default_factory=date.today), user: dict = Depends(PKG_READ)
) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM operations.dispatch_entries WHERE generated_at::date = $1 ORDER BY generated_at DESC",
            dispatch_date,
        )
    return recs(rows)


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


class DispatchLineBagOut(BaseModel):
    id: UUID
    dispatch_line_id: UUID
    packaging_record_id: UUID
    bag_code: str
    net_weight_kg: Decimal
    created_at: datetime


class DispatchDetailOut(BaseModel):
    dispatch: DispatchEntryOut
    lines: List[DispatchLineOut]
    bags: List[DispatchLineBagOut]


@router.get(
    "/dispatch-entries/{dispatch_id}",
    response_model=DispatchDetailOut,
    summary="A dispatch's full state — every line and every bag assigned to it so far, "
            "so progress survives a device switch instead of living only in local app state",
    responses={404: {"description": "Dispatch not found"}},
)
async def dispatch_detail(dispatch_id: UUID, user: dict = Depends(PKG_READ)) -> dict:
    async with get_pool().acquire() as conn:
        dispatch = await conn.fetchrow("SELECT * FROM operations.dispatch_entries WHERE id = $1", dispatch_id)
        if not dispatch:
            raise HTTPException(404, "Dispatch not found")
        lines = await conn.fetch(
            "SELECT * FROM operations.dispatch_lines WHERE dispatch_entry_id = $1 ORDER BY created_at",
            dispatch_id,
        )
        bags = await conn.fetch(
            """
            SELECT b.id, b.dispatch_line_id, b.packaging_record_id, pr.bag_code, pr.net_weight_kg, b.created_at
            FROM operations.dispatch_line_bags b
            JOIN operations.dispatch_lines dl ON dl.id = b.dispatch_line_id
            JOIN operations.packaging_records pr ON pr.id = b.packaging_record_id
            WHERE dl.dispatch_entry_id = $1
            ORDER BY b.created_at
            """,
            dispatch_id,
        )
    return {"dispatch": rec(dispatch), "lines": recs(lines), "bags": recs(bags)}
