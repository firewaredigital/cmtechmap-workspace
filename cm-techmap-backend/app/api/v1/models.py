"""
CM TECHMAP — 3D Models API

REST endpoints for serving 3D model assets (glTF/glb) and their
associated metadata (offset.xyz for RTE rendering).

These endpoints support the frontend's transition from synthetic
building extrusions to real photogrammetric 3D models:

  GET /models/by-project/{project_id}  → Model availability + metadata
  GET /models/{asset_id}/download      → Stream glTF/glb file
  GET /models/{asset_id}/offset        → Offset vector for RTE rendering
  GET /models/{asset_id}/metadata      → Vertex/triangle/bbox info

Architecture context (research transcript Cap. 5):
  - Models are served in local coordinates (centered near 0,0,0)
  - The offset endpoint provides the Float64 translation vector
  - Frontend applies RTE (Relative-To-Eye) in JS CPU for geo-placement
  - This prevents Z-jittering in WebGL Float32 pipelines
"""

import json
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_db, require_viewer

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter(prefix="/models", tags=["3D Models"])


@router.get("/by-project/{project_id}")
async def get_model_for_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Find available 3D model assets for a project.

    Searches flight_assets for model_3d and offset_xyz entries.
    Returns model metadata including vertex count, offset vector,
    and download URLs.
    """
    # Search for 3D model assets
    result = await db.execute(text(
        "SELECT fa.id, fa.file_key, fa.bucket_name, fa.metadata_json, "
        "fa.file_size_bytes, fa.created_at "
        "FROM flight_assets fa "
        "JOIN flights f ON fa.flight_id = f.id "
        "WHERE f.project_id = CAST(:pid AS uuid) "
        "AND fa.asset_type = 'model_3d' "
        "ORDER BY fa.created_at DESC LIMIT 1"
    ), {"pid": project_id})
    model_row = result.fetchone()

    # Search for offset_xyz
    offset_result = await db.execute(text(
        "SELECT fa.id, fa.file_key, fa.bucket_name, fa.metadata_json "
        "FROM flight_assets fa "
        "JOIN flights f ON fa.flight_id = f.id "
        "WHERE f.project_id = CAST(:pid AS uuid) "
        "AND fa.asset_type IN ('offset_xyz', 'model_offset') "
        "ORDER BY fa.created_at DESC LIMIT 1"
    ), {"pid": project_id})
    offset_row = offset_result.fetchone()

    # Search for DSM offset (fallback)
    dsm_offset_result = await db.execute(text(
        "SELECT fa.metadata_json "
        "FROM flight_assets fa "
        "JOIN flights f ON fa.flight_id = f.id "
        "WHERE f.project_id = CAST(:pid AS uuid) "
        "AND fa.asset_type = 'dsm' "
        "ORDER BY fa.created_at DESC LIMIT 1"
    ), {"pid": project_id})
    dsm_row = dsm_offset_result.fetchone()

    if not model_row:
        return {
            "model": None,
            "offset": _extract_offset_from_dsm(dsm_row) if dsm_row else None,
            "status": "no_model",
            "message": "No 3D model available for this project. "
                       "Upload drone images (≥3) to trigger photogrammetric processing.",
        }

    model_meta = model_row[3] or {}
    model_id = str(model_row[0])

    # Build offset info
    offset_info = None
    if offset_row:
        offset_meta = offset_row[3] or {}
        offset_info = offset_meta.get("offset", {})
    elif dsm_row:
        offset_info = _extract_offset_from_dsm(dsm_row)

    return {
        "model": {
            "asset_id": model_id,
            "file_key": model_row[1],
            "file_size_bytes": model_row[4],
            "created_at": str(model_row[5]) if model_row[5] else None,
            "download_url": f"/api/v1/models/{model_id}/download",
            "metadata_url": f"/api/v1/models/{model_id}/metadata",
            "offset_url": f"/api/v1/models/{model_id}/offset",
            "vertex_count": model_meta.get("vertex_count", 0),
            "triangle_count": model_meta.get("triangle_count", 0),
            "format": model_meta.get("format", "glb"),
        },
        "offset": offset_info,
        "status": "available",
    }


@router.get("/{asset_id}/download")
async def download_model(
    asset_id: str,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Stream a 3D model file (glTF/glb) for browser rendering.

    Returns the model in binary glTF format with appropriate MIME type
    for direct loading by Three.js GLTFLoader or similar WebGL loaders.

    Supports authentication via:
    - Authorization header (standard Bearer token)
    - Query parameter ?token=<jwt> (for WebGL loaders that can't set headers)
    """
    result = await db.execute(text(
        "SELECT file_key, bucket_name, metadata_json "
        "FROM flight_assets WHERE id = CAST(:aid AS uuid)"
    ), {"aid": asset_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Model asset not found")

    file_key = row[0]
    bucket = row[1]
    meta = row[2] or {}

    # Determine content type based on format
    fmt = meta.get("format", "glb")
    content_type_map = {
        "glb": "model/gltf-binary",
        "gltf": "model/gltf+json",
        "obj": "model/obj",
    }
    content_type = content_type_map.get(fmt, "application/octet-stream")

    # Build MinIO URL
    minio_ep = settings.minio_endpoint
    if not minio_ep.startswith("http"):
        minio_ep = f"http://{minio_ep}"
    url = f"{minio_ep}/{bucket}/{file_key}"

    async def stream_file():
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    yield chunk

    filename = file_key.split("/")[-1] if "/" in file_key else file_key

    return StreamingResponse(
        stream_file(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get("/{asset_id}/offset")
async def get_model_offset(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Retrieve the RTE offset vector for a 3D model.

    Returns the Float64 translation vector (offset.xyz) that maps
    the model's local coordinates back to global geodetic position.

    This is the vec{T} from the research transcript (Cap. 5):
      P_global = P_local + T

    The frontend uses this for:
    1. Computing camera-relative position in Float64 (JavaScript)
    2. Passing the small residual to GPU as Float32 uniform
    3. Preventing Z-jittering in WebGL rendering
    """
    result = await db.execute(text(
        "SELECT metadata_json FROM flight_assets "
        "WHERE id = CAST(:aid AS uuid)"
    ), {"aid": asset_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    meta = row[0] or {}

    # Try to extract offset from various metadata locations
    offset = (
        meta.get("model_offset") or
        meta.get("offset_xyz") or
        meta.get("offset") or
        {}
    )

    if not offset:
        raise HTTPException(
            status_code=404,
            detail="No offset data available for this asset"
        )

    return {
        "offset": {
            "x": offset.get("x", 0.0),
            "y": offset.get("y", 0.0),
            "z": offset.get("z", 0.0),
            "crs": offset.get("crs", "EPSG:4326"),
            "epsg": offset.get("epsg", 4326),
        },
        "usage": {
            "description": (
                "Apply RTE (Relative-To-Eye) rendering: "
                "compute camera_relative = offset - camera_position in Float64 (JS), "
                "then pass to GPU as Float32 uniform."
            ),
            "vertex_count": offset.get("vertex_count", 0),
        },
    }


@router.get("/{asset_id}/metadata")
async def get_model_metadata(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Get comprehensive metadata about a 3D model asset.

    Returns vertex count, triangle count, bounding box, file size,
    format, and offset information.
    """
    result = await db.execute(text(
        "SELECT file_key, bucket_name, file_size_bytes, metadata_json, created_at "
        "FROM flight_assets WHERE id = CAST(:aid AS uuid)"
    ), {"aid": asset_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Asset not found")

    meta = row[3] or {}

    return {
        "asset_id": asset_id,
        "file_key": row[0],
        "file_size_bytes": row[2],
        "created_at": str(row[4]) if row[4] else None,
        "format": meta.get("format", "unknown"),
        "vertex_count": meta.get("vertex_count", 0),
        "triangle_count": meta.get("triangle_count", 0),
        "material_count": meta.get("material_count", 0),
        "bbox_local": meta.get("bbox_local", {}),
        "offset": meta.get("model_offset") or meta.get("offset", {}),
        "dsm_source": meta.get("dsm_source", "unknown"),
    }


def _extract_offset_from_dsm(dsm_row) -> dict | None:
    """Extract offset info from DSM metadata as fallback."""
    if not dsm_row:
        return None
    meta = dsm_row[0] if isinstance(dsm_row[0], dict) else {}
    return meta.get("offset_xyz") or meta.get("offset")


# ══════════════════════════════════════════════════════════════════════════════
# 3D Gaussian Splatting (3DGS) Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/splat/by-project/{project_id}")
async def get_splat_for_project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Check if a 3D Gaussian Splat asset is available for a project.

    Returns splat metadata and offset vector if a .splat asset exists.
    The frontend uses this to determine whether to show the Gaussian
    Splatting toggle in the map viewer.

    Architecture context (research transcript Cap. 4):
      3DGS represents the environment with millions of ellipsoidal
      primitives trained via SGD, surpassing traditional meshes for
      transparency, reflections, and fine structures.
    """
    # Look for gaussian_splat asset type in any flight of this project
    result = await db.execute(text("""
        SELECT fa.id, fa.file_key, fa.file_size_bytes, fa.metadata_json, fa.created_at
        FROM flight_assets fa
        JOIN flights f ON fa.flight_id = f.id
        WHERE f.project_id = CAST(:pid AS uuid)
          AND fa.asset_type = 'gaussian_splat'
        ORDER BY fa.created_at DESC
        LIMIT 1
    """), {"pid": project_id})
    row = result.fetchone()

    if not row:
        return {"splat": None, "offset": None, "status": "no_splat"}

    meta = row[3] or {}
    asset_id = str(row[0])

    # Extract offset for RTE rendering
    offset_data = (
        meta.get("splat_offset") or
        meta.get("offset") or
        meta.get("model_offset") or
        {}
    )

    splat_info = {
        "asset_id": asset_id,
        "file_key": row[1],
        "file_size_bytes": row[2],
        "created_at": str(row[4]) if row[4] else None,
        "splat_count": meta.get("splat_count", 0),
        "format": meta.get("format", "splat"),
        "has_sh": meta.get("has_sh_coefficients", False),
        "sh_degree": meta.get("sh_degree", 0),
        "download_url": f"/api/v1/models/splat/{asset_id}/download",
        "offset_url": f"/api/v1/models/{asset_id}/offset",
        "metadata_url": f"/api/v1/models/splat/{asset_id}/metadata",
    }

    offset_info = None
    if offset_data:
        offset_info = {
            "x": offset_data.get("x", 0.0),
            "y": offset_data.get("y", 0.0),
            "z": offset_data.get("z", 0.0),
            "crs": offset_data.get("crs", "EPSG:4326"),
            "epsg": offset_data.get("epsg", 4326),
        }

    return {
        "splat": splat_info,
        "offset": offset_info,
        "status": "available",
    }


@router.get("/splat/{asset_id}/download")
async def download_splat(
    asset_id: str,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Stream a .splat binary file for WebGL Gaussian Splatting rendering.

    The .splat format uses 32 bytes per Gaussian primitive:
      [pos_x:f32][pos_y:f32][pos_z:f32][scale_x:f32][scale_y:f32][scale_z:f32]
      [r:u8][g:u8][b:u8][opacity:u8][qr:u8][qi:u8][qj:u8][qk:u8]

    Supports authentication via:
    - Authorization header (standard Bearer token)
    - Query parameter ?token=<jwt> (for WebGL loaders)
    """
    result = await db.execute(text(
        "SELECT file_key, bucket_name, file_size_bytes, metadata_json "
        "FROM flight_assets WHERE id = CAST(:aid AS uuid)"
    ), {"aid": asset_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Splat asset not found")

    file_key = row[0]
    bucket = row[1]
    file_size = row[2] or 0

    # Build MinIO URL
    minio_ep = settings.minio_endpoint
    if not minio_ep.startswith("http"):
        minio_ep = f"http://{minio_ep}"
    url = f"{minio_ep}/{bucket}/{file_key}"

    async def stream_file():
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=131072):
                    yield chunk

    filename = file_key.split("/")[-1] if "/" in file_key else file_key

    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "Access-Control-Expose-Headers": "Content-Disposition, Content-Length",
        "Cache-Control": "public, max-age=3600",
    }
    if file_size:
        headers["Content-Length"] = str(file_size)

    return StreamingResponse(
        stream_file(),
        media_type="application/octet-stream",
        headers=headers,
    )


@router.get("/splat/{asset_id}/metadata")
async def get_splat_metadata(
    asset_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Get comprehensive metadata about a Gaussian Splat asset.

    Returns splat count, file size, SH coefficient info,
    bounding box, and offset for RTE rendering.
    """
    result = await db.execute(text(
        "SELECT file_key, file_size_bytes, metadata_json, created_at "
        "FROM flight_assets WHERE id = CAST(:aid AS uuid)"
    ), {"aid": asset_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Splat asset not found")

    meta = row[2] or {}

    return {
        "asset_id": asset_id,
        "file_key": row[0],
        "file_size_bytes": row[1],
        "created_at": str(row[3]) if row[3] else None,
        "format": meta.get("format", "splat"),
        "splat_count": meta.get("splat_count", 0),
        "has_sh_coefficients": meta.get("has_sh_coefficients", False),
        "sh_degree": meta.get("sh_degree", 0),
        "bbox_min": meta.get("bbox_min", [0, 0, 0]),
        "bbox_max": meta.get("bbox_max", [0, 0, 0]),
        "centroid": meta.get("centroid", [0, 0, 0]),
        "offset": meta.get("splat_offset") or meta.get("offset", {}),
    }

