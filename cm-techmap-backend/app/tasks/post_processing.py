"""
CM TECHMAP — Post-Processing Celery Tasks
Tasks for COG conversion, thumbnail generation, and metadata extraction.
"""

import contextlib
import json
import logging
import os
import shutil
import tempfile

from celery import shared_task

from app.config import get_settings
from app.core.pubsub import publish_progress
from app.tasks.processing import _apply_tenant_search_path

logger = logging.getLogger(__name__)
settings = get_settings()


def _features_to_detection_rows(features: list[dict]) -> tuple[list[dict], int]:
    """
    Convert extractor GeoJSON features into ai_detections INSERT parameters.

    The extractor emits `height`/`max_height`/`area_m2`; the fiscal analysis
    reads the `area_sqm` COLUMN and `properties->>'height_m'`. This mapping is
    the contract between the two — without it the malha fina sees nothing.

    Returns (rows, skipped_count). Non-polygon geometries are skipped, not
    fatal: one bad footprint must not discard the other hundred.
    """
    rows: list[dict] = []
    skipped = 0
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") not in ("Polygon", "MultiPolygon"):
            skipped += 1
            continue
        props = dict(feature.get("properties") or {})

        height_m = props.get("height_m", props.get("height"))
        area_sqm = props.get("area_sqm", props.get("area_m2"))
        # Normalize the keys downstream consumers read from `properties`
        if height_m is not None:
            props["height_m"] = height_m
        if area_sqm is not None:
            props["area_sqm"] = area_sqm

        rows.append({
            "geom": json.dumps(geometry),
            "conf": float(props.get("confidence", 0.75)),
            "area": float(area_sqm or 0.0),
            "perim": float(props.get("perimeter_m", 0.0) or 0.0),
            "height": float(height_m) if height_m is not None else None,
            "props": json.dumps(props),
        })
    return rows, skipped


def _persist_building_detections(
    conn,
    orthomosaic_asset_id: str,
    features: list[dict],
    model_version: str,
) -> int:
    """
    Write extracted footprints into ai_detections — the table the IPTU malha
    fina joins against (ai_detections → flight_assets → flights → project).

    Historically the pipeline only uploaded footprints.geojson to MinIO and the
    fiscal analysis always saw zero detections. The GeoJSON upload remains (it
    feeds the 3D viewer); THIS is what feeds the analysis.

    Idempotent per (asset, model_version): re-running the pipeline replaces its
    own previous detections without touching results from other models (e.g.
    the Groq pipeline writes the same class with a different model_version).

    The caller must have applied the tenant search_path to `conn`.
    """
    from sqlalchemy import text as sa_text

    rows, skipped = _features_to_detection_rows(features)
    if skipped:
        logger.warning(f"[DETECTIONS] {skipped} feature(s) sem polígono — ignoradas")
    if not rows:
        return 0

    # Idempotência por (asset, versão do modelo) — reexecutar a MESMA versão
    # substitui o próprio resultado.
    conn.execute(sa_text(
        "DELETE FROM ai_detections "
        "WHERE flight_asset_id = CAST(:aid AS uuid) "
        "AND detection_class = 'building' AND model_version = :model"
    ), {"aid": orthomosaic_asset_id, "model": model_version})

    # E as gerações ANTERIORES do mesmo asset saem de cena: quando o detector
    # evolui (heurística → neural → fusão), manter as antigas ativas fazia o
    # mapa exibir duas gerações sobrepostas e a malha fina contar o mesmo
    # imóvel duas vezes. Medido: 97 detecções obsoletas convivendo com 238
    # novas no voo real.
    stale = conn.execute(sa_text(
        "DELETE FROM ai_detections "
        "WHERE flight_asset_id = CAST(:aid AS uuid) "
        "AND detection_class = 'building' AND model_version <> :model "
        "RETURNING id"
    ), {"aid": orthomosaic_asset_id, "model": model_version}).fetchall()
    if stale:
        logger.info(
            f"[DETECTIONS] {len(stale)} detecção(ões) de gerações anteriores "
            f"removida(s) — o mapa mostra apenas o resultado de {model_version}"
        )

    from app.core.geometry_sql import LARGEST_POLYGON_FROM_GEOJSON

    written = 0
    failed = 0
    for row in rows:
        try:
            # SAVEPOINT por linha: sem ele, a PRIMEIRA falha envenena a
            # transação e todas as detecções seguintes morrem com
            # "current transaction is aborted".
            with conn.begin_nested():
                conn.execute(sa_text(f"""
                    INSERT INTO ai_detections
                        (flight_asset_id, detection_class, polygon, confidence,
                         area_sqm, perimeter_m, height_m, properties, model_version)
                    VALUES
                        (CAST(:aid AS uuid), 'building',
                         {LARGEST_POLYGON_FROM_GEOJSON},
                         :conf, :area, :perim, :height,
                         CAST(:props AS jsonb), :model)
                """), {**row, "aid": orthomosaic_asset_id, "model": model_version})
            written += 1
        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.warning(f"[DETECTIONS] Falha ao gravar detecção: {str(e).splitlines()[0][:160]}")
    if failed:
        logger.warning(f"[DETECTIONS] {failed} detecção(ões) não gravadas de {len(rows)}")
    return written


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

        from app.core.cog_converter import convert_to_cog, extract_geospatial_metadata, validate_cog

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
        from sqlalchemy import create_engine
        from sqlalchemy import text as sa_text

        engine = create_engine(settings.database_url_sync)
        with engine.connect() as conn:
            bounds = metadata.get("bounds", {})
            conn.execute(sa_text(
                "UPDATE flight_assets SET "
                "resolution_cm = :res, crs_epsg = :srid, file_size_bytes = :fsz, "
                "bbox_min_lon = :bw, bbox_min_lat = :bs, "
                "bbox_max_lon = :be, bbox_max_lat = :bn, "
                "metadata_json = CAST(:meta AS jsonb), updated_at = NOW() "
                "WHERE id = CAST(:id AS uuid)"
            ), {
                "res": metadata.get("resolution_cm"),
                "srid": metadata.get("srid", 4326),
                "fsz": metadata.get("file_size_bytes"),
                "bw": bounds.get("west"),
                "bs": bounds.get("south"),
                "be": bounds.get("east"),
                "bn": bounds.get("north"),
                "meta": json.dumps(metadata),
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
    tenant_schema: str | None = None,
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
        from app.core.dsm_generator import extract_dsm_metadata, generate_synthetic_dsm

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
        # Detector NEURAL primeiro (segmentação treinada, ONNX/CPU); a
        # heurística de cor+altura vira contingência explícita — ela marcava
        # 55% dos pixels como edificação (medido) e afogava a malha fina em
        # falsos positivos, contra 1,4% do modelo na mesma ortofoto.
        detector_model_version = "geobase_onnx_v1"
        if settings.ai_detector == "ml":
            try:
                from app.core.ml_building_detector import extract_buildings_ml
                extract_buildings_ml(
                    local_ortho,
                    output_path=footprints_path,
                    dsm_path=dsm_raw,
                    base_elevation=base_elevation,
                    model_path=settings.ai_model_path,
                    model_url=settings.ai_model_url,
                )
            except Exception as ml_err:
                logger.error(
                    f"[ML] Detector neural falhou ({ml_err}) — usando heurística "
                    "de contingência. Detecções desta rodada terão mais ruído."
                )
                detector_model_version = "dsm_synthetic_v1"
                extract_building_footprints(
                    local_ortho, dsm_raw, output_path=footprints_path,
                    min_height_m=2.5, base_elevation=base_elevation,
                )
        else:
            detector_model_version = "dsm_synthetic_v1"
            extract_building_footprints(
                local_ortho, dsm_raw, output_path=footprints_path,
                min_height_m=2.5, base_elevation=base_elevation,
            )

        publish_progress(task_id, "uploading", 70, "Uploading DSM and footprints to storage...")

        # ── Step 5: Upload to MinIO ───────────────────────────────────────
        import json as json_mod

        from app.core.storage import upload_file as minio_upload

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
        from sqlalchemy import create_engine
        from sqlalchemy import text as sa_text

        engine = create_engine(settings.database_url_sync)
        bounds = dsm_meta.get("bounds", {})

        try:
            with engine.connect() as conn:
                # Point at the OWNING tenant schema only. Concatenating every
                # tenant_* schema into search_path made unqualified writes land
                # in whichever schema Postgres resolved first — cross-tenant
                # contamination in a multi-tenant product.
                _apply_tenant_search_path(conn, tenant_schema)

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

            # ── Step 7: Persist detections for the fiscal analysis ────────
            # Without this the malha fina always reports zero: it reads the
            # ai_detections table, not the GeoJSON in object storage.
            detections_written = 0
            with engine.connect() as conn:
                _apply_tenant_search_path(conn, tenant_schema)
                with open(footprints_path, encoding="utf-8") as f:
                    footprint_features = json_mod.load(f).get("features", [])
                detections_written = _persist_building_detections(
                    conn, orthomosaic_asset_id, footprint_features,
                    model_version=detector_model_version,
                )
                conn.commit()
                logger.info(
                    f"[DSM-PIPELINE] {detections_written} detecções gravadas em ai_detections"
                )

            # Update flight status to 'completed' after successful processing
            with engine.connect() as conn:
                _apply_tenant_search_path(conn, tenant_schema)
                conn.execute(sa_text(
                    "UPDATE flights SET status = 'completed' WHERE id = CAST(:fid AS uuid)"
                ), {"fid": flight_id})
                conn.commit()
                logger.info(f"[DSM-PIPELINE] Updated flight {flight_id} status to 'completed'")
        except Exception as e:
            # Surfacing this matters: silently swallowing it leaves the flight
            # stuck in 'processing' with assets on disk but nothing in the DB.
            logger.exception(f"[DSM-PIPELINE] DB insert failed: {e}")
            raise
        finally:
            engine.dispose()

        publish_progress(task_id, "completed", 100,
                         f"DSM + {detections_written} edificações detectadas ✓",
                         extra={"dsm_key": dsm_object_key,
                                "buildings_key": bld_object_key,
                                "detections_written": detections_written})

        return {
            "status": "completed",
            "dsm_key": dsm_object_key,
            "buildings_key": bld_object_key,
            "dsm_metadata": dsm_meta,
            "dsm_source": "synthetic",
            "detections_written": detections_written,
        }

    except Exception as exc:
        logger.error(f"[DSM-PIPELINE] Failed: {exc}")
        publish_progress(task_id, "failed", 0, str(exc))
        raise  # Do not retry — prevents OOM restart loops
    finally:
        # Release deduplication lock (falls back to TTL expiry on failure)
        with contextlib.suppress(Exception):
            redis_client.delete(lock_key)
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)


@shared_task(
    name="app.tasks.post_processing.adjudicate_weak_detections",
    bind=True,
    max_retries=1,
    queue="processing",
    time_limit=3600,
)
def adjudicate_weak_detections(self, flight_id: str, tenant_schema: str | None = None):
    """
    Caminho caro para a dúvida: cada detecção "weak" é reinferida no recorte
    NATIVO da ortofoto (sem downsample) com ensemble de thresholds + juízes
    3D. Grava consensus_votes/is_unanimous e promove/rejeita/mantém.
    """
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    import numpy as _np
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _sa

    from app.core.detection_adjudicator import adjudicate_detection
    from app.core.ml_building_detector import ensure_model
    from app.core.storage import get_minio_client

    work = _tempfile.mkdtemp(prefix="cm_adj_")
    try:
        import json as _json

        import onnxruntime as _ort
        import rasterio as _rio
        from rasterio.mask import mask as _rio_mask
        from rasterio.warp import transform_geom as _tg

        eng = _ce(settings.database_url_sync)
        try:
            with eng.connect() as conn:
                _apply_tenant_search_path(conn, tenant_schema)
                assets = dict(conn.execute(_sa(
                    "SELECT asset_type, file_key || '|' || COALESCE(bucket_name,'') "
                    "FROM flight_assets WHERE flight_id = CAST(:fid AS uuid) "
                    "AND is_active AND asset_type IN ('orthomosaic','dsm')"
                ), {"fid": flight_id}).fetchall())
                if "orthomosaic" not in assets or "dsm" not in assets:
                    return {"status": "missing_assets", "flight_id": flight_id}
                client = get_minio_client()
                paths = {}
                for atype, packed in assets.items():
                    key, bucket = packed.rsplit("|", 1)
                    local = _os.path.join(work, f"{atype}.tif")
                    client.fget_object(bucket or "elevation-models", key, local)
                    paths[atype] = local

                rows = conn.execute(_sa(
                    "SELECT d.id, ST_AsGeoJSON(d.polygon) FROM ai_detections d "
                    "JOIN flight_assets fa ON d.flight_asset_id = fa.id "
                    "WHERE fa.flight_id = CAST(:fid AS uuid) AND fa.is_active "
                    "AND d.validation_status = 'weak' AND d.polygon IS NOT NULL"
                ), {"fid": flight_id}).fetchall()
                if not rows:
                    return {"status": "nothing_weak", "flight_id": flight_id}

                sess = _ort.InferenceSession(
                    ensure_model(settings.ai_model_path, settings.ai_model_url),
                    providers=["CPUExecutionProvider"],
                )
                inp = sess.get_inputs()[0].name

                promoted = rejected = unanimous = kept = 0
                with _rio.open(paths["orthomosaic"]) as ortho, _rio.open(paths["dsm"]) as dsm:
                    def ndsm_win(geom_native):
                        try:
                            g = _tg(ortho.crs.to_string(), dsm.crs.to_string(), geom_native)
                            win, _tr = _rio_mask(dsm, [g], crop=True, filled=False)
                            v = win[0].compressed().astype("float64")
                            return v[_np.isfinite(v)]
                        except Exception:
                            return None

                    # COMMIT EM LOTES: a adjudicação roda inferência em
                    # resolução nativa e leva minutos. Uma única transação
                    # aberta o tempo todo trava a tabela inteira — medido:
                    # um DELETE trivial ficou 7 minutos em espera de lock.
                    # A cada 20 detecções o trabalho é firmado e os locks
                    # liberados; se o processo cair, o que já foi julgado
                    # está salvo.
                    batch = 0
                    for det_id, geo in rows:
                        r = adjudicate_detection(
                            sess, inp, ortho, ndsm_win,
                            {"id": str(det_id), "geometry": _json.loads(geo)},
                        )
                        st = r["promoted_status"]
                        if st == "confirmed_unanimous":
                            unanimous += 1
                        elif st == "confirmed":
                            promoted += 1
                        elif st == "rejected":
                            rejected += 1
                        else:
                            kept += 1
                        conn.execute(_sa(
                            "UPDATE ai_detections SET "
                            "consensus_votes = CAST(:v AS jsonb), "
                            "is_unanimous = :u, "
                            "validation_status = COALESCE(:st, validation_status), "
                            "validated_at = now() WHERE id = CAST(:id AS uuid)"
                        ), {"v": r.get("votes_json", "{}"), "u": r["unanimous"],
                            "st": st, "id": str(det_id)})
                        batch += 1
                        if batch % 20 == 0:
                            conn.commit()
                            logger.info(f"[ADJUDICATE] {batch}/{len(rows)} julgadas (commit parcial)")
                conn.commit()
                summary = {"status": "completed", "flight_id": flight_id,
                           "adjudicated": len(rows), "unanimous": unanimous,
                           "promoted": promoted, "rejected": rejected, "kept_weak": kept}
                logger.info(f"[ADJUDICATE] {summary}")
                return summary
        finally:
            eng.dispose()
    except Exception as exc:
        logger.error(f"[ADJUDICATE] Falha: {exc}")
        raise self.retry(exc=exc)
    finally:
        _shutil.rmtree(work, ignore_errors=True)


@shared_task(
    name="app.tasks.post_processing.audit_detection_coverage",
    bind=True,
    max_retries=0,
    queue="processing",
    time_limit=2400,
)
def audit_detection_coverage(self, flight_id: str, tenant_schema: str | None = None):
    """
    AUDITORIA DE COBERTURA — encontra o que a IA PULOU, sem depender de
    ninguém olhar o mapa.

    Para cada lote dentro da área do voo sem nenhuma detecção, mede a altura
    máxima do nDSM dentro do lote. Se há estrutura elevada e nenhuma
    detecção, é uma FALHA DE COBERTURA do modelo — e o lote entra numa lista
    nominal, com a altura e a área elevada encontradas. Um lote realmente
    vazio (terreno baldio) aparece separado, sem alarme falso.

    É o contraditório da própria IA: o sensor 3D fiscaliza o detector 2D.
    """
    import json as _json
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    import numpy as _np
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _sa

    from app.core.storage import get_minio_client

    work = _tempfile.mkdtemp(prefix="cm_cov_")
    try:
        import rasterio as _rio
        from rasterio.mask import mask as _rio_mask
        from rasterio.warp import transform_geom as _tg

        eng = _ce(settings.database_url_sync)
        try:
            with eng.connect() as conn:
                _apply_tenant_search_path(conn, tenant_schema)
                dsm = conn.execute(_sa(
                    "SELECT file_key, COALESCE(bucket_name,'elevation-models') "
                    "FROM flight_assets WHERE flight_id = CAST(:fid AS uuid) "
                    "AND is_active AND asset_type = 'dsm' LIMIT 1"
                ), {"fid": flight_id}).fetchone()
                if not dsm:
                    return {"status": "no_dsm"}
                local = _os.path.join(work, "dsm.tif")
                get_minio_client().fget_object(dsm[1], dsm[0], local)

                # A ortofoto entra para a auditoria distinguir ESTRUTURA de
                # ÁRVORE. Sem isso, uma copa de 4 m vira "falha de cobertura"
                # e o operador persegue um alarme falso — medido: o único
                # caso restante do voo real tinha 97% de pixels verdes.
                local_ortho = None
                ortho = conn.execute(_sa(
                    "SELECT file_key, COALESCE(bucket_name,'orthomosaics') "
                    "FROM flight_assets WHERE flight_id = CAST(:fid AS uuid) "
                    "AND is_active AND asset_type = 'orthomosaic' LIMIT 1"
                ), {"fid": flight_id}).fetchone()
                if ortho:
                    local_ortho = _os.path.join(work, "ortho.tif")
                    get_minio_client().fget_object(ortho[1], ortho[0], local_ortho)

                # Lotes na área do voo SEM nenhuma detecção
                rows = conn.execute(_sa("""
                    WITH voo AS (
                      SELECT ST_MakeEnvelope(min(bbox_min_lon), min(bbox_min_lat),
                                             max(bbox_max_lon), max(bbox_max_lat), 4326) g
                      FROM flight_assets WHERE flight_id = CAST(:fid AS uuid) AND is_active
                    )
                    SELECT p.id, p.cadastral_code, ST_AsGeoJSON(p.polygon)
                    FROM parcels p, voo
                    WHERE p.polygon IS NOT NULL AND ST_Intersects(p.polygon, voo.g)
                      AND NOT EXISTS (
                        SELECT 1 FROM ai_detections d
                        JOIN flight_assets fa ON d.flight_asset_id = fa.id
                        WHERE fa.flight_id = CAST(:fid AS uuid)
                          AND d.polygon IS NOT NULL
                          AND ST_Intersects(d.polygon, p.polygon))
                """), {"fid": flight_id}).fetchall()

                misses, empty, no_cover, vegetation = [], [], [], []
                import cv2 as _cv2
                ortho_ds = _rio.open(local_ortho) if local_ortho else None
                with _rio.open(local) as d:
                    px_area = abs(d.transform.a * d.transform.e)
                    for pid, code, geo in rows:
                        try:
                            gn = _tg("EPSG:4326", d.crs.to_string(), _json.loads(geo))
                            win, _tr = _rio_mask(d, [gn], crop=True, filled=False)
                            vals = win[0].compressed().astype("float64")
                            vals = vals[_np.isfinite(vals)]
                        except Exception:
                            no_cover.append({"parcel_id": str(pid), "code": code})
                            continue
                        if vals.size == 0:
                            no_cover.append({"parcel_id": str(pid), "code": code})
                            continue
                        elevated = vals[vals >= 1.5]  # 1,5 m: acima de muro/carro
                        elevated_area = float(elevated.size * px_area)
                        item = {
                            "parcel_id": str(pid), "code": code,
                            "max_height_m": round(float(vals.max()), 2),
                            "elevated_area_m2": round(elevated_area, 1),
                            "pixels_measured": int(vals.size),
                        }
                        if not (elevated_area >= 3.0 and float(vals.max()) >= 1.5):
                            empty.append(item)
                            continue

                        # Massa elevada existe. É telhado ou copa? O mesmo
                        # critério do detector decide, para a auditoria não
                        # acusar a IA de perder uma árvore.
                        green_pct = None
                        if ortho_ds is not None:
                            try:
                                gn2 = _tg("EPSG:4326", ortho_ds.crs.to_string(), _json.loads(geo))
                                rgbw, _t2 = _rio_mask(ortho_ds, [gn2], crop=True, filled=True, nodata=0)
                                rgb = rgbw[:3].astype("float32")
                                tot = _np.maximum(rgb.sum(axis=0), 1e-6)
                                exg = 2 * (rgb[1] / tot) - (rgb[0] / tot) - (rgb[2] / tot)
                                hmask = _np.ma.filled(win[0], 0).astype("float32") >= 1.5
                                hh = min(hmask.shape[0], exg.shape[0])
                                ww = min(hmask.shape[1], exg.shape[1])
                                sel = hmask[:hh, :ww]
                                if sel.any():
                                    green_pct = round(float((exg[:hh, :ww][sel] > 0.16).mean()) * 100, 1)
                            except Exception:
                                green_pct = None
                        item["green_pct"] = green_pct
                        if green_pct is not None and green_pct >= 50.0:
                            vegetation.append(item)   # copa de árvore: exclusão CORRETA
                        else:
                            misses.append(item)       # estrutura real não detectada

                if ortho_ds is not None:
                    ortho_ds.close()
                misses.sort(key=lambda m: -m["elevated_area_m2"])
                summary = {
                    "status": "completed",
                    "flight_id": flight_id,
                    "parcels_without_detection": len(rows),
                    "coverage_misses": len(misses),
                    "vegetation_only": len(vegetation),
                    "genuinely_empty": len(empty),
                    "outside_dsm_coverage": len(no_cover),
                    "perfect_coverage": len(misses) == 0,
                    "worst_misses": misses[:20],
                }
                logger.info(
                    f"[COVERAGE] {len(rows)} lotes sem detecção: {len(misses)} com "
                    f"ESTRUTURA não detectada, {len(vegetation)} só vegetação, "
                    f"{len(empty)} vazios, {len(no_cover)} fora da cobertura do voo"
                )
                return summary
        finally:
            eng.dispose()
    finally:
        _shutil.rmtree(work, ignore_errors=True)


@shared_task(
    name="app.tasks.post_processing.audit_measurements",
    bind=True,
    max_retries=0,
    queue="processing",
    time_limit=1800,
)
def audit_measurements(self, flight_id: str, sample_size: int = 10, tenant_schema: str | None = None):
    """
    AUTOPROVA: recomputa medições gravadas por um caminho de código
    INDEPENDENTE (rasterio puro, inline — nenhum helper compartilhado com o
    produtor) e emite certificado bit a bit em audit_certificates.
    """
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    import numpy as _np
    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _sa

    from app.core.storage import get_minio_client

    work = _tempfile.mkdtemp(prefix="cm_audit_")
    try:
        import json as _json

        import rasterio as _rio
        from rasterio.mask import mask as _rio_mask
        from rasterio.warp import transform_geom as _tg

        eng = _ce(settings.database_url_sync)
        try:
            with eng.connect() as conn:
                _apply_tenant_search_path(conn, tenant_schema)
                row = conn.execute(_sa(
                    "SELECT file_key, COALESCE(bucket_name,'elevation-models') "
                    "FROM flight_assets WHERE flight_id = CAST(:fid AS uuid) "
                    "AND is_active AND asset_type = 'dsm' LIMIT 1"
                ), {"fid": flight_id}).fetchone()
                if not row:
                    return {"status": "no_dsm"}
                local_dsm = _os.path.join(work, "dsm.tif")
                get_minio_client().fget_object(row[1], row[0], local_dsm)

                dets = conn.execute(_sa(
                    "SELECT d.id, ST_AsGeoJSON(d.polygon), d.height_measured_m, d.volume_m3 "
                    "FROM ai_detections d JOIN flight_assets fa ON d.flight_asset_id = fa.id "
                    "WHERE fa.flight_id = CAST(:fid AS uuid) AND fa.is_active "
                    "AND d.height_measured_m IS NOT NULL "
                    "ORDER BY md5(d.id::text) LIMIT :k"
                ), {"fid": flight_id, "k": sample_size}).fetchall()

                checks = []
                with _rio.open(local_dsm) as d:
                    px_area = abs(d.transform.a * d.transform.e)
                    for det_id, geo, h_stored, v_stored in dets:
                        g = _tg("EPSG:4326", d.crs.to_string(), _json.loads(geo))
                        win, _tr = _rio_mask(d, [g], crop=True, filled=False)
                        vals = win[0].compressed().astype("float64")
                        vals = vals[_np.isfinite(vals)]
                        h_ind = round(float(_np.mean(vals)), 3)
                        v_ind = round(float(_np.sum(_np.clip(vals, 0, None)) * px_area), 2)
                        checks.append({
                            "detection_id": str(det_id),
                            "height_stored": float(h_stored), "height_recomputed": h_ind,
                            "height_identical": h_ind == float(h_stored),
                            "volume_stored": float(v_stored), "volume_recomputed": v_ind,
                            "volume_identical": v_ind == float(v_stored),
                        })
                passed = sum(1 for c in checks if c["height_identical"] and c["volume_identical"])
                total = len(checks)
                cert = conn.execute(_sa(
                    "INSERT INTO public.audit_certificates "
                    "(flight_id, checks_total, checks_passed, passed, details) "
                    "VALUES (CAST(:fid AS uuid), :t, :p, :ok, CAST(:d AS jsonb)) "
                    "RETURNING id, run_at"
                ), {"fid": flight_id, "t": total, "p": passed,
                    "ok": passed == total and total > 0,
                    "d": _json.dumps(checks)}).fetchone()
                conn.commit()
                summary = {"status": "completed", "certificate_id": str(cert[0]),
                           "checks_total": total, "checks_passed": passed,
                           "passed": passed == total and total > 0}
                logger.info(f"[AUDIT] {summary}")
                return summary
        finally:
            eng.dispose()
    finally:
        _shutil.rmtree(work, ignore_errors=True)


@shared_task(
    name="app.tasks.post_processing.validate_detections_elevation",
    bind=True,
    max_retries=1,
    queue="processing",
    time_limit=1800,
)
def validate_detections_elevation(
    self,
    flight_id: str,
    tenant_schema: str | None = None,
):
    """
    Validação cruzada IA×fotogrametria de TODAS as detecções ativas do voo:
    baixa DSM/DTM publicados, mede altura/volume/planaridade por polígono e
    grava veredito + incertezas — a análise passa a citar números MEDIDOS.
    """
    import json as _json
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile

    from sqlalchemy import create_engine as _ce
    from sqlalchemy import text as _sa

    from app.core.detection_validation import validate_detections_against_elevation
    from app.core.storage import get_minio_client

    work = _tempfile.mkdtemp(prefix="cm_val_")
    try:
        eng = _ce(settings.database_url_sync)
        try:
            with eng.connect() as conn:
                _apply_tenant_search_path(conn, tenant_schema)
                assets = conn.execute(_sa(
                    "SELECT asset_type, file_key, bucket_name FROM flight_assets "
                    "WHERE flight_id = CAST(:fid AS uuid) AND is_active "
                    "AND asset_type IN ('dsm','dtm')"
                ), {"fid": flight_id}).fetchall()
                paths = {}
                client = get_minio_client()
                for atype, key, bucket in assets:
                    local = _os.path.join(work, f"{atype}.tif")
                    client.fget_object(bucket or "elevation-models", key, local)
                    paths[atype] = local
                if "dsm" not in paths:
                    logger.warning(f"[VALIDATE] voo {flight_id} sem DSM — nada a validar")
                    return {"status": "no_dsm", "flight_id": flight_id}

                rows = conn.execute(_sa(
                    "SELECT d.id, ST_AsGeoJSON(d.polygon) FROM ai_detections d "
                    "JOIN flight_assets fa ON d.flight_asset_id = fa.id "
                    "WHERE fa.flight_id = CAST(:fid AS uuid) AND fa.is_active "
                    "AND d.polygon IS NOT NULL"
                ), {"fid": flight_id}).fetchall()
                dets = [{"id": str(r[0]), "geometry": _json.loads(r[1])} for r in rows]
                if not dets:
                    return {"status": "no_detections", "flight_id": flight_id}

                results = validate_detections_against_elevation(
                    paths["dsm"], paths.get("dtm"), dets
                )

                updated = 0
                for r in results:
                    if r.get("validation_status") in ("no_data", "error"):
                        conn.execute(_sa(
                            "UPDATE ai_detections SET validation_status = :st, "
                            "validated_at = now() WHERE id = CAST(:id AS uuid)"
                        ), {"st": r["validation_status"], "id": r["id"]})
                        continue
                    # As MEDIÇÕES são sempre atualizadas; o VEREDITO só é
                    # rebaixado se ainda não passou pela adjudicação nativa
                    # (que custa minutos e decide com 5 juízes). Sem esta
                    # guarda, revalidar apagava todo 'confirmed_unanimous'.
                    conn.execute(_sa(
                        "UPDATE ai_detections SET "
                        "height_measured_m = :h, height_std_m = :hs, "
                        "volume_m3 = :v, area_uncertainty_sqm = :au, "
                        "planarity = :pl, evidence_score = :ev, "
                        "validation_status = CASE "
                        "  WHEN consensus_votes IS NOT NULL "
                        "   AND validation_status IN ('confirmed_unanimous','rejected') "
                        "  THEN validation_status ELSE :st END, "
                        "validated_at = now() "
                        "WHERE id = CAST(:id AS uuid)"
                    ), {
                        "h": r["height_measured_m"], "hs": r["height_std_m"],
                        "v": r["volume_m3"], "au": r["area_uncertainty_m2"],
                        "pl": r["planarity"], "ev": r["evidence_score"],
                        "st": r["validation_status"], "id": r["id"],
                    })
                    updated += 1
                conn.commit()
                summary = {
                    "status": "completed",
                    "flight_id": flight_id,
                    "validated": updated,
                    "confirmed": sum(1 for r in results if r.get("validation_status") == "confirmed"),
                    "weak": sum(1 for r in results if r.get("validation_status") == "weak"),
                    "contradicted": sum(1 for r in results if r.get("validation_status") == "contradicted"),
                }
                logger.info(f"[VALIDATE] {summary}")
                if summary["weak"] > 0:
                    # Dúvida não fica parada: as "weak" vão para o caminho
                    # CARO — reinferência em resolução nativa com ensemble
                    # de juízes. Unanimidade vira fato medido por objeto.
                    adjudicate_weak_detections.apply_async(
                        kwargs={"flight_id": flight_id,
                                "tenant_schema": tenant_schema},
                        countdown=5,
                    )
                    logger.info(f"[VALIDATE] {summary['weak']} weak enviadas à adjudicação nativa")
                return summary
        finally:
            eng.dispose()
    except Exception as exc:
        logger.error(f"[VALIDATE] Falha: {exc}")
        raise self.retry(exc=exc)
    finally:
        _shutil.rmtree(work, ignore_errors=True)


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
    tenant_schema: str | None = None,
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

        footprints_path = os.path.join(work_dir, "footprints.geojson")
        detector_model_version = "dsm_real_v1"

        # A rede neural segmenta a IMAGEM (ortomosaico); o DSM real entra só
        # para dar altura aos polígonos. A heurística por limiar de elevação
        # fica como reserva — sozinha, ela zera em DSMs de cota absoluta.
        if settings.ai_detector == "ml" and flight_id:
            try:
                from sqlalchemy import create_engine as _ce
                from sqlalchemy import text as _sa

                _eng = _ce(settings.database_url_sync)
                try:
                    with _eng.connect() as _conn:
                        _apply_tenant_search_path(_conn, tenant_schema)
                        _ortho = _conn.execute(_sa(
                            "SELECT file_key, bucket_name FROM flight_assets "
                            "WHERE flight_id = CAST(:fid AS uuid) "
                            "AND asset_type = 'orthomosaic' AND is_active "
                            "ORDER BY created_at DESC LIMIT 1"
                        ), {"fid": flight_id}).fetchone()
                finally:
                    _eng.dispose()
                if _ortho is None:
                    raise RuntimeError("voo sem ortomosaico ativo para a rede neural")

                local_ortho = os.path.join(work_dir, "orthomosaic.tif")
                client.fget_object(_ortho[1], _ortho[0], local_ortho)

                from app.core.ml_building_detector import (
                    estimate_ground_elevation,
                    extract_buildings_exhaustive,
                    extract_buildings_ml,
                )

                base_elev = estimate_ground_elevation(local_dsm)
                if settings.ai_exhaustive:
                    extract_buildings_exhaustive(
                        orthophoto_path=local_ortho,
                        output_path=footprints_path,
                        dsm_path=local_dsm,
                        base_elevation=base_elev,
                        threshold=settings.ai_threshold,
                        min_area_m2=settings.ai_min_area_m2,
                        model_path=settings.ai_model_path,
                        model_url=settings.ai_model_url,
                    )
                    detector_model_version = "geobase_onnx_exhaustive_v1"

                    # FUSÃO COM A GEOMETRIA: a rede não dispara em todo
                    # telhado, mas volume acima do solo existe independente de
                    # reconhecimento. O que a geometria vê e a rede não viu
                    # deixa de sumir do mapa — e a origem fica gravada em cada
                    # polígono (neural | elevation | both).
                    try:
                        import json as _js

                        from app.core.structure_detector_3d import (
                            detect_structures_from_elevation,
                            fuse_detections,
                        )

                        elev_path = os.path.join(work_dir, "structures_3d.geojson")
                        detect_structures_from_elevation(
                            dsm_path=local_dsm,
                            output_path=elev_path,
                            orthophoto_path=local_ortho,
                            min_height_m=settings.ai_min_structure_height_m,
                            min_area_m2=settings.ai_min_area_m2,
                        )
                        with open(footprints_path, encoding="utf-8") as _f:
                            neural_fc = _js.load(_f)
                        with open(elev_path, encoding="utf-8") as _f:
                            elev_fc = _js.load(_f)
                        fused = fuse_detections(
                            neural_fc.get("features", []), elev_fc.get("features", [])
                        )
                        neural_fc["features"] = fused
                        neural_fc.setdefault("properties", {}).update({
                            "fusion": True,
                            "total_buildings": len(fused),
                            "from_elevation_only": sum(
                                1 for f in fused
                                if (f.get("properties") or {}).get("detection_source") == "elevation"
                            ),
                        })
                        with open(footprints_path, "w", encoding="utf-8") as _f:
                            _js.dump(neural_fc, _f)
                        detector_model_version = "fusion_rgb3d_v1"
                    except Exception as _e:
                        logger.exception(f"[FUSÃO] falhou, seguindo só com a rede: {_e}")
                else:
                    extract_buildings_ml(
                        orthophoto_path=local_ortho,
                        output_path=footprints_path,
                        dsm_path=local_dsm,
                        base_elevation=base_elev,
                        model_path=settings.ai_model_path,
                        model_url=settings.ai_model_url,
                    )
                    detector_model_version = "geobase_onnx_v1"
                logger.info(
                    f"[REAL-DSM-BUILDINGS] Detecção neural sobre o ortomosaico "
                    f"(solo estimado em {base_elev:.1f} m)"
                )
            except Exception as exc:
                logger.exception(
                    f"[REAL-DSM-BUILDINGS] Rede neural indisponível — "
                    f"heurística de elevação assume: {exc}"
                )

        if not os.path.exists(footprints_path):
            from app.core.building_extractor import extract_buildings_from_elevation

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


        from sqlalchemy import create_engine
        from sqlalchemy import text as sa_text

        detections_written = 0
        engine = create_engine(settings.database_url_sync)
        try:
            with engine.connect() as conn:
                # Point at the OWNING tenant schema only. Concatenating every
                # tenant_* schema into search_path made unqualified writes land
                # in whichever schema Postgres resolved first — cross-tenant
                # contamination in a multi-tenant product.
                _apply_tenant_search_path(conn, tenant_schema)

                # Update the DSM asset's metadata_json to include dsm_source
                conn.execute(sa_text(
                    "UPDATE flight_assets SET metadata_json = "
                    "jsonb_set(COALESCE(metadata_json, '{}'::jsonb), '{dsm_source}', '\"real\"'::jsonb) "
                    "WHERE flight_id = CAST(:fid AS uuid) AND asset_type = 'dsm'"
                ), {"fid": flight_id})

                # Persist detections for the fiscal analysis. The footprints
                # hang off the flight's ORTHOMOSAIC asset — the malha fina
                # resolves the project through flight_assets → flights.
                ortho_row = conn.execute(sa_text(
                    "SELECT id FROM flight_assets "
                    "WHERE flight_id = CAST(:fid AS uuid) AND asset_type = 'orthomosaic' "
                    "ORDER BY created_at DESC LIMIT 1"
                ), {"fid": flight_id}).fetchone() if flight_id else None
                if ortho_row:
                    import json as json_mod2
                    with open(footprints_path, encoding="utf-8") as f:
                        footprint_features = json_mod2.load(f).get("features", [])
                    detections_written = _persist_building_detections(
                        conn, str(ortho_row[0]), footprint_features,
                        model_version=detector_model_version,
                    )
                    logger.info(
                        f"[REAL-DSM-BUILDINGS] {detections_written} detecções gravadas"
                    )
                    if detections_written and flight_id:
                        # Validação cruzada IA×fotogrametria: mede altura/
                        # volume/planaridade de cada polígono e grava o
                        # veredito — roda em background, não atrasa o voo.
                        validate_detections_elevation.apply_async(
                            kwargs={"flight_id": flight_id,
                                    "tenant_schema": tenant_schema},
                            countdown=5,
                        )
                        logger.info("[REAL-DSM-BUILDINGS] validação por elevação enfileirada")
                else:
                    logger.warning(
                        "[REAL-DSM-BUILDINGS] Voo sem ortomosaico registrado — "
                        "detecções não persistidas (análise fiscal não as verá)"
                    )
                conn.commit()
        except Exception as e:
            logger.warning(f"[REAL-DSM-BUILDINGS] DB update failed: {e}")
        finally:
            engine.dispose()

        publish_progress(task_id, "completed", 100,
                         f"{detections_written} edificações extraídas do DSM real ✓",
                         extra={"buildings_key": bld_object_key,
                                "dsm_source": dsm_source,
                                "detections_written": detections_written})

        return {
            "status": "completed",
            "buildings_key": bld_object_key,
            "dsm_source": dsm_source,
            "detections_written": detections_written,
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
    tenant_schema: str | None = None,
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

        import json as json_mod

        from sqlalchemy import create_engine
        from sqlalchemy import text as sa_text

        engine = create_engine(settings.database_url_sync)
        try:
            with engine.connect() as conn:
                # Point at the OWNING tenant schema only. Concatenating every
                # tenant_* schema into search_path made unqualified writes land
                # in whichever schema Postgres resolved first — cross-tenant
                # contamination in a multi-tenant product.
                _apply_tenant_search_path(conn, tenant_schema)

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
