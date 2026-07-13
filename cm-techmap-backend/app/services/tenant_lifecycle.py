"""
CM TECHMAP — Tenant Lifecycle Service
Complete provisioning, migration, activation/deactivation of tenant schemas.
Handles the full lifecycle: create → migrate → activate → deactivate → archive.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("cm_techmap.tenant_lifecycle")

# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA VERSION — increment when adding new tables/columns
# ══════════════════════════════════════════════════════════════════════════════
SCHEMA_VERSION = 5  # Current version of tenant schema definition

# All tables that must exist in each tenant schema
TENANT_TABLE_REGISTRY = [
    "users", "projects", "flights", "processing_jobs", "uploads",
    "flight_assets", "ai_detections", "parcels", "measurements",
    "reports", "activity_logs",
    # Sprint 1 additions:
    "discrepancies", "analysis_runs", "iptu_rules",
]


async def provision_tenant(
    session: AsyncSession,
    slug: str,
    *,
    municipality_name: str = "",
    city: str = "",
    state: str = "",
) -> dict:
    """
    Full tenant provisioning pipeline:
    1. Create PostgreSQL schema
    2. Create all tenant tables with indexes
    3. Create RLS policies
    4. Record schema version
    5. Return provisioning report
    """
    schema = f"tenant_{slug}"
    logger.info(f"[PROVISION] Starting provisioning for: {schema}")
    start = datetime.now(timezone.utc)
    report = {"schema": schema, "tables_created": [], "indexes_created": [], "rls_enabled": []}

    # ── Step 1: Create schema ─────────────────────────────────────────────
    await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    logger.info(f"[PROVISION] Schema created: {schema}")

    # ── Step 2: Create core tables ────────────────────────────────────────
    tables = await _create_all_tenant_tables(session, schema)
    report["tables_created"] = tables

    # ── Step 3: Create indexes ────────────────────────────────────────────
    indexes = await _create_tenant_indexes(session, schema)
    report["indexes_created"] = indexes

    # ── Step 4: RLS policies ──────────────────────────────────────────────
    rls = await _enable_rls_for_schema(session, schema)
    report["rls_enabled"] = rls

    # ── Step 5: Schema version tracking ───────────────────────────────────
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}"._schema_metadata (
            key VARCHAR(100) PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    await session.execute(text(f"""
        INSERT INTO "{schema}"._schema_metadata (key, value)
        VALUES ('schema_version', :ver), ('provisioned_at', :ts), ('municipality', :muni)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    """), {"ver": str(SCHEMA_VERSION), "ts": start.isoformat(), "muni": municipality_name or city})

    await session.commit()

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    report["elapsed_seconds"] = round(elapsed, 2)
    report["schema_version"] = SCHEMA_VERSION
    logger.info(f"[PROVISION] Complete: {schema} ({len(tables)} tables, {elapsed:.1f}s)")
    return report


async def migrate_tenant_schema(session: AsyncSession, slug: str) -> dict:
    """
    Idempotently migrate a tenant schema to the latest version.
    Creates any missing tables/columns without destroying existing data.
    """
    schema = f"tenant_{slug}"
    logger.info(f"[MIGRATE] Starting migration for: {schema}")

    # Check current version
    current_ver = 0
    try:
        result = await session.execute(text(
            f'SELECT value FROM "{schema}"._schema_metadata WHERE key = \'schema_version\''
        ))
        row = result.scalar()
        current_ver = int(row) if row else 0
    except Exception:
        pass

    if current_ver >= SCHEMA_VERSION:
        return {"schema": schema, "status": "up_to_date", "version": current_ver}

    # Apply missing tables idempotently
    tables = await _create_all_tenant_tables(session, schema)
    indexes = await _create_tenant_indexes(session, schema)
    rls = await _enable_rls_for_schema(session, schema)

    # Update version
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}"._schema_metadata (
            key VARCHAR(100) PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    await session.execute(text(f"""
        INSERT INTO "{schema}"._schema_metadata (key, value)
        VALUES ('schema_version', :ver), ('last_migration', :ts)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    """), {"ver": str(SCHEMA_VERSION), "ts": datetime.now(timezone.utc).isoformat()})

    await session.commit()

    return {
        "schema": schema,
        "status": "migrated",
        "from_version": current_ver,
        "to_version": SCHEMA_VERSION,
        "tables_synced": tables,
        "indexes_synced": indexes,
    }


async def migrate_all_tenants(session: AsyncSession) -> list[dict]:
    """Migrate ALL tenant schemas to the latest version."""
    result = await session.execute(text(
        "SELECT slug FROM public.tenants WHERE is_active = true ORDER BY slug"
    ))
    slugs = [row[0] for row in result.fetchall()]
    reports = []
    for slug in slugs:
        try:
            report = await migrate_tenant_schema(session, slug)
            reports.append(report)
        except Exception as e:
            reports.append({"schema": f"tenant_{slug}", "status": "error", "error": str(e)})
            logger.error(f"[MIGRATE] Failed for tenant_{slug}: {e}")
    return reports


async def deactivate_tenant(session: AsyncSession, slug: str) -> dict:
    """Deactivate a tenant (revoke schema access, mark inactive)."""
    schema = f"tenant_{slug}"
    await session.execute(text(
        "UPDATE public.tenants SET is_active = false, updated_at = NOW() WHERE slug = :slug"
    ), {"slug": slug})
    # Revoke connect privileges
    await session.execute(text(f'REVOKE ALL ON SCHEMA "{schema}" FROM PUBLIC'))
    await session.commit()
    logger.warning(f"[TENANT] Deactivated: {schema}")
    return {"schema": schema, "status": "deactivated"}


async def reactivate_tenant(session: AsyncSession, slug: str) -> dict:
    """Reactivate a previously deactivated tenant."""
    schema = f"tenant_{slug}"
    await session.execute(text(
        "UPDATE public.tenants SET is_active = true, updated_at = NOW() WHERE slug = :slug"
    ), {"slug": slug})
    await session.commit()
    logger.info(f"[TENANT] Reactivated: {schema}")
    return {"schema": schema, "status": "active"}


async def get_tenant_stats(session: AsyncSession, slug: str) -> dict:
    """Get comprehensive stats for a tenant schema."""
    schema = f"tenant_{slug}"
    stats = {"schema": schema}

    for table in ["users", "projects", "flights", "parcels", "ai_detections",
                  "discrepancies", "reports", "measurements"]:
        try:
            result = await session.execute(text(
                f'SELECT COUNT(*) FROM "{schema}"."{table}"'
            ))
            stats[f"{table}_count"] = result.scalar() or 0
        except Exception:
            stats[f"{table}_count"] = -1  # table doesn't exist

    # Storage usage
    try:
        result = await session.execute(text(f"""
            SELECT pg_size_pretty(
                sum(pg_total_relation_size(quote_ident(schemaname)||'.'||quote_ident(tablename)))
            )
            FROM pg_tables WHERE schemaname = :schema
        """), {"schema": schema})
        stats["disk_usage"] = result.scalar() or "0 bytes"
    except Exception:
        stats["disk_usage"] = "unknown"

    # Schema version
    try:
        result = await session.execute(text(
            f'SELECT value FROM "{schema}"._schema_metadata WHERE key = \'schema_version\''
        ))
        stats["schema_version"] = int(result.scalar() or 0)
    except Exception:
        stats["schema_version"] = 0

    return stats


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE: Table Creation (idempotent)
# ══════════════════════════════════════════════════════════════════════════════

async def _create_all_tenant_tables(session: AsyncSession, schema: str) -> list[str]:
    """Create all tenant tables idempotently. Returns list of table names."""
    created = []

    # ── users ─────────────────────────────────────────────────────────────
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
    created.append("users")

    # ── projects ──────────────────────────────────────────────────────────
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
    created.append("projects")

    # ── flights ───────────────────────────────────────────────────────────
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
    created.append("flights")

    # ── processing_jobs ───────────────────────────────────────────────────
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
    created.append("processing_jobs")

    # ── uploads ───────────────────────────────────────────────────────────
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
            uploaded_by UUID REFERENCES "{schema}".users(id),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """))
    created.append("uploads")

    # ── flight_assets ─────────────────────────────────────────────────────
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".flight_assets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_id UUID NOT NULL REFERENCES "{schema}".flights(id) ON DELETE CASCADE,
            asset_type VARCHAR(50) NOT NULL,
            file_key VARCHAR(1000) NOT NULL,
            s3_key VARCHAR(2048),
            bucket_name VARCHAR(255),
            file_size_bytes BIGINT,
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
    created.append("flight_assets")

    # ── ai_detections ─────────────────────────────────────────────────────
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".ai_detections (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_asset_id UUID NOT NULL REFERENCES "{schema}".flight_assets(id) ON DELETE CASCADE,
            detection_class VARCHAR(100) NOT NULL,
            polygon GEOMETRY(POLYGON, 4326) NOT NULL,
            confidence DOUBLE PRECISION,
            area_sqm DOUBLE PRECISION,
            perimeter_m DOUBLE PRECISION,
            height_m DOUBLE PRECISION,
            properties JSONB DEFAULT '{{}}',
            model_version VARCHAR(100),
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    created.append("ai_detections")

    # ── parcels ───────────────────────────────────────────────────────────
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
    created.append("parcels")

    # ── measurements ──────────────────────────────────────────────────────
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
    created.append("measurements")

    # ── reports ───────────────────────────────────────────────────────────
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
    created.append("reports")

    # ── activity_logs ─────────────────────────────────────────────────────
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
    created.append("activity_logs")

    # ── discrepancies (Sprint 1) ──────────────────────────────────────────
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".discrepancies (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID REFERENCES "{schema}".projects(id),
            analysis_run_id UUID,
            parcel_id UUID REFERENCES "{schema}".parcels(id),
            detection_id UUID REFERENCES "{schema}".ai_detections(id),
            discrepancy_type VARCHAR(50) NOT NULL,
            severity VARCHAR(20) DEFAULT 'medium',
            status VARCHAR(30) DEFAULT 'pending',
            cadastral_code VARCHAR(100),
            address TEXT,
            neighborhood VARCHAR(255),
            owner_name VARCHAR(500),
            registered_area_sqm DOUBLE PRECISION,
            detected_area_sqm DOUBLE PRECISION,
            difference_sqm DOUBLE PRECISION,
            difference_pct DOUBLE PRECISION,
            overlap_pct DOUBLE PRECISION,
            confidence DOUBLE PRECISION,
            detected_height_m DOUBLE PRECISION,
            registered_floors INTEGER,
            iptu_current_brl DOUBLE PRECISION DEFAULT 0,
            iptu_proposed_brl DOUBLE PRECISION DEFAULT 0,
            estimated_iptu_gap_brl DOUBLE PRECISION DEFAULT 0,
            parcel_geometry GEOMETRY(POLYGON, 4326),
            detection_geometry GEOMETRY(POLYGON, 4326),
            discrepancy_geometry GEOMETRY(POLYGON, 4326),
            calculation_details JSONB DEFAULT '{{}}',
            reviewed_by UUID REFERENCES "{schema}".users(id),
            reviewed_at TIMESTAMPTZ,
            review_notes TEXT,
            rejection_reason VARCHAR(100),
            inspection_date DATE,
            inspector_name VARCHAR(255),
            inspection_result VARCHAR(50),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    created.append("discrepancies")

    # ── analysis_runs (Sprint 1) ──────────────────────────────────────────
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".analysis_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID REFERENCES "{schema}".projects(id),
            run_type VARCHAR(50) NOT NULL,
            status VARCHAR(30) DEFAULT 'pending',
            triggered_by VARCHAR(255),
            parameters JSONB DEFAULT '{{}}',
            summary JSONB DEFAULT '{{}}',
            total_discrepancies INTEGER DEFAULT 0,
            elapsed_seconds DOUBLE PRECISION,
            started_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    created.append("analysis_runs")

    # ── iptu_rules (Sprint 1) ─────────────────────────────────────────────
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".iptu_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            municipality_code VARCHAR(20),
            municipality_name VARCHAR(255),
            year INTEGER NOT NULL,
            base_rate_per_sqm DOUBLE PRECISION DEFAULT 0,
            built_rate_per_sqm DOUBLE PRECISION DEFAULT 0,
            zone_name VARCHAR(100),
            zone_aliquot DOUBLE PRECISION DEFAULT 1.0,
            depreciation_rate DOUBLE PRECISION DEFAULT 0,
            is_active BOOLEAN DEFAULT true,
            properties JSONB DEFAULT '{{}}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    created.append("iptu_rules")

    return created


async def _create_tenant_indexes(session: AsyncSession, schema: str) -> list[str]:
    """Create spatial and performance indexes."""
    indexes = []
    idx_defs = [
        (f"idx_{schema}_ai_det_polygon", "ai_detections", "polygon", "GIST"),
        (f"idx_{schema}_parcels_polygon", "parcels", "polygon", "GIST"),
        (f"idx_{schema}_measurements_geom", "measurements", "geometry", "GIST"),
        (f"idx_{schema}_disc_parcel_geom", "discrepancies", "parcel_geometry", "GIST"),
        (f"idx_{schema}_disc_detect_geom", "discrepancies", "detection_geometry", "GIST"),
        (f"idx_{schema}_disc_status", "discrepancies", "status", "BTREE"),
        (f"idx_{schema}_disc_severity", "discrepancies", "severity", "BTREE"),
        (f"idx_{schema}_projects_code", "projects", "code", "BTREE"),
        (f"idx_{schema}_flights_project", "flights", "project_id", "BTREE"),
        (f"idx_{schema}_users_keycloak", "users", "keycloak_id", "BTREE"),
        (f"idx_{schema}_parcels_cadastral", "parcels", "cadastral_code", "BTREE"),
        (f"idx_{schema}_activity_created", "activity_logs", "created_at", "BTREE"),
    ]
    for idx_name, table, column, idx_type in idx_defs:
        safe_name = idx_name.replace("tenant_", "t_")[:63]  # PG identifier limit
        try:
            await session.execute(text(
                f'CREATE INDEX IF NOT EXISTS "{safe_name}" ON "{schema}"."{table}" USING {idx_type} ("{column}")'
            ))
            indexes.append(safe_name)
        except Exception as e:
            logger.warning(f"[IDX] Failed {safe_name}: {e}")
    return indexes


async def _enable_rls_for_schema(session: AsyncSession, schema: str) -> list[str]:
    """Enable Row-Level Security on tenant tables as a defense-in-depth measure."""
    enabled = []
    tables_with_rls = [
        "users", "projects", "flights", "parcels",
        "ai_detections", "discrepancies", "reports",
    ]
    for table in tables_with_rls:
        try:
            # Enable RLS on the table
            await session.execute(text(
                f'ALTER TABLE "{schema}"."{table}" ENABLE ROW LEVEL SECURITY'
            ))
            # Force RLS for table owner too (defense in depth)
            await session.execute(text(
                f'ALTER TABLE "{schema}"."{table}" FORCE ROW LEVEL SECURITY'
            ))
            # Policy: allow access only when search_path includes this schema
            policy_name = f"rls_{table}_schema_guard"
            await session.execute(text(f"""
                DO $$ BEGIN
                    CREATE POLICY "{policy_name}"
                        ON "{schema}"."{table}"
                        USING (current_setting('search_path') LIKE '%{schema}%');
                EXCEPTION WHEN duplicate_object THEN NULL;
                END $$
            """))
            enabled.append(table)
        except Exception as e:
            logger.warning(f"[RLS] Failed for {schema}.{table}: {e}")
    return enabled
