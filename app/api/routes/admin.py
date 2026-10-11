import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List, Literal, Optional, Union
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.routes.godown import BaleOut, MixingLotOut, _bale_out, _mixing_lot_out
from app.api.routes.packaging import DispatchEntryOut
from app.api.routes.supervisor import MACHINE_CODE_PREFIX, DoffOut, MachineIn, MachineOut, _create_machine
from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.security import hash_secret
from app.core.storage import presign_get

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


@router.get(
    "/users",
    response_model=List[UserOut],
    summary="List accounts — for spotting duplicates before creating, or finding who to deactivate",
)
async def list_users(
    role: Optional[Role] = None,
    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None,
    search: Optional[str] = Query(None, description="Matches employee_code or full_name (case-insensitive)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    admin: dict = Depends(ADMIN),
) -> List[dict]:
    conditions: list = []
    values: list = []
    if role:
        values.append(role)
        conditions.append(f"role = ${len(values)}")
    if status:
        values.append(status)
        conditions.append(f"status = ${len(values)}")
    if search:
        values.append(f"%{search}%")
        conditions.append(f"(employee_code ILIKE ${len(values)} OR full_name ILIKE ${len(values)})")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    values.append(limit)
    limit_idx = len(values)
    values.append(offset)
    offset_idx = len(values)

    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT id, employee_code, full_name, role, status FROM identity.users "
            f"{where} ORDER BY employee_code LIMIT ${limit_idx} OFFSET ${offset_idx}",
            *values,
        )
    return recs(rows)


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


class PalliOut(BaseModel):
    id: UUID
    party_id: UUID
    bobbin_colour_id: UUID
    display_name: str
    is_active: bool


class PalliIn(BaseModel):
    party_id: UUID
    bobbin_colour_id: UUID
    display_name: str

    model_config = {
        "json_schema_extra": {
            "example": {
                "party_id": "1a2b3c4d-5e6f-7890-abcd-ef1234567890",
                "bobbin_colour_id": "22222222-3333-4444-5555-666666666666",
                "display_name": "Vardhman - Red",
            }
        }
    }


@router.post(
    "/pallis",
    response_model=PalliOut,
    status_code=201,
    summary="Create a Palli (party + bobbin colour combo packaging packs bags against)",
    responses={409: {"description": "This party+colour combination already has a Palli"}},
)
async def create_palli(body: PalliIn, admin: dict = Depends(ADMIN)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO config.pallis (party_id, bobbin_colour_id, display_name)
            VALUES ($1,$2,$3)
            RETURNING id, party_id, bobbin_colour_id, display_name, is_active
            """,
            body.party_id, body.bobbin_colour_id, body.display_name,
        )
    return rec(row)


class StandardRecipeLineIn(BaseModel):
    mixing_category_id: UUID
    target_percentage: Decimal


class StandardRecipeLineOut(BaseModel):
    mixing_category_id: UUID
    category_name: str
    target_percentage: Decimal


class StandardRecipeIn(BaseModel):
    lines: List[StandardRecipeLineIn]

    model_config = {
        "json_schema_extra": {
            "example": {
                "lines": [
                    {"mixing_category_id": "cat-pc-uuid", "target_percentage": "44.00"},
                    {"mixing_category_id": "cat-break-uuid", "target_percentage": "33.00"},
                    {"mixing_category_id": "cat-sf-uuid", "target_percentage": "12.00"},
                    {"mixing_category_id": "cat-waste-uuid", "target_percentage": "11.00"},
                ]
            }
        }
    }


@router.put(
    "/mixing-recipe",
    response_model=List[StandardRecipeLineOut],
    summary="Set the standard mixing recipe — every new mixing lot uses this automatically; the "
            "Godown Manager can no longer set per-lot percentages",
    responses={400: {"description": "Percentages don't sum to 100, or reference an unknown/non-FIBRE category"}},
)
async def set_standard_recipe(body: StandardRecipeIn, admin: dict = Depends(ADMIN)) -> List[dict]:
    if not body.lines:
        raise HTTPException(400, "Recipe must have at least one line")
    category_ids = [line.mixing_category_id for line in body.lines]
    if len(set(category_ids)) != len(category_ids):
        raise HTTPException(400, "Duplicate mixing_category_id in recipe")
    total = sum(line.target_percentage for line in body.lines)
    if total != Decimal("100"):
        raise HTTPException(400, f"Percentages must sum to 100 (got {total})")

    async with get_pool().acquire() as conn, conn.transaction():
        found = {
            row["id"]: row["kind"]
            for row in await conn.fetch(
                "SELECT id, kind FROM config.mixing_categories WHERE id = ANY($1)", category_ids
            )
        }
        unknown = set(category_ids) - found.keys()
        if unknown:
            raise HTTPException(400, f"Unknown mixing_category_id: {', '.join(str(i) for i in unknown)}")
        non_fibre = [str(cid) for cid in category_ids if found[cid] != "FIBRE"]
        if non_fibre:
            raise HTTPException(400, f"Only FIBRE-kind categories belong in the recipe: {', '.join(non_fibre)}")

        await conn.execute("DELETE FROM config.standard_mixing_recipe")
        for line in body.lines:
            await conn.execute(
                "INSERT INTO config.standard_mixing_recipe (mixing_category_id, target_percentage, updated_by) "
                "VALUES ($1,$2,$3)",
                line.mixing_category_id, line.target_percentage, admin["id"],
            )
        rows = await conn.fetch(
            "SELECT r.mixing_category_id, mc.name AS category_name, r.target_percentage "
            "FROM config.standard_mixing_recipe r JOIN config.mixing_categories mc ON mc.id = r.mixing_category_id "
            "ORDER BY mc.sort_order"
        )
    return recs(rows)


@router.get(
    "/machines",
    response_model=List[MachineOut],
    summary="Every machine, including inactive/retired ones — filterable by process type",
)
async def list_machines(
    process_type: Optional[Literal["OE", "CARD", "DRAW_FRAME"]] = None, admin: dict = Depends(ADMIN)
) -> List[dict]:
    async with get_pool().acquire() as conn:
        if process_type:
            rows = await conn.fetch(
                "SELECT * FROM config.machines WHERE process_type = $1 ORDER BY display_no", process_type
            )
        else:
            rows = await conn.fetch("SELECT * FROM config.machines ORDER BY process_type, display_no")
    return recs(rows)


@router.post(
    "/machines",
    response_model=MachineOut,
    status_code=201,
    summary="Add a machine — same as a supervisor can do directly; here for bulk/initial setup",
    responses={400: {"description": "rotor_count given for a non-OE machine, or display_no already in use"}},
)
async def create_machine(body: MachineIn, admin: dict = Depends(ADMIN)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _create_machine(conn, body.process_type, body.display_no, body.rotor_count)
    return rec(row)


class MachineUpdateIn(BaseModel):
    display_no: Optional[int] = None
    rotor_count: Optional[int] = None
    is_active: Optional[bool] = None


@router.patch(
    "/machines/{machine_id}",
    response_model=MachineOut,
    summary="Change a machine's number or rotor count, or deactivate/reactivate it — never deleted",
    responses={
        400: {"description": "Nothing to update, rotor_count given for a non-OE machine, or display_no in use"},
        404: {"description": "Machine not found"},
    },
)
async def update_machine(machine_id: UUID, body: MachineUpdateIn, admin: dict = Depends(ADMIN)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        machine = await conn.fetchrow("SELECT * FROM config.machines WHERE id = $1", machine_id)
        if not machine:
            raise HTTPException(404, "Machine not found")

        fields: list = []
        values: list = []
        new_display_no = body.display_no if body.display_no is not None else machine["display_no"]

        if body.rotor_count is not None and machine["process_type"] != "OE":
            raise HTTPException(400, "rotor_count only applies to OE machines")

        if body.display_no is not None and body.display_no != machine["display_no"]:
            clash = await conn.fetchval(
                "SELECT 1 FROM config.machines WHERE process_type = $1 AND display_no = $2 AND id != $3",
                machine["process_type"], body.display_no, machine_id,
            )
            if clash:
                raise HTTPException(400, f"{machine['process_type']} machine #{body.display_no} already exists")
            values.append(body.display_no)
            fields.append(f"display_no=${len(values)}")
            # Keep machine_code in sync with the number it encodes.
            values.append(f"{MACHINE_CODE_PREFIX[machine['process_type']]}-{str(new_display_no).zfill(2)}")
            fields.append(f"machine_code=${len(values)}")
        if body.rotor_count is not None:
            values.append(body.rotor_count)
            fields.append(f"rotor_count=${len(values)}")
        if body.is_active is not None:
            values.append(body.is_active)
            fields.append(f"is_active=${len(values)}")
        if not fields:
            raise HTTPException(400, "Nothing to update")

        values.append(machine_id)
        row = await conn.fetchrow(
            f"UPDATE config.machines SET {', '.join(fields)} WHERE id = ${len(values)} RETURNING *",
            *values,
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


class StockSummaryOut(BaseModel):
    as_of_date: Optional[date]  # levels are a snapshot as of the end of this day (null = right now)
    start_date: Optional[date]
    end_date: Optional[date]
    raw_material_received_kg: Decimal
    total_inventory_kg: Decimal
    total_wip_raw_material_kg: Decimal
    total_wip_own_waste_kg: Decimal
    total_wip_kg: Decimal
    total_production_kg: Decimal
    packaging_inventory_kg: Decimal
    dispatched_kg: Decimal


@router.get(
    "/analytics/stock-summary",
    response_model=StockSummaryOut,
    summary="The governing numbers across the whole mill, as a snapshot at any moment — "
            "raw material received, still in godown, tied up in mixing (WIP), spun output, "
            "packaging inventory, and dispatched — so received ≈ godown + WIP + packaging "
            "+ dispatched (+ waste) holds at any as_of_date, not just today. Call this twice "
            "with two different as_of_date values to see material move between stages.",
)
async def analytics_stock_summary(
    as_of_date: Optional[date] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    admin: dict = Depends(ADMIN),
) -> dict:
    # Nothing here is a bespoke calculation — every figure is read straight off the same
    # append-only ledgers every operational screen already writes to (raw_material_ledger,
    # mixing_bale_scans/mixing_waste/mixing_scan_reversals/mixing_lot_status_events,
    # doff_records, production_stock_ledger). Because those ledgers are append-only, "the
    # state as of any past moment" is just the same SUM filtered to occurred_at <= that
    # moment — not a different computation. as_of_date governs the LEVELS (inventory, WIP,
    # packaging inventory) — pass two different as_of_date values on two calls to see
    # material move from one stage to the next while the total holds (minus waste). This
    # is independent of start_date/end_date, which scope the FLOW figures (received,
    # production output, dispatched) the same way as before.
    async with get_pool().acquire() as conn:
        # Levels are "< the day after as_of_date" when given, else unfiltered (now).
        as_of_cutoff = as_of_date + timedelta(days=1) if as_of_date else None
        as_of_params = [as_of_cutoff] if as_of_cutoff else []
        as_of_clause = "occurred_at < $1" if as_of_cutoff else "true"

        flow_conditions = []
        flow_params: list = []
        if start_date:
            flow_params.append(start_date)
            flow_conditions.append(f"occurred_at >= ${len(flow_params)}")
        if end_date:
            flow_params.append(end_date)
            flow_conditions.append(f"occurred_at::date <= ${len(flow_params)}")
        flow_where = f"AND {' AND '.join(flow_conditions)}" if flow_conditions else ""

        total_inventory = await conn.fetchval(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (bale_id) bale_id, tx_type
                FROM operations.raw_material_ledger
                WHERE {as_of_clause}
                ORDER BY bale_id, occurred_at DESC, created_at DESC
            )
            SELECT COALESCE(SUM(b.net_weight_kg), 0)
            FROM latest l JOIN operations.bales b ON b.id = l.bale_id
            WHERE l.tx_type = 'RECEIVED'
            """,
            *as_of_params,
        )
        lot_status_cte = f"""
            lot_status AS (
                SELECT DISTINCT ON (mixing_lot_id) mixing_lot_id, status
                FROM operations.mixing_lot_status_events
                WHERE {as_of_clause}
                ORDER BY mixing_lot_id, occurred_at DESC, created_at DESC
            )
        """
        total_wip_raw_material = await conn.fetchval(
            f"""
            WITH {lot_status_cte}
            SELECT COALESCE(SUM(s.net_weight_kg), 0)
            FROM operations.mixing_bale_scans s
            JOIN lot_status ls ON ls.mixing_lot_id = s.mixing_lot_id
            WHERE ls.status IN ('BUILDING', 'ACTIVE')
              AND {as_of_clause.replace('occurred_at', 's.occurred_at')}
              AND NOT EXISTS (
                  SELECT 1 FROM operations.mixing_scan_reversals r
                  WHERE r.scan_id = s.id AND {as_of_clause.replace('occurred_at', 'r.reversed_at')}
              )
            """,
            *as_of_params,
        )
        total_wip_own_waste = await conn.fetchval(
            f"""
            WITH {lot_status_cte}
            SELECT COALESCE(SUM(w.weight_kg), 0)
            FROM operations.mixing_waste w
            JOIN lot_status ls ON ls.mixing_lot_id = w.mixing_lot_id
            WHERE ls.status IN ('BUILDING', 'ACTIVE')
              AND {as_of_clause.replace('occurred_at', 'w.created_at')}
              AND (w.removed_at IS NULL OR NOT {as_of_clause.replace('occurred_at', 'w.removed_at')})
            """,
            *as_of_params,
        )
        packaging_inventory = await conn.fetchval(
            f"SELECT COALESCE(SUM(quantity_kg), 0) FROM operations.production_stock_ledger WHERE {as_of_clause}",
            *as_of_params,
        )
        raw_material_received = await conn.fetchval(
            f"SELECT COALESCE(SUM(quantity_kg), 0) FROM operations.raw_material_ledger "
            f"WHERE tx_type = 'RECEIVED' {flow_where}",
            *flow_params,
        )
        total_production = await conn.fetchval(
            f"SELECT COALESCE(SUM(loose_stock_kg), 0) FROM operations.doff_records WHERE true {flow_where}",
            *flow_params,
        )
        dispatched = await conn.fetchval(
            f"SELECT COALESCE(-SUM(quantity_kg), 0) FROM operations.production_stock_ledger "
            f"WHERE tx_type = 'DISPATCH' {flow_where}",
            *flow_params,
        )
    return {
        "as_of_date": as_of_date,
        "start_date": start_date,
        "end_date": end_date,
        "raw_material_received_kg": raw_material_received,
        "total_inventory_kg": total_inventory,
        "total_wip_raw_material_kg": total_wip_raw_material,
        "total_wip_own_waste_kg": total_wip_own_waste,
        "total_wip_kg": total_wip_raw_material + total_wip_own_waste,
        "total_production_kg": total_production,
        "packaging_inventory_kg": packaging_inventory,
        "dispatched_kg": dispatched,
    }


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


class VoidInfoOut(BaseModel):
    voided_at: datetime
    voided_by: UUID
    reason: Optional[str]


class BaleTraceOut(BaseModel):
    kind: Literal["bale"] = "bale"
    bale: BaleOut
    issued_to_lot: Optional[BaleLotLinkOut]
    voided: Optional[VoidInfoOut]
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
    # Dropping mixing_bale_scans' old UNIQUE(bale_id) (to allow re-scanning after an
    # unscan) means a bale can now have more than one historical scan row — only the
    # one with no reversal is the bale's current lot, if any.
    scan = await conn.fetchrow(
        "SELECT s.*, ml.lot_code FROM operations.mixing_bale_scans s "
        "JOIN operations.mixing_lots ml ON ml.id = s.mixing_lot_id "
        "WHERE s.bale_id = $1 AND NOT EXISTS ("
        "    SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = s.id"
        ") ORDER BY s.occurred_at DESC LIMIT 1",
        bale_id,
    )
    # Voiding never touches the (append-only) bale row — it's visible only as the
    # bale's latest ledger entry becoming ADJUSTED_OUT. Surface that here so a voided
    # bale's history stays traceable even though it's excluded from stock everywhere else.
    void_entry = await conn.fetchrow(
        "SELECT occurred_at, recorded_by, note FROM operations.raw_material_ledger "
        "WHERE bale_id = $1 AND tx_type = 'ADJUSTED_OUT' ORDER BY occurred_at DESC LIMIT 1",
        bale_id,
    )
    voided = None
    if void_entry:
        voided = {
            "voided_at": void_entry["occurred_at"],
            "voided_by": void_entry["recorded_by"],
            "reason": void_entry["note"],
        }
    return {
        "kind": "bale",
        "bale": bale,
        "issued_to_lot": rec(scan),
        "voided": voided,
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
        "WHERE s.mixing_lot_id = $1 AND NOT EXISTS ("
        "    SELECT 1 FROM operations.mixing_scan_reversals r WHERE r.scan_id = s.id"
        ")",
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


class SheetReportOut(BaseModel):
    id: UUID
    report_type: Literal["PRODUCTION", "MIXING", "PACKAGING"]
    report_date: date
    shift_code: Optional[Literal["SHIFT_A", "SHIFT_B"]]
    reference_id: UUID
    file_url: str  # always a ready-to-open signed URL, never the bare private S3 url
    generated_by: UUID
    generated_at: datetime


@router.get(
    "/reports",
    response_model=List[SheetReportOut],
    summary="Browse frozen Production/Mixing/Packaging sheet snapshots, newest first",
)
async def list_reports(
    report_type: Optional[Literal["PRODUCTION", "MIXING", "PACKAGING"]] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    admin: dict = Depends(ADMIN),
) -> List[dict]:
    conditions = ["1=1"]
    values: list = []
    if report_type:
        values.append(report_type)
        conditions.append(f"report_type = ${len(values)}")
    if start_date:
        values.append(start_date)
        conditions.append(f"report_date >= ${len(values)}")
    if end_date:
        values.append(end_date)
        conditions.append(f"report_date <= ${len(values)}")
    where = " AND ".join(conditions)
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM operations.sheet_reports WHERE {where} "
            f"ORDER BY report_date DESC, generated_at DESC",
            *values,
        )
    out = recs(rows)
    for r in out:
        r["file_url"] = presign_get(r["file_url"])
    return out
