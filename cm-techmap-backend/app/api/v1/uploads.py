"""CM TECHMAP — Upload Routes (Chunked Upload Pipeline)"""

import math
import uuid
from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.database import current_tenant_schema
from app.core.storage import upload_file, compose_parts
from app.dependencies import get_db, get_current_user, require_operador, require_viewer
from app.schemas.upload import (UploadInitRequest, UploadInitResponse,
                                 UploadChunkResponse, UploadCompleteResponse)

router = APIRouter(prefix="/uploads", tags=["Uploads"])
settings = get_settings()


@router.post("/init", response_model=UploadInitResponse, status_code=status.HTTP_201_CREATED)
async def init_upload(
    body: UploadInitRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Initialize a chunked upload. Returns upload_id and expected chunk count."""
    chunk_size_bytes = body.chunk_size_mb * 1024 * 1024
    chunk_count = math.ceil(body.file_size_bytes / chunk_size_bytes)

    tenant = current_tenant_schema.get() or "public"
    file_key = f"{tenant}/{body.project_id}/{uuid.uuid4()}/{body.filename}"

    result = await db.execute(text(
        "INSERT INTO uploads (project_id, filename, original_filename, file_key, bucket, "
        "size_bytes, content_type, chunk_count, status) "
        "VALUES (:pid, :fn, :ofn, :fk, :bucket, :size, :ct, :cc, 'uploading') RETURNING id"
    ), {
        "pid": str(body.project_id), "fn": body.filename, "ofn": body.filename,
        "fk": file_key, "bucket": settings.minio_bucket_raw_uploads,
        "size": body.file_size_bytes, "ct": body.content_type, "cc": chunk_count,
    })

    upload_id = result.scalar()
    await db.commit()

    return UploadInitResponse(upload_id=upload_id, chunk_count=chunk_count,
                              chunk_size_bytes=chunk_size_bytes)


@router.post("/{upload_id}/chunk", response_model=UploadChunkResponse)
async def upload_chunk(
    upload_id: uuid.UUID,
    chunk_index: int | None = None,
    chunk_number: int | None = Form(default=None),
    part_number: int | None = Form(default=None),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Upload a single chunk of a file."""
    resolved_chunk_index = chunk_index
    if resolved_chunk_index is None:
        resolved_chunk_index = chunk_number
    if resolved_chunk_index is None:
        resolved_chunk_index = part_number
    if resolved_chunk_index is None:
        raise HTTPException(
            status_code=422,
            detail="chunk_index is required (or legacy chunk_number/part_number)",
        )

    # Get upload record
    result = await db.execute(text(
        "SELECT file_key, bucket, chunk_count, chunks_received, status "
        "FROM uploads WHERE id = :id"
    ), {"id": str(upload_id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found")

    file_key, bucket, chunk_count, chunks_received, upload_status = row
    if upload_status != "uploading":
        raise HTTPException(status_code=400, detail=f"Upload is {upload_status}, not accepting chunks")

    # Read chunk data and upload to MinIO as a part
    data = await file.read()
    part_key = f"{file_key}.part{resolved_chunk_index:05d}"
    upload_file(bucket, part_key, BytesIO(data), len(data), content_type="application/octet-stream")

    # Update counter
    new_received = chunks_received + 1
    await db.execute(text(
        "UPDATE uploads SET chunks_received = :cr WHERE id = :id"
    ), {"cr": new_received, "id": str(upload_id)})
    await db.commit()

    return UploadChunkResponse(
        upload_id=upload_id, chunk_index=resolved_chunk_index,
        chunks_received=new_received, chunks_total=chunk_count,
        status="uploading" if new_received < chunk_count else "ready_to_complete",
    )


@router.post("/{upload_id}/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    upload_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Finalize a chunked upload — merges parts in MinIO and triggers processing."""
    result = await db.execute(text(
        "SELECT file_key, bucket, chunk_count, chunks_received "
        "FROM uploads WHERE id = :id AND status = 'uploading'"
    ), {"id": str(upload_id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found or already completed")

    file_key, bucket, chunk_count, chunks_received = row
    if chunks_received < chunk_count:
        raise HTTPException(status_code=400,
                            detail=f"Missing chunks: {chunks_received}/{chunk_count}")

    # Compose all parts into final object
    part_keys = [f"{file_key}.part{i:05d}" for i in range(chunk_count)]
    compose_parts(bucket, file_key, part_keys)

    # Dispatch Celery task for processing
    from app.celery_app import celery_app
    job = celery_app.send_task(
        "app.tasks.processing.process_drone_upload",
        args=[str(upload_id), file_key, bucket],
        queue="processing",
    )

    await db.execute(text(
        "UPDATE uploads SET status = 'processing', processing_job_id = :jid, "
        "completed_at = NOW() WHERE id = :id"
    ), {"jid": job.id, "id": str(upload_id)})
    await db.commit()

    return UploadCompleteResponse(
        upload_id=upload_id, file_key=file_key,
        status="processing", processing_job_id=job.id, celery_task_id=job.id,
    )


@router.get("/{upload_id}/status")
async def get_upload_status(
    upload_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Check the status of an upload and its processing job."""
    result = await db.execute(text(
        "SELECT id, filename, size_bytes, status, chunks_received, chunk_count, "
        "processing_job_id, created_at FROM uploads WHERE id = :id"
    ), {"id": str(upload_id)})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Upload not found")

    response = {
        "id": row[0], "filename": row[1], "size_bytes": row[2],
        "status": row[3], "chunks_received": row[4], "chunk_count": row[5],
        "processing_job_id": row[6], "created_at": str(row[7]),
    }

    # Check Celery task status if processing
    if row[6]:
        from app.celery_app import celery_app
        task_result = celery_app.AsyncResult(row[6])
        response["processing_status"] = task_result.status
        if task_result.ready():
            response["processing_result"] = str(task_result.result)

    return response
