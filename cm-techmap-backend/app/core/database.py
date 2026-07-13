"""
CM TECHMAP — Async Database Engine
SQLAlchemy 2.0 async with PostGIS support, schema-per-tenant isolation,
and PgBouncer-compatible connection management.
"""

from collections.abc import AsyncGenerator
from contextvars import ContextVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

settings = get_settings()

# ── Context variable for tenant schema ────────────────────────────────────────
# Set by the TenantMiddleware on each request
current_tenant_schema: ContextVar[str | None] = ContextVar("current_tenant_schema", default=None)

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE: Main (via PgBouncer in production)
# ══════════════════════════════════════════════════════════════════════════════
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_timeout=settings.database_pool_timeout,
    echo=settings.database_echo,
    pool_pre_ping=True,               # Detect stale connections
    pool_recycle=1800,                 # Match PgBouncer server_lifetime
    connect_args={
        "server_settings": {
            "application_name": "cm-techmap-backend",
        },
        # PgBouncer compatibility: disable prepared statements in transaction mode
        "prepared_statement_cache_size": 0,
    },
)

# ══════════════════════════════════════════════════════════════════════════════
# ENGINE: Direct PostgreSQL (bypasses PgBouncer)
# Used for DDL operations (CREATE SCHEMA, ALTER TABLE, etc.)
# Falls back to main engine if DATABASE_URL_DIRECT is not set
# ══════════════════════════════════════════════════════════════════════════════
_direct_url = settings.database_url_direct or settings.database_url
engine_direct = create_async_engine(
    _direct_url,
    pool_size=5,                       # Small pool — only for DDL
    max_overflow=2,
    pool_timeout=15,
    echo=settings.database_echo,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={
        "server_settings": {
            "application_name": "cm-techmap-ddl",
        },
    },
)

# ── Session Factories ─────────────────────────────────────────────────────────
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,            # Required for async — prevents lazy-load issues
    autoflush=False,
)

async_session_direct_factory = async_sessionmaker(
    bind=engine_direct,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an AsyncSession scoped to the current tenant.

    Uses SQLAlchemy's schema_translate_map to dynamically route all queries
    to the correct tenant schema without modifying model definitions.

    The tenant schema is resolved from the ContextVar `current_tenant_schema`,
    which is set by the TenantMiddleware from the JWT claims.
    """
    tenant_schema = current_tenant_schema.get()

    bind_engine = engine.execution_options(schema_translate_map={None: tenant_schema}) if tenant_schema else engine
    
    from sqlalchemy import text
    async with async_session_factory(bind=bind_engine) as session:
        try:
            if tenant_schema:
                await session.execute(text(f"SET search_path TO {tenant_schema}, public, topology"))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_public_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an AsyncSession for the PUBLIC schema.
    Used for cross-tenant operations (tenant management, subscriptions).
    """
    bind_engine = engine.execution_options(schema_translate_map={None: "public"})
    from sqlalchemy import text
    async with async_session_factory(bind=bind_engine) as session:
        try:
            await session.execute(text("SET search_path TO public, topology"))
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def get_direct_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency for DDL operations that bypass PgBouncer.
    Use for: CREATE SCHEMA, ALTER TABLE, CREATE INDEX, RLS policies.
    """
    from sqlalchemy import text
    async with async_session_direct_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
