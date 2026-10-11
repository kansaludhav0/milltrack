"""PDF rendering + storage for frozen sheet snapshots (Production/Mixing/Packaging).

These functions take already-fetched data and return PDF bytes — no DB access here.
Each trigger site (supervisor.py's submit_shift, godown.py's complete_mixing_lot,
packaging.py's finalize_day) gathers its own data with its own queries, then calls the
matching render_*_sheet() function, then store_sheet_report() to upload + record it.
Once stored, a sheet_report row is never touched again (operations.sheet_reports is
append-only) — it's a frozen document, not a live view.
"""
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.db import rec
from app.core.storage import upload_bytes

_styles = getSampleStyleSheet()


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, Decimal):
        return f"{v:.2f}"
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _table(headers: list[str], col_keys: list[str], rows: Iterable[dict]) -> Table:
    data = [headers] + [[_fmt(r.get(k)) for k in col_keys] for r in rows]
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _build_pdf(title: str, subtitle_lines: list[str], sections: list[tuple[str, list[str], list[str], list[dict]]]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), topMargin=18 * mm, bottomMargin=14 * mm)
    story = [Paragraph(title, _styles["Title"])]
    for line in subtitle_lines:
        story.append(Paragraph(line, _styles["Normal"]))
    story.append(Spacer(1, 10))
    for heading, headers, col_keys, rows in sections:
        story.append(Paragraph(heading, _styles["Heading3"]))
        if rows:
            story.append(_table(headers, col_keys, rows))
        else:
            story.append(Paragraph("No rows.", _styles["Normal"]))
        story.append(Spacer(1, 12))
    doc.build(story)
    return buf.getvalue()


def render_production_sheet(
    shift_date: date, shift_code: str, submitted_at: datetime,
    oe_rows: list[dict], card_rows: list[dict], draw_frame_rows: list[dict],
) -> bytes:
    sections = [
        (
            "OE (Open-End) Machines",
            ["Machine", "Count", "Del%", "Rotor Speed", "Production (kg)", "Efficiency %",
             "Breakage", "Run Hrs", "JSI %", "Note"],
            ["machine_code", "count_label", "del_rate", "rotor_speed", "production_kg",
             "efficiency_pct", "total_breakage", "run_time_hours", "jsi_pct", "detention_note"],
            oe_rows,
        ),
        (
            "Card Machines",
            ["Machine", "Del%", "Efficiency %", "Production (kg)", "Breakage", "Run Hrs", "Idle Hrs", "Note"],
            ["machine_code", "del_rate", "efficiency_pct", "production_kg", "total_breakage",
             "run_time_hours", "idle_time_hours", "detention_note"],
            card_rows,
        ),
        (
            "Draw Frame Machines",
            ["Machine", "Count", "Del%", "Efficiency %", "Production (kg)", "Breakage", "Note"],
            ["machine_code", "count_label", "del_rate", "efficiency_pct", "production_kg",
             "total_breakage", "detention_note"],
            draw_frame_rows,
        ),
    ]
    subtitle = [f"Shift: {shift_date.isoformat()} — {shift_code}", f"Submitted at: {submitted_at.isoformat()}"]
    return _build_pdf("Production Sheet", subtitle, sections)


def render_mixing_sheet(lot: dict, completed_at: datetime, recipe_rows: list[dict], bale_rows: list[dict], waste_rows: list[dict]) -> bytes:
    subtitle = [
        f"Lot: {lot['lot_code']}",
        f"Target: {_fmt(lot['target_weight_kg'])} kg — Scanned: {_fmt(lot['scanned_kg'])} kg — Bales: {lot['bale_count']}",
        f"Completed at: {completed_at.isoformat()}",
    ]
    sections = [
        (
            "Recipe",
            ["Category", "Target %", "Target (kg)", "Scanned (kg)"],
            ["category", "target_percentage", "target_weight_kg", "scanned_kg"],
            recipe_rows,
        ),
        (
            "Bales Scanned",
            ["Bale Code", "Party", "Category", "Net Wt (kg)", "Scanned At"],
            ["bale_code", "party_name", "mixing_category", "net_weight_kg", "occurred_at"],
            bale_rows,
        ),
        (
            "Waste",
            ["Category", "Weight (kg)", "Note"],
            ["mixing_category", "weight_kg", "note"],
            waste_rows,
        ),
    ]
    return _build_pdf("Mixing Sheet", subtitle, sections)


def render_packaging_sheet(log_date: date, rows: list[dict]) -> bytes:
    subtitle = [f"Date: {log_date.isoformat()}"]
    sections = [
        (
            "Packaging Paper Log",
            ["Palli", "Colour", "Count", "Cone Type", "Loose", "Packed", "Dispatched", "Closing"],
            ["palli", "colour", "count", "cone_type", "loose_display", "packed_display",
             "dispatched_display", "closing_display"],
            rows,
        ),
    ]
    return _build_pdf("Packaging Sheet", subtitle, sections)


async def store_sheet_report(
    conn, report_type: str, report_date: date, reference_id, shift_code: Optional[str],
    pdf_bytes: bytes, generated_by, filename: str,
) -> Optional[dict]:
    file_url = upload_bytes(prefix=report_type.lower(), filename=filename, data=pdf_bytes)
    row = await conn.fetchrow(
        """
        INSERT INTO operations.sheet_reports
            (report_type, report_date, shift_code, reference_id, file_url, generated_by)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (report_type, reference_id) DO NOTHING
        RETURNING *
        """,
        report_type, report_date, shift_code, reference_id, file_url, generated_by,
    )
    if row is None:
        row = await conn.fetchrow(
            "SELECT * FROM operations.sheet_reports WHERE report_type = $1 AND reference_id = $2",
            report_type, reference_id,
        )
    return rec(row)
