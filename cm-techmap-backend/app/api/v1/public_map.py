"""
CM TECHMAP — Public Map API
Endpoints for the citizen-facing public map consultation.
NO AUTHENTICATION REQUIRED — these are intentionally public.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_public_db

logger = logging.getLogger("cm_techmap.api.public_map")

router = APIRouter(prefix="/public/map", tags=["Public Map"])


@router.get("/config")
async def get_map_config(db: AsyncSession = Depends(get_public_db)):
    """
    Get public map configuration: available projects with bounds,
    tile URLs, and zoom levels. No authentication required.
    """
    result = await db.execute(text("""
        SELECT p.id, p.name, p.city, p.state,
               p.bbox_min_lon, p.bbox_min_lat, p.bbox_max_lon, p.bbox_max_lat,
               p.updated_at
        FROM projects p
        WHERE p.is_active = TRUE
          AND p.status = 'concluido'
          AND p.bbox_min_lon IS NOT NULL
        ORDER BY p.updated_at DESC
    """))

    projects = []
    for r in result.mappings().all():
        proj = dict(r)
        proj["id"] = str(proj["id"])
        if proj.get("updated_at"):
            proj["updated_at"] = proj["updated_at"].isoformat()
        proj["bounds"] = [
            [proj.pop("bbox_min_lon"), proj.pop("bbox_min_lat")],
            [proj.pop("bbox_max_lon"), proj.pop("bbox_max_lat")],
        ]
        projects.append(proj)

    return {
        "projects": projects,
        "total": len(projects),
        "tile_url_template": "/api/v1/tiles/raster/{asset_id}/tiles/{z}/{x}/{y}.png",
        "default_zoom": 15,
        "max_zoom": 22,
    }


@router.get("/search")
async def search_parcels(
    q: str = Query(..., min_length=2, description="Address or cadastral code"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_public_db),
):
    """
    Search parcels by address or cadastral code.
    Returns basic public info (no tax data). No auth required.
    """
    result = await db.execute(text("""
        SELECT p.id, p.cadastral_code, p.address, p.neighborhood,
               p.land_use, p.registered_area_sqm,
               ST_X(ST_Centroid(p.polygon)) as lng,
               ST_Y(ST_Centroid(p.polygon)) as lat,
               ST_AsGeoJSON(ST_Envelope(p.polygon)) as bounds_geojson
        FROM parcels p
        WHERE p.cadastral_code ILIKE :q
           OR p.address ILIKE :q
           OR p.neighborhood ILIKE :q
        ORDER BY
            CASE WHEN p.cadastral_code ILIKE :exact THEN 0 ELSE 1 END,
            p.address
        LIMIT :limit
    """), {"q": f"%{q}%", "exact": q, "limit": limit})

    results = []
    for r in result.mappings().all():
        item = dict(r)
        item["id"] = str(item["id"])
        if item.get("bounds_geojson"):
            item["bounds"] = json.loads(item.pop("bounds_geojson"))
        else:
            item.pop("bounds_geojson", None)
        # Do NOT include tax values — this is public
        results.append(item)

    return {"results": results, "count": len(results)}


@router.get("/parcel/{cadastral_code}")
async def get_public_parcel(
    cadastral_code: str,
    db: AsyncSession = Depends(get_public_db),
):
    """
    Get public data for a specific parcel by cadastral code.
    Returns area, land use, location — but NO tax values or owner details.
    """
    result = await db.execute(text("""
        SELECT p.cadastral_code, p.address, p.neighborhood,
               p.registered_area_sqm, p.land_use,
               ST_AsGeoJSON(p.polygon) as geojson,
               ST_X(ST_Centroid(p.polygon)) as lng,
               ST_Y(ST_Centroid(p.polygon)) as lat,
               p.imported_at
        FROM parcels p
        WHERE p.cadastral_code = :code
    """), {"code": cadastral_code})

    r = result.mappings().first()
    if not r:
        raise HTTPException(404, "Lote não encontrado")

    item = dict(r)
    if item.get("geojson"):
        item["geometry"] = json.loads(item.pop("geojson"))
    if item.get("imported_at"):
        item["imported_at"] = item["imported_at"].isoformat()

    return item


@router.get("/stats")
async def get_public_stats(db: AsyncSession = Depends(get_public_db)):
    """
    Public statistics: total mapped area, neighborhoods, last flight date.
    No auth required.
    """
    try:
        result = await db.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM public.projects WHERE is_active = TRUE) as total_projects,
                (SELECT COALESCE(SUM(area_sqm), 0) FROM public.projects WHERE is_active = TRUE) as total_area_sqm,
                (SELECT COUNT(DISTINCT neighborhood) FROM public.parcels WHERE neighborhood IS NOT NULL) as neighborhoods,
                (SELECT COUNT(*) FROM public.parcels) as total_parcels,
                (SELECT MAX(f.created_at) FROM public.flights f) as last_flight_date
        """))

        r = result.mappings().first()
        stats = dict(r) if r else {}
        if stats.get("last_flight_date"):
            stats["last_flight_date"] = stats["last_flight_date"].isoformat()
        if stats.get("total_area_sqm"):
            stats["total_area_km2"] = round(float(stats["total_area_sqm"]) / 1_000_000, 2)

        return stats
    except Exception as e:
        logger.warning(f"Public stats query failed: {e}")
        return {"total_projects": 0, "total_parcels": 0, "neighborhoods": 0}

