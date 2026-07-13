"""
CM TECHMAP — Post-Processing Celery Tasks
Tasks for COG conversion, thumbnail generation, and metadata extraction.
"""

import logging
import os
import shutil
import tempfile
from io import BytesIO

from celery import shared_task

from app.config import get_settings
from app.core.pubsub import publish_progress

logger = logging.getLogger(__name__)
settings = get_settings()


@shared_task(
    name="app.tasks.post_processing.convert_orthomosaic_to_cog",
    bind=True,
    queue="processing",
    time_limit=1800,
)
def convert_orthomosaic_to_cog(
    self,
    source_bucket: str,
    source_key: str,
    target_bucket: str,
    target_key: str,
    project_id: str | None = None,
):
    """
    Download a GeoTIFF from MinIO, convert to COG, upload back.
    Useful for re-processing or external data imports.
    """
    task_id = self.request.id
    work_dir = tempfile.mkdtemp(prefix="cm_cog_")

    try:
        publish_progress(task_id, "downloading", 10, "Downloading source file...")

        from app.core.storage import get_minio_client
        client = get_minio_client()

        local_input = os.path.join(work_dir, "input.tif")
        client.fget_object(source_bucket, source_key, local_input)

        publish_progress(task_id, "converting", 40, "Converting to COG...")

        from app.core.cog_converter import convert_to_cog, validate_cog, extract_geospatial_metadata

        local_cog = convert_to_cog(local_input)

        if not validate_cog(local_cog):
            raise RuntimeError("Generated file is not a valid COG")

        publish_progress(task_id, "extracting_metadata", 60, "Extracting metadata...")
        metadata = extract_geospatial_metadata(local_cog)

        publish_progress(task_id, "uploading", 80, "Uploading COG to storage...")

        from app.core.storage import upload_file as minio_upload
        with open(local_cog, "rb") as f:
            file_size = os.path.getsize(str(local_cog))
            minio_upload(
                target_bucket, target_key, f, file_size,
                content_type="image/tiff",
            )

        publish_progress(task_id, "completed", 100, "COG conversion complete ✓")

        return {
            "status": "completed",
            "target_key": target_key,
            "metadata": metadata,
        }

    except Exception as exc:
        logger.error(f"[COG] Conversion failed: {exc}")
        publish_progress(task_id, "failed", 0, str(exc))
        raise self.retry(exc=exc)
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)


@shared_task(
    name="app.tasks.post_processing.extract_and_store_metadata",
    queue="default",
    time_limit=300,
)
def extract_and_store_metadata(
    orthomosaic_id: str,
    bucket: str,
    file_key: str,
):
    """Extract geospatial metadata from a COG and store in the database."""
    work_dir = tempfile.mkdtemp(prefix="cm_meta_")

    try:
        from app.core.storage import get_minio_client
        client = get_minio_client()

        local_path = os.path.join(work_dir, "ortho.tif")
        client.fget_object(bucket, file_key, local_path)

        from app.core.cog_converter import extract_geospatial_metadata
        metadata = extract_geospatial_metadata(local_path)

        # Update database
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(settings.database_url_sync)
        with engine.connect() as conn:
            bounds = metadata.get("bounds", {})
            conn.execute(sa_text(
                "UPDATE orthomosaics SET "
                "resolution_cm = :res, width_px = :w, height_px = :h, "
                "srid = :srid, file_size_bytes = :fsz "
                "WHERE id = :id"
            ), {
                "res": metadata.get("resolution_cm"),
                "w": metadata.get("width_px"),
                "h": metadata.get("height_px"),
                "srid": metadata.get("srid", 4326),
                "fsz": metadata.get("file_size_bytes"),
                "id": orthomosaic_id,
            })
            conn.commit()
        engine.dispose()

        logger.info(f"[META] Updated metadata for orthomosaic {orthomosaic_id}")
        return {"status": "completed", "metadata": metadata}

    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)


@shared_task(
    name="app.tasks.post_processing.generate_thumbnail",
    queue="default",
    time_limit=120,
)
def generate_thumbnail(
    source_bucket: str,
    source_key: str,
    thumbnail_key: str,
    max_size: int = 512,
):
    """Generate a JPEG thumbnail from a GeoTIFF for UI display."""
    work_dir = tempfile.mkdtemp(prefix="cm_thumb_")

    try:
        from app.core.storage import get_minio_client
        client = get_minio_client()

        local_input = os.path.join(work_dir, "input.tif")
        client.fget_object(source_bucket, source_key, local_input)

        import subprocess
        local_thumb = os.path.join(work_dir, "thumb.jpg")

        cmd = [
            "gdal_translate",
            "-of", "JPEG",
            "-outsize", str(max_size), "0",
            "-co", "QUALITY=85",
            local_input, local_thumb,
        ]
        subprocess.run(cmd, capture_output=True, timeout=60)

        if os.path.exists(local_thumb):
            from app.core.storage import upload_file as minio_upload
            with open(local_thumb, "rb") as f:
                file_size = os.path.getsize(local_thumb)
                minio_upload(
                    source_bucket, thumbnail_key, f, file_size,
                    content_type="image/jpeg",
                )
            logger.info(f"[THUMB] Generated: {thumbnail_key}")
            return {"status": "completed", "key": thumbnail_key}

        return {"status": "failed", "message": "Thumbnail generation failed"}

    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)


@shared_task(
    name="app.tasks.post_processing.generate_dsm_and_buildings",
    bind=True,
    queue="processing",
    time_limit=3600,
    soft_time_limit=3500,
    acks_late=False,
    reject_on_worker_lost=True,
    max_retries=0,
)
def generate_dsm_and_buildings(
    self,
    orthomosaic_asset_id: str,
    orthomosaic_bucket: str,
    orthomosaic_key: str,
    flight_id: str,
    project_id: str,
    base_elevation: float = 800.0,
):
    """
    Generate a synthetic DSM and extract building footprints from an orthomosaic.

    Pipeline:
      1. Download orthomosaic COG from MinIO
      2. Generate synthetic DSM via image analysis
      3. Convert DSM to COG
      4. Extract building footprints → GeoJSON
      5. Upload DSM COG + footprints.geojson to MinIO
      6. Insert flight_asset records for DSM and buildings
    """
    task_id = self.request.id
    work_dir = tempfile.mkdtemp(prefix="cm_dsm_")

    # ── Deduplication lock ─────────────────────────────────────────────
    # Prevents multiple workers from processing the same flight simultaneously.
    # This can happen when: (1) assets.py auto-triggers on upload, (2) user
    # clicks "Processar" manually, (3) OOM kill causes task redelivery.
    import redis as redis_lib
    redis_client = redis_lib.from_url(settings.redis_url)
    lock_key = f"dsm_processing_lock:{flight_id}"

    if not redis_client.set(lock_key, task_id, nx=True, ex=3600):
        existing_task = redis_client.get(lock_key)
        existing_str = existing_task.decode() if existing_task else "unknown"
        logger.warning(
            f"[DSM-PIPELINE] Skipping duplicate: flight {flight_id} "
            f"already being processed by task {existing_str}"
        )
        return {"status": "skipped", "reason": "duplicate", "existing_task": existing_str}

    try:
        publish_progress(task_id, "dsm_generation", 0, "Starting DSM generation...")
        logger.info(f"[DSM-PIPELINE] Starting for orthomosaic {orthomosaic_asset_id}")

        # ── Step 1: Download orthomosaic ──────────────────────────────────
        publish_progress(task_id, "downloading", 5, "Downloading orthomosaic...")
        from app.core.storage import get_minio_client
        client = get_minio_client()

        local_ortho = os.path.join(work_dir, "orthomosaic.tif")
        client.fget_object(orthomosaic_bucket, orthomosaic_key, local_ortho)
        logger.info(f"[DSM-PIPELINE] Downloaded orthomosaic: {os.path.getsize(local_ortho)} bytes")

        # ── Step 2: Generate synthetic DSM ────────────────────────────────
        publish_progress(task_id, "dsm_generation", 15, "Analyzing orthophoto textures...")
        from app.core.dsm_generator import generate_synthetic_dsm, extract_dsm_metadata

        dsm_raw = os.path.join(work_dir, "dsm_raw.tif")
        generate_synthetic_dsm(
            local_ortho,
            output_path=dsm_raw,
            base_elevation=base_elevation,
            max_building_height=25.0,
            max_tree_height=12.0,
            smoothing_sigma=3.0,
        )

        publish_progress(task_id, "dsm_generation", 40, "DSM generated, converting to COG...")

        # ── Step 3: Convert DSM to COG ────────────────────────────────────
        from app.core.cog_converter import convert_to_cog

        dsm_cog_path = convert_to_cog(dsm_raw)
        dsm_meta = extract_dsm_metadata(dsm_cog_path)

        publish_progress(task_id, "building_extraction", 55, "Extracting building footprints...")

        # ── Step 4: Extract building footprints ───────────────────────────
        from app.core.building_extractor import extract_building_footprints

        footprints_path = os.path.join(work_dir, "footprints.geojson")
        extract_building_footprints(
            local_ortho,
            dsm_raw,
            output_path=footprints_path,
            min_height_m=2.5,
            base_elevation=base_elevation,
        )

        publish_progress(task_id, "uploading", 70, "Uploading DSM and footprints to storage...")

        # ── Step 5: Upload to MinIO ───────────────────────────────────────
        from app.core.storage import upload_file as minio_upload
        import json as json_mod

        tenant_prefix = orthomosaic_key.split("/")[0] if "/" in orthomosaic_key else "default"

        # Upload DSM COG
        dsm_object_key = f"{tenant_prefix}/{project_id}/dsm/{os.path.basename(str(dsm_cog_path))}"
        with open(dsm_cog_path, "rb") as f:
            dsm_size = os.path.getsize(str(dsm_cog_path))
            minio_upload(
                settings.minio_bucket_elevation_models,
                dsm_object_key, f, dsm_size,
                content_type="image/tiff",
                metadata={"asset_type": "dsm", "project_id": project_id},
            )
        logger.info(f"[DSM-PIPELINE] Uploaded DSM: {dsm_object_key}")

        # Upload footprints GeoJSON
        bld_object_key = f"{tenant_prefix}/{project_id}/buildings/footprints.geojson"
        with open(footprints_path, "rb") as f:
            bld_size = os.path.getsize(footprints_path)
            minio_upload(
                settings.minio_bucket_elevation_models,
                bld_object_key, f, bld_size,
                content_type="application/geo+json",
                metadata={"asset_type": "buildings", "project_id": project_id},
            )
        logger.info(f"[DSM-PIPELINE] Uploaded footprints: {bld_object_key}")

        publish_progress(task_id, "database", 85, "Updating database records...")

        # ── Step 6: Insert flight_asset records ───────────────────────────
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(settings.database_url_sync)
        bounds = dsm_meta.get("bounds", {})

        try:
            with engine.connect() as conn:
                # Set search_path to include all tenant schemas
                # The flight_assets table is in the tenant schema, not public
                conn.execute(sa_text(
                    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_%' LIMIT 1"
                ))
                tenant_rows = conn.execute(sa_text(
                    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_%'"
                )).fetchall()
                if tenant_rows:
                    schemas = ", ".join(r[0] for r in tenant_rows)
                    conn.execute(sa_text(f"SET search_path TO {schemas}, public, topology"))
                
                # Insert DSM asset
                conn.execute(sa_text(
                    "INSERT INTO flight_assets "
                    "(flight_id, asset_type, file_key, bucket_name, "
                    "file_size_bytes, content_type, resolution_cm, "
                    "cog_validated, crs_epsg, "
                    "bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, "
                    "metadata_json) "
                    "VALUES (CAST(:fid AS uuid), 'dsm', :fk, :bucket, "
                    ":fsz, 'image/tiff', :res, "
                    "true, :crs, "
                    ":bw, :bs, :be, :bn, "
                    "CAST(:meta AS jsonb))"
                ), {
                    "fid": flight_id,
                    "fk": dsm_object_key,
                    "bucket": settings.minio_bucket_elevation_models,
                    "fsz": dsm_size,
                    "res": dsm_meta.get("resolution_cm"),
                    "crs": dsm_meta.get("srid", 4326),
                    "bw": bounds.get("west"),
                    "bs": bounds.get("south"),
                    "be": bounds.get("east"),
                    "bn": bounds.get("north"),
                    "meta": json_mod.dumps({**dsm_meta, "dsm_source": "synthetic"}),
                })

                conn.commit()
                logger.info("[DSM-PIPELINE] Inserted DSM flight_asset record")

            # Update flight status to 'completed' after successful processing
            with engine.connect() as conn:
                if tenant_rows:
                    schemas = ", ".join(r[0] for r in tenant_rows)
                    conn.execute(sa_text(f"SET search_path TO {schemas}, public, topology"))
                conn.execute(sa_text(
                    "UPDATE flights SET status = 'completed' WHERE id = CAST(:fid AS uuid)"
                ), {"fid": flight_id})
                conn.commit()
                logger.info(f"[DSM-PIPELINE] Updated flight {flight_id} status to 'completed'")
        except Exception as e:
            logger.error(f"[DSM-PIPELINE] DB insert failed: {e}")
        finally:
            engine.dispose()

        publish_progress(task_id, "completed", 100,
                         "DSM + building footprints generated ✓",
                         extra={"dsm_key": dsm_object_key, "buildings_key": bld_object_key})

        return {
            "status": "completed",
            "dsm_key": dsm_object_key,
            "buildings_key": bld_object_key,
            "dsm_metadata": dsm_meta,
            "dsm_source": "synthetic",
        }

    except Exception as exc:
        logger.error(f"[DSM-PIPELINE] Failed: {exc}")
        publish_progress(task_id, "failed", 0, str(exc))
        raise  # Do not retry — prevents OOM restart loops
    finally:
        # Release deduplication lock
        try:
            redis_client.delete(lock_key)
        except Exception:
            pass  # Lock will expire via TTL anyway
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)


@shared_task(
    name="app.tasks.post_processing.extract_buildings_from_real_dsm",
    bind=True,
    queue="processing",
    time_limit=1800,
    soft_time_limit=1700,
)
def extract_buildings_from_real_dsm(
    self,
    dsm_asset_key: str,
    dsm_bucket: str,
    dtm_asset_key: str | None = None,
    dtm_bucket: str | None = None,
    flight_id: str = "",
    project_id: str = "",
    dsm_source: str = "real",
):
    """
    Extract building footprints from a REAL DSM (from NodeODM photogrammetry).

    Unlike the synthetic pipeline, this uses actual photogrammetric elevation
    data for accurate building height extraction. When DTM is available,
    computes nDSM (DSM - DTM) for normalized heights above ground.

    Pipeline:
      1. Download real DSM COG from MinIO
      2. Download real DTM if available (for nDSM calculation)
      3. Extract building footprints with real elevation data
      4. Upload footprints GeoJSON to MinIO
    """
    task_id = self.request.id
    work_dir = tempfile.mkdtemp(prefix="cm_real_bld_")

    try:
        publish_progress(task_id, "building_extraction", 0,
                         "Starting building extraction from real DSM...")
        logger.info(f"[REAL-DSM-BUILDINGS] Starting for DSM {dsm_asset_key}")

        from app.core.storage import get_minio_client
        client = get_minio_client()

        # ── Step 1: Download DSM ──────────────────────────────────────────
        publish_progress(task_id, "downloading", 10, "Downloading real DSM...")
        local_dsm = os.path.join(work_dir, "dsm.tif")
        client.fget_object(dsm_bucket, dsm_asset_key, local_dsm)
        logger.info(f"[REAL-DSM-BUILDINGS] Downloaded DSM: {os.path.getsize(local_dsm)} bytes")

        # ── Step 2: Download DTM if available ─────────────────────────────
        local_dtm = None
        if dtm_asset_key and dtm_bucket:
            try:
                publish_progress(task_id, "downloading", 20, "Downloading DTM for nDSM...")
                local_dtm = os.path.join(work_dir, "dtm.tif")
                client.fget_object(dtm_bucket, dtm_asset_key, local_dtm)
                logger.info(f"[REAL-DSM-BUILDINGS] Downloaded DTM: {os.path.getsize(local_dtm)} bytes")
            except Exception as e:
                logger.warning(f"[REAL-DSM-BUILDINGS] DTM download failed, using DSM only: {e}")
                local_dtm = None

        # ── Step 3: Extract building footprints ───────────────────────────
        publish_progress(task_id, "extraction", 40,
                         "Extracting building footprints from real elevation data...")

        from app.core.building_extractor import extract_buildings_from_elevation

        footprints_path = os.path.join(work_dir, "footprints.geojson")
        extract_buildings_from_elevation(
            dsm_path=local_dsm,
            dtm_path=local_dtm,
            output_path=footprints_path,
            min_height_m=2.0,
        )

        # ── Step 4: Upload footprints to MinIO ────────────────────────────
        publish_progress(task_id, "uploading", 75, "Uploading building footprints...")

        from app.core.storage import upload_file as minio_upload

        tenant_prefix = dsm_asset_key.split("/")[0] if "/" in dsm_asset_key else "default"
        bld_object_key = f"{tenant_prefix}/{project_id}/buildings/footprints.geojson"

        with open(footprints_path, "rb") as f:
            bld_size = os.path.getsize(footprints_path)
            minio_upload(
                "elevation-models",
                bld_object_key, f, bld_size,
                content_type="application/geo+json",
                metadata={
                    "asset_type": "buildings",
                    "project_id": project_id,
                    "dsm_source": dsm_source,
                },
            )
        logger.info(f"[REAL-DSM-BUILDINGS] Uploaded footprints: {bld_object_key}")

        # ── Step 5: Update DSM metadata with dsm_source flag ──────────────
        publish_progress(task_id, "database", 90, "Updating database...")

        from sqlalchemy import create_engine, text as sa_text
        import json as json_mod

        engine = create_engine(settings.database_url_sync)
        try:
            with engine.connect() as conn:
                # Find tenant schemas
                tenant_rows = conn.execute(sa_text(
                    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_%'"
                )).fetchall()
                if tenant_rows:
                    schemas = ", ".join(r[0] for r in tenant_rows)
                    conn.execute(sa_text(f"SET search_path TO {schemas}, public, topology"))

                # Update the DSM asset's metadata_json to include dsm_source
                conn.execute(sa_text(
                    "UPDATE flight_assets SET metadata_json = "
                    "jsonb_set(COALESCE(metadata_json, '{}'::jsonb), '{dsm_source}', '\"real\"'::jsonb) "
                    "WHERE flight_id = CAST(:fid AS uuid) AND asset_type = 'dsm'"
                ), {"fid": flight_id})
                conn.commit()
        except Exception as e:
            logger.warning(f"[REAL-DSM-BUILDINGS] DB update failed: {e}")
        finally:
            engine.dispose()

        publish_progress(task_id, "completed", 100,
                         "Building footprints extracted from real DSM ✓",
                         extra={"buildings_key": bld_object_key, "dsm_source": dsm_source})

        return {
            "status": "completed",
            "buildings_key": bld_object_key,
            "dsm_source": dsm_source,
        }

    except Exception as exc:
        logger.error(f"[REAL-DSM-BUILDINGS] Failed: {exc}")
        publish_progress(task_id, "failed", 0, str(exc))
        raise self.retry(exc=exc, max_retries=1)
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)


@shared_task(
    name="app.tasks.post_processing.normalize_and_process_real_dsm",
    bind=True,
    queue="processing",
    time_limit=1800,
    soft_time_limit=1700,
)
def normalize_and_process_real_dsm(
    self,
    dsm_asset_key: str,
    dsm_bucket: str,
    dtm_asset_key: str | None = None,
    dtm_bucket: str | None = None,
    flight_id: str = "",
    project_id: str = "",
):
    """
    Normalize a real DSM from NodeODM and extract RTE offset metadata.

    This standalone task is used to retroactively process DSM assets that
    were uploaded before the normalization pipeline was integrated. It:

    1. Downloads the raw DSM and optional DTM from MinIO
    2. Computes nDSM (DSM - DTM) or min-subtraction normalization
    3. Extracts offset.xyz for RTE anti-Z-jittering
    4. Converts normalized DSM to COG
    5. Uploads normalized DSM + offset back to MinIO
    6. Updates the flight_asset metadata_json with normalization info

    This task is idempotent — running it twice on the same DSM will
    overwrite the previous normalized output.
    """
    task_id = self.request.id
    work_dir = tempfile.mkdtemp(prefix="cm_dsm_norm_")

    try:
        publish_progress(task_id, "dsm_normalization", 0,
                         "Starting DSM normalization pipeline...")
        logger.info(f"[DSM-NORM-TASK] Starting for DSM {dsm_asset_key}")

        from app.core.storage import get_minio_client
        client = get_minio_client()

        # ── Step 1: Download DSM ──────────────────────────────────────────
        publish_progress(task_id, "downloading", 10, "Downloading DSM...")
        local_dsm = os.path.join(work_dir, "dsm.tif")
        client.fget_object(dsm_bucket, dsm_asset_key, local_dsm)
        logger.info(
            f"[DSM-NORM-TASK] Downloaded DSM: "
            f"{os.path.getsize(local_dsm) / 1024 / 1024:.1f} MB"
        )

        # ── Step 2: Download DTM if available ─────────────────────────────
        local_dtm = None
        if dtm_asset_key and dtm_bucket:
            try:
                publish_progress(task_id, "downloading", 20, "Downloading DTM...")
                local_dtm = os.path.join(work_dir, "dtm.tif")
                client.fget_object(dtm_bucket, dtm_asset_key, local_dtm)
                logger.info(
                    f"[DSM-NORM-TASK] Downloaded DTM: "
                    f"{os.path.getsize(local_dtm) / 1024 / 1024:.1f} MB"
                )
            except Exception as e:
                logger.warning(f"[DSM-NORM-TASK] DTM download failed: {e}")
                local_dtm = None

        # ── Step 3: Normalize DSM ─────────────────────────────────────────
        publish_progress(task_id, "normalizing", 30,
                         "Computing normalized elevation model...")

        from app.core.dsm_normalizer import normalize_dsm

        norm_result = normalize_dsm(
            dsm_path=local_dsm,
            dtm_path=local_dtm,
            max_height_clamp=200.0,
            smoothing_sigma=0.0,
        )

        logger.info(
            f"[DSM-NORM-TASK] Normalized via '{norm_result.method}': "
            f"range=[{norm_result.statistics.min_m:.2f}, "
            f"{norm_result.statistics.max_m:.2f}]m, "
            f"offset=({norm_result.offset.x:.8f}, "
            f"{norm_result.offset.y:.8f}, "
            f"{norm_result.offset.z:.2f})"
        )

        # ── Step 4: Convert to COG ────────────────────────────────────────
        publish_progress(task_id, "converting", 50, "Converting to COG...")
        from app.core.cog_converter import convert_to_cog
        norm_cog = convert_to_cog(norm_result.normalized_path)

        # ── Step 5: Write offset file ─────────────────────────────────────
        offset_path = os.path.join(work_dir, "offset.xyz")
        norm_result.offset.to_file(offset_path)

        # ── Step 6: Upload to MinIO ───────────────────────────────────────
        publish_progress(task_id, "uploading", 65, "Uploading normalized DSM...")
        from app.core.storage import upload_file as minio_upload

        tenant_prefix = dsm_asset_key.split("/")[0] if "/" in dsm_asset_key else "default"

        # Upload normalized DSM (replace original)
        norm_key = f"{tenant_prefix}/{project_id}/dsm/{os.path.basename(str(norm_cog))}"
        with open(norm_cog, "rb") as f:
            norm_size = os.path.getsize(str(norm_cog))
            minio_upload(
                dsm_bucket, norm_key, f, norm_size,
                content_type="image/tiff",
                metadata={
                    "asset_type": "dsm",
                    "project_id": project_id,
                    "dsm_source": "real",
                    "normalization": norm_result.method,
                },
            )
        logger.info(f"[DSM-NORM-TASK] Uploaded normalized DSM: {norm_key}")

        # Upload offset.xyz
        offset_key = f"{tenant_prefix}/{project_id}/offset_xyz/offset.xyz"
        with open(offset_path, "rb") as f:
            offset_size = os.path.getsize(offset_path)
            minio_upload(
                dsm_bucket, offset_key, f, offset_size,
                content_type="text/plain",
                metadata={"asset_type": "offset_xyz", "project_id": project_id},
            )
        logger.info(f"[DSM-NORM-TASK] Uploaded offset: {offset_key}")

        # ── Step 7: Update database ───────────────────────────────────────
        publish_progress(task_id, "database", 85, "Updating database records...")

        from sqlalchemy import create_engine, text as sa_text
        import json as json_mod

        engine = create_engine(settings.database_url_sync)
        try:
            with engine.connect() as conn:
                # Find tenant schemas
                tenant_rows = conn.execute(sa_text(
                    "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'tenant_%'"
                )).fetchall()
                if tenant_rows:
                    schemas = ", ".join(r[0] for r in tenant_rows)
                    conn.execute(sa_text(f"SET search_path TO {schemas}, public, topology"))

                # Update DSM flight_asset metadata with normalization info
                norm_metadata = {
                    "dsm_source": "real",
                    "normalization_method": norm_result.method,
                    "offset_xyz": norm_result.offset.to_dict(),
                    "elevation_stats": norm_result.statistics.to_dict(),
                    "normalized_dsm_key": norm_key,
                    "offset_key": offset_key,
                }

                conn.execute(sa_text(
                    "UPDATE flight_assets SET "
                    "metadata_json = COALESCE(metadata_json, '{}'::jsonb) || "
                    "CAST(:meta AS jsonb), "
                    "file_key = :new_key "
                    "WHERE flight_id = CAST(:fid AS uuid) AND asset_type = 'dsm'"
                ), {
                    "meta": json_mod.dumps(norm_metadata),
                    "new_key": norm_key,
                    "fid": flight_id,
                })
                conn.commit()
                logger.info("[DSM-NORM-TASK] Updated DSM flight_asset metadata")
        except Exception as e:
            logger.warning(f"[DSM-NORM-TASK] DB update failed: {e}")
        finally:
            engine.dispose()

        publish_progress(task_id, "completed", 100,
                         "DSM normalization complete ✓",
                         extra={
                             "norm_key": norm_key,
                             "offset_key": offset_key,
                             "method": norm_result.method,
                         })

        return {
            "status": "completed",
            "normalized_dsm_key": norm_key,
            "offset_key": offset_key,
            "method": norm_result.method,
            "statistics": norm_result.statistics.to_dict(),
            "offset": norm_result.offset.to_dict(),
        }

    except Exception as exc:
        logger.error(f"[DSM-NORM-TASK] Failed: {exc}")
        publish_progress(task_id, "failed", 0, str(exc))
        raise self.retry(exc=exc, max_retries=1)
    finally:
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
