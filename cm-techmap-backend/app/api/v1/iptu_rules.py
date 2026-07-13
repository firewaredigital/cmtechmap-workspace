"""
CM TECHMAP — IPTU Rules CRUD API
Configurable per-municipality IPTU calculation rules.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_gestor, require_tenant_admin

logger = logging.getLogger("cm_techmap.api.iptu_rules")

router = APIRouter(prefix="/iptu/rules", tags=["IPTU Rules"])


# ══════════════════════════════════════════════════════════════════════════════
# RULE SETS (per municipality)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("")
async def list_rule_sets(
    state: str | None = Query(None, max_length=2),
    is_active: bool | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """List all IPTU rule sets with optional filters."""
    conditions = []
    params: dict[str, Any] = {}

    if state:
        conditions.append("rs.state = :state")
        params["state"] = state.upper()
    if is_active is not None:
        conditions.append("rs.is_active = :active")
        params["active"] = is_active

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    result = await db.execute(text(f"""
        SELECT rs.id, rs.municipality_name, rs.municipality_code, rs.state,
               rs.is_active, rs.base_year,
               rs.default_land_value_per_sqm, rs.default_built_value_per_sqm,
               rs.default_aliquot_pct, rs.pool_surcharge_pct,
               rs.created_at, rs.updated_at,
               (SELECT COUNT(*) FROM iptu_rules r WHERE r.rule_set_id = rs.id) as zone_count
        FROM iptu_rule_sets rs
        {where}
        ORDER BY rs.municipality_name
    """), params)

    rule_sets = []
    for r in result.mappings().all():
        rs = dict(r)
        rs["id"] = str(rs["id"])
        for ts in ("created_at", "updated_at"):
            if rs.get(ts):
                rs[ts] = rs[ts].isoformat()
        rule_sets.append(rs)

    return {"rule_sets": rule_sets, "count": len(rule_sets)}


@router.post("")
async def create_rule_set(
    municipality_name: str = Query(..., min_length=2),
    municipality_code: str = Query(..., min_length=5, max_length=10),
    state: str = Query(..., min_length=2, max_length=2),
    base_year: int = Query(..., ge=2000, le=2100),
    default_land_value_per_sqm: float = Query(50.0, ge=0),
    default_built_value_per_sqm: float = Query(800.0, ge=0),
    default_aliquot_pct: float = Query(1.0, ge=0, le=100),
    pool_surcharge_pct: float = Query(20.0, ge=0, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Create a new IPTU rule set for a municipality."""
    try:
        result = await db.execute(text("""
            INSERT INTO iptu_rule_sets
                (municipality_name, municipality_code, state, base_year,
                 default_land_value_per_sqm, default_built_value_per_sqm,
                 default_aliquot_pct, pool_surcharge_pct)
            VALUES (:name, :code, :state, :year, :land_val, :built_val, :aliquot, :pool)
            RETURNING id, created_at
        """), {
            "name": municipality_name,
            "code": municipality_code,
            "state": state.upper(),
            "year": base_year,
            "land_val": default_land_value_per_sqm,
            "built_val": default_built_value_per_sqm,
            "aliquot": default_aliquot_pct,
            "pool": pool_surcharge_pct,
        })
        row = result.fetchone()
        await db.commit()
        return {
            "id": str(row[0]),
            "municipality_name": municipality_name,
            "municipality_code": municipality_code,
            "created_at": row[1].isoformat() if row[1] else None,
        }
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(400, "Já existe um rule set para este município")
        logger.error(f"Failed to create rule set: {e}")
        raise HTTPException(500, "Falha ao criar rule set")


@router.get("/{rule_set_id}")
async def get_rule_set(
    rule_set_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Get a rule set with all its zone rules."""
    rs_result = await db.execute(text("""
        SELECT * FROM iptu_rule_sets WHERE id = :id
    """), {"id": str(rule_set_id)})
    rs = rs_result.mappings().first()
    if not rs:
        raise HTTPException(404, "Rule set não encontrado")

    rules_result = await db.execute(text("""
        SELECT * FROM iptu_rules WHERE rule_set_id = :rsid ORDER BY zone_name
    """), {"rsid": str(rule_set_id)})

    rs_dict = dict(rs)
    rs_dict["id"] = str(rs_dict["id"])
    for ts in ("created_at", "updated_at"):
        if rs_dict.get(ts):
            rs_dict[ts] = rs_dict[ts].isoformat()

    zones = []
    for r in rules_result.mappings().all():
        z = dict(r)
        z["id"] = str(z["id"])
        z["rule_set_id"] = str(z["rule_set_id"])
        for ts in ("created_at", "updated_at"):
            if z.get(ts):
                z[ts] = z[ts].isoformat()
        zones.append(z)

    rs_dict["zones"] = zones
    return rs_dict


@router.put("/{rule_set_id}")
async def update_rule_set(
    rule_set_id: UUID,
    municipality_name: str | None = Query(None),
    base_year: int | None = Query(None, ge=2000, le=2100),
    is_active: bool | None = Query(None),
    default_land_value_per_sqm: float | None = Query(None, ge=0),
    default_built_value_per_sqm: float | None = Query(None, ge=0),
    default_aliquot_pct: float | None = Query(None, ge=0, le=100),
    pool_surcharge_pct: float | None = Query(None, ge=0, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Update an existing IPTU rule set."""
    updates = []
    params: dict[str, Any] = {"id": str(rule_set_id)}

    for field, val in [
        ("municipality_name", municipality_name), ("base_year", base_year),
        ("is_active", is_active), ("default_land_value_per_sqm", default_land_value_per_sqm),
        ("default_built_value_per_sqm", default_built_value_per_sqm),
        ("default_aliquot_pct", default_aliquot_pct),
        ("pool_surcharge_pct", pool_surcharge_pct),
    ]:
        if val is not None:
            updates.append(f"{field} = :{field}")
            params[field] = val

    if not updates:
        raise HTTPException(400, "Nenhum campo para atualizar")

    updates.append("updated_at = NOW()")
    result = await db.execute(text(f"""
        UPDATE iptu_rule_sets SET {', '.join(updates)} WHERE id = :id RETURNING id
    """), params)
    if not result.fetchone():
        raise HTTPException(404, "Rule set não encontrado")
    await db.commit()
    return {"status": "updated", "id": str(rule_set_id)}


@router.delete("/{rule_set_id}")
async def delete_rule_set(
    rule_set_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Delete a rule set and all its zone rules."""
    await db.execute(text("DELETE FROM iptu_rule_sets WHERE id = :id"), {"id": str(rule_set_id)})
    await db.commit()
    return {"status": "deleted"}


# ══════════════════════════════════════════════════════════════════════════════
# ZONE RULES (within a rule set)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/{rule_set_id}/zones")
async def add_zone_rule(
    rule_set_id: UUID,
    zone_name: str = Query(..., min_length=2),
    land_value_per_sqm_brl: float = Query(..., ge=0),
    built_value_per_sqm_brl: float = Query(..., ge=0),
    aliquot_pct: float = Query(..., ge=0, le=100),
    depreciation_rate_per_year: float = Query(0.01, ge=0, le=1),
    min_area_sqm: float = Query(0.0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Add a zone-specific rule to a rule set."""
    try:
        result = await db.execute(text("""
            INSERT INTO iptu_rules
                (rule_set_id, zone_name, land_value_per_sqm_brl,
                 built_value_per_sqm_brl, aliquot_pct, depreciation_rate_per_year, min_area_sqm)
            VALUES (:rsid, :zone, :land, :built, :aliquot, :dep, :min_area)
            RETURNING id
        """), {
            "rsid": str(rule_set_id), "zone": zone_name.lower(),
            "land": land_value_per_sqm_brl, "built": built_value_per_sqm_brl,
            "aliquot": aliquot_pct, "dep": depreciation_rate_per_year,
            "min_area": min_area_sqm,
        })
        row = result.fetchone()
        await db.commit()
        return {"id": str(row[0]), "zone_name": zone_name, "status": "created"}
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(400, f"Zona '{zone_name}' já existe neste rule set")
        raise HTTPException(500, f"Falha ao criar zona: {str(e)[:200]}")


@router.put("/{rule_set_id}/zones/{zone_id}")
async def update_zone_rule(
    rule_set_id: UUID,
    zone_id: UUID,
    land_value_per_sqm_brl: float | None = Query(None, ge=0),
    built_value_per_sqm_brl: float | None = Query(None, ge=0),
    aliquot_pct: float | None = Query(None, ge=0, le=100),
    depreciation_rate_per_year: float | None = Query(None, ge=0, le=1),
    min_area_sqm: float | None = Query(None, ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Update a zone rule."""
    updates = []
    params: dict[str, Any] = {"id": str(zone_id), "rsid": str(rule_set_id)}

    for field, val in [
        ("land_value_per_sqm_brl", land_value_per_sqm_brl),
        ("built_value_per_sqm_brl", built_value_per_sqm_brl),
        ("aliquot_pct", aliquot_pct),
        ("depreciation_rate_per_year", depreciation_rate_per_year),
        ("min_area_sqm", min_area_sqm),
    ]:
        if val is not None:
            updates.append(f"{field} = :{field}")
            params[field] = val

    if not updates:
        raise HTTPException(400, "Nenhum campo para atualizar")

    updates.append("updated_at = NOW()")
    result = await db.execute(text(f"""
        UPDATE iptu_rules SET {', '.join(updates)}
        WHERE id = :id AND rule_set_id = :rsid RETURNING id
    """), params)
    if not result.fetchone():
        raise HTTPException(404, "Zona não encontrada")
    await db.commit()
    return {"status": "updated"}


@router.delete("/{rule_set_id}/zones/{zone_id}")
async def delete_zone_rule(
    rule_set_id: UUID,
    zone_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Delete a zone rule."""
    await db.execute(text(
        "DELETE FROM iptu_rules WHERE id = :id AND rule_set_id = :rsid"
    ), {"id": str(zone_id), "rsid": str(rule_set_id)})
    await db.commit()
    return {"status": "deleted"}


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/{rule_set_id}/simulate")
async def simulate_iptu_calculation(
    rule_set_id: UUID,
    zone_name: str = Query("residencial"),
    registered_area_sqm: float = Query(100.0, ge=0),
    detected_area_sqm: float = Query(150.0, ge=0),
    has_pool: bool = Query(False),
    building_age_years: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Simulate IPTU calculation with given parameters (no DB side effects)."""
    # Get rule set
    rs_result = await db.execute(text(
        "SELECT * FROM iptu_rule_sets WHERE id = :id"
    ), {"id": str(rule_set_id)})
    rs = rs_result.mappings().first()
    if not rs:
        raise HTTPException(404, "Rule set não encontrado")

    # Get zone rule
    zone_result = await db.execute(text("""
        SELECT * FROM iptu_rules
        WHERE rule_set_id = :rsid AND zone_name = :zone
    """), {"rsid": str(rule_set_id), "zone": zone_name.lower()})
    zone = zone_result.mappings().first()

    # Use zone-specific or default values
    land_val = zone["land_value_per_sqm_brl"] if zone else rs["default_land_value_per_sqm"]
    built_val = zone["built_value_per_sqm_brl"] if zone else rs["default_built_value_per_sqm"]
    aliquot = zone["aliquot_pct"] if zone else rs["default_aliquot_pct"]
    dep_rate = zone["depreciation_rate_per_year"] if zone else 0.01

    # Depreciation
    depreciation_pct = min(building_age_years * dep_rate * 100, 50.0)
    depreciation_factor = 1 - (depreciation_pct / 100)

    # Calculate current IPTU (based on registered area)
    venal_registered = registered_area_sqm * built_val * depreciation_factor
    iptu_registered = venal_registered * (aliquot / 100)

    # Calculate proposed IPTU (based on detected area)
    venal_detected = detected_area_sqm * built_val * depreciation_factor
    iptu_detected = venal_detected * (aliquot / 100)

    # Pool surcharge
    pool_surcharge = 0.0
    if has_pool:
        pool_surcharge = iptu_detected * (rs["pool_surcharge_pct"] / 100)
        iptu_detected += pool_surcharge

    return {
        "simulation": True,
        "parameters": {
            "zone": zone_name,
            "registered_area_sqm": registered_area_sqm,
            "detected_area_sqm": detected_area_sqm,
            "has_pool": has_pool,
            "building_age_years": building_age_years,
        },
        "rates": {
            "built_value_per_sqm": built_val,
            "aliquot_pct": aliquot,
            "depreciation_pct": round(depreciation_pct, 2),
            "pool_surcharge_pct": rs["pool_surcharge_pct"] if has_pool else 0,
        },
        "calculation": {
            "venal_value_registered": round(venal_registered, 2),
            "venal_value_detected": round(venal_detected, 2),
            "iptu_current_brl": round(iptu_registered, 2),
            "iptu_proposed_brl": round(iptu_detected, 2),
            "pool_surcharge_brl": round(pool_surcharge, 2),
            "difference_brl": round(iptu_detected - iptu_registered, 2),
            "difference_pct": round(
                ((iptu_detected - iptu_registered) / iptu_registered * 100) if iptu_registered > 0 else 0, 1
            ),
        },
    }
