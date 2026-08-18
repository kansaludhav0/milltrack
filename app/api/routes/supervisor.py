from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.core.db import get_pool, rec, recs
from app.core.deps import require_role

router = APIRouter(prefix="/supervisor", tags=["supervisor"])
SUP = require_role("SUPERVISOR")

# Whitelisted table/column names per process type — the only things ever interpolated
# into SQL below are keys drawn from these fixed dicts, never raw request input, so
# building the INSERT dynamically doesn't open an injection path.
PROCESS_TABLES = {"OE": "oe_shift_logs", "CARD": "card_shift_logs", "DRAW_FRAME": "draw_frame_shift_logs"}
PROCESS_COLUMNS = {
    "OE": {"machine_id", "shift_date", "shift_code", "mixing_lot_id", "count_id", "del", "rotor_speed",
           "production_kg", "efficiency_pct", "total_breakage", "run_time_hours", "detention_note"},
    "CARD": {"machine_id", "shift_date", "shift_code", "del", "efficiency_pct", "production_kg",
             "total_breakage", "run_time_hours", "idle_time_hours", "detention_note"},
    "DRAW_FRAME": {"machine_id", "shift_date", "shift_code", "count_id", "del", "efficiency_pct",
                   "production_kg", "total_breakage", "detention_note"},
}


class MachineOut(BaseModel):
    id: UUID
    machine_code: str
    process_type: Literal["OE", "CARD", "DRAW_FRAME"]
    display_no: int
    rotor_count: Optional[int]
    is_active: bool
    created_at: datetime


@router.get("/machines", response_model=List[MachineOut], summary="Machines assigned to the logged-in supervisor")
async def my_machines(user: dict = Depends(SUP)) -> List[dict]:
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT m.* FROM identity.user_machine_assignments a
            JOIN config.machines m ON m.id = a.machine_id
            WHERE a.user_id = $1 ORDER BY m.process_type, m.display_no
            """,
            user["id"],
        )
    return recs(rows)


class ShiftLogIn(BaseModel):
    machine_id: UUID
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]
    mixing_lot_id: Optional[UUID] = None
    count_id: Optional[UUID] = None
    del_rate: Optional[Decimal] = Field(None, alias="del")
    rotor_speed: Optional[int] = None
    production_kg: Decimal
    efficiency_pct: Decimal
    total_breakage: int = 0
    run_time_hours: Optional[Decimal] = None
    idle_time_hours: Optional[Decimal] = None
    detention_note: Optional[str] = None

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "machine_id": "99999999-1111-2222-3333-444444444444",
                "shift_date": "2026-07-14",
                "shift_code": "SHIFT_B",
                "mixing_lot_id": "44444444-5555-6666-7777-888888888888",
                "count": "23.5s",
                "del": "95.8",
                "rotor_speed": 85000,
                "production_kg": "748.00",
                "efficiency_pct": "88.00",
                "total_breakage": 6,
                "run_time_hours": "12.00",
            }
        },
    }


class ShiftLogOut(BaseModel):
    # Superset of OE/CARD/Draw Frame columns — fields not applicable to a given
    # process_type are simply absent from that row, same shape either way.
    id: UUID
    machine_id: UUID
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]
    mixing_lot_id: Optional[UUID] = None
    count_id: Optional[UUID] = None
    del_rate: Optional[Decimal] = Field(None, alias="del")
    rotor_speed: Optional[int] = None
    production_kg: Decimal
    efficiency_pct: Decimal
    total_breakage: int
    run_time_hours: Optional[Decimal] = None
    idle_time_hours: Optional[Decimal] = None
    detention_note: Optional[str] = None
    recorded_by: UUID
    created_at: datetime

    model_config = {"populate_by_name": True}


async def _insert_shift_log(conn, process_type: str, row: ShiftLogIn, user_id: UUID):
    allowed = PROCESS_COLUMNS[process_type]
    data = {k: v for k, v in row.model_dump(by_alias=True).items() if v is not None and k in allowed}
    cols = list(data) + ["recorded_by"]
    vals = list(data.values()) + [user_id]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(vals)))
    table = PROCESS_TABLES[process_type]
    query = f"INSERT INTO operations.{table} ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *"
    return await conn.fetchrow(query, *vals)


@router.post(
    "/shift-logs/{process_type}",
    response_model=List[ShiftLogOut],
    status_code=201,
    summary="Submit every machine's reading for one shift, one process type at a time",
)
async def submit_shift_logs(
    process_type: Literal["OE", "CARD", "DRAW_FRAME"], rows: List[ShiftLogIn], user: dict = Depends(SUP)
) -> List[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        results = [await _insert_shift_log(conn, process_type, r, user["id"]) for r in rows]
    return recs(results)


class SubmissionIn(BaseModel):
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]


class ShiftSubmissionOut(BaseModel):
    id: UUID
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]
    submitted_by: UUID
    submitted_at: datetime


@router.post(
    "/shift-submissions",
    response_model=ShiftSubmissionOut,
    status_code=201,
    summary="Lock a shift's entries",
    responses={409: {"description": "This shift has already been submitted"}},
)
async def submit_shift(body: SubmissionIn, user: dict = Depends(SUP)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO operations.shift_submissions (shift_date, shift_code, submitted_by) "
            "VALUES ($1,$2,$3) RETURNING *",
            body.shift_date, body.shift_code, user["id"],
        )
    return rec(row)


class DoffIntervalOut(BaseModel):
    doff_id: UUID
    machine_id: UUID
    occurred_at: datetime
    previous_occurred_at: Optional[datetime]
    hours_since_previous: Optional[Decimal]


@router.get(
    "/doff/last/{machine_id}",
    response_model=Optional[DoffIntervalOut],
    summary="Previous DOFF timestamp + elapsed hours (null if this machine has no prior DOFF yet)",
)
async def last_doff(machine_id: UUID, user: dict = Depends(SUP)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM operations.vw_doff_interval_check WHERE machine_id = $1 "
            "ORDER BY occurred_at DESC LIMIT 1",
            machine_id,
        )
    return rec(row)


class DoffIn(BaseModel):
    id: UUID  # client-generated offline idempotency key
    machine_id: UUID
    count_id: UUID
    cone_type_id: UUID
    bobbin_colour_id: UUID
    rotor_count: int

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "55555555-6666-7777-8888-999999999999",
                "machine_id": "99999999-1111-2222-3333-444444444444",
                "count_id": "77777777-8888-9999-0000-111111111111",
                "cone_type_id": "cone-24-uuid",
                "bobbin_colour_id": "colour-yellow-uuid",
                "rotor_count": 448,
            }
        }
    }


class DoffOut(BaseModel):
    id: UUID
    doff_code: str
    machine_id: UUID
    count_id: UUID
    cone_type_id: UUID
    bobbin_colour_id: UUID
    mixing_lot_id: Optional[UUID]
    rotor_count: int
    loose_stock_kg: Decimal
    previous_doff_id: Optional[UUID]
    recorded_by: UUID
    occurred_at: datetime
    created_at: datetime

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "55555555-6666-7777-8888-999999999999",
                "doff_code": "DOFF-2208",
                "machine_id": "99999999-1111-2222-3333-444444444444",
                "count_id": "77777777-8888-9999-0000-111111111111",
                "cone_type_id": "cone-24-uuid",
                "bobbin_colour_id": "colour-yellow-uuid",
                "mixing_lot_id": "44444444-5555-6666-7777-888888888888",
                "rotor_count": 448,
                "loose_stock_kg": "940.80",
                "previous_doff_id": None,
                "recorded_by": "22222222-3333-4444-5555-666666666666",
                "occurred_at": "2026-07-14T20:14:00Z",
                "created_at": "2026-07-14T20:14:00Z",
            }
        }
    }


@router.post(
    "/doff",
    response_model=DoffOut,
    status_code=201,
    summary="Confirm a DOFF — auto-computes loose stock and links to whichever lot is ACTIVE",
)
async def create_doff(body: DoffIn, user: dict = Depends(SUP)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        # Whichever lot is ACTIVE right now feeds this DOFF — resolved from the fast
        # cache table, per the append-only design's own traceability rule (design doc §7).
        active_lot = await conn.fetchval(
            "SELECT mixing_lot_id FROM operations.mixing_lot_current_state WHERE current_status = 'ACTIVE'"
        )
        row = await conn.fetchrow(
            """
            INSERT INTO operations.doff_records
                (id, machine_id, count_id, cone_type_id, bobbin_colour_id, mixing_lot_id, rotor_count, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (id) DO UPDATE SET id = EXCLUDED.id
            RETURNING *
            """,
            body.id, body.machine_id, body.count_id, body.cone_type_id, body.bobbin_colour_id,
            active_lot, body.rotor_count, user["id"],
        )
    return rec(row)
