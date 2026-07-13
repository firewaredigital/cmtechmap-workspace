import uuid

from app.schemas.upload import UploadInitRequest


def test_upload_init_accepts_canonical_fields() -> None:
    payload = {
        "project_id": str(uuid.uuid4()),
        "filename": "drone_001.jpg",
        "file_size_bytes": 1024,
        "content_type": "image/jpeg",
        "chunk_size_mb": 50,
    }

    data = UploadInitRequest.model_validate(payload)

    assert str(data.project_id) == payload["project_id"]
    assert data.file_size_bytes == 1024
    assert data.chunk_size_mb == 50


def test_upload_init_accepts_legacy_fields() -> None:
    project_id = str(uuid.uuid4())
    payload = {
        "projectId": project_id,
        "filename": "drone_legacy.jpg",
        "fileSize": 2048,
        "contentType": "image/png",
        "chunkSize": 25,
    }

    data = UploadInitRequest.model_validate(payload)

    assert str(data.project_id) == project_id
    assert data.file_size_bytes == 2048
    assert data.content_type == "image/png"
    assert data.chunk_size_mb == 25
