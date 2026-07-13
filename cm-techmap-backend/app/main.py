"""
CM TECHMAP — FastAPI Application Entry Point
Smart Cities GovTech Platform — Backend API Server
"""

import logging
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import get_settings
from app.api.v1.router import api_v1_router
from app.middleware.tenant import TenantMiddleware
from app.middleware.request_logging import RequestLoggingMiddleware

settings = get_settings()

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.app_debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("cm_techmap")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown lifecycle."""
    logger.info("=" * 60)
    logger.info(f"  CM TECHMAP Backend v{settings.app_version}")
    logger.info(f"  Environment: {settings.app_env}")
    logger.info("=" * 60)

    # ── Startup: verify database connection ──────────────────────────────
    try:
        from app.core.database import engine
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version()"))
            pg_version = result.scalar()
            logger.info(f"  PostgreSQL: {pg_version}")

            result = await conn.execute(text("SELECT PostGIS_Version()"))
            postgis_version = result.scalar()
            logger.info(f"  PostGIS: {postgis_version}")
    except Exception as e:
        logger.error(f"  Database connection failed: {e}")
        logger.warning("  Starting in degraded mode — database unavailable")

    # ── Startup: schema managed by Alembic ──────────────────────────────────
    # Tables are created via: alembic upgrade head
    # See alembic/versions/ for migration history.
    # To run migrations inside Docker:
    #   docker compose exec backend alembic upgrade head
    logger.info("  Schema management: Alembic (run 'alembic upgrade head' to apply)")

    # ── Startup: verify MinIO connection + ensure required buckets ──────
    try:
        from app.core.storage import get_minio_client
        client = get_minio_client()
        existing_buckets = {b.name for b in client.list_buckets()}
        logger.info(f"  MinIO existing buckets: {sorted(existing_buckets)}")

        # Ensure all required buckets exist (including 3d-models for Phase 3/4)
        required_buckets = [
            settings.minio_bucket_raw_uploads,
            settings.minio_bucket_orthomosaics,
            settings.minio_bucket_elevation_models,
            settings.minio_bucket_point_clouds,
            settings.minio_bucket_3d_models,
        ]
        for bucket_name in required_buckets:
            if bucket_name and bucket_name not in existing_buckets:
                try:
                    client.make_bucket(bucket_name)
                    logger.info(f"  MinIO: created bucket '{bucket_name}'")
                except Exception as be:
                    logger.warning(f"  MinIO: failed to create bucket '{bucket_name}': {be}")
    except Exception as e:
        logger.warning(f"  MinIO connection failed: {e}")

    # ── Startup: verify NodeODM connection (Phase 2) ─────────────────────
    try:
        from app.core.odm_client import NodeODMClient
        odm = NodeODMClient()
        odm_info = await odm.get_node_info()
        logger.info(f"  NodeODM: v{odm_info.get('version', '?')} "
                     f"(max images: {odm_info.get('maxParallelTasks', '?')})")
    except Exception as e:
        logger.warning(f"  NodeODM not available: {e} (will use simulation mode)")

    # ── Startup: verify TiTiler connection (Phase 2) ─────────────────────
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.titiler_url}/healthz")
            if resp.status_code == 200:
                logger.info("  TiTiler: healthy ✓")
            else:
                logger.warning(f"  TiTiler: unhealthy (status {resp.status_code})")
    except Exception as e:
        logger.warning(f"  TiTiler not available: {e}")

    # ── Startup: verify Martin connection (Phase 2) ──────────────────────
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.martin_url}/health")
            if resp.status_code == 200:
                logger.info("  Martin: healthy ✓")
            else:
                logger.warning(f"  Martin: unhealthy (status {resp.status_code})")
    except Exception as e:
        logger.warning(f"  Martin not available: {e}")

    logger.info("  Startup complete ✓")
    logger.info("=" * 60)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("Shutting down CM TECHMAP Backend...")
    from app.core.database import engine
    await engine.dispose()
    logger.info("Database connections closed")


# ── Create FastAPI app ────────────────────────────────────────────────────────
app = FastAPI(
    title="CM TECHMAP API",
    description=(
        "Smart Cities GovTech Platform — API for drone image processing, "
        "digital twin generation, and AI-powered urban analytics."
    ),
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Middleware (order matters: last added = first executed) ────────────────────
from app.middleware.metrics import PrometheusMetricsMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
app.add_middleware(PrometheusMetricsMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(TenantMiddleware)

# CORS — restrictive in production, permissive in development
_cors_origins = settings.cors_origins_list
_cors_allow_all = "*" in _cors_origins
_cors_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_cors_headers = [
    "Authorization", "Content-Type", "Accept", "X-Tenant-ID",
    "X-Request-ID", "Cache-Control",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_allow_all else _cors_origins,
    allow_credentials=not _cors_allow_all,  # credentials incompatible with wildcard
    allow_methods=_cors_methods,
    allow_headers=_cors_headers if settings.is_production else ["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining", "Retry-After"],
    max_age=3600 if settings.is_production else 0,
)

# ── Routes ────────────────────────────────────────────────────────────────────
app.include_router(api_v1_router)

# ── WebSocket routes (mounted directly WITHOUT /api/v1 prefix) ────────────────
# WebSocket connections from the frontend expect /ws/processing/{task_id}
# but the websocket_router was only reachable at /api/v1/ws/processing/{id}
# because it was nested inside api_v1_router (prefix="/api/v1").
# Mounting directly on `app` ensures the path matches the frontend expectation
# and the Vite dev-server proxy config (/ws → ws://localhost:8000).
from app.api.v1.websocket import router as ws_direct_router
app.include_router(ws_direct_router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "CM TECHMAP API",
        "version": settings.app_version,
        "environment": settings.app_env,
        "docs": "/docs" if not settings.is_production else None,
    }


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint for scraping."""
    from starlette.responses import PlainTextResponse
    from app.middleware.metrics import generate_metrics_text
    return PlainTextResponse(generate_metrics_text(), media_type="text/plain; version=0.0.4")

