import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.db import get_pool, rec, recs
from app.core.deps import require_role
from app.core.sheets import render_production_sheet, store_sheet_report

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/supervisor", tags=["supervisor"])
SUP = require_role("SUPERVISOR")
# Read-only visibility for GM/MD — see gate.py's GATE_READ for the reasoning. Never used
# on writes; only the supervisor can submit shift logs, lock a shift, or confirm a DOFF.
SUP_READ = require_role("SUPERVISOR", "GM_MD_ADMIN")

# Whitelisted table/column names per process type — the only things ever interpolated
# into SQL below are keys drawn from these fixed dicts, never raw request input, so
# building the INSERT dynamically doesn't open an injection path.
PROCESS_TABLES = {"OE": "oe_shift_logs", "CARD": "card_shift_logs", "DRAW_FRAME": "draw_frame_shift_logs"}
PROCESS_COLUMNS = {
    "OE": {"machine_id", "shift_date", "shift_code", "count_id", "del", "rotor_speed",
           "production_kg", "efficiency_pct", "total_breakage", "run_time_hours", "jsi_pct", "detention_note"},
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


MACHINE_CODE_PREFIX = {"OE": "OE", "CARD": "CARD", "DRAW_FRAME": "DF"}


class MachineIn(BaseModel):
    process_type: Literal["OE", "CARD", "DRAW_FRAME"]
    display_no: Optional[int] = None  # auto-assigned to the next free number if omitted
    rotor_count: Optional[int] = None  # OE only


async def _create_machine(conn, process_type: str, display_no: Optional[int], rotor_count: Optional[int]):
    if rotor_count is not None and process_type != "OE":
        raise HTTPException(400, "rotor_count only applies to OE machines")

    existing_numbers = {
        row["display_no"]
        for row in await conn.fetch("SELECT display_no FROM config.machines WHERE process_type = $1", process_type)
    }
    if display_no is not None:
        if display_no in existing_numbers:
            raise HTTPException(400, f"{process_type} machine #{display_no} already exists")
    else:
        # Next free number — fills a gap left by an old number if one exists, otherwise
        # one past the highest in use. No cap: as many machines as the floor actually has.
        display_no = next(n for n in range(1, len(existing_numbers) + 2) if n not in existing_numbers)

    machine_code = f"{MACHINE_CODE_PREFIX[process_type]}-{str(display_no).zfill(2)}"
    return await conn.fetchrow(
        "INSERT INTO config.machines (machine_code, process_type, display_no, rotor_count) "
        "VALUES ($1,$2,$3,$4) RETURNING *",
        machine_code, process_type, display_no, rotor_count,
    )


@router.post(
    "/machines",
    response_model=MachineOut,
    status_code=201,
    summary="Add a new machine — no admin step required; a supervisor adds it directly "
            "the moment a new physical machine arrives",
    responses={400: {"description": "rotor_count given for a non-OE machine, or display_no already in use"}},
)
async def create_machine(body: MachineIn, user: dict = Depends(SUP)) -> Optional[dict]:
    async with get_pool().acquire() as conn, conn.transaction():
        row = await _create_machine(conn, body.process_type, body.display_no, body.rotor_count)
    return rec(row)


@router.delete(
    "/machines/{machine_id}",
    status_code=204,
    summary="Delete a machine added by mistake — only while it has zero history (no shift "
            "logs, no DOFFs); once it has any, this is blocked and Admin must deactivate it instead",
    responses={409: {"description": "This machine already has shift logs or DOFFs recorded against it"}},
)
async def delete_machine(machine_id: UUID, user: dict = Depends(SUP)) -> None:
    async with get_pool().acquire() as conn:
        if not await conn.fetchval("SELECT 1 FROM config.machines WHERE id = $1", machine_id):
            return  # idempotent — already gone
        has_history = await conn.fetchval(
            """
            SELECT
                EXISTS(SELECT 1 FROM operations.oe_shift_logs WHERE machine_id = $1)
                OR EXISTS(SELECT 1 FROM operations.card_shift_logs WHERE machine_id = $1)
                OR EXISTS(SELECT 1 FROM operations.draw_frame_shift_logs WHERE machine_id = $1)
                OR EXISTS(SELECT 1 FROM operations.doff_records WHERE machine_id = $1)
            """,
            machine_id,
        )
        if has_history:
            raise HTTPException(
                409,
                "This machine already has shift logs or DOFFs recorded against it — "
                "ask an admin to deactivate it instead of deleting",
            )
        await conn.execute("DELETE FROM config.machines WHERE id = $1", machine_id)


@router.get(
    "/machines",
    response_model=List[MachineOut],
    summary="Every active machine — all supervisors see the same full list, no per-supervisor assignment",
)
async def my_machines(
    include_inactive: bool = Query(
        False,
        description="Include retired machines too — needed when reopening an old shift "
                    "that has a reading from a machine later deactivated",
    ),
    user: dict = Depends(SUP_READ),
) -> List[dict]:
    async with get_pool().acquire() as conn:
        if include_inactive:
            rows = await conn.fetch("SELECT * FROM config.machines ORDER BY process_type, display_no")
        else:
            rows = await conn.fetch(
                "SELECT * FROM config.machines WHERE is_active ORDER BY process_type, display_no"
            )
    return recs(rows)


class ShiftLogIn(BaseModel):
    id: UUID  # client-generated offline idempotency key — safe to retry the same row
    machine_id: UUID
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]
    count_id: Optional[UUID] = None
    del_rate: Optional[Decimal] = Field(None, alias="del")
    rotor_speed: Optional[int] = None
    production_kg: Decimal
    efficiency_pct: Decimal
    total_breakage: int = 0
    run_time_hours: Optional[Decimal] = None
    idle_time_hours: Optional[Decimal] = None
    jsi_pct: Optional[Decimal] = None  # OE only — Joint Spinning-In piecing success rate after a restart
    detention_note: Optional[str] = None
    # Submitting a correction: set this to the id of the row being corrected, and the
    # table's append-only rule (and the shift-lock check below) are bypassed for it —
    # this is the ONLY way to fix a reading once submitted or once the shift is locked.
    correction_of_id: Optional[UUID] = None
    correction_note: Optional[str] = None

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "example": {
                "id": "66666666-1111-2222-3333-444444444444",
                "machine_id": "99999999-1111-2222-3333-444444444444",
                "shift_date": "2026-07-14",
                "shift_code": "SHIFT_B",
                "count": "23.5s",
                "del": "95.8",
                "rotor_speed": 85000,
                "production_kg": "748.00",
                "efficiency_pct": "88.00",
                "total_breakage": 6,
                "run_time_hours": "12.00",
                "jsi_pct": "91.70",
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
    count_id: Optional[UUID] = None
    del_rate: Optional[Decimal] = Field(None, alias="del")
    rotor_speed: Optional[int] = None
    production_kg: Decimal
    efficiency_pct: Decimal
    total_breakage: int
    run_time_hours: Optional[Decimal] = None
    idle_time_hours: Optional[Decimal] = None
    jsi_pct: Optional[Decimal] = None
    detention_note: Optional[str] = None
    correction_of_id: Optional[UUID] = None
    correction_note: Optional[str] = None
    recorded_by: UUID
    created_at: datetime

    model_config = {"populate_by_name": True}


async def _insert_shift_log(conn, process_type: str, row: ShiftLogIn, user_id: UUID):
    table = PROCESS_TABLES[process_type]

    if row.correction_of_id is None:
        # Fresh entry — blocked once the shift is locked. A correction (below) is the
        # only way to change a reading after that point.
        locked = await conn.fetchval(
            "SELECT 1 FROM operations.shift_submissions WHERE shift_date = $1 AND shift_code = $2",
            row.shift_date, row.shift_code,
        )
        if locked:
            raise HTTPException(
                409,
                "This shift has already been submitted — further entries must reference the "
                "row being corrected via correction_of_id",
            )
    else:
        exists = await conn.fetchval(f"SELECT 1 FROM operations.{table} WHERE id = $1", row.correction_of_id)
        if not exists:
            raise HTTPException(400, f"correction_of_id does not reference an existing {process_type} shift log")

    allowed = PROCESS_COLUMNS[process_type]
    data = {k: v for k, v in row.model_dump(by_alias=True).items() if v is not None and k in allowed}
    cols = ["id"] + list(data) + ["correction_of_id", "correction_note", "recorded_by"]
    vals = [row.id] + list(data.values()) + [row.correction_of_id, row.correction_note, user_id]
    placeholders = ", ".join(f"${i + 1}" for i in range(len(vals)))
    # DO NOTHING, not DO UPDATE — this table is append-only, and an UPDATE (even a
    # value-preserving "set id = itself") still fires the append-only trigger and fails.
    # A retried submission just finds nothing to insert, so fall back to fetching the
    # row that's already there.
    query = (
        f"INSERT INTO operations.{table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT (id, shift_date) DO NOTHING RETURNING *"
    )
    inserted = await conn.fetchrow(query, *vals)
    if inserted is not None:
        return inserted
    return await conn.fetchrow(f"SELECT * FROM operations.{table} WHERE id = $1", row.id)


@router.post(
    "/shift-logs/{process_type}",
    response_model=List[ShiftLogOut],
    status_code=201,
    summary="Submit every machine's reading for one shift, one process type at a time",
    responses={409: {"description": "Shift already submitted — further entries must be corrections"}},
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


async def _shift_logs_for_sheet(conn, process_type: str, shift_date: date, shift_code: str) -> List[dict]:
    table = PROCESS_TABLES[process_type]
    rows = await conn.fetch(
        f"""
        SELECT sl.*, m.machine_code, yc.label AS count_label
        FROM operations.{table} sl
        JOIN config.machines m ON m.id = sl.machine_id
        LEFT JOIN config.yarn_counts yc ON yc.id = sl.count_id
        WHERE sl.shift_date = $1 AND sl.shift_code = $2
          AND sl.id NOT IN (
              SELECT correction_of_id FROM operations.{table}
              WHERE correction_of_id IS NOT NULL AND shift_date = $1 AND shift_code = $2
          )
        ORDER BY m.display_no
        """,
        shift_date, shift_code,
    )
    return recs(rows)


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
        # Freeze today's Production Sheet the moment the shift locks. A PDF/S3 hiccup
        # here must never fail the shift lock itself — the lock already succeeded and
        # is what matters operationally; the sheet can be missing without blocking work.
        try:
            oe_rows = await _shift_logs_for_sheet(conn, "OE", body.shift_date, body.shift_code)
            card_rows = await _shift_logs_for_sheet(conn, "CARD", body.shift_date, body.shift_code)
            draw_frame_rows = await _shift_logs_for_sheet(conn, "DRAW_FRAME", body.shift_date, body.shift_code)
            pdf_bytes = render_production_sheet(
                body.shift_date, body.shift_code, row["submitted_at"], oe_rows, card_rows, draw_frame_rows
            )
            await store_sheet_report(
                conn, "PRODUCTION", body.shift_date, row["id"], body.shift_code,
                pdf_bytes, user["id"], f"production-{body.shift_date}-{body.shift_code}.pdf",
            )
        except Exception:
            logger.exception("Failed to generate Production Sheet for %s %s", body.shift_date, body.shift_code)
    return rec(row)


@router.get(
    "/shift-submissions",
    response_model=Optional[ShiftSubmissionOut],
    summary="Whether a shift is locked yet — null if it hasn't been submitted",
)
async def get_shift_submission(
    shift_date: date, shift_code: Literal["SHIFT_A", "SHIFT_B"], user: dict = Depends(SUP_READ)
) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM operations.shift_submissions WHERE shift_date = $1 AND shift_code = $2",
            shift_date, shift_code,
        )
    return rec(row)


@router.get(
    "/shift-logs/{process_type}",
    response_model=List[ShiftLogOut],
    summary="A shift's saved readings — the current (latest-corrected) version of each machine's row",
)
async def get_shift_logs(
    process_type: Literal["OE", "CARD", "DRAW_FRAME"],
    shift_date: date,
    shift_code: Literal["SHIFT_A", "SHIFT_B"],
    user: dict = Depends(SUP_READ),
) -> List[dict]:
    table = PROCESS_TABLES[process_type]
    async with get_pool().acquire() as conn:
        # A correction appends a new row referencing the old one via correction_of_id —
        # "current" means the row nothing else points at as its correction_of_id, which
        # resolves correctly regardless of how many corrections deep a chain goes.
        rows = await conn.fetch(
            f"""
            SELECT * FROM operations.{table}
            WHERE shift_date = $1 AND shift_code = $2
              AND id NOT IN (
                  SELECT correction_of_id FROM operations.{table}
                  WHERE correction_of_id IS NOT NULL AND shift_date = $1 AND shift_code = $2
              )
            ORDER BY machine_id
            """,
            shift_date, shift_code,
        )
    return recs(rows)


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
async def last_doff(machine_id: UUID, user: dict = Depends(SUP_READ)) -> Optional[dict]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM operations.vw_doff_interval_check WHERE machine_id = $1 "
            "ORDER BY occurred_at DESC LIMIT 1",
            machine_id,
        )
    return rec(row)


# Shifts run 7am-7pm and 7pm-7am local mill time — DOFF records only carry a UTC
# timestamp (occurred_at), not a shift_date/shift_code of their own, so "this shift's
# total" has to be computed by converting the shift's local window to UTC and filtering.
MILL_TZ = ZoneInfo("Asia/Kolkata")
SHIFT_START_HOUR = {"SHIFT_A": 7, "SHIFT_B": 19}


def _shift_window(shift_date: date, shift_code: str) -> tuple[datetime, datetime]:
    start = datetime(shift_date.year, shift_date.month, shift_date.day, SHIFT_START_HOUR[shift_code], tzinfo=MILL_TZ)
    return start, start + timedelta(hours=12)


class CurrentShiftOut(BaseModel):
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]


@router.get(
    "/current-shift",
    response_model=CurrentShiftOut,
    summary="The shift (date + code) that's active right now in mill local time — the app "
            "should call this instead of asking the supervisor to pick a shift manually",
)
async def current_shift(user: dict = Depends(SUP_READ)) -> dict:
    now_local = datetime.now(MILL_TZ)
    if SHIFT_START_HOUR["SHIFT_A"] <= now_local.hour < SHIFT_START_HOUR["SHIFT_B"]:
        return {"shift_date": now_local.date(), "shift_code": "SHIFT_A"}
    # SHIFT_B spans midnight (7pm today -> 7am tomorrow local time). Before 7am, "now" is
    # still within the SHIFT_B window that started the PREVIOUS calendar day.
    shift_date = now_local.date() if now_local.hour >= SHIFT_START_HOUR["SHIFT_B"] else now_local.date() - timedelta(days=1)
    return {"shift_date": shift_date, "shift_code": "SHIFT_B"}


class DoffTotalOut(BaseModel):
    machine_id: UUID
    shift_date: date
    shift_code: Literal["SHIFT_A", "SHIFT_B"]
    doff_count: int
    total_loose_stock_kg: Decimal


@router.get(
    "/doff/total/{machine_id}",
    response_model=DoffTotalOut,
    summary="This machine's cumulative DOFF output (loose stock) during one shift window",
)
async def doff_total(
    machine_id: UUID,
    shift_date: date,
    shift_code: Literal["SHIFT_A", "SHIFT_B"],
    user: dict = Depends(SUP_READ),
) -> dict:
    start, end = _shift_window(shift_date, shift_code)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS doff_count, COALESCE(SUM(loose_stock_kg), 0) AS total_loose_stock_kg "
            "FROM operations.doff_records WHERE machine_id = $1 AND occurred_at >= $2 AND occurred_at < $3",
            machine_id, start, end,
        )
    return {
        "machine_id": machine_id,
        "shift_date": shift_date,
        "shift_code": shift_code,
        "doff_count": row["doff_count"],
        "total_loose_stock_kg": row["total_loose_stock_kg"],
    }


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
    summary="Confirm a DOFF — auto-computes loose stock",
)
async def create_doff(body: DoffIn, user: dict = Depends(SUP)) -> Optional[dict]:
    # No mixing-lot link: mixing/spinning is a continuous process, not discrete batches —
    # a DOFF's output can't be validly attributed to a single mixing lot, so this never
    # attempts to resolve or record one (previously, and wrongly, auto-linked to
    # whichever lot was ACTIVE).
    async with get_pool().acquire() as conn:
        # DO NOTHING, not DO UPDATE — doff_records is append-only; a retried submission
        # finds nothing to insert, so fall back to fetching the row already there.
        row = await conn.fetchrow(
            """
            INSERT INTO operations.doff_records
                (id, machine_id, count_id, cone_type_id, bobbin_colour_id, rotor_count, recorded_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (id) DO NOTHING
            RETURNING *
            """,
            body.id, body.machine_id, body.count_id, body.cone_type_id, body.bobbin_colour_id,
            body.rotor_count, user["id"],
        )
        if row is None:
            row = await conn.fetchrow("SELECT * FROM operations.doff_records WHERE id = $1", body.id)
    return rec(row)
