"""
CM TECHMAP — Alembic Environment Configuration
Async-compatible migration runner with PostGIS and multi-schema support.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import all models so Alembic can detect them
from app.models import Base  # noqa: F401
import app.models  # noqa: F401 — triggers relationship resolution

# Alembic Config object
config = context.config

# Setup logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def get_url() -> str:
    """Get database URL from environment or alembic.ini."""
    import os
    url = os.environ.get("DATABASE_URL_SYNC")
    if url:
        return url
    # Fallback to alembic.ini
    return config.get_main_option("sqlalchemy.url", "")


def include_object(object, name, type_, reflected, compare_to):
    """
    Filter which objects Alembic should track.
    Only include objects in the 'public' schema.
    Exclude PostGIS internal tables.
    """
    if type_ == "table":
        # Skip PostGIS internal tables
        if name in ("spatial_ref_sys", "geography_columns", "geometry_columns", "raster_columns", "raster_overviews"):
            return False
        # Only include tables we define (in public schema)
        schema = getattr(object, "schema", None)
        if schema and schema != "public":
            return False
    return True


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without connecting.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_object=include_object,
        version_table_schema="public",
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations with a live connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_object=include_object,
        version_table_schema="public",
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations using async engine.
    We create a sync-compatible URL for Alembic.
    """
    url = get_url()
    # Alembic needs a sync driver — use psycopg2
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Override to use sync driver
    )

    # Since we're using psycopg2 URL, use sync approach
    from sqlalchemy import create_engine
    sync_engine = create_engine(url, poolclass=pool.NullPool)

    with sync_engine.connect() as connection:
        do_run_migrations(connection)

    sync_engine.dispose()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — connects to database.
    Uses sync psycopg2 driver for compatibility.
    """
    from sqlalchemy import create_engine

    url = get_url()
    connectable = create_engine(url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
