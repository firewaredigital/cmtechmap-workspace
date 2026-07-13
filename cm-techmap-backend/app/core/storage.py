"""
CM TECHMAP — MinIO Object Storage Client
Wrapper around the MinIO Python SDK for file operations.
"""

import logging
from datetime import timedelta
from typing import BinaryIO

from minio import Minio
from minio.commonconfig import ComposeSource
from minio.error import S3Error

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_client: Minio | None = None


def get_minio_client() -> Minio:
    global _client
    if _client is None:
        _client = Minio(
            endpoint=settings.minio_endpoint,
            access_key=settings.minio_root_user,
            secret_key=settings.minio_root_password,
            secure=settings.minio_use_ssl,
        )
    return _client


def upload_file(bucket: str, object_name: str, data: BinaryIO, length: int,
                content_type: str = "application/octet-stream",
                metadata: dict[str, str] | None = None) -> str:
    client = get_minio_client()
    client.put_object(bucket, object_name, data, length, content_type=content_type, metadata=metadata)
    logger.info(f"Uploaded {object_name} to {bucket} ({length} bytes)")
    return object_name


def download_file(bucket: str, object_name: str) -> bytes:
    client = get_minio_client()
    response = client.get_object(bucket, object_name)
    data = response.read()
    response.close()
    response.release_conn()
    return data


def get_presigned_url(bucket: str, object_name: str, expires_hours: int = 1) -> str:
    client = get_minio_client()
    return client.presigned_get_object(bucket, object_name, expires=timedelta(hours=expires_hours))


def delete_file(bucket: str, object_name: str) -> None:
    client = get_minio_client()
    client.remove_object(bucket, object_name)


def compose_parts(bucket: str, object_name: str, part_keys: list[str]) -> str:
    client = get_minio_client()
    sources = [ComposeSource(bucket, key) for key in part_keys]
    client.compose_object(bucket, object_name, sources)
    for key in part_keys:
        try:
            client.remove_object(bucket, key)
        except S3Error:
            pass
    return object_name


def list_objects(bucket: str, prefix: str = "", recursive: bool = True) -> list[dict]:
    client = get_minio_client()
    return [{"name": o.object_name, "size": o.size, "last_modified": str(o.last_modified)}
            for o in client.list_objects(bucket, prefix=prefix, recursive=recursive)]
