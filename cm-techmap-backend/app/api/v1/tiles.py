"""
CM TECHMAP — Tile Serving Routes (TiTiler + Martin proxy)
Serves raster tiles from COG orthomosaics via TiTiler and vector tiles via Martin.
Uses `flight_assets` table for asset lookups.
"""

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.dependencies import get_db, require_viewer

router = APIRouter(prefix="/tiles", tags=["Tiles"])
settings = get_settings()
logger = logging.getLogger(__name__)


# ── Helper ────────────────────────────────────────────────────────────────────

def _valid_uuid(value: str) -> str:
    """UUID malformado na URL não pode virar 500 — vira 404 explícito."""
    import uuid as _uuid

    try:
        return str(_uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Identificador de asset inválido")


async def _resolve_asset(
    asset_id: str, db: AsyncSession
) -> dict[str, Any]:
    """
    Look up a flight_asset by ID and return its metadata.
    Falls back to searching by file_key if UUID lookup fails.
    """
    # Try UUID lookup first
    asset_id = _valid_uuid(asset_id)
    result = await db.execute(text(
        "SELECT id, file_key, bucket_name, resolution_cm, "
        "crs_epsg, bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, "
        "file_size_bytes, metadata_json "
        "FROM flight_assets WHERE id = CAST(:id AS uuid) AND asset_type = 'orthomosaic'"
    ), {"id": asset_id})
    row = result.fetchone()

    if not row:
        # Try by file_key (for backward compat)
        result = await db.execute(text(
            "SELECT id, file_key, bucket_name, resolution_cm, "
            "crs_epsg, bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, "
            "file_size_bytes, metadata_json "
            "FROM flight_assets WHERE file_key = :fk AND asset_type = 'orthomosaic'"
        ), {"fk": asset_id})
        row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Orthomosaic asset not found")

    return {
        "id": str(row[0]),
        "file_key": row[1].lstrip("/") if row[1] else row[1],
        "bucket_name": row[2] or settings.minio_bucket_orthomosaics,
        "resolution_cm": row[3],
        "crs_epsg": row[4],
        "bbox_min_lon": row[5],
        "bbox_min_lat": row[6],
        "bbox_max_lon": row[7],
        "bbox_max_lat": row[8],
        "file_size_bytes": row[9],
        "metadata_json": row[10],
    }


# ── Asset Discovery ──────────────────────────────────────────────────────────

@router.get("/raster/assets")
async def list_raster_assets(
    project_id: str = Query(None),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    List all available orthomosaic raster assets, optionally filtered by project.
    Returns asset IDs, bounds, and resolution for map overlay.
    """
    if project_id:
        result = await db.execute(text(
            "SELECT fa.id, fa.file_key, fa.resolution_cm, "
            "fa.bbox_min_lon, fa.bbox_min_lat, fa.bbox_max_lon, fa.bbox_max_lat, "
            "fa.file_size_bytes, fa.crs_epsg, f.project_id, fa.created_at "
            "FROM flight_assets fa "
            "JOIN flights f ON fa.flight_id = f.id "
            "WHERE f.project_id = CAST(:pid AS uuid) AND fa.asset_type = 'orthomosaic' "
            "ORDER BY fa.created_at DESC"
        ), {"pid": project_id})
    else:
        result = await db.execute(text(
            "SELECT fa.id, fa.file_key, fa.resolution_cm, "
            "fa.bbox_min_lon, fa.bbox_min_lat, fa.bbox_max_lon, fa.bbox_max_lat, "
            "fa.file_size_bytes, fa.crs_epsg, f.project_id, fa.created_at "
            "FROM flight_assets fa "
            "JOIN flights f ON fa.flight_id = f.id "
            "WHERE fa.asset_type = 'orthomosaic' "
            "ORDER BY fa.created_at DESC "
            "LIMIT 50"
        ))

    assets = []
    for r in result.fetchall():
        assets.append({
            # `asset_id` is the canonical name across the API (flight assets,
            # /raster/{id}/info, /terrain/*, /models/*). `id` is kept as an
            # alias so older clients keep working.
            "asset_id": str(r[0]),
            "id": str(r[0]),
            "file_key": r[1],
            "resolution_cm": r[2],
            "bounds": {
                "west": r[3], "south": r[4],
                "east": r[5], "north": r[6],
            } if r[3] is not None else None,
            "file_size_bytes": r[7],
            "crs_epsg": r[8],
            "project_id": str(r[9]),
            "created_at": str(r[10]),
            "tilejson_url": f"/api/v1/tiles/raster/{r[0]}/tilejson.json",
        })

    return {"assets": assets, "total": len(assets)}


# ── TiTiler (Raster Tiles) ────────────────────────────────────────────────────

@router.get("/raster/{asset_id}/info")
async def get_raster_info(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Get orthomosaic metadata, bounds, and TiTiler info for map overlay."""
    asset = await _resolve_asset(asset_id, db)

    # Build S3 URL for TiTiler (uses AWS_S3_ENDPOINT env var)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"

    # Get info from TiTiler
    titiler_info = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.titiler_url}/cog/info",
                params={"url": s3_url},
            )
            if resp.status_code == 200:
                titiler_info = resp.json()
    except Exception as e:
        logger.warning(f"TiTiler info failed: {e}")

    return {
        "asset_id": asset["id"],
        "file_key": asset["file_key"],
        "resolution_cm": asset["resolution_cm"],
        "crs_epsg": asset["crs_epsg"],
        "file_size_bytes": asset["file_size_bytes"],
        "bounds": {
            "west": asset["bbox_min_lon"],
            "south": asset["bbox_min_lat"],
            "east": asset["bbox_max_lon"],
            "north": asset["bbox_max_lat"],
        },
        "tilejson_url": f"/api/v1/tiles/raster/{asset['id']}/tilejson.json",
        "titiler_info": titiler_info,
    }


@router.get("/raster/{asset_id}/tilejson.json")
async def get_raster_tilejson(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Generate TileJSON for an orthomosaic via TiTiler."""
    asset = await _resolve_asset(asset_id, db)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.titiler_url}/cog/WebMercatorQuad/tilejson.json",
                params={"url": s3_url},
            )
            resp.raise_for_status()
            tilejson = resp.json()

            # Rewrite tile URLs to go through our API
            tilejson["tiles"] = [
                f"/api/v1/tiles/raster/{asset['id']}/{{z}}/{{x}}/{{y}}.png"
            ]

            # Inject bounds from our DB if TiTiler didn't provide them
            if asset["bbox_min_lon"] is not None:
                tilejson["bounds"] = [
                    asset["bbox_min_lon"], asset["bbox_min_lat"],
                    asset["bbox_max_lon"], asset["bbox_max_lat"],
                ]

            return tilejson
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"TiTiler unavailable: {e}")


@router.get("/raster/{asset_id}/{z}/{x}/{y}.png")
async def get_raster_tile(
    asset_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Proxy a raster tile request to TiTiler."""
    asset = await _resolve_asset(asset_id, db)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}",
                params={"url": s3_url},
            )
            if resp.status_code == 200:
                return Response(
                    content=resp.content,
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
            elif resp.status_code == 404:
                # Empty tile — return transparent PNG
                raise HTTPException(status_code=204)
            else:
                raise HTTPException(status_code=resp.status_code)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"TiTiler error: {e}")


# ── Terrain Tiles (DSM → RGB-encoded elevation for MapLibre 3D terrain) ───────

# Pre-generated flat DEM tile: 256×256 PNG with Terrarium elevation = 0
# Terrarium formula: elevation = (R * 256 + G + B / 256) - 32768
# For elevation = 0: value = 32768 → R = 128, G = 0, B = 0
_FLAT_DEM_TILE_CACHE: bytes | None = None


def _normalize_terrain_tile(
    png_bytes: bytes,
    base_elevation: float,
    max_relative_elev: float,
    dsm_source: str,
) -> bytes:
    """
    Rebase a Terrarium-encoded elevation tile to relative heights.

    Pure CPU (PIL + numpy + optional scipy) — must be called through
    run_in_threadpool so it never blocks the API event loop.

    Terrarium formula: elevation = (R * 256 + G + B / 256) - 32768
    """
    import io

    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    arr = np.array(img, dtype=np.float64)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    elevation = (r * 256.0 + g + b / 256.0) - 32768.0

    # Subtract base elevation → relative heights
    elevation = np.clip(elevation - base_elevation, 0.0, max_relative_elev)

    # Synthetic DSMs carry pixel-level noise that reads as jagged spikes
    if dsm_source == "synthetic":
        from scipy.ndimage import gaussian_filter
        elevation = gaussian_filter(elevation, sigma=2.0)

    val = elevation + 32768.0
    out = np.stack([
        np.floor(val / 256.0).astype(np.uint8),
        np.floor(val % 256.0).astype(np.uint8),
        np.floor((val - np.floor(val)) * 256.0).astype(np.uint8),
    ], axis=-1)

    buf = io.BytesIO()
    Image.fromarray(out, "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _flat_dem_tile() -> Response:
    """Return a valid 256×256 flat Terrarium DEM PNG (elevation=0 everywhere).

    MapLibre's raster-dem source requires consistent 256×256 tiles.
    If a tile outside the DSM bounds returns an error or empty body,
    MapLibre throws 'dem dimension mismatch' and renders white.
    This function returns a valid flat tile to keep the map working.
    """
    global _FLAT_DEM_TILE_CACHE
    if _FLAT_DEM_TILE_CACHE is None:
        import io

        import numpy as np
        from PIL import Image

        # Elevation 0 → Terrarium value = 32768 → R=128, G=0, B=0
        flat = np.zeros((256, 256, 3), dtype=np.uint8)
        flat[:, :, 0] = 128  # R channel
        img = Image.fromarray(flat, "RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        _FLAT_DEM_TILE_CACHE = buf.getvalue()

    return Response(
        content=_FLAT_DEM_TILE_CACHE,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},  # Cache for 7 days
    )


async def _resolve_dsm_asset(
    asset_id: str, db: AsyncSession
) -> dict[str, Any]:
    """Look up a DSM/DTM flight_asset by ID."""
    asset_id = _valid_uuid(asset_id)
    result = await db.execute(text(
        "SELECT id, file_key, bucket_name, resolution_cm, "
        "crs_epsg, bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, "
        "file_size_bytes, metadata_json "
        "FROM flight_assets WHERE id = CAST(:id AS uuid) AND asset_type = 'dsm'"
    ), {"id": asset_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="DSM asset not found")

    return {
        "id": str(row[0]),
        "file_key": row[1].lstrip("/") if row[1] else row[1],
        "bucket_name": row[2] or settings.minio_bucket_elevation_models,
        "resolution_cm": row[3],
        "crs_epsg": row[4],
        "bbox_min_lon": row[5],
        "bbox_min_lat": row[6],
        "bbox_max_lon": row[7],
        "bbox_max_lat": row[8],
        "file_size_bytes": row[9],
        "metadata_json": row[10],
    }


@router.get("/terrain/{asset_id}/info")
async def get_terrain_info(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Get DSM terrain metadata for 3D terrain rendering."""
    asset = await _resolve_dsm_asset(asset_id, db)

    return {
        "asset_id": asset["id"],
        "file_key": asset["file_key"],
        "resolution_cm": asset["resolution_cm"],
        "crs_epsg": asset["crs_epsg"],
        "file_size_bytes": asset["file_size_bytes"],
        "bounds": {
            "west": asset["bbox_min_lon"],
            "south": asset["bbox_min_lat"],
            "east": asset["bbox_max_lon"],
            "north": asset["bbox_max_lat"],
        },
        "terrain_tile_url": f"/api/v1/tiles/terrain/{asset['id']}/{{z}}/{{x}}/{{y}}.png",
        "encoding": "terrarium",
    }


@router.get("/terrain/{asset_id}/{z}/{x}/{y}.png")
async def get_terrain_tile(
    asset_id: str,
    z: int,
    x: int,
    y: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Serve terrain tiles as RGB-encoded elevation data (Terrarium encoding).
    Used by MapLibre's raster-dem source for 3D terrain rendering.

    CRITICAL: DSM assets often contain absolute elevations (e.g. 800m above
    sea level for Goiás, Brazil). MapLibre renders Terrarium values directly
    as vertical displacement — so absolute elevations produce enormous walls.

    Solution: we normalize by decoding the Terrarium tile, subtracting the
    base elevation from asset metadata, then re-encoding. This produces
    relative elevations (0–N meters) suitable for terrain rendering.

    Terrarium formula: elevation = (R * 256 + G + B / 256) - 32768
    """
    asset = await _resolve_dsm_asset(asset_id, db)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"

    # Get base elevation to subtract (normalize DSM)
    base_elevation = 0.0
    meta = asset.get("metadata_json") or {}
    if meta.get("min_elevation_m") is not None:
        base_elevation = float(meta["min_elevation_m"])

    # Determine DSM source — synthetic needs extra smoothing
    dsm_source = meta.get("dsm_source", "synthetic")
    # Max relative elevation: real DSM can go higher, synthetic must be clamped
    max_relative_elev = 200.0 if dsm_source == "real" else 15.0

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}",
                params={
                    "url": s3_url,
                    "algorithm": "terrarium",
                },
            )
            if resp.status_code == 200:
                # Normalize the tile: subtract base elevation + apply source-specific processing
                if base_elevation > 10.0 or dsm_source == "synthetic":
                    try:
                        # Decode/filter/re-encode is pure CPU work. Panning the
                        # map fires hundreds of tile requests; doing this inline
                        # would block the event loop of the single API worker
                        # and stall every other request.
                        normalized = await run_in_threadpool(
                            _normalize_terrain_tile,
                            resp.content, base_elevation, max_relative_elev, dsm_source,
                        )
                        return Response(
                            content=normalized,
                            media_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"},
                        )
                    except Exception:
                        # If normalization fails, serve the original tile
                        pass

                return Response(
                    content=resp.content,
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
            else:
                # Tile outside DSM bounds — return a flat Terrarium DEM tile
                # (elevation=0 → R=128, G=0, B=0 in Terrarium encoding)
                # This prevents MapLibre "dem dimension mismatch" errors
                return _flat_dem_tile()
    except httpx.HTTPError:
        # Network/TiTiler error — return flat tile to keep the map functional
        return _flat_dem_tile()


@router.get("/terrain/by-project/{project_id}")
async def get_terrain_for_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Find the DSM terrain asset associated with a project.
    Looks for DSM assets in flights belonging to this project.
    Falls back to MinIO convention-based lookup.
    """
    result = await db.execute(text(
        "SELECT fa.id, fa.file_key, fa.bucket_name, fa.resolution_cm, "
        "fa.bbox_min_lon, fa.bbox_min_lat, fa.bbox_max_lon, fa.bbox_max_lat, "
        "fa.metadata_json "
        "FROM flight_assets fa "
        "JOIN flights f ON fa.flight_id = f.id "
        "WHERE f.project_id = CAST(:pid AS uuid) AND fa.asset_type = 'dsm' "
        "ORDER BY fa.created_at DESC LIMIT 1"
    ), {"pid": project_id})
    row = result.fetchone()

    if row:
        meta = row[8] or {}
        dsm_source = meta.get("dsm_source", "synthetic")
        return {
            "dsm": {
                "asset_id": str(row[0]),
                "file_key": row[1],
                "bounds": {
                    "west": row[4], "south": row[5],
                    "east": row[6], "north": row[7],
                } if row[4] is not None else None,
                "terrain_tile_url": f"/api/v1/tiles/terrain/{row[0]}/{{z}}/{{x}}/{{y}}.png",
                "encoding": "terrarium",
                "dsm_source": dsm_source,
                "elevation": {
                    "min_m": meta.get("min_elevation_m"),
                    "max_m": meta.get("max_elevation_m"),
                    "mean_m": meta.get("mean_elevation_m"),
                } if meta else None,
            }
        }

    # Fallback: check if DSM exists in MinIO using convention-based path
    minio_ep = settings.minio_endpoint
    if not minio_ep.startswith("http"):
        minio_ep = f"http://{minio_ep}"
    bucket = "elevation-models"

    for prefix in [f"/{project_id}/dsm/", f"{project_id}/dsm/"]:
        dsm_url = f"{minio_ep}/{bucket}{prefix}dsm_raw_cog.tif"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.head(dsm_url, timeout=5.0)
                if resp.status_code == 200:
                    # Get bounds from orthomosaic asset in flight_assets
                    bounds_result = await db.execute(text(
                        "SELECT fa.bbox_min_lon, fa.bbox_min_lat, fa.bbox_max_lon, fa.bbox_max_lat "
                        "FROM flight_assets fa "
                        "JOIN flights f ON fa.flight_id = f.id "
                        "WHERE f.project_id = CAST(:pid AS uuid) AND fa.asset_type = 'orthomosaic' "
                        "ORDER BY fa.created_at DESC LIMIT 1"
                    ), {"pid": project_id})
                    brow = bounds_result.fetchone()
                    bounds_dict = {
                        "west": brow[0], "south": brow[1],
                        "east": brow[2], "north": brow[3],
                    } if brow else None

                    return {
                        "dsm": {
                            "asset_id": None,
                            "file_key": f"{prefix}dsm_raw_cog.tif",
                            "bounds": bounds_dict,
                            "minio_url": dsm_url,
                            "encoding": "terrarium",
                            "status": "available_no_tiles",
                        }
                    }
        except httpx.HTTPError:
            continue

    return {"dsm": None}


@router.get("/terrain/by-orthomosaic/{orthomosaic_id}")
async def get_terrain_for_orthomosaic(
    orthomosaic_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Find the DSM terrain asset associated with a specific orthomosaic.
    Looks for DSM in the same flight as the orthomosaic.
    """
    # First, find the flight_id of the orthomosaic
    result = await db.execute(text(
        "SELECT flight_id FROM flight_assets "
        "WHERE id = CAST(:oid AS uuid) AND asset_type = 'orthomosaic'"
    ), {"oid": orthomosaic_id})
    ortho_row = result.fetchone()

    if not ortho_row:
        return {"dsm": None}

    flight_id = str(ortho_row[0])

    # Now find DSM for the same flight
    result = await db.execute(text(
        "SELECT fa.id, fa.file_key, fa.bucket_name, fa.resolution_cm, "
        "fa.bbox_min_lon, fa.bbox_min_lat, fa.bbox_max_lon, fa.bbox_max_lat, "
        "fa.metadata_json "
        "FROM flight_assets fa "
        "WHERE fa.flight_id = CAST(:fid AS uuid) AND fa.asset_type = 'dsm' "
        "ORDER BY fa.created_at DESC LIMIT 1"
    ), {"fid": flight_id})
    row = result.fetchone()

    if not row:
        return {"dsm": None}

    meta = row[8] or {}
    dsm_source = meta.get("dsm_source", "synthetic")

    return {
        "dsm": {
            "asset_id": str(row[0]),
            "file_key": row[1],
            "bounds": {
                "west": row[4], "south": row[5],
                "east": row[6], "north": row[7],
            } if row[4] is not None else None,
            "terrain_tile_url": f"/api/v1/tiles/terrain/{row[0]}/{{z}}/{{x}}/{{y}}.png",
            "encoding": "terrarium",
            "dsm_source": dsm_source,
            "elevation": {
                "min_m": meta.get("min_elevation_m"),
                "max_m": meta.get("max_elevation_m"),
                "mean_m": meta.get("mean_elevation_m"),
            } if meta else None,
        }
    }



# ── 3D Building Footprints (GeoJSON for fill-extrusion) ───────────────────

@router.get("/buildings/by-project/{project_id}")
async def get_buildings_for_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Serve building footprints GeoJSON for 3D fill-extrusion rendering.
    Footprints are extracted from the DSM and stored in MinIO.
    Falls back to direct MinIO lookup if no DB record exists.
    """
    buildings_key = None
    bucket = "elevation-models"

    # Try DB lookup first
    result = await db.execute(text(
        "SELECT fa.file_key, fa.bucket_name "
        "FROM flight_assets fa "
        "JOIN flights f ON fa.flight_id = f.id "
        "WHERE f.project_id = CAST(:pid AS uuid) AND fa.asset_type = 'dsm' "
        "ORDER BY fa.created_at DESC LIMIT 1"
    ), {"pid": project_id})
    row = result.fetchone()

    if row:
        dsm_key = row[0]
        bucket = row[1] or "elevation-models"
        buildings_key = dsm_key.rsplit("/dsm/", 1)[0] + "/buildings/footprints.geojson"
    else:
        # Fallback: try direct MinIO path convention
        # Convention: {project_id}/buildings/footprints.geojson
        # or /{project_id}/buildings/footprints.geojson (with leading slash)
        buildings_key = f"{project_id}/buildings/footprints.geojson"

    # Try both path variants
    minio_ep = settings.minio_endpoint
    if not minio_ep.startswith("http"):
        minio_ep = f"http://{minio_ep}"

    for key_variant in [buildings_key, f"/{buildings_key}", buildings_key.lstrip("/")]:
        minio_url = f"{minio_ep}/{bucket}/{key_variant}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(minio_url, timeout=15.0)
                if resp.status_code == 200:
                    return Response(
                        content=resp.content,
                        media_type="application/geo+json",
                        headers={"Cache-Control": "public, max-age=3600"},
                    )
        except httpx.HTTPError:
            continue

    # No GeoJSON found in MinIO — determine if buildings are still being generated
    # or genuinely not available for this project.
    if row:
        # DSM asset exists in DB but GeoJSON not in MinIO yet → still processing
        return Response(
            content='{"type":"FeatureCollection","features":[],"status":"processing"}',
            media_type="application/geo+json",
            status_code=202,
            headers={"Retry-After": "30"},
        )

    # No DSM at all — buildings genuinely not available (no processing has run)
    return Response(
        content='{"type":"FeatureCollection","features":[],"status":"not_available"}',
        media_type="application/geo+json",
        status_code=200,
    )


# ── DSM VISUAL (colorizado) + medições exatas ─────────────────────────────────
# O DSM já alimentava o terreno 3D (tiles Terrarium), mas não tinha camada
# VISUAL: o usuário baixava o TIFF e precisava do QGIS para enxergar. Estes
# endpoints entregam o DSM colorizado por tiles (COG lossless via TiTiler,
# reescala pelos valores REAIS do raster) e a elevação EXATA de qualquer
# ponto clicado — o mesmo valor que o QGIS mostraria, sem arredondar.


@router.get("/dsm/{asset_id}/visual-info")
async def get_dsm_visual_info(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Estatísticas exatas do DSM (min/máx/média/desvio) + bounds — base da
    reescala visual e da legenda."""
    asset = await _resolve_dsm_asset(asset_id, db)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            stats_resp = await client.get(
                f"{settings.titiler_url}/cog/statistics", params={"url": s3_url}
            )
            stats_resp.raise_for_status()
            stats = stats_resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"TiTiler indisponível: {e}")

    band = stats.get("b1") or next(iter(stats.values()), {})
    vmin, vmax = band.get("min"), band.get("max")
    p2, p98 = band.get("percentile_2"), band.get("percentile_98")

    # O pipeline publica o DSM NORMALIZADO (altura acima do solo). Chamar
    # isso de "elevação" induz o operador a ler cota absoluta. A detecção é
    # explícita: cota máxima baixa + mínima ~0 ⇒ nDSM.
    is_normalized = (
        vmax is not None and vmin is not None
        and vmax < 100.0 and abs(vmin) < 5.0
    )
    # Faixa de COR robusta: min/max são dominados por outliers (um poste de
    # 18 m achata todo o casario em 3 m). p2–p98 dá contraste real; os
    # extremos continuam no retorno para a legenda ser honesta.
    lo = p2 if p2 is not None else vmin
    hi = p98 if p98 is not None else vmax
    if lo is not None and hi is not None and hi - lo < 0.5:
        lo, hi = vmin, vmax  # faixa degenerada: volta ao total

    return {
        "asset_id": asset["id"],
        "resolution_cm": asset["resolution_cm"],
        "bounds": {
            "west": asset["bbox_min_lon"], "south": asset["bbox_min_lat"],
            "east": asset["bbox_max_lon"], "north": asset["bbox_max_lat"],
        },
        # Valores EXATOS do raster — sem arredondamento no servidor
        "elevation": {
            "min_m": vmin,
            "max_m": vmax,
            "mean_m": band.get("mean"),
            "std_m": band.get("std"),
            "percentile_2": p2,
            "percentile_98": p98,
        },
        "is_normalized": is_normalized,
        "measure_label": (
            "Altura acima do solo (nDSM)" if is_normalized
            else "Elevação absoluta (DSM)"
        ),
        # O cliente usa esta faixa para pedir os tiles E desenhar a legenda —
        # cor e número nunca divergem.
        "recommended_rescale": {"min": lo, "max": hi},
        "nodata": 0 if is_normalized else None,
        "available_modes": ["height", "hillshade", "contours"],
    }


@router.get("/dsm/{asset_id}/visual/{z}/{x}/{y}.png")
async def get_dsm_visual_tile(
    asset_id: str,
    z: int,
    x: int,
    y: int,
    rescale: str | None = None,
    colormap: str = "viridis",
    mode: str = "height",
    nodata: float | None = None,
    resampling: str = "bilinear",
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Tile visual do DSM. `rescale=min,max` vem do visual-info — a MESMA faixa
    vira legenda, então cor e valor nunca divergem.

    Três lições de renderização, todas visíveis a olho nu antes da correção:
    - `nodata` PRECISA ser transparente: o retângulo do voo é ~59% vazio e,
      pintado, virava um borrão sólido sobre o bairro;
    - a faixa min–max é sequestrada por outliers (um poste de 18 m achata
      todo o casario) — a faixa robusta p2–p98 devolve o contraste;
    - `nearest` num raster de 2,31 cm/px visto de longe vira chuvisco:
      cada pixel de tela amostra 1 de ~50. O dado permanece exato (a
      medição lê o COG direto); a EXIBIÇÃO usa reamostragem suave.

    `mode`: height (colorizado) | hillshade (sombreado de relevo) | contours.
    """
    asset = await _resolve_dsm_asset(asset_id, db)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"
    params: dict[str, Any] = {"url": s3_url, "resampling": resampling}
    if nodata is not None:
        params["nodata"] = nodata
    if mode in ("hillshade", "contours"):
        params["algorithm"] = mode
    else:
        params["colormap_name"] = colormap
        if rescale:
            params["rescale"] = rescale
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}",
                params=params,
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"TiTiler error: {e}")
    if resp.status_code == 200:
        return Response(
            content=resp.content,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    if resp.status_code in (404, 204):
        raise HTTPException(status_code=204)
    raise HTTPException(status_code=resp.status_code)


@router.get("/dsm/{asset_id}/point")
async def get_dsm_point_elevation(
    asset_id: str,
    lon: float,
    lat: float,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Elevação EXATA no ponto clicado — leitura do pixel do COG via TiTiler,
    valor bruto do raster (o mesmo que o QGIS mostra), sem arredondamento."""
    asset = await _resolve_dsm_asset(asset_id, db)
    s3_url = f"s3://{asset['bucket_name']}/{asset['file_key']}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{settings.titiler_url}/cog/point/{lon},{lat}", params={"url": s3_url}
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"TiTiler error: {e}")
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Ponto fora da cobertura do DSM")
    data = resp.json()
    values = data.get("values") or []
    # Voo cobre um polígono irregular dentro do bbox: ~59% do retângulo é
    # nodata. Distinguir "fora do voo" de "erro" evita que o operador ache
    # que o sistema falhou ao clicar numa área não sobrevoada.
    inside_bbox = (
        asset["bbox_min_lon"] is not None
        and asset["bbox_min_lon"] <= lon <= asset["bbox_max_lon"]
        and asset["bbox_min_lat"] <= lat <= asset["bbox_max_lat"]
    )
    if not values or values[0] is None or values[0] == 0:
        raise HTTPException(
            status_code=404,
            detail=(
                "Ponto dentro da área do projeto, mas SEM cobertura do voo "
                "(sem dado de elevação medido aqui)."
                if inside_bbox else
                "Ponto fora da área coberta por este levantamento."
            ),
        )
    return {
        "lon": lon,
        "lat": lat,
        "elevation_m": values[0],
        "asset_id": asset["id"],
        "resolution_cm": asset["resolution_cm"],
    }


# ── MEDIÇÕES SOB DEMANDA no DSM (leitura direta do COG, sem aproximação) ────
# O operador desenha no mapa e recebe número medido, não estimado: estatística
# zonal de um polígono e perfil de elevação de uma linha. Ambos leem o raster
# publicado — o mesmo que ele pode baixar e conferir no QGIS.


class GeometryPayload(BaseModel):
    """GeoJSON (EPSG:4326) desenhado pelo usuário no mapa."""

    geometry: dict[str, Any]
    samples: int = 256  # só para perfil


@router.post("/dsm/{asset_id}/zonal-stats")
async def dsm_zonal_stats(
    asset_id: str,
    payload: GeometryPayload,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Estatística ZONAL exata de um polígono desenhado: área projetada,
    mínimo/máximo/média/desvio/mediana, VOLUME (Σ altura × área do pixel),
    contagem de pixels e cobertura válida. Tudo lido do COG, sem reamostrar.
    """
    import os
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    asset = await _resolve_dsm_asset(asset_id, db)
    geom = payload.geometry
    if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
        raise HTTPException(status_code=422, detail="Envie um Polygon ou MultiPolygon em GeoJSON")

    work = tempfile.mkdtemp(prefix="cm_zonal_")
    local = os.path.join(work, "dsm.tif")
    try:
        from app.core.storage import get_minio_client
        get_minio_client().fget_object(asset["bucket_name"], asset["file_key"], local)

        with rasterio.open(local) as d:
            gn = transform_geom("EPSG:4326", d.crs.to_string(), geom)
            try:
                win, _tr = rio_mask(d, [gn], crop=True, filled=False)
            except ValueError:
                raise HTTPException(status_code=404, detail="Polígono fora da área do levantamento")
            px_area = abs(d.transform.a * d.transform.e)
            arr = win[0]
            vals = arr.compressed().astype("float64") if np.ma.isMaskedArray(arr) else arr.astype("float64").ravel()
            vals = vals[np.isfinite(vals)]
            # nodata=0 no nDSM: separar "sem dado" de "solo" evita média mentirosa
            nonzero = vals[vals != 0]
            # Cobertura relativa ao POLÍGONO, não ao retângulo do recorte: um
            # polígono diagonal fino ocupa fração pequena do bbox e reportar
            # 5% de cobertura assustaria o operador sem motivo.
            from rasterio.features import geometry_mask
            inside = ~geometry_mask(
                [gn], out_shape=arr.shape, transform=_tr, invert=False
            )
            total_px = int(inside.sum()) or int(arr.size)
            valid_px = int(vals.size)
            poly_area_m2 = float(shape(gn).area) if d.crs.is_projected else None

        if valid_px == 0:
            raise HTTPException(status_code=404, detail="Sem dado de elevação sob o polígono")

        base = nonzero if nonzero.size else vals
        return {
            "asset_id": asset["id"],
            "area_m2": round(poly_area_m2, 3) if poly_area_m2 is not None else None,
            "pixel_area_m2": round(px_area, 6),
            "pixels_total": total_px,
            "pixels_with_data": valid_px,
            "coverage_pct": round(100.0 * valid_px / total_px, 2) if total_px else 0.0,
            "elevation": {
                "min_m": round(float(np.min(base)), 3),
                "max_m": round(float(np.max(base)), 3),
                "mean_m": round(float(np.mean(base)), 3),
                "median_m": round(float(np.median(base)), 3),
                "std_m": round(float(np.std(base)), 3),
                "p95_m": round(float(np.percentile(base, 95)), 3),
            },
            "volume_m3": round(float(np.sum(np.clip(vals, 0, None)) * px_area), 3),
            "resolution_cm": asset["resolution_cm"],
            "note": (
                "Volume = soma das alturas acima do solo × área do pixel; "
                "estatísticas ignoram pixels sem dado."
            ),
        }
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


@router.post("/dsm/{asset_id}/profile")
async def dsm_elevation_profile(
    asset_id: str,
    payload: GeometryPayload,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    PERFIL de elevação ao longo de uma linha desenhada: N amostras lidas
    pixel a pixel, distância acumulada, ganho/perda, declividade média e
    máxima. Base para corte/aterro, drenagem e verificação de rampa.
    """
    import os
    import tempfile

    import numpy as np
    import rasterio
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    asset = await _resolve_dsm_asset(asset_id, db)
    geom = payload.geometry
    if not geom or geom.get("type") not in ("LineString", "MultiLineString"):
        raise HTTPException(status_code=422, detail="Envie uma LineString em GeoJSON")
    n = max(8, min(payload.samples, 2000))

    work = tempfile.mkdtemp(prefix="cm_prof_")
    local = os.path.join(work, "dsm.tif")
    try:
        from app.core.storage import get_minio_client
        get_minio_client().fget_object(asset["bucket_name"], asset["file_key"], local)

        with rasterio.open(local) as d:
            gn = transform_geom("EPSG:4326", d.crs.to_string(), geom)
            line = shape(gn)
            total_len = float(line.length)  # metros quando o CRS é projetado
            pts = [line.interpolate(i / (n - 1), normalized=True) for i in range(n)]
            coords = [(p.x, p.y) for p in pts]
            sampled = [float(v[0]) for v in d.sample(coords)]

        nodata_mask = [(v == 0 or not np.isfinite(v)) for v in sampled]
        dist = [round(total_len * i / (n - 1), 3) for i in range(n)]
        series = [
            {"d_m": dist[i], "z_m": (None if nodata_mask[i] else round(sampled[i], 3))}
            for i in range(n)
        ]
        zs = np.array([v for v, bad in zip(sampled, nodata_mask, strict=False) if not bad])
        gain = loss = 0.0
        slopes = []
        prev = None
        step = total_len / (n - 1) if n > 1 else 0.0
        for i, v in enumerate(sampled):
            if nodata_mask[i]:
                prev = None
                continue
            if prev is not None and step > 0:
                dz = v - prev
                gain += max(0.0, dz)
                loss += max(0.0, -dz)
                slopes.append(abs(dz) / step * 100.0)
            prev = v
        return {
            "asset_id": asset["id"],
            "length_m": round(total_len, 3),
            "samples": n,
            "sample_spacing_m": round(step, 4),
            "profile": series,
            "stats": {
                "min_m": round(float(zs.min()), 3) if zs.size else None,
                "max_m": round(float(zs.max()), 3) if zs.size else None,
                "mean_m": round(float(zs.mean()), 3) if zs.size else None,
                "gain_m": round(gain, 3),
                "loss_m": round(loss, 3),
                "slope_mean_pct": round(float(np.mean(slopes)), 2) if slopes else None,
                "slope_max_pct": round(float(np.max(slopes)), 2) if slopes else None,
                "coverage_pct": round(100.0 * zs.size / n, 2),
            },
            "resolution_cm": asset["resolution_cm"],
        }
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


# ── Detecções da IA com medições exatas (fonte: banco, mesma da malha fina) ──


@router.get("/detections/by-project/{project_id}")
async def get_detections_for_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    GeoJSON das detecções de edificação da IA para o projeto, com as MESMAS
    medições que alimentam a análise fiscal: area_sqm, height_m, confidence e
    model_version saem direto de ai_detections — o que o mapa mostra é
    exatamente o que o banco guarda, sem recálculo no caminho.
    """
    rows = (await db.execute(text(
        "SELECT d.id, ST_AsGeoJSON(d.polygon), d.area_sqm, d.height_m, "
        "d.confidence, d.model_version, d.detection_class, "
        "d.height_measured_m, d.height_std_m, d.volume_m3, "
        "d.area_uncertainty_sqm, d.evidence_score, d.validation_status, "
        "d.is_unanimous, d.consensus_votes, "
        "d.roof_type, d.roof_design, d.roof_waters, d.roof_type_confidence, "
        "d.roof_material, d.roof_material_confidence, d.roof_slope_pct, "
        "d.roof_slope_deg, d.area_projected_sqm, d.area_real_sqm, d.area_gain_pct "
        "FROM ai_detections d "
        "JOIN flight_assets fa ON d.flight_asset_id = fa.id "
        "JOIN flights f ON fa.flight_id = f.id "
        "WHERE f.project_id = CAST(:pid AS uuid) AND fa.is_active "
        "AND d.polygon IS NOT NULL "
        # Sombra não é imóvel: sai do mapa, da contagem e da malha fina.
        "AND COALESCE(d.shadow_rejected, FALSE) = FALSE "
        "ORDER BY d.area_sqm DESC"
    ), {"pid": project_id})).fetchall()

    features = []
    for r in rows:
        features.append({
            "type": "Feature",
            "geometry": json.loads(r[1]),
            "properties": {
                "detection_id": str(r[0]),
                "area_sqm": float(r[2]) if r[2] is not None else None,
                "height_m": float(r[3]) if r[3] is not None else None,
                "confidence": float(r[4]) if r[4] is not None else None,
                "model_version": r[5],
                "detection_class": r[6],
                # Medições fotogramétricas + veredito da validação cruzada
                "height_measured_m": float(r[7]) if r[7] is not None else None,
                "height_std_m": float(r[8]) if r[8] is not None else None,
                "volume_m3": float(r[9]) if r[9] is not None else None,
                "area_uncertainty_sqm": float(r[10]) if r[10] is not None else None,
                "evidence_score": float(r[11]) if r[11] is not None else None,
                "validation_status": r[12],
                "is_unanimous": r[13],
                "consensus_votes": r[14],
                # Tipologia e material do telhado + as DUAS áreas
                "roof_type": r[15],
                "roof_design": r[16],
                "roof_waters": r[17],
                "roof_type_confidence": float(r[18]) if r[18] is not None else None,
                "roof_material": r[19],
                "roof_material_confidence": float(r[20]) if r[20] is not None else None,
                "roof_slope_pct": float(r[21]) if r[21] is not None else None,
                "roof_slope_deg": float(r[22]) if r[22] is not None else None,
                "area_projected_sqm": float(r[23]) if r[23] is not None else None,
                "area_real_sqm": float(r[24]) if r[24] is not None else None,
                "area_gain_pct": float(r[25]) if r[25] is not None else None,
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {"total": len(features), "project_id": project_id},
    }


# ── Martin (Vector Tiles) ─────────────────────────────────────────────────────


@router.get("/vector/catalog")
async def get_vector_catalog(
    user: dict[str, Any] = Depends(require_viewer),
):
    """List available vector tile sources from Martin."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.martin_url}/catalog")
            if resp.status_code == 200:
                return resp.json()
            return {"sources": [], "message": "Martin not available"}
    except httpx.HTTPError:
        return {"sources": [], "message": "Martin not reachable"}


@router.get("/vector/{source_name}/tilejson.json")
async def get_vector_tilejson(
    source_name: str,
    user: dict[str, Any] = Depends(require_viewer),
):
    """Get TileJSON for a vector tile source from Martin."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{settings.martin_url}/{source_name}",
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Martin error: {e}")
