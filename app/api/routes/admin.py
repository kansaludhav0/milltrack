import json
from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.routes.godown import BaleOut, MixingLotOut, _bale_out, _mixing_lot_out
from app.api.routes.packaging import DispatchEntryOut
from app.api.routes.supervisor import DoffOut
from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.security import hash_secret

router = APIRouter(prefix="/admin", tags=["admin"])
ADMIN = require_role("GM_MD_ADMIN")

Role = Literal["GATEMAN", "GODOWN_MANAGER_RM", "SUPERVISOR", "GODOWN_MANAGER_PKG", "GM_MD_ADMIN"]


class UserOut(BaseModel):
    # Deliberately narrow — never includes secret_hash, token_version, or anything
    # else that isn't safe to hand back over the wire, on create AND on update.
    id: UUID
    employee_code: str
    full_name: str
    role: Role
    status: Literal["ACTIVE", "INACTIVE"]


class UserIn(BaseModel):
    employee_code: str
    full_name: str
    role: Role
    secret: str  # PIN for shop-floor roles, password for GM_MD_ADMIN
    email: Optional[str] = None  # required in practice for GM_MD_ADMIN, who logs in by email
    phone: Optional[str] = None
    gate_id: Optional[UUID] = None
    godown_type_scope: Optional[Literal["RAW_MATERIAL", "FINISHED_GOODS"]] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "employee_code": "GATE001",
                "full_name": "Ramesh Kumar",
                "role": "GATEMAN",
                "secret": "4821",
            }
        }
    }


@router.post(
    "/users",
    response_model=UserOut,
    status_code=201,
    summary="Create any role's account — the only way an account is ever created",
    responses={409: {"description": "employee_code or email already in use"}},
)
async def create_user(body: UserIn, admin: dict = Depends(ADMIN)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        user = await conn.fetchrow(
            """
            INSERT INTO identity.users
                (employee_code, full_name, role, email, phone, gate_id, godown_type_scope,
                 secret_hash, created_by_admin_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            RETURNING id, employee_code, full_name, role, status
            """,
            body.employee_code, body.full_name, body.role, body.email, body.phone,
            body.gate_id, body.godown_type_scope, hash_secret(body.secret), admin["id"],
        )
        await conn.execute(
            "INSERT INTO identity.user_change_log (user_id, changed_by, change_type, after_value) "
            "VALUES ($1,$2,'CREATED',$3)",
            user["id"], admin["id"], json.dumps({"role": body.role, "employee_code": body.employee_code}),
        )
    return rec(user)


class UserUpdateIn(BaseModel):
    role: Optional[Role] = None
    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None
    secret: Optional[str] = None


@router.patch(
    "/users/{user_id}",
    response_model=UserOut,
    summary="Change role, active status, or reset the PIN/password",
    responses={400: {"description": "No fields supplied to update"}},
)
async def update_user(user_id: UUID, body: UserUpdateIn, admin: dict = Depends(ADMIN)) -> Optional[dict]:
    values: list = []
    fields: list = []
    if body.role is not None:
        values.append(body.role)
        fields.append(f"role=${len(values)}")
    if body.status is not None:
        values.append(body.status)
        fields.append(f"status=${len(values)}")
    if body.secret is not None:
        values.append(hash_secret(body.secret))
        fields.append(f"secret_hash=${len(values)}")
    if not fields:
        raise HTTPException(400, "Nothing to update")
    values.append(user_id)

    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"UPDATE identity.users SET {', '.join(fields)}, updated_at = now() "
            f"WHERE id = ${len(values)} RETURNING id, employee_code, full_name, role, status",
            *values,
        )
        await conn.execute(
            "INSERT INTO identity.user_change_log (user_id, changed_by, change_type, after_value) "
            "VALUES ($1,$2,'UPDATED',$3)",
            user_id, admin["id"],
            json.dumps({k: v for k, v in body.model_dump().items() if v is not None and k != "secret"}),
        )
    return rec(row)


@router.post(
    "/users/{user_id}/revoke",
    status_code=204,
    summary="Invalidate every token already issued to this user, everywhere, immediately",
)
async def revoke_user(user_id: UUID, admin: dict = Depends(ADMIN)) -> None:
    # Bumping token_version invalidates every JWT already issued to this user, everywhere —
    # the equivalent of Cognito's AdminUserGlobalSignOut, now that we own auth ourselves.
    async with get_pool().acquire() as conn:
        await conn.execute("UPDATE identity.users SET token_version = token_version + 1 WHERE id = $1", user_id)
        await conn.execute(
            "INSERT INTO identity.user_change_log (user_id, changed_by, change_type) VALUES ($1,$2,'REVOKED')",
            user_id, admin["id"],
        )


class InventoryRowOut(BaseModel):
    godown_location_id: UUID
    location_name: str
    material_variety_id: UUID
    variety_name: str
    quality_grade: Literal["A", "B", "C"]
    bale_count: int
    total_weight_kg: Decimal


@router.get(
    "/analytics/inventory",
    response_model=List[InventoryRowOut],
    summary="Raw-material godown stock (materialized view — a few minutes stale, by design)",
)
async def analytics_inventory(admin: dict = Depends(ADMIN)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM analytics.mv_raw_material_godown_stock")
    return recs(rows)


class ProductionSummaryRowOut(BaseModel):
    process_type: Literal["OE", "CARD", "DRAW_FRAME"]
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]
    machine_id: UUID
    total_production_kg: Decimal
    avg_efficiency_pct: Decimal
    total_breakage: int


@router.get(
    "/analytics/production-summary",
    response_model=List[ProductionSummaryRowOut],
    summary="Production/efficiency/breakage by machine, shift, and day",
)
async def analytics_production(admin: dict = Depends(ADMIN)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM analytics.mv_production_summary_daily ORDER BY shift_date DESC")
    return recs(rows)


class ReconciliationRowOut(BaseModel):
    as_of_date: date
    raw_material_received_kg: Decimal
    issued_to_blow_room_kg: Decimal
    finished_and_packed_kg: Decimal
    dispatched_kg: Decimal


@router.get(
    "/analytics/reconciliation",
    response_model=List[ReconciliationRowOut],
    summary="Received vs. issued vs. packed vs. dispatched, per day",
)
async def analytics_reconciliation(admin: dict = Depends(ADMIN)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM analytics.mv_stock_reconciliation_daily ORDER BY as_of_date DESC")
    return recs(rows)


# ---- Traceability search — reuses the exact same models (and, where possible, the
# exact same query helpers) as each entity's own domain endpoint, so e.g. a bale here
# looks identical to a bale returned from GET /godown/bales/{id}. ----

class BaleLotLinkOut(BaseModel):
    id: UUID
    mixing_lot_id: UUID
    bale_id: UUID
    mixing_category_id: UUID
    net_weight_kg: Decimal
    scanned_by: UUID
    occurred_at: datetime
    created_at: datetime
    lot_code: str


class BaleTraceOut(BaseModel):
    kind: Literal["bale"] = "bale"
    bale: BaleOut
    issued_to_lot: Optional[BaleLotLinkOut]
    note: str


class LotBaleSummaryOut(BaseModel):
    bale_code: str
    party_name: str


class LotDoffSummaryOut(BaseModel):
    doff_code: str
    occurred_at: datetime


class LotTraceOut(BaseModel):
    kind: Literal["mixing_lot"] = "mixing_lot"
    lot: MixingLotOut
    bales_in: List[LotBaleSummaryOut]
    doffs_out: List[LotDoffSummaryOut]


class DoffTraceOut(BaseModel):
    kind: Literal["doff"] = "doff"
    doff: DoffOut
    note: str


class DispatchLineSummaryOut(BaseModel):
    id: UUID
    bag_count: int
    bag_codes: List[str]


class DispatchTraceOut(BaseModel):
    kind: Literal["dispatch"] = "dispatch"
    dispatch: DispatchEntryOut
    lines: List[DispatchLineSummaryOut]


@router.get(
    "/trace/{code}",
    response_model=Union[BaleTraceOut, LotTraceOut, DoffTraceOut, DispatchTraceOut],
    summary="Forward/backward chain of custody for a Bale/Lot/DOFF/Dispatch code",
    responses={404: {"description": "No entity with this code exists"}, 400: {"description": "Unrecognized ID format"}},
)
async def trace(code: str, admin: dict = Depends(ADMIN)) -> dict:
    async with get_pool().acquire() as conn:
        if code.startswith("BL-"):
            return await _trace_bale(conn, code)
        if code.startswith("MX-"):
            return await _trace_lot(conn, code)
        if code.startswith("DOFF-"):
            return await _trace_doff(conn, code)
        if code.startswith("DSP-"):
            return await _trace_dispatch(conn, code)
    raise HTTPException(400, "Unrecognized ID format — expected a BL-/MX-/DOFF-/DSP- code")


async def _trace_bale(conn, code: str) -> dict:
    bale_id = await conn.fetchval("SELECT id FROM operations.bales WHERE bale_code = $1", code)
    if not bale_id:
        raise HTTPException(404, "Bale not found")
    bale = await _bale_out(conn, bale_id)
    scan = await conn.fetchrow(
        "SELECT s.*, ml.lot_code FROM operations.mixing_bale_scans s "
        "JOIN operations.mixing_lots ml ON ml.id = s.mixing_lot_id WHERE s.bale_id = $1",
        bale_id,
    )
    return {
        "kind": "bale",
        "bale": bale,
        "issued_to_lot": rec(scan),
        "note": "Once mixed, fibre from many bales blends together; traceability beyond "
                "this point resolves to the lot, not this specific bale (design doc §7).",
    }


async def _trace_lot(conn, code: str) -> dict:
    lot_id = await conn.fetchval("SELECT id FROM operations.mixing_lots WHERE lot_code = $1", code)
    if not lot_id:
        raise HTTPException(404, "Mixing lot not found")
    lot = await _mixing_lot_out(conn, lot_id)
    bales = await conn.fetch(
        "SELECT b.bale_code, p.name AS party_name FROM operations.mixing_bale_scans s "
        "JOIN operations.bales b ON b.id = s.bale_id JOIN config.parties p ON p.id = b.party_id "
        "WHERE s.mixing_lot_id = $1",
        lot_id,
    )
    doffs = await conn.fetch(
        "SELECT doff_code, occurred_at FROM operations.doff_records WHERE mixing_lot_id = $1", lot_id
    )
    return {"kind": "mixing_lot", "lot": lot, "bales_in": recs(bales), "doffs_out": recs(doffs)}


async def _trace_doff(conn, code: str) -> dict:
    doff = await conn.fetchrow("SELECT * FROM operations.doff_records WHERE doff_code = $1", code)
    if not doff:
        raise HTTPException(404, "DOFF not found")
    return {
        "kind": "doff",
        "doff": rec(doff),
        "note": "Loose stock is a fungible pool by count/cone/colour, not a per-DOFF lineage "
                "— any bag packed from that pool afterward may include this DOFF's output.",
    }


async def _trace_dispatch(conn, code: str) -> dict:
    dispatch = await conn.fetchrow("SELECT * FROM operations.dispatch_entries WHERE dispatch_code = $1", code)
    if not dispatch:
        raise HTTPException(404, "Dispatch not found")
    lines = await conn.fetch(
        """
        SELECT dl.id, dl.bag_count, array_agg(pr.bag_code) AS bag_codes
        FROM operations.dispatch_lines dl
        JOIN operations.dispatch_line_bags b ON b.dispatch_line_id = dl.id
        JOIN operations.packaging_records pr ON pr.id = b.packaging_record_id
        WHERE dl.dispatch_entry_id = $1
        GROUP BY dl.id, dl.bag_count
        """,
        dispatch["id"],
    )
    return {"kind": "dispatch", "dispatch": rec(dispatch), "lines": recs(lines)}
