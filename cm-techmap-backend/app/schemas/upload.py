"""CM TECHMAP — Upload Schemas"""

import uuid
from datetime import datetime
from pydantic import BaseModel, Field, model_validator


class UploadInitRequest(BaseModel):
    project_id: uuid.UUID
    filename: str
    file_size_bytes: int = Field(..., gt=0)
    content_type: str = "image/jpeg"
    chunk_size_mb: int = Field(default=50, ge=5, le=200)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, data):
        """Accept legacy frontend keys while keeping canonical internal names."""
        if not isinstance(data, dict):
            return data

        normalized = dict(data)

        if "project_id" not in normalized:
            normalized["project_id"] = (
                normalized.get("projectId")
                or normalized.get("project")
            )

        if "file_size_bytes" not in normalized:
            normalized["file_size_bytes"] = (
                normalized.get("fileSizeBytes")
                or normalized.get("file_size")
                or normalized.get("fileSize")
                or normalized.get("size_bytes")
            )

        if "chunk_size_mb" not in normalized:
            normalized["chunk_size_mb"] = (
                normalized.get("chunkSizeMb")
                or normalized.get("chunk_size")
                or normalized.get("chunkSize")
            )

        if "content_type" not in normalized:
            normalized["content_type"] = (
                normalized.get("contentType")
                or normalized.get("mime_type")
                or "image/jpeg"
            )

        return normalized


class UploadInitResponse(BaseModel):
    upload_id: uuid.UUID
    chunk_count: int
    chunk_size_bytes: int


class UploadChunkResponse(BaseModel):
    upload_id: uuid.UUID
    chunk_index: int
    chunks_received: int
    chunks_total: int
    status: str


class UploadCompleteResponse(BaseModel):
    upload_id: uuid.UUID
    file_key: str
    status: str
    processing_job_id: str | None = None
    celery_task_id: str | None = None


class UploadStatusRead(BaseModel):
    id: uuid.UUID
    filename: str
    size_bytes: int
    status: str
    chunks_received: int
    chunk_count: int
    processing_job_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
