"""
CM TECHMAP — Tile Serving Routes (TiTiler + Martin proxy)
Serves raster tiles from COG orthomosaics via TiTiler and vector tiles via Martin.
Uses `flight_assets` table for asset lookups.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_db, require_viewer

router = APIRouter(prefix="/tiles", tags=["Tiles"])
settings = get_settings()
logger = logging.getLogger(__name__)


# ── Helper ────────────────────────────────────────────────────────────────────

async def _resolve_asset(
    asset_id: str, db: AsyncSession
) -> dict[str, Any]:
    """
    Look up a flight_asset by ID and return its metadata.
    Falls back to searching by file_key if UUID lookup fails.
    """
    # Try UUID lookup first
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
        from PIL import Image
        import numpy as np

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
                        import io
                        from PIL import Image
                        import numpy as np

                        # Decode Terrarium PNG → elevation array
                        img = Image.open(io.BytesIO(resp.content)).convert("RGB")
                        arr = np.array(img, dtype=np.float64)
                        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
                        elevation = (r * 256.0 + g + b / 256.0) - 32768.0

                        # Subtract base elevation → relative heights
                        elevation = elevation - base_elevation
                        elevation = np.clip(elevation, 0.0, max_relative_elev)

                        # For synthetic DSMs: apply per-tile Gaussian smooth
                        # to eliminate the jagged pixel-level noise
                        if dsm_source == "synthetic":
                            from scipy.ndimage import gaussian_filter
                            elevation = gaussian_filter(elevation, sigma=2.0)

                        # Re-encode to Terrarium: val = elevation + 32768
                        val = elevation + 32768.0
                        r_out = np.floor(val / 256.0).astype(np.uint8)
                        g_out = np.floor(val % 256.0).astype(np.uint8)
                        b_out = np.floor((val - np.floor(val)) * 256.0).astype(np.uint8)

                        out = np.stack([r_out, g_out, b_out], axis=-1)
                        out_img = Image.fromarray(out, "RGB")
                        buf = io.BytesIO()
                        out_img.save(buf, format="PNG")
                        buf.seek(0)

                        return Response(
                            content=buf.getvalue(),
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
