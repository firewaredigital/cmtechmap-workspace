"""
CM TECHMAP — Tenant Lifecycle Service
Complete provisioning, migration, activation/deactivation of tenant schemas.
Handles the full lifecycle: create → migrate → activate → deactivate → archive.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("cm_techmap.tenant_lifecycle")

# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA VERSION — increment when adding new tables/columns
# ══════════════════════════════════════════════════════════════════════════════
# v7: parcels ganhou o formato fiscal canônico (owner_cpf_cnpj,
# iptu_value_current_brl, properties) e polygon passou a ser ANULÁVEL — o
# cadastro chega antes da geometria na vida real, e a malha fina consulta
# essas colunas: em schemas v6 a query falhava e era engolida como lista
# vazia, fazendo a análise reportar zero para sempre.
SCHEMA_VERSION = 7  # Current version of tenant schema definition

# All tables that must exist in each tenant schema
TENANT_TABLE_REGISTRY = [
    "users", "projects", "flights", "processing_jobs", "uploads",
    "flight_assets", "ai_detections", "parcels", "measurements",
    "reports", "activity_logs",
    # Sprint 1 additions:
    "discrepancies", "analysis_runs", "iptu_rule_sets", "iptu_rules",
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
    start = datetime.now(UTC)
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

    elapsed = (datetime.now(UTC) - start).total_seconds()
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

    # Check current version.
    # A missing _schema_metadata table (never-provisioned or partially
    # provisioned tenant) makes this SELECT fail, which ABORTS the Postgres
    # transaction — every following statement would then die with
    # InFailedSQLTransactionError. Roll back before continuing.
    current_ver = 0
    try:
        result = await session.execute(text(
            f'SELECT value FROM "{schema}"._schema_metadata WHERE key = \'schema_version\''
        ))
        row = result.scalar()
        current_ver = int(row) if row else 0
    except Exception as exc:
        logger.info(f"[MIGRATE] No schema version for {schema} ({exc.__class__.__name__}); treating as v0")
        await session.rollback()

    if current_ver >= SCHEMA_VERSION:
        return {"schema": schema, "status": "up_to_date", "version": current_ver}

    # The schema itself may not exist yet (tenant row created without
    # provisioning) — CREATE TABLE would fail on a missing schema.
    await session.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))

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
    """), {"ver": str(SCHEMA_VERSION), "ts": datetime.now(UTC).isoformat()})

    await session.commit()

    return {
        "schema": schema,
        "status": "migrated",
        "from_version": current_ver,
        "to_version": SCHEMA_VERSION,
        "tables_synced": tables,
        "indexes_synced": indexes,
        "rls_synced": rls,
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
            # Clear the aborted transaction so one broken tenant does not
            # cascade into failures for every tenant that follows it.
            await session.rollback()
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
        result = await session.execute(text("""
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
    # Canonical fiscal shape (v7) — must stay aligned with public.parcels
    # (migration 005): the CSV import and the malha fina run against whichever
    # of the two the session search_path resolves.
    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".parcels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cadastral_code VARCHAR(100) UNIQUE,
            address TEXT,
            neighborhood VARCHAR(255),
            polygon GEOMETRY(POLYGON, 4326),
            registered_area_sqm DOUBLE PRECISION,
            registered_built_area_sqm DOUBLE PRECISION,
            land_use VARCHAR(100),
            iptu_zone VARCHAR(100),
            owner_name VARCHAR(500),
            owner_cpf_cnpj VARCHAR(30),
            iptu_value_current_brl DOUBLE PRECISION,
            properties JSONB DEFAULT '{{}}',
            imported_at TIMESTAMPTZ DEFAULT NOW(),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    # Converge schemas provisioned before v7 (idempotent on re-run)
    await session.execute(text(f"""
        ALTER TABLE "{schema}".parcels
            ADD COLUMN IF NOT EXISTS owner_cpf_cnpj VARCHAR(30),
            ADD COLUMN IF NOT EXISTS iptu_value_current_brl DOUBLE PRECISION,
            ADD COLUMN IF NOT EXISTS properties JSONB DEFAULT '{{}}',
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
            ALTER COLUMN polygon DROP NOT NULL
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
            estimated_total_gap_brl DOUBLE PRECISION DEFAULT 0,
            elapsed_seconds DOUBLE PRECISION,
            started_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """))
    # Converge schemas provisioned before v7: a malha fina grava o gap total
    # no run — sem a coluna, a análise inteira falhava (e era engolida).
    await session.execute(text(f"""
        ALTER TABLE "{schema}".analysis_runs
            ADD COLUMN IF NOT EXISTS estimated_total_gap_brl DOUBLE PRECISION DEFAULT 0,
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()
    """))
    created.append("analysis_runs")

    # ── iptu_rule_sets + iptu_rules (canonical, schema v6) ────────────────
    # Earlier versions created a tenant iptu_rules with a completely different
    # column set (municipality_code/year/base_rate_per_sqm/...). Because the
    # session search_path is "tenant, public", that table shadowed the public
    # one and broke every /iptu/rules endpoint for tenant users. The legacy
    # table was unusable by the API (all queries failed), so it is safe to
    # drop it and recreate with the canonical shape.
    await session.execute(text(f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = '{schema}' AND table_name = 'iptu_rules'
                  AND column_name = 'base_rate_per_sqm'
            ) THEN
                DROP TABLE "{schema}".iptu_rules;
            END IF;
        END;
        $$
    """))

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".iptu_rule_sets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            municipality_name VARCHAR(200) NOT NULL,
            municipality_code VARCHAR(10) NOT NULL UNIQUE,
            state VARCHAR(2) NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            base_year INTEGER NOT NULL,
            default_land_value_per_sqm DOUBLE PRECISION DEFAULT 50.0,
            default_built_value_per_sqm DOUBLE PRECISION DEFAULT 800.0,
            default_aliquot_pct DOUBLE PRECISION DEFAULT 1.0,
            pool_surcharge_pct DOUBLE PRECISION DEFAULT 20.0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    created.append("iptu_rule_sets")

    await session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS "{schema}".iptu_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_set_id UUID NOT NULL REFERENCES "{schema}".iptu_rule_sets(id) ON DELETE CASCADE,
            zone_name VARCHAR(100) NOT NULL,
            land_value_per_sqm_brl DOUBLE PRECISION NOT NULL,
            built_value_per_sqm_brl DOUBLE PRECISION NOT NULL,
            aliquot_pct DOUBLE PRECISION NOT NULL,
            depreciation_rate_per_year DOUBLE PRECISION DEFAULT 0.01,
            min_area_sqm DOUBLE PRECISION DEFAULT 0.0,
            max_depreciation_pct DOUBLE PRECISION DEFAULT 50.0,
            exemption_rules JSONB DEFAULT '{{}}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(rule_set_id, zone_name)
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
