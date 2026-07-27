"""
CM TECHMAP — Asset Management Routes
Direct upload of pre-processed GeoTIFF files for immediate map overlay.
"""

import logging
import os
import shutil
import tempfile
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.core.storage import upload_file as minio_upload
from app.dependencies import get_db, require_operador
from app.middleware.subscription_enforcement import enforce_storage_limit

router = APIRouter(prefix="/assets", tags=["Assets"])
settings = get_settings()
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".tif", ".tiff", ".geotiff"}


@router.post(
    "/upload-geotiff",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_storage_limit)],
)
async def upload_geotiff(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    flight_id: str = Form(None),
    description: str = Form(""),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """
    Upload a pre-processed georeferenced TIFF directly for map overlay.

    This bypasses the NodeODM pipeline — for users who already have an
    orthomosaic, DSM, or other georeferenced raster ready to display.

    Pipeline:
    1. Save uploaded file to temp
    2. Validate it's a valid GeoTIFF with geospatial metadata
    3. Convert to COG if needed
    4. Extract bounds, CRS, resolution
    5. Upload COG to MinIO
    6. Insert into flight_assets
    7. Return asset ID + bounds for immediate map overlay
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validar os identificadores ANTES de qualquer trabalho pesado: sem isto,
    # um project_id malformado só estoura lá na frente, dentro do CAST(... AS
    # uuid), e o handler genérico devolve 500 com a query SQL e os parâmetros
    # expostos ao cliente.
    try:
        uuid.UUID(project_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="project_id inválido: informe o identificador (UUID) de um projeto existente.",
        )
    if flight_id:
        try:
            uuid.UUID(flight_id)
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=422,
                detail="flight_id inválido: informe o identificador (UUID) de um voo existente.",
            )

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {ext}. Allowed: {ALLOWED_EXTENSIONS}",
        )

    work_dir = tempfile.mkdtemp(prefix="cm_geotiff_")

    try:
        # ── Step 1: Stream to temp ────────────────────────────────────
        # Never `await file.read()` here: a single 2 GB GeoTIFF (the gateway
        # limit) would be loaded whole into a 384 MB container and OOM-kill
        # the API for every other user. Stream in chunks and stop early if
        # the file exceeds the configured ceiling.
        max_bytes = settings.upload_max_file_size_gb * 1024 * 1024 * 1024
        local_input = os.path.join(work_dir, os.path.basename(file.filename))
        file_size_raw = 0
        with open(local_input, "wb") as f:
            while chunk := await file.read(8 * 1024 * 1024):
                file_size_raw += len(chunk)
                if file_size_raw > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Arquivo excede o limite de "
                            f"{settings.upload_max_file_size_gb} GB."
                        ),
                    )
                f.write(chunk)
        logger.info(f"[GEOTIFF] Received: {file.filename} ({file_size_raw / 1024 / 1024:.1f} MB)")

        # ── Step 2: Extract geospatial metadata ───────────────────────
        from app.core.cog_converter import extract_geospatial_metadata

        try:
            metadata = extract_geospatial_metadata(local_input)
        except Exception as e:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid GeoTIFF — cannot extract geospatial metadata: {e}",
            )

        bounds = metadata.get("bounds", {})
        if not bounds or bounds.get("west") is None:
            raise HTTPException(
                status_code=422,
                detail="GeoTIFF has no geospatial bounds. Ensure the file is georeferenced.",
            )

        # ── Step 3: Convert to COG ────────────────────────────────────
        from app.core.cog_converter import convert_to_cog, validate_cog

        # GDAL conversion takes minutes on this hardware and is fully
        # blocking — running it inline would freeze every other request
        # (health check included) served by this single-worker process.
        cog_path = await run_in_threadpool(convert_to_cog, local_input)
        is_valid = await run_in_threadpool(validate_cog, cog_path)

        if not is_valid:
            # If COG conversion fails, use original
            logger.warning("[GEOTIFF] COG validation failed, using original file")
            cog_path = local_input

        # Re-extract metadata from COG (size may differ)
        cog_metadata = extract_geospatial_metadata(cog_path)

        # ── Step 4: Upload to MinIO ───────────────────────────────────
        tenant_id = user.get("tenant_id") or "default"
        asset_uuid = str(uuid.uuid4())
        cog_filename = os.path.basename(str(cog_path))
        object_key = f"{tenant_id}/{project_id}/orthomosaic/{asset_uuid}_{cog_filename}"
        object_key = object_key.lstrip("/")

        with open(cog_path, "rb") as f:
            cog_size = os.path.getsize(str(cog_path))
            minio_upload(
                settings.minio_bucket_orthomosaics,
                object_key,
                f,
                cog_size,
                content_type="image/tiff",
                metadata={
                    "asset_type": "orthomosaic",
                    "project_id": project_id,
                    "original_filename": file.filename,
                },
            )

        logger.info(f"[GEOTIFF] Uploaded COG: {object_key} ({cog_size / 1024 / 1024:.1f} MB)")

        # ── Step 5: Resolve flight_id ─────────────────────────────────
        resolved_flight_id = flight_id
        if not resolved_flight_id:
            result = await db.execute(text(
                "SELECT id FROM flights WHERE project_id = CAST(:pid AS uuid) "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"pid": project_id})
            row = result.fetchone()
            if row:
                resolved_flight_id = str(row[0])
            else:
                # Create a placeholder flight
                result = await db.execute(text(
                    "INSERT INTO flights (project_id, flight_date, status, notes) "
                    "VALUES (CAST(:pid AS uuid), CURRENT_DATE, 'completed', :notes) "
                    "RETURNING id"
                ), {"pid": project_id, "notes": f"Auto-created for direct GeoTIFF upload: {file.filename}"})
                resolved_flight_id = str(result.scalar())

        # ── Step 6: Insert into flight_assets ─────────────────────────
        import json

        result = await db.execute(text(
            "INSERT INTO flight_assets "
            "(flight_id, asset_type, file_key, bucket_name, "
            "file_size_bytes, content_type, resolution_cm, "
            "cog_validated, crs_epsg, "
            "bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, "
            "metadata_json) "
            "VALUES (CAST(:fid AS uuid), 'orthomosaic', :fk, :bucket, "
            ":fsz, 'image/tiff', :res, "
            ":cog, :crs, "
            ":bw, :bs, :be, :bn, "
            "CAST(:meta AS jsonb)) "
            "RETURNING id"
        ), {
            "fid": resolved_flight_id,
            "fk": object_key,
            "bucket": settings.minio_bucket_orthomosaics,
            "fsz": cog_size,
            "res": cog_metadata.get("resolution_cm"),
            "cog": is_valid,
            "crs": cog_metadata.get("srid", 4326),
            "bw": bounds["west"],
            "bs": bounds["south"],
            "be": bounds["east"],
            "bn": bounds["north"],
            "meta": json.dumps(cog_metadata),
        })

        asset_id = str(result.scalar())

        # ── Step 7: Update project bounds ─────────────────────────────
        await db.execute(text(
            "UPDATE projects SET "
            "bbox_min_lon = LEAST(COALESCE(bbox_min_lon, :bw), :bw), "
            "bbox_min_lat = LEAST(COALESCE(bbox_min_lat, :bs), :bs), "
            "bbox_max_lon = GREATEST(COALESCE(bbox_max_lon, :be), :be), "
            "bbox_max_lat = GREATEST(COALESCE(bbox_max_lat, :bn), :bn), "
            "status = 'processado', updated_at = NOW() "
            "WHERE id = CAST(:pid AS uuid)"
        ), {
            "pid": project_id,
            "bw": bounds["west"],
            "bs": bounds["south"],
            "be": bounds["east"],
            "bn": bounds["north"],
        })

        await db.commit()

        logger.info(f"[GEOTIFF] Asset created: {asset_id} with bounds {bounds}")

        # ── Step 8: Trigger DSM + Building extraction (async) ─────────
        # Direct GeoTIFF uploads always use synthetic DSM (no photogrammetry data)
        dsm_task_id = None
        try:
            from app.tasks.post_processing import generate_dsm_and_buildings
            result = generate_dsm_and_buildings.delay(
                orthomosaic_asset_id=asset_id,
                orthomosaic_bucket=settings.minio_bucket_orthomosaics,
                orthomosaic_key=object_key,
                flight_id=resolved_flight_id,
                project_id=project_id,
            )
            dsm_task_id = result.id
            logger.info(f"[GEOTIFF] Synthetic DSM generation queued: task={dsm_task_id}")
        except Exception as e:
            logger.warning(f"[GEOTIFF] Could not queue DSM generation: {e}")

        return {
            "asset_id": asset_id,
            "file_key": object_key,
            "bounds": bounds,
            "center": cog_metadata.get("center"),
            "resolution_cm": cog_metadata.get("resolution_cm"),
            "crs_epsg": cog_metadata.get("srid"),
            "cog_validated": is_valid,
            "file_size_bytes": cog_size,
            "tilejson_url": f"/api/v1/tiles/raster/{asset_id}/tilejson.json",
            "dsm_task_id": dsm_task_id,
            "dsm_source": "synthetic",
            "message": "GeoTIFF uploaded and ready for map overlay. Synthetic DSM generation queued (for real 3D, process via NodeODM pipeline).",
        }

    except HTTPException:
        raise
    except Exception as e:
        # Não repassar `e` ao cliente: o texto traz a query SQL, os parâmetros
        # e detalhes internos do driver. O rastro completo fica no log.
        logger.exception(f"[GEOTIFF] Upload failed: {e}")
        raise HTTPException(
            status_code=500,
            detail="Falha ao processar o GeoTIFF. Consulte os logs do servidor.",
        )
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
