"""
CM TECHMAP — Tenant Schema Management
Provisioning and migration of per-tenant PostgreSQL schemas.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Tables that exist in each tenant schema (managed by Alembic)
TENANT_TABLES = [
    "users", "projects", "drone_flights", "uploads",
    "orthomosaics", "elevation_models", "point_clouds",
    "ai_detections", "parcels", "measurements",
    "reports", "activity_logs",
]


async def create_tenant_schema(session: AsyncSession, tenant_slug: str) -> None:
    """Create a new PostgreSQL schema for a tenant and apply the base tables."""
    schema_name = f"tenant_{tenant_slug}"
    logger.info(f"Creating tenant schema: {schema_name}")

    await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))

    # Copy table structure from a template schema or use Alembic
    # For now, we create the essential tables using raw SQL
    # In production, Alembic --schema flag handles this
    await _create_tenant_tables(session, schema_name)
    await session.commit()
    logger.info(f"Tenant schema '{schema_name}' created successfully")


async def drop_tenant_schema(session: AsyncSession, tenant_slug: str) -> None:
    """Drop a tenant schema (DANGEROUS — use only for cleanup)."""
    schema_name = f"tenant_{tenant_slug}"
    logger.warning(f"Dropping tenant schema: {schema_name}")
    await session.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
    await session.commit()


async def schema_exists(session: AsyncSession, tenant_slug: str) -> bool:
    """Check if a tenant schema exists."""
    schema_name = f"tenant_{tenant_slug}"
    result = await session.execute(
        text("SELECT 1 FROM information_schema.schemata WHERE schema_name = :name"),
        {"name": schema_name},
    )
    return result.scalar() is not None


async def list_tenant_schemas(session: AsyncSession) -> list[str]:
    """List all tenant schemas in the database."""
    result = await session.execute(
        text("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%' ORDER BY schema_name")
    )
    return [row[0] for row in result.fetchall()]


async def _create_tenant_tables(session: AsyncSession, schema: str) -> None:
    """Create the core tenant tables within the given schema."""

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            keycloak_id VARCHAR(255) UNIQUE NOT NULL,
            email VARCHAR(320) NOT NULL,
            name VARCHAR(255) NOT NULL,
            role VARCHAR(50) NOT NULL DEFAULT 'viewer',
            department VARCHAR(255),
            position VARCHAR(255),
            phone VARCHAR(50),
            avatar_url TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            last_login_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".projects (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(20) UNIQUE NOT NULL,
            name VARCHAR(500) NOT NULL,
            description TEXT,
            status VARCHAR(50) NOT NULL DEFAULT 'pendente',
            city VARCHAR(255),
            state VARCHAR(2),
            area_sqm DOUBLE PRECISION,
            flight_count INTEGER DEFAULT 0,
            image_count INTEGER DEFAULT 0,
            bbox_min_lon DOUBLE PRECISION,
            bbox_min_lat DOUBLE PRECISION,
            bbox_max_lon DOUBLE PRECISION,
            bbox_max_lat DOUBLE PRECISION,
            created_by UUID REFERENCES "{schema}".users(id),
            responsible VARCHAR(500),
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".flights (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES "{schema}".projects(id) ON DELETE CASCADE,
            flight_date DATE NOT NULL,
            altitude_m DOUBLE PRECISION,
            overlap_pct DOUBLE PRECISION,
            sidelap_pct DOUBLE PRECISION,
            images_count INTEGER DEFAULT 0,
            camera_model VARCHAR(200),
            sensor_width_mm DOUBLE PRECISION,
            focal_length_mm DOUBLE PRECISION,
            gsd_cm DOUBLE PRECISION,
            area_coverage_sqm DOUBLE PRECISION,
            status VARCHAR(50) DEFAULT 'pending',
            notes TEXT,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".processing_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_id UUID NOT NULL REFERENCES "{schema}".flights(id) ON DELETE CASCADE,
            celery_task_id VARCHAR(255) UNIQUE,
            stage VARCHAR(50) NOT NULL DEFAULT 'queued',
            progress INTEGER DEFAULT 0,
            status_message TEXT,
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            error_message TEXT,
            odm_task_uuid VARCHAR(255),
            odm_options JSONB,
            started_at TIMESTAMPTZ,
            completed_at TIMESTAMPTZ,
            processing_time_sec DOUBLE PRECISION,
            result_metadata JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".uploads (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES "{schema}".projects(id) ON DELETE CASCADE,
            flight_id UUID REFERENCES "{schema}".flights(id),
            filename VARCHAR(1024) NOT NULL,
            original_filename VARCHAR(1024) NOT NULL,
            file_key VARCHAR(2048) NOT NULL,
            bucket VARCHAR(255) NOT NULL,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            content_type VARCHAR(255),
            chunk_count INTEGER DEFAULT 0,
            chunks_received INTEGER DEFAULT 0,
            status VARCHAR(50) DEFAULT 'uploading',
            checksum_sha256 VARCHAR(64),
            processing_job_id VARCHAR(255),
            uploaded_by UUID REFERENCES "{schema}".users(id),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".flight_assets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_id UUID NOT NULL REFERENCES "{schema}".flights(id) ON DELETE CASCADE,
            asset_type VARCHAR(50) NOT NULL,
            file_key VARCHAR(1000) NOT NULL,
            bucket_name VARCHAR(255),
            file_size_bytes INTEGER,
            content_type VARCHAR(100),
            checksum_sha256 VARCHAR(64),
            resolution_cm DOUBLE PRECISION,
            cog_validated BOOLEAN DEFAULT false,
            crs_epsg INTEGER,
            bbox_min_lon DOUBLE PRECISION,
            bbox_min_lat DOUBLE PRECISION,
            bbox_max_lon DOUBLE PRECISION,
            bbox_max_lat DOUBLE PRECISION,
            metadata_json JSONB,
            is_active BOOLEAN DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".ai_detections (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_asset_id UUID NOT NULL REFERENCES "{schema}".flight_assets(id) ON DELETE CASCADE,
            detection_class VARCHAR(100) NOT NULL,
            polygon GEOMETRY(POLYGON, 4326) NOT NULL,
            confidence DOUBLE PRECISION,
            area_sqm DOUBLE PRECISION,
            perimeter_m DOUBLE PRECISION,
            properties JSONB DEFAULT '{{}}',
            model_version VARCHAR(100),
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".parcels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cadastral_code VARCHAR(100) UNIQUE,
            address TEXT,
            neighborhood VARCHAR(255),
            polygon GEOMETRY(POLYGON, 4326) NOT NULL,
            registered_area_sqm DOUBLE PRECISION,
            registered_built_area_sqm DOUBLE PRECISION,
            land_use VARCHAR(100),
            iptu_zone VARCHAR(100),
            owner_name VARCHAR(500),
            imported_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".measurements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES "{schema}".projects(id) ON DELETE CASCADE,
            measurement_type VARCHAR(50) NOT NULL,
            geometry GEOMETRY(GEOMETRY, 4326) NOT NULL,
            value DOUBLE PRECISION,
            unit VARCHAR(20),
            label VARCHAR(500),
            notes TEXT,
            measured_by UUID REFERENCES "{schema}".users(id),
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".reports (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID REFERENCES "{schema}".projects(id),
            report_type VARCHAR(100) NOT NULL,
            title VARCHAR(500) NOT NULL,
            file_key VARCHAR(2048),
            file_format VARCHAR(10) DEFAULT 'PDF',
            file_size_bytes BIGINT,
            parameters JSONB DEFAULT '{{}}',
            generated_by UUID REFERENCES "{schema}".users(id),
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".activity_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES "{schema}".users(id),
            action VARCHAR(100) NOT NULL,
            entity_type VARCHAR(100),
            entity_id UUID,
            details JSONB DEFAULT '{{}}',
            ip_address VARCHAR(45),
            user_agent TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))

    # Create spatial indexes for geometry columns
    for tbl, col in [
        ("ai_detections", "polygon"), 
        ("parcels", "polygon"),
        ("measurements", "geometry"),
    ]:
        idx_name = f"idx_{tbl}_{col}_gist"
        await session.execute(text(
            f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{schema}"."{tbl}" USING GIST ("{col}")'
        ))
