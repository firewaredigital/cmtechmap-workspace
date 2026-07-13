"""
CM TECHMAP — Celery Processing Tasks
Real photogrammetry pipeline: upload → NodeODM → COG → MinIO → DB.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from io import BytesIO
from pathlib import Path

from celery import shared_task

from app.config import get_settings
from app.core.pubsub import publish_progress

logger = logging.getLogger(__name__)
settings = get_settings()


def _run_async(coro):
    """Helper to run async code from sync Celery tasks."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@shared_task(
    name="app.tasks.processing.process_drone_upload",
    bind=True,
    max_retries=2,
    default_retry_delay=120,
    queue="processing",
    time_limit=14400,      # 4 hours hard limit
    soft_time_limit=14100,  # 3h55m soft limit
)
def process_drone_upload(
    self,
    upload_id: str,
    file_key: str,
    bucket: str,
    flight_id: str | None = None,
    project_id: str | None = None,
    odm_options: dict | None = None,
):
    """
    Full photogrammetry pipeline with 7 stages:

    1. VALIDATION — Verify upload record and file existence
    2. PREPARATION — Download images from MinIO to temp workspace
    3. ODM_SUBMISSION — Submit images to NodeODM for processing
    4. ODM_MONITORING — Poll NodeODM every 10s, publish progress
    5. ODM_DOWNLOAD — Download results (orthophoto, DSM, point cloud)
    6. POST_PROCESSING — Convert to COG, extract metadata
    7. PUBLICATION — Upload to MinIO, update database records
    """
    task_id = self.request.id
    work_dir = None

    try:
        # ── Stage 1: VALIDATION ──────────────────────────────────────────
        publish_progress(task_id, "validation", 0, "Validating upload...")
        self.update_state(state="PROGRESS", meta={"stage": "validation", "progress": 0})

        logger.info(f"[PIPELINE] Starting processing for upload {upload_id}")
        logger.info(f"  file_key={file_key}, bucket={bucket}")
        logger.info(f"  flight_id={flight_id}, project_id={project_id}")

        # Verify MinIO file exists
        from app.core.storage import get_minio_client
        client = get_minio_client()
        try:
            stat = client.stat_object(bucket, file_key)
            logger.info(f"  File verified: {stat.size} bytes")
        except Exception as e:
            publish_progress(task_id, "failed", 0, f"File not found: {e}")
            raise

        publish_progress(task_id, "validation", 10, "Upload validated ✓")

        # ── Stage 2: PREPARATION ─────────────────────────────────────────
        publish_progress(task_id, "preparation", 15, "Preparing workspace...")

        work_dir = tempfile.mkdtemp(prefix="cm_odm_")
        images_dir = os.path.join(work_dir, "images")
        results_dir = os.path.join(work_dir, "results")
        os.makedirs(images_dir, exist_ok=True)
        os.makedirs(results_dir, exist_ok=True)

        # List all image parts/files in the upload path
        prefix = "/".join(file_key.split("/")[:-1]) + "/"
        image_files = []

        objects = client.list_objects(bucket, prefix=prefix, recursive=True)
        for obj in objects:
            if obj.object_name and any(
                obj.object_name.lower().endswith(ext)
                for ext in (".jpg", ".jpeg", ".tif", ".tiff", ".png", ".dng")
            ):
                local_path = os.path.join(images_dir, os.path.basename(obj.object_name))
                client.fget_object(bucket, obj.object_name, local_path)
                image_files.append(local_path)

        if not image_files:
            # If no individual images found, treat the upload itself as content
            local_path = os.path.join(images_dir, os.path.basename(file_key))
            client.fget_object(bucket, file_key, local_path)
            image_files.append(local_path)

        logger.info(f"  Downloaded {len(image_files)} images to workspace")
        publish_progress(
            task_id, "preparation", 25,
            f"Downloaded {len(image_files)} images",
            extra={"images_count": len(image_files)},
        )

        # ── Stage 3: ODM SUBMISSION ──────────────────────────────────────
        publish_progress(task_id, "odm_submission", 30, "Submitting to NodeODM...")

        from app.core.odm_client import NodeODMClient, NodeODMError
        odm = NodeODMClient()

        # Check if NodeODM is available
        odm_healthy = _run_async(odm.is_healthy())

        if odm_healthy and len(image_files) >= 3:
            # Real ODM processing
            options = odm_options or json.loads(settings.nodeodm_default_options)
            task_name = f"CM-TECHMAP-{upload_id[:8]}"

            odm_uuid = _run_async(odm.create_task(image_files, options, task_name))
            logger.info(f"  ODM task created: {odm_uuid}")
            publish_progress(
                task_id, "odm_submission", 35,
                f"NodeODM task created: {odm_uuid}",
                extra={"odm_uuid": odm_uuid},
            )

            # ── Stage 4: ODM MONITORING ──────────────────────────────────
            publish_progress(task_id, "odm_processing", 40, "Processing in NodeODM...")

            poll_interval = settings.nodeodm_poll_interval
            max_polls = 1440  # 4 hours at 10s intervals

            for poll in range(max_polls):
                time.sleep(poll_interval)

                progress_info = _run_async(odm.get_task_progress(odm_uuid))
                odm_status = progress_info["status"]
                odm_progress = progress_info["progress"]

                # Map ODM progress (0-100) to our stage range (40-70)
                mapped_progress = 40 + int(odm_progress * 0.3)

                publish_progress(
                    task_id, "odm_processing", mapped_progress,
                    f"NodeODM: {odm_status} ({odm_progress}%)",
                    extra={
                        "odm_status": odm_status,
                        "odm_progress": odm_progress,
                        "processing_time": progress_info.get("processing_time", 0),
                    },
                )

                self.update_state(state="PROGRESS", meta={
                    "stage": "odm_processing",
                    "progress": mapped_progress,
                    "odm_status": odm_status,
                })

                if odm_status == "completed":
                    logger.info(f"  ODM processing completed!")
                    break
                elif odm_status in ("failed", "canceled"):
                    publish_progress(task_id, "failed", mapped_progress,
                                     f"NodeODM {odm_status}")
                    raise RuntimeError(f"NodeODM task {odm_status}")

            # ── Stage 5: ODM DOWNLOAD ────────────────────────────────────
            publish_progress(task_id, "odm_download", 72, "Downloading results from NodeODM...")

            # Capture processing log for diagnostics before downloading
            odm_log = _run_async(odm.get_task_output_log(odm_uuid, tail=50))
            if odm_log:
                logger.info(f"[PIPELINE] ODM processing log (last 50 lines):")
                for log_line in odm_log[-10:]:
                    logger.info(f"  [ODM-LOG] {log_line}")

            # Use bulk download with asset discovery (includes textured_model.zip)
            downloaded_paths = _run_async(
                odm.download_all_assets(
                    odm_uuid, results_dir, include_3d_model=True
                )
            )
            # Convert to legacy format for downstream compatibility
            assets_downloaded = {k: str(v) for k, v in downloaded_paths.items()}

            publish_progress(
                task_id, "odm_download", 78,
                f"Downloaded {len(assets_downloaded)} assets: {list(assets_downloaded.keys())}",
            )

            # Clean up ODM task
            _run_async(odm.remove_task(odm_uuid))

        else:
            # NodeODM not available or insufficient images — simulate
            logger.warning("  NodeODM unavailable or <3 images. Running simulation.")
            publish_progress(task_id, "odm_processing", 40,
                             "NodeODM unavailable — running simulation...")

            # Simulate processing stages
            for pct in range(40, 78, 5):
                time.sleep(2)
                publish_progress(task_id, "odm_processing", pct,
                                 f"Simulated processing ({pct}%)...")

            assets_downloaded = {}
            publish_progress(task_id, "odm_download", 78,
                             "Simulation complete (no real assets)")

        # ── Stage 6: POST-PROCESSING ─────────────────────────────────────
        publish_progress(task_id, "post_processing", 80,
                         "Converting to COG format...")

        processed_assets = {}

        if "orthophoto.tif" in assets_downloaded:
            from app.core.cog_converter import convert_to_cog, extract_geospatial_metadata

            ortho_path = assets_downloaded["orthophoto.tif"]
            cog_path = convert_to_cog(ortho_path)
            ortho_meta = extract_geospatial_metadata(cog_path)
            processed_assets["orthomosaic"] = {
                "local_path": str(cog_path),
                "metadata": ortho_meta,
            }
            publish_progress(task_id, "post_processing", 82,
                             "Orthomosaic COG created ✓")

        # ── 6B: DSM Normalization + Offset Extraction ─────────────────────
        dsm_raw_path = assets_downloaded.get("dsm.tif")
        dtm_raw_path = assets_downloaded.get("dtm.tif")

        if dsm_raw_path:
            from app.core.cog_converter import convert_to_cog, extract_geospatial_metadata, extract_elevation_stats
            from app.core.dsm_normalizer import normalize_dsm, extract_offset_metadata

            publish_progress(task_id, "post_processing", 83,
                             "Normalizing DSM elevation data...")

            # Normalize DSM: compute nDSM (DSM - DTM) or subtract minimum
            try:
                norm_result = normalize_dsm(
                    dsm_path=dsm_raw_path,
                    dtm_path=dtm_raw_path,
                    max_height_clamp=200.0,
                    smoothing_sigma=0.0,  # No smoothing for real photogrammetric data
                )
                logger.info(
                    f"[PIPELINE] DSM normalized via '{norm_result.method}': "
                    f"range=[{norm_result.statistics.min_m:.2f}, "
                    f"{norm_result.statistics.max_m:.2f}]m"
                )

                # Convert normalized DSM to COG
                norm_cog = convert_to_cog(norm_result.normalized_path)
                dsm_meta = extract_geospatial_metadata(norm_cog)
                dsm_elev = extract_elevation_stats(norm_cog)

                # Store offset.xyz file for RTE rendering
                offset_path = os.path.join(results_dir, "offset.xyz")
                norm_result.offset.to_file(offset_path)

                processed_assets["dsm"] = {
                    "local_path": str(norm_cog),
                    "metadata": {
                        **dsm_meta,
                        **dsm_elev,
                        "dsm_source": "real",
                        "normalization_method": norm_result.method,
                        "offset_xyz": norm_result.offset.to_dict(),
                        "elevation_stats": norm_result.statistics.to_dict(),
                    },
                }
                processed_assets["offset_xyz"] = {
                    "local_path": offset_path,
                    "metadata": {
                        "offset": norm_result.offset.to_dict(),
                        "dsm_source": "real",
                    },
                }

            except Exception as e:
                logger.warning(f"[PIPELINE] DSM normalization failed, using raw: {e}")
                # Fallback: use raw DSM without normalization
                dsm_cog = convert_to_cog(dsm_raw_path)
                dsm_meta = extract_geospatial_metadata(dsm_cog)
                dsm_elev = extract_elevation_stats(dsm_cog)
                processed_assets["dsm"] = {
                    "local_path": str(dsm_cog),
                    "metadata": {**dsm_meta, **dsm_elev, "dsm_source": "real"},
                }

            publish_progress(task_id, "post_processing", 86,
                             "DSM normalized + offset extracted ✓")

        if dtm_raw_path and "dsm" in processed_assets:
            # DTM was already used for nDSM computation
            # Still convert DTM to COG for archival
            try:
                from app.core.cog_converter import convert_to_cog, extract_elevation_stats
                dtm_cog = convert_to_cog(dtm_raw_path)
                dtm_elev = extract_elevation_stats(dtm_cog)
                processed_assets["dtm"] = {
                    "local_path": str(dtm_cog),
                    "metadata": dtm_elev,
                }
            except Exception as e:
                logger.warning(f"[PIPELINE] DTM COG conversion failed: {e}")
        elif dtm_raw_path:
            from app.core.cog_converter import convert_to_cog, extract_elevation_stats
            dtm_cog = convert_to_cog(dtm_raw_path)
            dtm_elev = extract_elevation_stats(dtm_cog)
            processed_assets["dtm"] = {
                "local_path": str(dtm_cog),
                "metadata": dtm_elev,
            }

        # ── 6C: 3D Textured Model Conversion (OBJ → glTF/glb) ────────────
        if "textured_model.zip" in assets_downloaded:
            publish_progress(task_id, "post_processing", 87,
                             "Converting 3D model to web format...")

            try:
                from app.core.model_converter import (
                    extract_textured_model,
                    compute_model_offset,
                    convert_obj_to_gltf,
                    get_model_metadata,
                )

                model_extract_dir = os.path.join(work_dir, "model_extract")
                model_zip = assets_downloaded["textured_model.zip"]

                # Step 1: Extract ZIP
                model_files = extract_textured_model(model_zip, model_extract_dir)

                if model_files.is_valid():
                    # Step 2: Compute vertex barycenter (offset for RTE)
                    model_offset = compute_model_offset(model_files.obj_path)

                    # Step 3: Convert OBJ → glb (binary glTF)
                    glb_path = os.path.join(results_dir, "model.glb")
                    converted_path = convert_obj_to_gltf(
                        model_files.obj_path,
                        output_path=glb_path,
                        binary=True,
                    )

                    # Step 4: Extract model metadata
                    model_meta = get_model_metadata(converted_path, model_offset)

                    # Write model offset to file
                    model_offset_path = os.path.join(results_dir, "model_offset.xyz")
                    with open(model_offset_path, "w") as f:
                        f.write(
                            f"{model_offset.x:.10f} {model_offset.y:.10f} "
                            f"{model_offset.z:.6f}\n"
                        )
                        f.write(f"# Vertex count: {model_offset.vertex_count}\n")
                        f.write(f"# Generated by CM TECHMAP Model Converter\n")

                    processed_assets["model_3d"] = {
                        "local_path": str(converted_path),
                        "metadata": {
                            **model_meta.to_dict(),
                            "model_offset": model_offset.to_dict(),
                            "dsm_source": "real",
                        },
                    }
                    processed_assets["model_offset"] = {
                        "local_path": model_offset_path,
                        "metadata": {
                            "offset": model_offset.to_dict(),
                        },
                    }
                    logger.info(
                        f"[PIPELINE] 3D model converted: "
                        f"{model_meta.vertex_count:,} vertices, "
                        f"{model_meta.triangle_count:,} triangles, "
                        f"{model_meta.file_size_bytes / 1024 / 1024:.1f} MB"
                    )
                else:
                    logger.warning("[PIPELINE] Extracted model files are invalid")

            except Exception as e:
                logger.warning(f"[PIPELINE] 3D model conversion failed (non-critical): {e}")

        # ── 6D: Point Cloud Processing + Gaussian Splat Conversion ─────────
        # NodeODM downloads georeferenced_model.laz as "pointcloud.laz".
        # We process it in two ways:
        #   1. Upload raw LAZ as "point_cloud" asset
        #   2. Convert to .splat binary for Gaussian Splatting frontend
        pointcloud_path = assets_downloaded.get("georeferenced_model.laz")
        if pointcloud_path and os.path.exists(str(pointcloud_path)):
            publish_progress(task_id, "post_processing", 88,
                             "Processing point cloud and generating Gaussian Splats...")

            # Step 1: Register raw point cloud as asset
            pc_path = Path(str(pointcloud_path))
            processed_assets["point_cloud"] = {
                "local_path": str(pc_path),
                "metadata": {
                    "format": "laz",
                    "file_size_bytes": pc_path.stat().st_size,
                },
            }
            logger.info(f"[PIPELINE] Point cloud registered: {pc_path.stat().st_size / 1024 / 1024:.1f} MB")

            # Step 2: Convert point cloud to .splat for Gaussian Splatting
            try:
                from app.core.splat_converter import convert_pointcloud_to_splat

                splat_output = os.path.join(results_dir, "gaussian.splat")
                splat_path, splat_meta, splat_offset = convert_pointcloud_to_splat(
                    input_path=str(pointcloud_path),
                    output_path=splat_output,
                )

                processed_assets["gaussian_splat"] = {
                    "local_path": str(splat_path),
                    "metadata": {
                        **splat_meta.to_dict(),
                        "splat_offset": splat_offset.to_dict(),
                    },
                }
                logger.info(
                    f"[PIPELINE] Gaussian Splat generated: "
                    f"{splat_meta.splat_count:,} splats, "
                    f"{splat_meta.file_size_bytes / 1024 / 1024:.1f} MB"
                )
            except Exception as e:
                logger.warning(f"[PIPELINE] Gaussian Splat conversion failed (non-critical): {e}")

        publish_progress(task_id, "post_processing", 89,
                         f"Post-processing complete: {list(processed_assets.keys())}")

        # ── Stage 7: PUBLICATION ─────────────────────────────────────────
        publish_progress(task_id, "publication", 90,
                         "Uploading results to storage...")

        from app.core.storage import upload_file as minio_upload

        # Determine tenant prefix from file_key
        tenant_prefix = file_key.split("/")[0] if "/" in file_key else "default"
        pid = project_id or "unknown"

        for asset_type, asset_info in processed_assets.items():
            local_path = Path(asset_info["local_path"])
            if not local_path.exists():
                continue

            # Determine target bucket based on asset type
            if asset_type == "orthomosaic":
                target_bucket = settings.minio_bucket_orthomosaics
            elif asset_type in ("dsm", "dtm"):
                target_bucket = settings.minio_bucket_elevation_models
            elif asset_type == "point_cloud":
                target_bucket = settings.minio_bucket_point_clouds
            elif asset_type in ("model_3d", "model_offset", "offset_xyz", "gaussian_splat"):
                target_bucket = settings.minio_bucket_3d_models
            else:
                target_bucket = settings.minio_bucket_orthomosaics

            object_key = f"{tenant_prefix}/{pid}/{asset_type}/{local_path.name}"

            # Determine content type based on file extension
            suffix = local_path.suffix.lower()
            content_type_map = {
                ".tif": "image/tiff",
                ".tiff": "image/tiff",
                ".glb": "model/gltf-binary",
                ".gltf": "model/gltf+json",
                ".obj": "model/obj",
                ".xyz": "text/plain",
                ".json": "application/json",
                ".geojson": "application/geo+json",
                ".laz": "application/octet-stream",
                ".las": "application/octet-stream",
                ".splat": "application/octet-stream",
                ".ply": "application/octet-stream",
            }
            content_type = content_type_map.get(suffix, "application/octet-stream")

            with open(local_path, "rb") as f:
                file_size = local_path.stat().st_size
                minio_upload(
                    target_bucket, object_key, f, file_size,
                    content_type=content_type,
                    metadata={"asset_type": asset_type, "project_id": pid},
                )

            asset_info["object_key"] = object_key
            asset_info["bucket"] = target_bucket
            logger.info(f"  Uploaded {asset_type} → {target_bucket}/{object_key}")

        publish_progress(task_id, "publication", 93,
                         "Updating database records...")

        # Update database records (sync since we're in Celery)
        _update_database_records(upload_id, project_id, processed_assets, flight_id=flight_id)

        # ── Stage 8: CONDITIONAL 3D PIPELINE ─────────────────────────────
        # Determine DSM source: real (NodeODM) vs synthetic (fallback)
        has_real_dsm = "dsm" in processed_assets
        dsm_source = "real" if has_real_dsm else "synthetic"
        logger.info(f"[PIPELINE] DSM source: {dsm_source}")

        resolved_flight_id = flight_id
        if not resolved_flight_id and project_id:
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(settings.database_url_sync)
            try:
                with engine.connect() as conn:
                    result = conn.execute(sa_text(
                        "SELECT id FROM flights WHERE project_id = :pid "
                        "ORDER BY created_at DESC LIMIT 1"
                    ), {"pid": project_id})
                    row = result.fetchone()
                    if row:
                        resolved_flight_id = str(row[0])
            finally:
                engine.dispose()

        if has_real_dsm and resolved_flight_id and project_id:
            # Real DSM from NodeODM — extract buildings from real elevation data
            publish_progress(task_id, "building_extraction", 95,
                             "Extracting 3D buildings from real DSM...")
            try:
                dsm_info = processed_assets["dsm"]
                dtm_info = processed_assets.get("dtm")
                from app.tasks.post_processing import extract_buildings_from_real_dsm
                extract_buildings_from_real_dsm.delay(
                    dsm_asset_key=dsm_info["object_key"],
                    dsm_bucket=dsm_info["bucket"],
                    dtm_asset_key=dtm_info["object_key"] if dtm_info else None,
                    dtm_bucket=dtm_info["bucket"] if dtm_info else None,
                    flight_id=resolved_flight_id,
                    project_id=project_id,
                    dsm_source="real",
                )
                logger.info("[PIPELINE] Queued building extraction from REAL DSM")
            except Exception as e:
                logger.warning(f"[PIPELINE] Building extraction from real DSM failed to queue: {e}")

        elif not has_real_dsm and project_id:
            # No real DSM — check if there's an orthomosaic to run synthetic fallback
            ortho_info = processed_assets.get("orthomosaic")
            if ortho_info and resolved_flight_id:
                publish_progress(task_id, "synthetic_dsm", 95,
                                 "No real DSM — generating synthetic terrain (fallback)...")
                try:
                    from app.tasks.post_processing import generate_dsm_and_buildings
                    generate_dsm_and_buildings.delay(
                        orthomosaic_asset_id="synthetic-fallback",
                        orthomosaic_bucket=ortho_info["bucket"],
                        orthomosaic_key=ortho_info["object_key"],
                        flight_id=resolved_flight_id,
                        project_id=project_id,
                    )
                    logger.info("[PIPELINE] Queued SYNTHETIC DSM fallback pipeline")
                except Exception as e:
                    logger.warning(f"[PIPELINE] Synthetic DSM fallback failed to queue: {e}")

        # ── COMPLETE ─────────────────────────────────────────────────────
        publish_progress(task_id, "completed", 100,
                         "Processing pipeline complete ✓",
                         extra={
                             "assets": list(processed_assets.keys()),
                             "dsm_source": dsm_source,
                         })

        logger.info(f"[PIPELINE] Upload {upload_id} processing COMPLETE (dsm_source={dsm_source})")

        # ── AUTO-TRIGGER: AI Pipeline ─────────────────────────────────
        ortho_info = processed_assets.get("orthomosaic")
        dsm_info_for_ai = processed_assets.get("dsm")
        if ortho_info and project_id:
            try:
                run_ai_pipeline.apply_async(
                    kwargs={
                        "orthomosaic_id": ortho_info.get("asset_id", upload_id),
                        "file_key": ortho_info["object_key"],
                        "dsm_file_key": dsm_info_for_ai["object_key"] if dsm_info_for_ai else None,
                        "flight_id": resolved_flight_id,
                        "project_id": project_id,
                    },
                    countdown=5,  # 5s delay to let DB commits settle
                )
                logger.info("[PIPELINE] AI pipeline auto-triggered for orthomosaic")
            except Exception as e:
                logger.warning(f"[PIPELINE] AI pipeline auto-trigger failed: {e}")

        # ── NOTIFY: Processing complete ───────────────────────────────
        try:
            from app.services.notification_service import NotificationService
            _run_async(NotificationService.notify_processing_complete(
                project_id=project_id or upload_id,
                project_name=f"Upload {upload_id[:8]}",
                assets_count=len(processed_assets),
            ))
        except Exception as e:
            logger.warning(f"[PIPELINE] Notification failed (non-critical): {e}")

        return {
            "status": "completed",
            "upload_id": upload_id,
            "assets_generated": list(processed_assets.keys()),
            "dsm_source": dsm_source,
            "task_id": task_id,
        }

    except Exception as exc:
        logger.error(f"[PIPELINE] Processing failed for {upload_id}: {exc}")
        publish_progress(task_id, "failed", 0, str(exc))

        # Notify failure
        try:
            from app.services.notification_service import NotificationService
            _run_async(NotificationService.notify_processing_failed(
                project_id=project_id or upload_id,
                project_name=f"Upload {upload_id[:8]}",
                error_message=str(exc)[:200],
            ))
        except Exception:
            pass

        raise self.retry(exc=exc)

    finally:
        # Cleanup temp workspace
        if work_dir and os.path.exists(work_dir):
            try:
                shutil.rmtree(work_dir)
                logger.info(f"  Cleaned up workspace: {work_dir}")
            except Exception:
                pass


def _update_database_records(
    upload_id: str,
    project_id: str | None,
    assets: dict,
    flight_id: str | None = None,
) -> None:
    """
    Update DB records after processing (synchronous for Celery).

    Inserts each generated asset into `flight_assets` with full geospatial
    metadata (bounds, CRS, resolution), and propagates bounding box to
    the parent project for spatial queries.
    """
    from sqlalchemy import create_engine, text as sa_text
    from app.config import get_settings
    import json

    s = get_settings()
    engine = create_engine(s.database_url_sync)

    try:
        with engine.connect() as conn:
            # ── Update upload status ──────────────────────────────────
            conn.execute(sa_text(
                "UPDATE uploads SET status = 'completed' WHERE id = :id"
            ), {"id": upload_id})

            # ── Resolve flight_id ─────────────────────────────────────
            resolved_flight_id = flight_id
            if not resolved_flight_id and project_id:
                # Get latest flight for this project
                result = conn.execute(sa_text(
                    "SELECT id FROM flights WHERE project_id = :pid "
                    "ORDER BY created_at DESC LIMIT 1"
                ), {"pid": project_id})
                row = result.fetchone()
                if row:
                    resolved_flight_id = str(row[0])

            # ── Insert each asset into flight_assets ──────────────────
            project_bounds = {"west": None, "south": None, "east": None, "north": None}

            for asset_type, asset_info in assets.items():
                meta = asset_info.get("metadata", {})
                bounds = meta.get("bounds", {})
                object_key = asset_info.get("object_key", "")
                bucket = asset_info.get("bucket", "")

                if not object_key:
                    continue

                # Determine content_type based on asset type
                ct_map = {
                    "orthomosaic": "image/tiff",
                    "dsm": "image/tiff",
                    "dtm": "image/tiff",
                    "point_cloud": "application/octet-stream",
                    "model_3d": "model/gltf-binary",
                    "model_offset": "text/plain",
                    "gaussian_splat": "application/octet-stream",
                }
                ct = ct_map.get(asset_type, "application/octet-stream")

                # Insert into flight_assets
                insert_params = {
                    "flight_id": resolved_flight_id,
                    "asset_type": asset_type,
                    "file_key": object_key,
                    "bucket_name": bucket,
                    "file_size_bytes": meta.get("file_size_bytes"),
                    "content_type": ct,
                    "resolution_cm": meta.get("resolution_cm"),
                    "cog_validated": asset_type in ("orthomosaic", "dsm", "dtm"),
                    "crs_epsg": meta.get("srid", 4326),
                    "bbox_min_lon": bounds.get("west"),
                    "bbox_min_lat": bounds.get("south"),
                    "bbox_max_lon": bounds.get("east"),
                    "bbox_max_lat": bounds.get("north"),
                    "metadata_json": json.dumps(meta) if meta else None,
                }

                if resolved_flight_id:
                    conn.execute(sa_text(
                        "INSERT INTO flight_assets "
                        "(flight_id, asset_type, file_key, bucket_name, "
                        "file_size_bytes, content_type, resolution_cm, "
                        "cog_validated, crs_epsg, "
                        "bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, "
                        "metadata_json) "
                        "VALUES (:flight_id, :asset_type, :file_key, :bucket_name, "
                        ":file_size_bytes, :content_type, :resolution_cm, "
                        ":cog_validated, :crs_epsg, "
                        ":bbox_min_lon, :bbox_min_lat, :bbox_max_lon, :bbox_max_lat, "
                        "CAST(:metadata_json AS jsonb))"
                    ), insert_params)

                    logger.info(
                        f"  Inserted flight_asset: {asset_type} "
                        f"(key={object_key}, bounds={bounds})"
                    )

                # Track project-level bounds (union of all assets)
                if bounds:
                    for k in ("west", "south", "east", "north"):
                        val = bounds.get(k)
                        if val is not None:
                            if k in ("west", "south"):
                                if project_bounds[k] is None or val < project_bounds[k]:
                                    project_bounds[k] = val
                            else:
                                if project_bounds[k] is None or val > project_bounds[k]:
                                    project_bounds[k] = val

            # ── Update project with status + bounding box ─────────────
            if project_id:
                update_parts = [
                    "status = 'processado'",
                    "image_count = COALESCE(image_count, 0) + 1",
                    "updated_at = NOW()",
                ]
                update_params: dict = {"pid": project_id}

                if project_bounds["west"] is not None:
                    update_parts.extend([
                        "bbox_min_lon = :bw",
                        "bbox_min_lat = :bs",
                        "bbox_max_lon = :be",
                        "bbox_max_lat = :bn",
                    ])
                    update_params.update({
                        "bw": project_bounds["west"],
                        "bs": project_bounds["south"],
                        "be": project_bounds["east"],
                        "bn": project_bounds["north"],
                    })

                conn.execute(sa_text(
                    f"UPDATE projects SET {', '.join(update_parts)} "
                    f"WHERE id = :pid"
                ), update_params)

            # ── Update flight status to completed ─────────────────────
            if resolved_flight_id:
                conn.execute(sa_text(
                    "UPDATE flights SET status = 'completed', "
                    "updated_at = NOW() WHERE id = :fid"
                ), {"fid": resolved_flight_id})

            conn.commit()
            logger.info(
                f"  DB records updated: upload={upload_id}, "
                f"project={project_id}, flight={resolved_flight_id}, "
                f"assets={list(assets.keys())}"
            )

    except Exception as e:
        logger.error(f"  DB update failed: {e}")
    finally:
        engine.dispose()


@shared_task(
    name="app.tasks.processing.run_ai_pipeline",
    bind=True,
    queue="processing",
    time_limit=3600,
)
def run_ai_pipeline(
    self,
    orthomosaic_id: str,
    file_key: str,
    dsm_file_key: str | None = None,
    flight_id: str | None = None,
    project_id: str | None = None,
    ai_provider_override: str | None = None,
):
    """
    Run AI building segmentation on a generated orthomosaic.
    
    Pipeline:
    1. Download orthomosaic + DSM from MinIO
    2. Run building footprint extraction
    3. Upload GeoJSON results to MinIO
    4. Write detections to ai_detections table
    """
    task_id = self.request.id
    publish_progress(task_id, "ai_pipeline", 0, "Iniciando pipeline de IA...")
    logger.info(f"[AI] Pipeline started: orthomosaic={orthomosaic_id}, dsm={dsm_file_key}")

    try:
        from minio import Minio
        from app.core.building_extractor import extract_building_footprints
        from app.core.terrain_extractor import extract_terrain_footprints
        from app.services.groq_inference import (
            GroqInferenceService,
            normalize_vision_output_to_features,
        )
        import json

        minio_client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

        workdir = Path(tempfile.mkdtemp(prefix="ai_pipeline_"))

        # ── Step 1: Download orthomosaic ───────────────────────────
        publish_progress(task_id, "ai_download", 10, "Baixando ortomosaico...")
        ortho_local = workdir / "orthomosaic.tif"
        minio_client.fget_object(settings.minio_bucket_orthomosaics, file_key, str(ortho_local))
        logger.info(f"[AI] Downloaded orthomosaic: {ortho_local.stat().st_size / 1024 / 1024:.1f} MB")

        # ── Step 2: Download or generate DSM ──────────────────────
        publish_progress(task_id, "ai_download_dsm", 20, "Preparando modelo de elevação...")
        dsm_local = workdir / "dsm.tif"
        if dsm_file_key:
            try:
                minio_client.fget_object(settings.minio_bucket_elevation_models, dsm_file_key, str(dsm_local))
                logger.info(f"[AI] Downloaded DSM: {dsm_local.stat().st_size / 1024 / 1024:.1f} MB")
            except Exception as e:
                logger.warning(f"[AI] DSM download failed, generating synthetic: {e}")
                from app.core.dsm_generator import generate_synthetic_dsm
                generate_synthetic_dsm(str(ortho_local), str(dsm_local))
        else:
            # Generate synthetic DSM
            from app.core.dsm_generator import generate_synthetic_dsm
            generate_synthetic_dsm(str(ortho_local), str(dsm_local))
            logger.info("[AI] Generated synthetic DSM")

        # ── Step 3: Run extraction (Groq-first in hybrid mode, local fallback) ──
        publish_progress(task_id, "ai_extraction", 40, "Detectando edificações...")
        output_geojson = workdir / "buildings.geojson"
        terrain_geojson_path = workdir / "terrain.geojson"

        features = []
        terrain_features = []
        used_model_version = "building_extractor_v1"
        used_terrain_model_version = "terrain_extractor_v1"

        provider_mode = (ai_provider_override or settings.ai_provider or "local").strip().lower()
        use_groq = settings.groq_enabled and provider_mode in ("groq", "hybrid")
        if use_groq:
            try:
                groq_service = GroqInferenceService()
                vision_result = groq_service.analyze_orthomosaic(
                    image_path=str(ortho_local),
                    dsm_path=str(dsm_local),
                    project_context={
                        "project_id": project_id,
                        "orthomosaic_id": orthomosaic_id,
                        "file_key": file_key,
                    },
                )
                features, terrain_features = normalize_vision_output_to_features(vision_result)
                used_model_version = str(vision_result.get("_meta", {}).get("model") or settings.groq_vision_model)
                used_terrain_model_version = used_model_version
                logger.info("[AI] Groq vision extraction succeeded: buildings=%s terrain=%s", len(features), len(terrain_features))
            except Exception as exc:
                logger.warning("[AI] Groq vision failed, local fallback active: %s", exc)
                if provider_mode == "groq":
                    raise

        if not features:
            result_path = extract_building_footprints(
                orthophoto_path=ortho_local,
                dsm_path=dsm_local,
                output_path=output_geojson,
                min_height_m=2.5,
                base_elevation=800.0,
            )
            logger.info(f"[AI] Building extraction complete: {result_path}")
            with open(result_path) as f:
                geojson_data = json.load(f)
            features = geojson_data.get("features", [])
        else:
            result_path = output_geojson
            with open(result_path, "w", encoding="utf-8") as bf:
                json.dump({"type": "FeatureCollection", "features": features}, bf)

        total_buildings = len(features)
        logger.info(f"[AI] Detected {total_buildings} buildings")

        publish_progress(task_id, "ai_extraction", 55, "Detectando área de terreno...")
        if not terrain_features:
            terrain_path = extract_terrain_footprints(
                orthophoto_path=ortho_local,
                output_path=terrain_geojson_path,
                buildings_geojson_path=output_geojson,
            )
            with open(terrain_path) as tf:
                terrain_geojson = json.load(tf)
            terrain_features = terrain_geojson.get("features", [])
        else:
            terrain_path = terrain_geojson_path
            with open(terrain_path, "w", encoding="utf-8") as tf:
                json.dump({"type": "FeatureCollection", "features": terrain_features}, tf)

        total_terrain_patches = len(terrain_features)

        publish_progress(
            task_id,
            "ai_extraction",
            60,
            f"{total_buildings} edificações e {total_terrain_patches} áreas de terreno detectadas",
        )

        # ── Step 4: Upload GeoJSONs to MinIO ───────────────────────
        publish_progress(task_id, "ai_upload", 70, "Salvando resultados...")
        geojson_key = file_key.rsplit("/", 1)[0] + "/buildings.geojson" if "/" in file_key else f"buildings/{orthomosaic_id}.geojson"
        terrain_geojson_key = file_key.rsplit("/", 1)[0] + "/terrain.geojson" if "/" in file_key else f"terrain/{orthomosaic_id}.geojson"
        minio_client.fput_object(
            settings.minio_bucket_orthomosaics,
            geojson_key,
            str(result_path),
            content_type="application/geo+json",
        )
        minio_client.fput_object(
            settings.minio_bucket_orthomosaics,
            terrain_geojson_key,
            str(terrain_path),
            content_type="application/geo+json",
        )
        logger.info(f"[AI] Uploaded GeoJSON to {geojson_key}")
        logger.info(f"[AI] Uploaded terrain GeoJSON to {terrain_geojson_key}")

        # ── Step 5: Write detections to DB ────────────────────────
        publish_progress(task_id, "ai_db_write", 80, "Registrando detecções...")
        detections_written = 0
        total_built_area_sqm = 0.0
        if features:
            from sqlalchemy import create_engine, text as sa_text
            engine = create_engine(settings.database_url_sync, isolation_level="AUTOCOMMIT")
            try:
                with engine.connect() as conn:
                    for feature in features:
                        props = feature.get("properties", {})
                        geom = json.dumps(feature.get("geometry", {}))
                        try:
                            conn.execute(sa_text("""
                                INSERT INTO ai_detections
                                    (flight_asset_id, detection_class, polygon, confidence, area_sqm, perimeter_m, properties, model_version)
                                VALUES
                                    (:asset_id, :cls, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326),
                                     :conf, :area, :perim, :props, :model)
                            """), {
                                "asset_id": orthomosaic_id,
                                "cls": "building",
                                "geom": geom,
                                "conf": props.get("confidence", 0.85),
                                "area": props.get("area_sqm", 0),
                                "perim": props.get("perimeter_m", 0),
                                "props": json.dumps(props),
                                "model": used_model_version,
                            })
                            detections_written += 1
                            total_built_area_sqm += float(props.get("area_sqm", 0) or 0)
                        except Exception as e:
                            logger.warning(f"[AI] Failed to write detection: {e}")
            finally:
                engine.dispose()

        logger.info(f"[AI] Wrote {detections_written}/{total_buildings} detections to DB")

        # ── Step 6: Persist metrology run (terrain + buildings) ──────────
        run_id = _persist_ai_measurements(
            project_id=project_id,
            orthomosaic_id=orthomosaic_id,
            flight_id=flight_id,
            file_key=file_key,
            dsm_file_key=dsm_file_key,
            building_features=features,
            terrain_features=terrain_features,
            total_built_area_sqm=total_built_area_sqm,
            model_version=used_model_version,
            terrain_model_version=used_terrain_model_version,
        )

        # ── Cleanup ───────────────────────────────────────────────
        shutil.rmtree(workdir, ignore_errors=True)

        publish_progress(task_id, "completed", 100,
                         f"Pipeline concluído: {total_buildings} edificações detectadas")

        return {
            "status": "completed",
            "orthomosaic_id": orthomosaic_id,
            "total_buildings": total_buildings,
            "total_terrain_patches": total_terrain_patches,
            "detections_written": detections_written,
            "geojson_key": geojson_key,
            "terrain_geojson_key": terrain_geojson_key,
            "measurement_run_id": run_id,
        }

    except Exception as e:
        logger.error(f"[AI] Pipeline failed: {e}", exc_info=True)
        publish_progress(task_id, "error", -1, f"Falha no pipeline: {str(e)[:200]}")
        # Cleanup on failure
        if 'workdir' in locals():
            shutil.rmtree(workdir, ignore_errors=True)
        raise


@shared_task(
    name="app.tasks.processing.run_ai_pipeline_groq",
    bind=True,
    queue="ai_llm",
    time_limit=3600,
)
def run_ai_pipeline_groq(
    self,
    orthomosaic_id: str,
    file_key: str,
    dsm_file_key: str | None = None,
    flight_id: str | None = None,
    project_id: str | None = None,
):
    """Dedicated Groq queue entrypoint for AI extraction without shared global state mutation."""
    return run_ai_pipeline.run(
        orthomosaic_id=orthomosaic_id,
        file_key=file_key,
        dsm_file_key=dsm_file_key,
        flight_id=flight_id,
        project_id=project_id,
        ai_provider_override="groq",
    )


def _persist_ai_measurements(
    *,
    project_id: str | None,
    orthomosaic_id: str,
    flight_id: str | None,
    file_key: str,
    dsm_file_key: str | None,
    building_features: list[dict],
    terrain_features: list[dict],
    total_built_area_sqm: float,
    model_version: str = "building_extractor_v1",
    terrain_model_version: str = "terrain_extractor_v1",
) -> str | None:
    """Persist normalized building and terrain metrics for operational reporting.

    Safe fallback: if runtime tables are unavailable (migration not applied yet),
    logs warning and returns None without breaking the pipeline.
    """
    if not project_id:
        return None

    from sqlalchemy import create_engine, text as sa_text

    engine = create_engine(settings.database_url_sync, isolation_level="AUTOCOMMIT")
    run_id: str | None = None

    try:
        with engine.connect() as conn:
            run_res = conn.execute(sa_text("""
                INSERT INTO ai_measurement_runs (
                    project_id, flight_asset_id, flight_id,
                    orthomosaic_file_key, dsm_file_key,
                    status, model_version, terrain_model_version,
                    built_area_total_sqm, total_buildings, total_terrain_patches,
                    started_at, completed_at
                )
                VALUES (
                    CAST(:pid AS uuid), CAST(:asset_id AS uuid),
                    CASE WHEN :flight_id IS NULL THEN NULL ELSE CAST(:flight_id AS uuid) END,
                    :ortho_key, :dsm_key,
                    'completed', :model_version, :terrain_model_version,
                    :built_area, :building_count, :terrain_count,
                    NOW(), NOW()
                )
                RETURNING id
            """), {
                "pid": project_id,
                "asset_id": orthomosaic_id,
                "flight_id": flight_id,
                "ortho_key": file_key,
                "dsm_key": dsm_file_key,
                "model_version": model_version,
                "terrain_model_version": terrain_model_version,
                "built_area": total_built_area_sqm,
                "building_count": len(building_features),
                "terrain_count": len(terrain_features),
            })
            run_id = str(run_res.scalar())

            terrain_area_total = 0.0
            terrain_compactness_values: list[float] = []
            for feat in terrain_features:
                props = feat.get("properties", {})
                geom = feat.get("geometry")
                if not geom:
                    continue
                area = float(props.get("area_sqm", 0) or 0)
                terrain_area_total += area
                terrain_compactness_values.append(float(props.get("compactness", 0) or 0))
                conn.execute(sa_text("""
                    INSERT INTO ai_terrain_measurements (
                        run_id, confidence, area_sqm, perimeter_m,
                        compactness, surface_type, properties, polygon
                    )
                    VALUES (
                        CAST(:run_id AS uuid), :confidence, :area_sqm, :perimeter_m,
                        :compactness, :surface_type, CAST(:props AS jsonb),
                        ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)
                    )
                """), {
                    "run_id": run_id,
                    "confidence": float(props.get("confidence", 0.75) or 0.75),
                    "area_sqm": area,
                    "perimeter_m": float(props.get("perimeter_m", 0) or 0),
                    "compactness": float(props.get("compactness", 0) or 0),
                    "surface_type": str(props.get("surface_type", "terrain")),
                    "props": json.dumps(props),
                    "geom": json.dumps(geom),
                })

            building_confidences: list[float] = []
            for feat in building_features:
                props = feat.get("properties", {})
                geom = feat.get("geometry")
                if not geom:
                    continue

                area = float(props.get("area_sqm", props.get("area_m2", 0)) or 0)
                perimeter = float(props.get("perimeter_m", 0) or 0)
                height = float(props.get("height", props.get("height_m", 0)) or 0)
                compactness = float(props.get("compactness", 0) or 0)
                confidence = float(props.get("confidence", 0.85) or 0.85)
                building_confidences.append(confidence)

                floors_estimate = None
                if height > 0:
                    floors_estimate = max(1, int(round(height / 3.0)))

                conn.execute(sa_text("""
                    INSERT INTO ai_building_measurements (
                        run_id, confidence, area_sqm, perimeter_m,
                        height_m, floors_estimate, building_type,
                        compactness, quality_score, properties, polygon
                    )
                    VALUES (
                        CAST(:run_id AS uuid), :confidence, :area_sqm, :perimeter_m,
                        :height_m, :floors, :building_type,
                        :compactness, :quality_score, CAST(:props AS jsonb),
                        ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)
                    )
                """), {
                    "run_id": run_id,
                    "confidence": confidence,
                    "area_sqm": area,
                    "perimeter_m": perimeter,
                    "height_m": height,
                    "floors": floors_estimate,
                    "building_type": str(props.get("building_type", "unknown")),
                    "compactness": compactness,
                    "quality_score": confidence,
                    "props": json.dumps(props),
                    "geom": json.dumps(geom),
                })

            avg_building_conf = (
                sum(building_confidences) / len(building_confidences)
                if building_confidences else 0.0
            )
            avg_terrain_compactness = (
                sum(terrain_compactness_values) / len(terrain_compactness_values)
                if terrain_compactness_values else 0.0
            )
            occupation_ratio = (total_built_area_sqm / terrain_area_total) if terrain_area_total > 0 else 0.0
            coverage_component = max(0.0, min(1.0, 1.0 - abs(occupation_ratio - 0.35)))
            qa_score = (
                0.45 * max(0.0, min(1.0, avg_building_conf))
                + 0.30 * coverage_component
                + 0.25 * max(0.0, min(1.0, avg_terrain_compactness))
            )

            conn.execute(sa_text("""
                UPDATE ai_measurement_runs
                SET terrain_area_total_sqm = :terrain_area,
                    qa_score = :qa_score,
                    updated_at = NOW()
                WHERE id = CAST(:run_id AS uuid)
            """), {
                "run_id": run_id,
                "terrain_area": terrain_area_total,
                "qa_score": round(qa_score, 4),
            })

    except Exception as e:
        logger.warning("[AI] Measurement runtime persistence skipped: %s", e)
        return None
    finally:
        engine.dispose()

    return run_id


@shared_task(
    name="app.tasks.processing.generate_report",
    bind=True,
    queue="reports",
    time_limit=600,
)
def generate_report(self, project_id: str, report_type: str, parameters: dict):
    """Generate a PDF/Excel/CSV report for a project."""
    task_id = self.request.id
    publish_progress(task_id, "report_generation", 0, "Gerando relatório...")
    logger.info(f"[REPORT] Generating {report_type} report for project {project_id}")

    try:
        from sqlalchemy import create_engine, text as sa_text
        from app.services.report_generator import ReportGeneratorService

        engine = create_engine(settings.database_url_sync, isolation_level="AUTOCOMMIT")
        generator = ReportGeneratorService()

        with engine.connect() as conn:
            # Fetch project data
            publish_progress(task_id, "report_data", 10, "Coletando dados do projeto...")
            r = conn.execute(sa_text("""
                SELECT id, code, name, description, status, city, state,
                       area_sqm, flight_count, image_count, responsible,
                       bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat,
                       created_at
                FROM projects WHERE id = :pid
            """), {"pid": project_id})
            project_row = r.mappings().first()
            if not project_row:
                raise ValueError(f"Project {project_id} not found")
            project_data = dict(project_row)

            # Fetch flights
            r = conn.execute(sa_text("""
                SELECT flight_date, altitude_m, overlap_pct, sidelap_pct,
                       images_count, camera_model, gsd_cm, area_coverage_sqm, status
                FROM flights WHERE project_id = :pid AND is_active = true
                ORDER BY flight_date DESC
            """), {"pid": project_id})
            flights_data = [dict(row) for row in r.mappings().all()]

            # Fetch assets
            r = conn.execute(sa_text("""
                SELECT fa.asset_type, fa.file_key, fa.file_size_bytes,
                       fa.resolution_cm, fa.crs_epsg, fa.cog_validated
                FROM flight_assets fa
                JOIN flights f ON f.id = fa.flight_id
                WHERE f.project_id = :pid AND fa.is_active = true
            """), {"pid": project_id})
            assets_data = [dict(row) for row in r.mappings().all()]

            # Fetch building detections (for urban analytics)
            buildings_data = []
            try:
                r = conn.execute(sa_text("""
                    SELECT ad.area_sqm, ad.perimeter_m, ad.confidence,
                           (ad.properties->>'height_m')::float as height_m,
                           ad.detection_class
                    FROM ai_detections ad
                    JOIN flight_assets fa ON fa.id = ad.flight_asset_id
                    JOIN flights f ON f.id = fa.flight_id
                    WHERE f.project_id = :pid AND ad.detection_class = 'building'
                """), {"pid": project_id})
                buildings_data = [dict(row) for row in r.mappings().all()]
            except Exception:
                pass

        engine.dispose()

        # Generate report
        publish_progress(task_id, "report_render", 50, f"Renderizando {report_type.upper()}...")

        report_config = parameters.get("config", {})
        report_config["title"] = parameters.get("title", f"Relatório — {project_data.get('name', 'Projeto')}")

        if report_type == "pdf":
            report_bytes = generator.generate_pdf(
                project_data, flights_data, assets_data,
                report_config=report_config,
                buildings_data=buildings_data,
            )
            content_type = "application/pdf"
            ext = "pdf"
        elif report_type == "excel":
            report_bytes = generator.generate_excel(
                project_data, flights_data, assets_data,
                report_config=report_config,
            )
            content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ext = "xlsx"
        elif report_type == "csv":
            report_bytes = generator.generate_csv(
                project_data, flights_data, assets_data,
                buildings_data=buildings_data,
            )
            content_type = "text/csv"
            ext = "csv"
        else:
            raise ValueError(f"Unknown report type: {report_type}")

        # Upload to MinIO
        publish_progress(task_id, "report_upload", 80, "Salvando relatório...")
        from minio import Minio
        minio_client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )
        report_key = f"reports/{project_id}/{report_type}_{int(time.time())}.{ext}"
        minio_client.put_object(
            settings.minio_bucket_reports,
            report_key,
            BytesIO(report_bytes),
            len(report_bytes),
            content_type=content_type,
        )

        # Save report record to DB
        engine2 = create_engine(settings.database_url_sync, isolation_level="AUTOCOMMIT")
        try:
            with engine2.connect() as conn:
                conn.execute(sa_text("""
                    INSERT INTO reports (project_id, report_type, title, file_key, file_format, file_size_bytes, parameters)
                    VALUES (:pid, :type, :title, :key, :fmt, :size, :params)
                """), {
                    "pid": project_id,
                    "type": report_type,
                    "title": report_config.get("title", "Relatório"),
                    "key": report_key,
                    "fmt": ext.upper(),
                    "size": len(report_bytes),
                    "params": json.dumps(parameters),
                })
        finally:
            engine2.dispose()

        publish_progress(task_id, "completed", 100,
                         f"Relatório {report_type.upper()} gerado ({len(report_bytes) / 1024:.0f} KB)")

        return {
            "status": "completed",
            "project_id": project_id,
            "report_type": report_type,
            "file_key": report_key,
            "file_size": len(report_bytes),
        }

    except Exception as e:
        logger.error(f"[REPORT] Failed: {e}", exc_info=True)
        publish_progress(task_id, "error", -1, f"Falha: {str(e)[:200]}")
        raise

