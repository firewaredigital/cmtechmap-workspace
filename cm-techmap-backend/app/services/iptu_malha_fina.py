"""
CM TECHMAP — IPTU "Malha Fina" Service v2
Cross-references AI building detections against the official cadastral registry
to identify tax discrepancies. Results are PERSISTED to the database.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("cm_techmap.iptu")


class DiscrepancyType:
    UNREGISTERED = "unregistered"
    AREA_UNDER_DECLARED = "area_under_declared"
    AREA_OVER_DECLARED = "area_over_declared"
    DEMOLISHED = "demolished"
    HEIGHT_MISMATCH = "height_mismatch"
    POOL_DETECTED = "pool_detected"


class IPTUMalhaFinaService:
    """
    Cross-reference AI detections with cadastral parcels to find tax irregularities.
    v2: Uses configurable iptu_rules per municipality and persists to discrepancies table.
    """

    @staticmethod
    async def run_analysis(
        session: AsyncSession,
        project_id: str,
        *,
        triggered_by: str = "system",
        area_tolerance_pct: float = 15.0,
        height_tolerance_m: float = 3.0,
        min_overlap_pct: float = 50.0,
        municipality_code: str | None = None,
    ) -> dict[str, Any]:
        logger.info(f"[IPTU] Starting Malha Fina v2 for project {project_id}")
        start_time = datetime.now(timezone.utc)

        # ── Step 0: Create analysis_run record ────────────────────────────
        run_result = await session.execute(text("""
            INSERT INTO analysis_runs (project_id, run_type, status, triggered_by, parameters)
            VALUES (:pid, 'iptu_malha_fina', 'running', :triggered_by, :params)
            RETURNING id
        """), {
            "pid": project_id,
            "triggered_by": triggered_by,
            "params": json.dumps({
                "area_tolerance_pct": area_tolerance_pct,
                "height_tolerance_m": height_tolerance_m,
                "min_overlap_pct": min_overlap_pct,
                "municipality_code": municipality_code,
            }),
        })
        run_id = str(run_result.scalar())

        # ── Step 1: Load IPTU rules for the municipality ──────────────────
        rule_set = await _load_iptu_rules(session, municipality_code, project_id)
        logger.info(f"[IPTU] Using rule set: {rule_set.get('municipality_name', 'default')}")

        # ── Step 2: Get detections + parcels ──────────────────────────────
        detections = await _get_building_detections(session, project_id)
        parcels = await _get_overlapping_parcels(session, project_id)
        logger.info(f"[IPTU] Found {len(detections)} detections, {len(parcels)} parcels")

        # ── Step 3: Spatial cross-reference ───────────────────────────────
        discrepancies = []
        matched_detections = set()
        matched_parcels = set()

        for parcel in parcels:
            best_match = None
            best_overlap = 0.0

            for detection in detections:
                overlap = await _compute_overlap(session, detection["id"], parcel["id"])
                if overlap > best_overlap and overlap >= min_overlap_pct:
                    best_match = detection
                    best_overlap = overlap

            if best_match:
                matched_detections.add(best_match["id"])
                matched_parcels.add(parcel["id"])

                detected_area = best_match.get("area_sqm", 0) or 0
                registered_area = parcel.get("registered_built_area_sqm", 0) or 0

                if registered_area > 0 and detected_area > 0:
                    diff_pct = ((detected_area - registered_area) / registered_area) * 100

                    if diff_pct > area_tolerance_pct:
                        calc = _calculate_iptu(
                            rule_set, parcel, registered_area, detected_area,
                            has_pool=False, height_m=best_match.get("height_m"),
                        )
                        discrepancies.append({
                            "type": DiscrepancyType.AREA_UNDER_DECLARED,
                            "severity": "critical" if diff_pct > 100 else "high" if diff_pct > 50 else "medium",
                            "parcel_id": str(parcel["id"]),
                            "detection_id": str(best_match["id"]),
                            "cadastral_code": parcel.get("cadastral_code"),
                            "address": parcel.get("address"),
                            "neighborhood": parcel.get("neighborhood"),
                            "owner_name": None,
                            "registered_area_sqm": registered_area,
                            "detected_area_sqm": round(detected_area, 2),
                            "difference_sqm": round(detected_area - registered_area, 2),
                            "difference_pct": round(diff_pct, 1),
                            "overlap_pct": round(best_overlap, 1),
                            "confidence": best_match.get("confidence", 0),
                            "detected_height_m": best_match.get("height_m"),
                            "iptu_current_brl": calc["iptu_current"],
                            "iptu_proposed_brl": calc["iptu_proposed"],
                            "estimated_iptu_gap_brl": calc["gap"],
                            "calculation_details": json.dumps(calc),
                        })
                    elif diff_pct < -area_tolerance_pct:
                        discrepancies.append({
                            "type": DiscrepancyType.AREA_OVER_DECLARED,
                            "severity": "low",
                            "parcel_id": str(parcel["id"]),
                            "detection_id": str(best_match["id"]),
                            "cadastral_code": parcel.get("cadastral_code"),
                            "address": parcel.get("address"),
                            "neighborhood": parcel.get("neighborhood"),
                            "registered_area_sqm": registered_area,
                            "detected_area_sqm": round(detected_area, 2),
                            "difference_sqm": round(detected_area - registered_area, 2),
                            "difference_pct": round(diff_pct, 1),
                            "overlap_pct": round(best_overlap, 1),
                            "confidence": best_match.get("confidence", 0),
                            "estimated_iptu_gap_brl": 0.0,
                        })

                # Height mismatch
                detected_height = best_match.get("height_m") or 0
                if detected_height > 0:
                    props = parcel.get("properties") or {}
                    registered_floors = props.get("registered_floors", 0) if isinstance(props, dict) else 0
                    if registered_floors and registered_floors > 0:
                        expected = registered_floors * 3.0
                        if detected_height > expected + height_tolerance_m:
                            discrepancies.append({
                                "type": DiscrepancyType.HEIGHT_MISMATCH,
                                "severity": "medium",
                                "parcel_id": str(parcel["id"]),
                                "detection_id": str(best_match["id"]),
                                "cadastral_code": parcel.get("cadastral_code"),
                                "address": parcel.get("address"),
                                "neighborhood": parcel.get("neighborhood"),
                                "registered_floors": registered_floors,
                                "detected_height_m": round(detected_height, 1),
                                "estimated_iptu_gap_brl": 0.0,
                            })
            else:
                if (parcel.get("registered_built_area_sqm") or 0) > 0:
                    discrepancies.append({
                        "type": DiscrepancyType.DEMOLISHED,
                        "severity": "low",
                        "parcel_id": str(parcel["id"]),
                        "cadastral_code": parcel.get("cadastral_code"),
                        "address": parcel.get("address"),
                        "neighborhood": parcel.get("neighborhood"),
                        "registered_area_sqm": parcel.get("registered_built_area_sqm", 0),
                        "estimated_iptu_gap_brl": 0.0,
                    })

        # ── Step 4: Unregistered buildings ────────────────────────────────
        for detection in detections:
            if detection["id"] not in matched_detections:
                area = detection.get("area_sqm", 0) or 0
                calc = _calculate_iptu(rule_set, {}, 0, area, has_pool=False)
                discrepancies.append({
                    "type": DiscrepancyType.UNREGISTERED,
                    "severity": "high",
                    "detection_id": str(detection["id"]),
                    "detected_area_sqm": round(area, 2),
                    "detected_height_m": detection.get("height_m"),
                    "confidence": detection.get("confidence", 0),
                    "iptu_proposed_brl": calc["iptu_proposed"],
                    "estimated_iptu_gap_brl": calc["iptu_proposed"],
                    "calculation_details": json.dumps(calc),
                })

        # ── Step 5: PERSIST all discrepancies ─────────────────────────────
        total_gap = 0.0
        for d in discrepancies:
            gap = d.get("estimated_iptu_gap_brl", 0) or 0
            total_gap += gap
            await session.execute(text("""
                INSERT INTO discrepancies (
                    project_id, parcel_id, detection_id, analysis_run_id,
                    discrepancy_type, severity,
                    cadastral_code, address, neighborhood, owner_name,
                    registered_area_sqm, detected_area_sqm,
                    difference_sqm, difference_pct, overlap_pct, confidence,
                    detected_height_m, registered_floors,
                    iptu_current_brl, iptu_proposed_brl, estimated_iptu_gap_brl,
                    calculation_details, status
                ) VALUES (
                    :pid, :parcel_id, :detection_id, :run_id,
                    :dtype, :severity,
                    :cadastral_code, :address, :neighborhood, :owner_name,
                    :registered_area, :detected_area,
                    :diff_sqm, :diff_pct, :overlap, :confidence,
                    :height, :floors,
                    :iptu_current, :iptu_proposed, :gap,
                    :calc_details, 'pending'
                )
            """), {
                "pid": project_id,
                "parcel_id": d.get("parcel_id"),
                "detection_id": d.get("detection_id"),
                "run_id": run_id,
                "dtype": d["type"],
                "severity": d.get("severity", "medium"),
                "cadastral_code": d.get("cadastral_code"),
                "address": d.get("address"),
                "neighborhood": d.get("neighborhood"),
                "owner_name": d.get("owner_name"),
                "registered_area": d.get("registered_area_sqm"),
                "detected_area": d.get("detected_area_sqm"),
                "diff_sqm": d.get("difference_sqm"),
                "diff_pct": d.get("difference_pct"),
                "overlap": d.get("overlap_pct"),
                "confidence": d.get("confidence"),
                "height": d.get("detected_height_m"),
                "floors": d.get("registered_floors"),
                "iptu_current": d.get("iptu_current_brl"),
                "iptu_proposed": d.get("iptu_proposed_brl"),
                "gap": gap,
                "calc_details": d.get("calculation_details"),
            })

        # ── Step 6: Update analysis_run with results ──────────────────────
        elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
        summary = {
            "total_detections": len(detections),
            "total_parcels": len(parcels),
            "matched_buildings": len(matched_detections),
            "unregistered_buildings": len(detections) - len(matched_detections),
            "total_discrepancies": len(discrepancies),
            "discrepancies_by_type": {
                dt: len([d for d in discrepancies if d["type"] == dt])
                for dt in [DiscrepancyType.UNREGISTERED, DiscrepancyType.AREA_UNDER_DECLARED,
                           DiscrepancyType.AREA_OVER_DECLARED, DiscrepancyType.DEMOLISHED,
                           DiscrepancyType.HEIGHT_MISMATCH]
            },
        }

        await session.execute(text("""
            UPDATE analysis_runs
            SET status = 'completed',
                summary = :summary,
                total_discrepancies = :total,
                estimated_total_gap_brl = :gap,
                completed_at = NOW(),
                elapsed_seconds = :elapsed
            WHERE id = :rid
        """), {
            "rid": run_id,
            "summary": json.dumps(summary),
            "total": len(discrepancies),
            "gap": round(total_gap, 2),
            "elapsed": round(elapsed, 2),
        })

        await session.commit()

        logger.info(
            f"[IPTU] Analysis complete: {len(discrepancies)} discrepancies persisted, "
            f"gap=R$ {total_gap:,.2f}, elapsed={elapsed:.1f}s"
        )

        return {
            "run_id": run_id,
            "summary": summary,
            "estimated_total_gap_brl": round(total_gap, 2),
            "elapsed_seconds": round(elapsed, 2),
        }


# ── IPTU Calculation Engine ───────────────────────────────────────────────────

async def _load_iptu_rules(session: AsyncSession, municipality_code: str | None, project_id: str) -> dict:
    """Load IPTU rules for the municipality. Falls back to defaults if not found."""
    if municipality_code:
        rs = await session.execute(text("""
            SELECT rs.*, json_agg(json_build_object(
                'zone_name', r.zone_name,
                'land_value', r.land_value_per_sqm_brl,
                'built_value', r.built_value_per_sqm_brl,
                'aliquot_pct', r.aliquot_pct,
                'depreciation_rate', r.depreciation_rate_per_year
            )) as zones
            FROM iptu_rule_sets rs
            LEFT JOIN iptu_rules r ON r.rule_set_id = rs.id
            WHERE rs.municipality_code = :code AND rs.is_active = true
            GROUP BY rs.id
        """), {"code": municipality_code})
        row = rs.mappings().first()
        if row:
            return dict(row)

    # Try to infer from project's city
    proj = await session.execute(text(
        "SELECT city, state FROM projects WHERE id = :pid"
    ), {"pid": project_id})
    proj_row = proj.mappings().first()

    if proj_row and proj_row.get("city"):
        rs = await session.execute(text("""
            SELECT rs.*, json_agg(json_build_object(
                'zone_name', r.zone_name,
                'land_value', r.land_value_per_sqm_brl,
                'built_value', r.built_value_per_sqm_brl,
                'aliquot_pct', r.aliquot_pct,
                'depreciation_rate', r.depreciation_rate_per_year
            )) as zones
            FROM iptu_rule_sets rs
            LEFT JOIN iptu_rules r ON r.rule_set_id = rs.id
            WHERE rs.municipality_name ILIKE :city AND rs.is_active = true
            GROUP BY rs.id
            LIMIT 1
        """), {"city": f"%{proj_row['city']}%"})
        row = rs.mappings().first()
        if row:
            return dict(row)

    # Default fallback
    return {
        "municipality_name": "Padrão",
        "default_land_value_per_sqm": 50.0,
        "default_built_value_per_sqm": 800.0,
        "default_aliquot_pct": 1.0,
        "pool_surcharge_pct": 20.0,
        "zones": None,
    }


def _calculate_iptu(
    rule_set: dict, parcel: dict,
    registered_area: float, detected_area: float,
    has_pool: bool = False, height_m: float | None = None,
) -> dict:
    """Calculate IPTU values using the municipality's rule set."""
    zone = (parcel.get("iptu_zone") or "residencial").lower()
    zones = rule_set.get("zones")

    # Find zone-specific rates
    built_val = rule_set.get("default_built_value_per_sqm", 800.0)
    aliquot = rule_set.get("default_aliquot_pct", 1.0)

    if zones and isinstance(zones, list):
        for z in zones:
            if isinstance(z, dict) and z.get("zone_name") == zone:
                built_val = z.get("built_value", built_val)
                aliquot = z.get("aliquot_pct", aliquot)
                break

    # Calculate
    venal_registered = registered_area * built_val
    venal_detected = detected_area * built_val

    iptu_current = venal_registered * (aliquot / 100)
    iptu_proposed = venal_detected * (aliquot / 100)

    if has_pool:
        surcharge = rule_set.get("pool_surcharge_pct", 20.0)
        iptu_proposed *= (1 + surcharge / 100)

    gap = max(0, iptu_proposed - iptu_current)

    return {
        "built_value_per_sqm": built_val,
        "aliquot_pct": aliquot,
        "venal_registered": round(venal_registered, 2),
        "venal_detected": round(venal_detected, 2),
        "iptu_current": round(iptu_current, 2),
        "iptu_proposed": round(iptu_proposed, 2),
        "gap": round(gap, 2),
        "pool_surcharge_applied": has_pool,
    }


# ── Query helpers ─────────────────────────────────────────────────────────────

async def _get_building_detections(session: AsyncSession, project_id: str) -> list[dict]:
    try:
        result = await session.execute(text("""
            SELECT ad.id, ad.area_sqm, ad.perimeter_m, ad.confidence,
                   (ad.properties->>'height_m')::float as height_m,
                   ST_AsGeoJSON(ad.polygon) as geojson
            FROM ai_detections ad
            JOIN flight_assets fa ON fa.id = ad.flight_asset_id
            JOIN flights f ON f.id = fa.flight_id
            WHERE f.project_id = :pid AND ad.detection_class = 'building'
        """), {"pid": project_id})
        return [dict(r) for r in result.mappings().all()]
    except Exception as e:
        logger.warning(f"[IPTU] Failed to fetch detections: {e}")
        return []


async def _get_overlapping_parcels(session: AsyncSession, project_id: str) -> list[dict]:
    try:
        result = await session.execute(text("""
            SELECT p.id, p.cadastral_code, p.address, p.neighborhood,
                   p.owner_name, p.registered_area_sqm, p.registered_built_area_sqm,
                   p.land_use, p.iptu_zone, p.iptu_value_current_brl,
                   p.properties,
                   ST_AsGeoJSON(p.polygon) as geojson
            FROM parcels p
            WHERE ST_Intersects(
                p.polygon,
                (SELECT ST_MakeEnvelope(
                    bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, 4326
                ) FROM projects WHERE id = :pid)
            )
        """), {"pid": project_id})
        return [dict(r) for r in result.mappings().all()]
    except Exception as e:
        logger.warning(f"[IPTU] Failed to fetch parcels: {e}")
        return []


async def _compute_overlap(session: AsyncSession, detection_id: str, parcel_id: str) -> float:
    try:
        result = await session.execute(text("""
            SELECT
                CASE
                    WHEN ST_Area(ad.polygon::geography) > 0
                    THEN ST_Area(ST_Intersection(ad.polygon, p.polygon)::geography)
                         / ST_Area(ad.polygon::geography) * 100
                    ELSE 0
                END as overlap_pct
            FROM ai_detections ad, parcels p
            WHERE ad.id = :did AND p.id = :pid
                AND ST_Intersects(ad.polygon, p.polygon)
        """), {"did": str(detection_id), "pid": str(parcel_id)})
        row = result.scalar()
        return float(row) if row else 0.0
    except Exception:
        return 0.0
