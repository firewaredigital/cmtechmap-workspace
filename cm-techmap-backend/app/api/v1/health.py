"""CM TECHMAP — Health Check Routes"""

import logging
from fastapi import APIRouter
import redis.asyncio as aioredis

from app.config import get_settings
from app.schemas.common import HealthResponse

router = APIRouter(tags=["Health"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Liveness probe — returns OK if the API is running."""
    return HealthResponse(
        status="healthy",
        version=settings.app_version,
        services={"api": "up"},
    )


@router.get("/health/ready", response_model=HealthResponse)
async def readiness_check():
    """Readiness probe — checks all downstream dependencies."""
    services: dict[str, str] = {"api": "up"}

    # Check Redis
    try:
        r = aioredis.from_url(settings.redis_url, socket_connect_timeout=2)
        await r.ping()
        services["redis"] = "up"
        await r.aclose()
    except Exception as e:
        services["redis"] = f"down: {e}"
        logger.warning(f"Redis health check failed: {e}")

    # Check PostgreSQL
    try:
        from sqlalchemy import text
        from app.core.database import engine
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            result.scalar()
            services["postgres"] = "up"
    except Exception as e:
        services["postgres"] = f"down: {e}"
        logger.warning(f"PostgreSQL health check failed: {e}")

    # Check MinIO
    try:
        from app.core.storage import get_minio_client
        client = get_minio_client()
        client.list_buckets()
        services["minio"] = "up"
    except Exception as e:
        services["minio"] = f"down: {e}"
        logger.warning(f"MinIO health check failed: {e}")

    # Check NodeODM (Phase 2)
    try:
        from app.core.odm_client import NodeODMClient
        odm = NodeODMClient()
        if await odm.is_healthy():
            services["nodeodm"] = "up"
        else:
            services["nodeodm"] = "degraded"
    except Exception:
        services["nodeodm"] = "unavailable"

    # Check TiTiler (Phase 2)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{settings.titiler_url}/healthz")
            services["titiler"] = "up" if resp.status_code == 200 else "degraded"
    except Exception:
        services["titiler"] = "unavailable"

    # Check Martin (Phase 2)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{settings.martin_url}/health")
            services["martin"] = "up" if resp.status_code == 200 else "degraded"
    except Exception:
        services["martin"] = "unavailable"

    # Core services must be up; Phase 2 services can be "unavailable"
    core_up = all(services.get(s) == "up" for s in ("api", "redis", "postgres", "minio"))
    return HealthResponse(
        status="healthy" if core_up else "degraded",
        version=settings.app_version,
        services=services,
    )

