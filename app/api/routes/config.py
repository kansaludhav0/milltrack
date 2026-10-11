from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.db import get_pool, recs
from app.core.deps import get_current_user

# Every role's forms need a different slice of this reference data (a gateman needs
# parties + material varieties, packaging needs pallis + cone types + bobbin colours,
# and so on) — bundling it all into one call is simpler for a client to fetch once
# (e.g. right after login) and cache than firing off ten separate lookup requests.
# Open to any authenticated role: none of this is sensitive, and which subset a given
# screen needs varies by role anyway.
router = APIRouter(prefix="/config", tags=["config"])


class GateOut(BaseModel):
    id: UUID
    code: str
    name: str


class PartyOut(BaseModel):
    id: UUID
    name: str
    party_type: str
    contact_name: Optional[str]
    contact_phone: Optional[str]


class MaterialVarietyOut(BaseModel):
    id: UUID
    name: str
    default_mixing_category_id: Optional[UUID]


class GodownLocationOut(BaseModel):
    id: UUID
    code: str
    name: str
    godown_type: str


class MixingCategoryOut(BaseModel):
    id: UUID
    name: str
    code: str
    sort_order: int
    kind: Literal["FIBRE", "WASTE"]


class StandardRecipeLineOut(BaseModel):
    mixing_category_id: UUID
    category_name: str
    target_percentage: Decimal


class YarnCountOut(BaseModel):
    id: UUID
    label: str


class ConeTypeOut(BaseModel):
    id: UUID
    bundle_count: int
    net_weight_kg: Decimal


class BobbinColourOut(BaseModel):
    id: UUID
    name: str


class PalliOut(BaseModel):
    id: UUID
    party_id: UUID
    bobbin_colour_id: UUID
    display_name: str


class MachineOut(BaseModel):
    id: UUID
    machine_code: str
    process_type: str
    display_no: int
    rotor_count: Optional[int]


class LookupsOut(BaseModel):
    gates: List[GateOut]
    parties: List[PartyOut]
    material_varieties: List[MaterialVarietyOut]
    godown_locations: List[GodownLocationOut]
    mixing_categories: List[MixingCategoryOut]
    standard_mixing_recipe: List[StandardRecipeLineOut]
    yarn_counts: List[YarnCountOut]
    cone_types: List[ConeTypeOut]
    bobbin_colours: List[BobbinColourOut]
    pallis: List[PalliOut]
    machines: List[MachineOut]


@router.get(
    "/lookups",
    response_model=LookupsOut,
    summary="Every active reference table in one call — for populating dropdowns/pickers",
)
async def lookups(user: dict = Depends(get_current_user)) -> dict:
    async with get_pool().acquire() as conn:
        gates = await conn.fetch("SELECT id, code, name FROM config.gates WHERE is_active ORDER BY code")
        parties = await conn.fetch(
            "SELECT id, name, party_type, contact_name, contact_phone FROM config.parties "
            "WHERE is_active ORDER BY name"
        )
        material_varieties = await conn.fetch(
            "SELECT id, name, default_mixing_category_id FROM config.material_varieties "
            "WHERE is_active ORDER BY name"
        )
        godown_locations = await conn.fetch(
            "SELECT id, code, name, godown_type FROM config.godown_locations WHERE is_active ORDER BY code"
        )
        mixing_categories = await conn.fetch(
            "SELECT id, name, code, sort_order, kind FROM config.mixing_categories "
            "WHERE is_active ORDER BY sort_order"
        )
        standard_mixing_recipe = await conn.fetch(
            "SELECT r.mixing_category_id, mc.name AS category_name, r.target_percentage "
            "FROM config.standard_mixing_recipe r "
            "JOIN config.mixing_categories mc ON mc.id = r.mixing_category_id "
            "ORDER BY mc.sort_order"
        )
        yarn_counts = await conn.fetch("SELECT id, label FROM config.yarn_counts WHERE is_active ORDER BY label")
        cone_types = await conn.fetch(
            "SELECT id, bundle_count, net_weight_kg FROM config.cone_types WHERE is_active ORDER BY net_weight_kg"
        )
        bobbin_colours = await conn.fetch(
            "SELECT id, name FROM config.bobbin_colours WHERE is_active ORDER BY name"
        )
        pallis = await conn.fetch(
            "SELECT id, party_id, bobbin_colour_id, display_name FROM config.pallis "
            "WHERE is_active ORDER BY display_name"
        )
        machines = await conn.fetch(
            "SELECT id, machine_code, process_type, display_no, rotor_count FROM config.machines "
            "WHERE is_active ORDER BY process_type, display_no"
        )
    return {
        "gates": recs(gates),
        "parties": recs(parties),
        "material_varieties": recs(material_varieties),
        "godown_locations": recs(godown_locations),
        "mixing_categories": recs(mixing_categories),
        "standard_mixing_recipe": recs(standard_mixing_recipe),
        "yarn_counts": recs(yarn_counts),
        "cone_types": recs(cone_types),
        "bobbin_colours": recs(bobbin_colours),
        "pallis": recs(pallis),
        "machines": recs(machines),
    }
