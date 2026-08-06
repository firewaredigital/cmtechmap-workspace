"""CM TECHMAP — Drone Flight Routes"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.database import current_tenant_schema
from app.dependencies import get_db, require_gestor, require_operador, require_viewer
from app.schemas.flight import FlightAsset, FlightAssetsRead, FlightCreate, FlightProcessRequest, FlightRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/flights", tags=["Flights"])
settings = get_settings()


@router.get("", response_model=list[FlightRead])
async def list_flights(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """List all drone flights for a project."""
    result = await db.execute(text(
        "SELECT id, project_id, flight_date, altitude_m, overlap_pct, "
        "images_count, camera_model, status, notes, created_at "
        "FROM flights WHERE project_id = :pid ORDER BY flight_date DESC"
    ), {"pid": str(project_id)})

    return [FlightRead(
        id=r[0], project_id=r[1], flight_date=r[2], altitude_m=r[3],
        overlap_pct=r[4], images_count=r[5], camera_model=r[6],
        status=r[7], notes=r[8], created_at=r[9],
    ) for r in result.fetchall()]


@router.post("", response_model=FlightRead, status_code=status.HTTP_201_CREATED)
async def create_flight(
    project_id: uuid.UUID,
    body: FlightCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Register a new drone flight for a project."""
    result = await db.execute(text(
        "INSERT INTO flights (project_id, flight_date, altitude_m, "
        "overlap_pct, images_count, camera_model, notes, status) "
        "VALUES (:pid, :fd, :alt, :olp, :img, :cam, :notes, 'pending') "
        "RETURNING id, project_id, flight_date, altitude_m, overlap_pct, "
        "images_count, camera_model, status, notes, created_at"
    ), {
        "pid": str(project_id), "fd": body.flight_date,
        "alt": body.altitude_m or 0, "olp": body.overlap_pct or 0,
        "img": getattr(body, 'images_count', 0) or 0,
        "cam": body.camera_model or "", "notes": body.notes or "",
    })
    row = result.fetchone()
    await db.commit()

    return FlightRead(
        id=row[0], project_id=row[1], flight_date=row[2], altitude_m=row[3],
        overlap_pct=row[4], images_count=row[5] or 0, camera_model=row[6] or "",
        status=row[7], notes=row[8], created_at=row[9],
    )


@router.get("/{flight_id}/assets", response_model=FlightAssetsRead)
async def get_flight_assets(
    project_id: uuid.UUID,
    flight_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Get all generated assets (orthomosaic, DSM, point cloud) for a flight."""
    # Check flight exists
    flight_result = await db.execute(text(
        "SELECT status FROM flights WHERE id = :fid AND project_id = :pid"
    ), {"fid": str(flight_id), "pid": str(project_id)})
    flight_row = flight_result.fetchone()
    if not flight_row:
        raise HTTPException(status_code=404, detail="Flight not found")

    assets: list[FlightAsset] = []

    # Query all assets from flight_assets table
    # DISTINCT ON: reprocessamentos publicam novas linhas por asset_type; o
    # usuário deve ver UM card por tipo (o mais recente), nunca triplicatas.
    assets_result = await db.execute(text(
        "SELECT DISTINCT ON (asset_type) id, asset_type, file_key, "
        "bucket_name, file_size_bytes, resolution_cm "
        "FROM flight_assets WHERE flight_id = :fid AND is_active = true "
        "ORDER BY asset_type, created_at DESC"
    ), {"fid": str(flight_id)})

    for r in assets_result.fetchall():
        asset_id, asset_type, file_key, bucket_name, file_size, res_cm = r
        # URL pré-assinada do MinIO aponta para o host INTERNO da rede Docker
        # (minio:9000) — nenhum navegador resolve. O download passa pelo
        # backend, autenticado e alcançável de qualquer ambiente.
        assets.append(FlightAsset(
            asset_id=asset_id,
            asset_type=asset_type,
            file_key=file_key,
            file_size_bytes=file_size,
            resolution_cm=res_cm,
            download_url=(
                f"/api/v1/projects/{project_id}/flights/{flight_id}"
                f"/assets/{asset_id}/download"
            ),
        ))

    # Get processing job id from upload
    upload_result = await db.execute(text(
        "SELECT processing_job_id FROM uploads "
        "WHERE project_id = :pid AND processing_job_id IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1"
    ), {"pid": str(project_id)})
    job_row = upload_result.fetchone()

    return FlightAssetsRead(
        flight_id=flight_id,
        project_id=project_id,
        status=flight_row[0],
        assets=assets,
        processing_job_id=job_row[0] if job_row else None,
    )


@router.post("/{flight_id}/validate-detections", status_code=202)
async def trigger_detection_validation(
    project_id: uuid.UUID,
    flight_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """
    Dispara a validação cruzada IA×fotogrametria das detecções do voo:
    mede altura/volume/planaridade por polígono nos DSM/DTM publicados e
    grava veredito + incertezas em cada detecção.
    """
    flight = await db.execute(text(
        "SELECT 1 FROM flights WHERE id = :fid AND project_id = :pid"
    ), {"fid": str(flight_id), "pid": str(project_id)})
    if not flight.fetchone():
        raise HTTPException(status_code=404, detail="Flight not found")

    from app.tasks.post_processing import validate_detections_elevation
    schema = current_tenant_schema.get() or None
    task = validate_detections_elevation.apply_async(
        kwargs={"flight_id": str(flight_id), "tenant_schema": schema}
    )
    return {"status": "queued", "celery_task_id": task.id}


@router.get("/{flight_id}/assets/{asset_id}/download")
async def download_flight_asset(
    project_id: uuid.UUID,
    flight_id: uuid.UUID,
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Stream a flight asset (ortomosaico, DSM, GeoJSON) through the API."""
    from fastapi import HTTPException
    from fastapi.responses import StreamingResponse

    result = await db.execute(text(
        "SELECT fa.file_key, fa.bucket_name, fa.asset_type "
        "FROM flight_assets fa JOIN flights f ON f.id = fa.flight_id "
        "WHERE fa.id = :aid AND fa.flight_id = :fid AND f.project_id = :pid"
    ), {"aid": str(asset_id), "fid": str(flight_id), "pid": str(project_id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Asset não encontrado neste voo")

    file_key, bucket, asset_type = row[0], row[1] or settings.minio_bucket_orthomosaics, row[2]
    from app.core.storage import get_minio_client
    try:
        obj = get_minio_client().get_object(bucket, file_key)
    except Exception as e:
        # Só "objeto não existe" é 404. Falha transitória do storage virava
        # 404 indistinguível — o usuário via "não encontrado" para um
        # arquivo que existe, e o log não contava nada.
        code = getattr(e, "code", "")
        if code in ("NoSuchKey", "NoSuchBucket"):
            raise HTTPException(status_code=404, detail="Arquivo não encontrado no storage")
        logger.error(f"[DOWNLOAD] Storage indisponível para {bucket}/{file_key}: {e}")
        raise HTTPException(status_code=502, detail="Storage temporariamente indisponível — tente novamente")

    ext = file_key.rsplit(".", 1)[-1].lower() if "." in file_key else "bin"
    media = {"tif": "image/tiff", "tiff": "image/tiff",
             "geojson": "application/geo+json", "json": "application/json",
             "laz": "application/octet-stream", "zip": "application/zip"}.get(ext, "application/octet-stream")

    def _iter():
        try:
            while chunk := obj.read(1024 * 512):
                yield chunk
        finally:
            obj.close()
            obj.release_conn()

    return StreamingResponse(_iter(), media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{asset_type}_{asset_id}.{ext}"',
    })


@router.post("/{flight_id}/process", status_code=status.HTTP_202_ACCEPTED)
async def trigger_flight_processing(
    project_id: uuid.UUID,
    flight_id: uuid.UUID,
    body: FlightProcessRequest | None = None,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Trigger processing for a flight.

    Strategy:
    1. If the flight already has orthomosaic assets in flight_assets,
       trigger DSM + building extraction pipeline on the existing ortho.
    2. If no orthomosaics exist but there are raw uploads in the uploads table,
       trigger the full photogrammetry pipeline (ODM).
    3. Otherwise, return a clear error with instructions.

    `force=true` reprocessa mesmo com DSM já gerado — necessário para
    regenerar as detecções após atualização do extrator (o pipeline substitui
    as detecções da própria versão de modelo, sem duplicar).
    """
    # Validate flight exists
    flight_result = await db.execute(text(
        "SELECT id, status FROM flights WHERE id = :fid AND project_id = :pid"
    ), {"fid": str(flight_id), "pid": str(project_id)})
    flight_row = flight_result.fetchone()
    if not flight_row:
        raise HTTPException(status_code=404, detail="Flight not found")

    from app.celery_app import celery_app

    # ── Strategy 1: Re-process existing orthomosaic assets ─────────────────
    ortho_result = await db.execute(text(
        "SELECT id, file_key, bucket_name "
        "FROM flight_assets "
        "WHERE flight_id = :fid AND asset_type = 'orthomosaic' AND is_active = true "
        "ORDER BY created_at DESC LIMIT 1"
    ), {"fid": str(flight_id)})
    ortho_row = ortho_result.fetchone()

    if ortho_row:
        asset_id = str(ortho_row[0])
        file_key = ortho_row[1]
        bucket = ortho_row[2] or settings.minio_bucket_orthomosaics

        # ── Guard: Prevent duplicate DSM/building processing ──────────
        # The GeoTIFF upload endpoint (assets.py) already auto-triggers
        # generate_dsm_and_buildings. If a DSM is already generated or
        # in progress, return the existing task info instead of launching
        # a duplicate that would cause progress bar oscillation and
        # race conditions on flight_assets DB records.
        dsm_check = await db.execute(text(
            "SELECT id FROM flight_assets "
            "WHERE flight_id = :fid AND asset_type = 'dsm' AND is_active = true"
        ), {"fid": str(flight_id)})
        existing_dsm = dsm_check.fetchone()

        if existing_dsm and not force:
            # DSM already exists — no need to reprocess
            return {
                "message": "DSM já gerado para este voo. Use force=true para reprocessar.",
                "celery_task_id": None,
                "flight_id": str(flight_id),
                "pipeline": "dsm_buildings",
                "status": "already_completed",
                "dsm_asset_id": str(existing_dsm[0]),
            }

        if existing_dsm and force:
            # Reprocessing replaces the previous DSM record — keeping both
            # would make the terrain endpoint resolve an arbitrary one.
            await db.execute(text(
                "UPDATE flight_assets SET is_active = false "
                "WHERE flight_id = :fid AND asset_type = 'dsm'"
            ), {"fid": str(flight_id)})
            await db.commit()

        # Trigger DSM + Building extraction on existing orthomosaic
        job = celery_app.send_task(
            "app.tasks.post_processing.generate_dsm_and_buildings",
            args=[asset_id, bucket, file_key],
            kwargs={
                "flight_id": str(flight_id),
                "project_id": str(project_id),
                "tenant_schema": current_tenant_schema.get(),
            },
            queue="processing",
        )

        await db.execute(text(
            "UPDATE flights SET status = 'processing' WHERE id = :fid"
        ), {"fid": str(flight_id)})
        await db.commit()

        return {
            "message": "Processamento DSM + Edifícios 3D iniciado",
            "celery_task_id": job.id,
            "flight_id": str(flight_id),
            "pipeline": "dsm_buildings",
            "dsm_source": "synthetic",
            "orthomosaic_asset_id": asset_id,
            "websocket_url": f"/ws/processing/{job.id}",
        }

    # ── Strategy 2: Process raw uploads (legacy chunked upload) ────────────
    try:
        uploads_result = await db.execute(text(
            "SELECT id, file_key, bucket FROM uploads "
            "WHERE project_id = :pid AND status IN ('completed', 'uploaded') "
            "ORDER BY created_at"
        ), {"pid": str(project_id)})
        uploads = uploads_result.fetchall()
    except Exception:
        uploads = []

    if uploads:
        options = body.options if body else None
        job = celery_app.send_task(
            "app.tasks.processing.process_drone_upload",
            args=[
                str(uploads[0][0]),
                uploads[0][1],
                uploads[0][2],
            ],
            kwargs={"flight_id": str(flight_id), "project_id": str(project_id),
                    "odm_options": options,
                    "tenant_schema": current_tenant_schema.get()},
            queue="processing",
        )

        await db.execute(text(
            "UPDATE flights SET status = 'processing' WHERE id = :fid"
        ), {"fid": str(flight_id)})
        await db.commit()

        return {
            "message": "Processamento fotogramétrico iniciado",
            "celery_task_id": job.id,
            "flight_id": str(flight_id),
            "pipeline": "photogrammetry",
            "websocket_url": f"/ws/processing/{job.id}",
        }

    # ── No processable data found ──────────────────────────────────────────
    raise HTTPException(
        status_code=400,
        detail=(
            "Nenhum dado processável encontrado para este voo. "
            "Faça upload de imagens de drone ou de um GeoTIFF (ortomosaico) "
            "na aba 'Assets Gerados' antes de processar."
        ),
    )
