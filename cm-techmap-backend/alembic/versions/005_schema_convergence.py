"""Schema convergence — reconcile Alembic-managed and raw-SQL-managed tables

Revision ID: 005
Revises: 004
Create Date: 2026-07-25

Databases in the field were provisioned through three different paths
(Alembic chain, docker-entrypoint init-scripts + migrations/*.sql, and
manual bootstraps), which produced divergent shapes for the same tables.
This revision converges every known state onto the canonical schema:

- Creates `uploads` / `activity_logs` when absent (some DBs were stamped
  past revision 002 without ever running it).
- Creates the fiscal tables (`parcels`, `ai_detections`, `analysis_runs`,
  `discrepancies`, `iptu_rule_sets`, `iptu_rules`, `measurements`) that
  previously existed only in migrations/001_fiscal_virtual.sql.
- Creates the AI metrology tables (`ai_measurement_runs`,
  `ai_building_measurements`, `ai_terrain_measurements`) that previously
  existed only in migrations/002_ai_measurements.sql.
- Converges legacy `reports` (format/parameters/file_path/generated_by)
  to the canonical columns (output_format/config/file_key/requested_by/
  celery_task_id), copying data across.
- Converges legacy `report_sections` (section_order) to include the
  canonical "order" column.
- Converges legacy `notifications` (user_id UUID, no entity columns) to
  the canonical shape (user_id VARCHAR(320), entity_type/entity_id).
- Backfills NULL/empty `projects.code` values with sequential codes.

Every statement is idempotent (IF NOT EXISTS / guarded DO blocks), so the
revision is safe on databases that are already canonical.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Prerequisites ─────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # ── uploads (canonical: revision 002) ────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.uploads (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
            flight_id UUID REFERENCES public.flights(id) ON DELETE SET NULL,
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
            uploaded_by VARCHAR(320),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_uploads_project_id ON public.uploads(project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_uploads_status ON public.uploads(status)")

    # ── activity_logs (canonical: revision 002) ──────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.activity_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR(320),
            action VARCHAR(100) NOT NULL,
            entity_type VARCHAR(100),
            entity_id VARCHAR(255),
            details JSONB DEFAULT '{}',
            ip_address VARCHAR(45),
            user_agent TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_activity_logs_user_id ON public.activity_logs(user_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_activity_logs_action ON public.activity_logs(action)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_activity_logs_created_at ON public.activity_logs(created_at)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_activity_logs_entity "
        "ON public.activity_logs(entity_type, entity_id)"
    )

    # ── notifications: create canonical when absent, converge legacy shape ───
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.notifications (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id VARCHAR(320) NOT NULL,
            title VARCHAR(200) NOT NULL,
            message TEXT NOT NULL,
            type VARCHAR(20) NOT NULL DEFAULT 'info',
            category VARCHAR(50) NOT NULL DEFAULT 'system',
            link VARCHAR(500),
            entity_type VARCHAR(100),
            entity_id VARCHAR(255),
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Legacy variant used UUID for user_id — the app writes Keycloak subs and
    # e-mails (strings), so every insert failed on those databases.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'notifications'
                  AND column_name = 'user_id' AND udt_name = 'uuid'
            ) THEN
                ALTER TABLE public.notifications
                    ALTER COLUMN user_id TYPE VARCHAR(320) USING user_id::text;
            END IF;
        END;
        $$
    """)
    op.execute("ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS entity_type VARCHAR(100)")
    op.execute("ALTER TABLE public.notifications ADD COLUMN IF NOT EXISTS entity_id VARCHAR(255)")
    op.execute("UPDATE public.notifications SET category = 'system' WHERE category IS NULL")
    op.execute("ALTER TABLE public.notifications ALTER COLUMN category SET DEFAULT 'system'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON public.notifications(user_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_user_unread "
        "ON public.notifications(user_id, is_read)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_notifications_created_at ON public.notifications(created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notifications_category ON public.notifications(category)")

    # ── reports: add canonical columns, copy data from legacy columns ────────
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS output_format VARCHAR(20) DEFAULT 'pdf'")
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS config JSONB DEFAULT '{}'")
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS file_key VARCHAR(2048)")
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT")
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS requested_by VARCHAR(320)")
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS celery_task_id VARCHAR(255)")
    op.execute("ALTER TABLE public.reports ADD COLUMN IF NOT EXISTS error_message TEXT")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'reports'
                  AND column_name = 'format'
            ) THEN
                UPDATE public.reports SET output_format = format
                WHERE output_format IS NULL AND format IS NOT NULL;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'reports'
                  AND column_name = 'parameters'
            ) THEN
                UPDATE public.reports SET config = parameters
                WHERE (config IS NULL OR config = '{}'::jsonb) AND parameters IS NOT NULL;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'reports'
                  AND column_name = 'file_path'
            ) THEN
                UPDATE public.reports SET file_key = file_path
                WHERE file_key IS NULL AND file_path IS NOT NULL;
            END IF;
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'reports'
                  AND column_name = 'generated_by'
            ) THEN
                UPDATE public.reports SET requested_by = generated_by
                WHERE requested_by IS NULL AND generated_by IS NOT NULL;
            END IF;
        END;
        $$
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_reports_project_id ON public.reports(project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_reports_status ON public.reports(status)")

    # ── report_sections: ensure canonical "order" column ─────────────────────
    op.execute('ALTER TABLE public.report_sections ADD COLUMN IF NOT EXISTS "order" INTEGER DEFAULT 0')
    op.execute("ALTER TABLE public.report_sections ADD COLUMN IF NOT EXISTS content TEXT")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'report_sections'
                  AND column_name = 'section_order'
            ) THEN
                UPDATE public.report_sections SET "order" = section_order
                WHERE "order" IS DISTINCT FROM section_order AND section_order IS NOT NULL;
            END IF;
        END;
        $$
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_report_sections_report_id "
        "ON public.report_sections(report_id)"
    )

    # ── Fiscal Virtual tables (canonical: migrations/001_fiscal_virtual.sql) ─
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.parcels (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            cadastral_code VARCHAR(100) NOT NULL UNIQUE,
            address VARCHAR(500),
            neighborhood VARCHAR(200),
            owner_name VARCHAR(500),
            owner_cpf_cnpj VARCHAR(20),
            registered_area_sqm DOUBLE PRECISION,
            registered_built_area_sqm DOUBLE PRECISION,
            land_use VARCHAR(50),
            iptu_zone VARCHAR(50),
            iptu_value_current_brl DOUBLE PRECISION,
            polygon geometry(Polygon, 4326),
            properties JSONB DEFAULT '{}',
            imported_at TIMESTAMPTZ DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_parcels_cadastral_code ON public.parcels(cadastral_code)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_parcels_neighborhood ON public.parcels(neighborhood)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_parcels_land_use ON public.parcels(land_use)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_parcels_iptu_zone ON public.parcels(iptu_zone)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_parcels_polygon ON public.parcels USING GIST(polygon)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.ai_detections (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_asset_id UUID NOT NULL,
            detection_class VARCHAR(50) NOT NULL DEFAULT 'building',
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            area_sqm DOUBLE PRECISION,
            perimeter_m DOUBLE PRECISION,
            polygon geometry(Polygon, 4326),
            properties JSONB DEFAULT '{}',
            model_version VARCHAR(100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_detections_flight_asset_id "
        "ON public.ai_detections(flight_asset_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_detections_class ON public.ai_detections(detection_class)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_detections_confidence ON public.ai_detections(confidence)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_detections_polygon ON public.ai_detections USING GIST(polygon)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.analysis_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
            run_type VARCHAR(50) NOT NULL,
            status VARCHAR(30) NOT NULL DEFAULT 'running',
            triggered_by VARCHAR(320),
            parameters JSONB DEFAULT '{}',
            summary JSONB DEFAULT '{}',
            total_discrepancies INTEGER DEFAULT 0,
            estimated_total_gap_brl DOUBLE PRECISION DEFAULT 0.0,
            started_at TIMESTAMPTZ DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            elapsed_seconds DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_analysis_runs_project_id ON public.analysis_runs(project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_analysis_runs_status ON public.analysis_runs(status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_analysis_runs_run_type ON public.analysis_runs(run_type)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.discrepancies (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
            parcel_id UUID REFERENCES public.parcels(id) ON DELETE SET NULL,
            detection_id UUID REFERENCES public.ai_detections(id) ON DELETE SET NULL,
            analysis_run_id UUID REFERENCES public.analysis_runs(id) ON DELETE SET NULL,
            discrepancy_type VARCHAR(50) NOT NULL,
            severity VARCHAR(20) NOT NULL DEFAULT 'medium',
            cadastral_code VARCHAR(100),
            address VARCHAR(500),
            neighborhood VARCHAR(200),
            owner_name VARCHAR(500),
            registered_area_sqm DOUBLE PRECISION,
            detected_area_sqm DOUBLE PRECISION,
            difference_sqm DOUBLE PRECISION,
            difference_pct DOUBLE PRECISION,
            overlap_pct DOUBLE PRECISION,
            confidence DOUBLE PRECISION,
            detected_height_m DOUBLE PRECISION,
            registered_floors INTEGER,
            iptu_current_brl DOUBLE PRECISION,
            iptu_proposed_brl DOUBLE PRECISION,
            estimated_iptu_gap_brl DOUBLE PRECISION DEFAULT 0.0,
            calculation_details JSONB,
            status VARCHAR(30) NOT NULL DEFAULT 'pending',
            reviewed_by VARCHAR(320),
            reviewed_at TIMESTAMPTZ,
            reviewer_notes TEXT,
            rejection_reason VARCHAR(200),
            inspection_date TIMESTAMPTZ,
            inspector_name VARCHAR(300),
            inspector_report TEXT,
            inspection_result VARCHAR(50),
            polygon geometry(Polygon, 4326),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_status ON public.discrepancies(status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_type ON public.discrepancies(discrepancy_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_severity ON public.discrepancies(severity)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_project_id ON public.discrepancies(project_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_discrepancies_analysis_run_id "
        "ON public.discrepancies(analysis_run_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_discrepancies_polygon ON public.discrepancies USING GIST(polygon)")

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.iptu_rule_sets (
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
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_iptu_rule_sets_municipality "
        "ON public.iptu_rule_sets(municipality_code)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.iptu_rules (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_set_id UUID NOT NULL REFERENCES public.iptu_rule_sets(id) ON DELETE CASCADE,
            zone_name VARCHAR(100) NOT NULL,
            land_value_per_sqm_brl DOUBLE PRECISION NOT NULL,
            built_value_per_sqm_brl DOUBLE PRECISION NOT NULL,
            aliquot_pct DOUBLE PRECISION NOT NULL,
            depreciation_rate_per_year DOUBLE PRECISION DEFAULT 0.01,
            min_area_sqm DOUBLE PRECISION DEFAULT 0.0,
            max_depreciation_pct DOUBLE PRECISION DEFAULT 50.0,
            exemption_rules JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(rule_set_id, zone_name)
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_iptu_rules_rule_set_zone "
        "ON public.iptu_rules(rule_set_id, zone_name)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.measurements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL,
            measurement_type VARCHAR(30) NOT NULL,
            geometry geometry(Geometry, 4326),
            value DOUBLE PRECISION NOT NULL,
            unit VARCHAR(20) DEFAULT 'm',
            label VARCHAR(200),
            notes TEXT,
            measured_by VARCHAR(320),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_measurements_project_id ON public.measurements(project_id)")

    # ── AI metrology tables (canonical: migrations/002_ai_measurements.sql) ──
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.ai_measurement_runs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            project_id UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
            flight_asset_id UUID,
            flight_id UUID,
            orthomosaic_file_key VARCHAR(1000),
            dsm_file_key VARCHAR(1000),
            status VARCHAR(30) NOT NULL DEFAULT 'running',
            model_version VARCHAR(120) NOT NULL DEFAULT 'building_extractor_v1',
            terrain_model_version VARCHAR(120) NOT NULL DEFAULT 'terrain_extractor_v1',
            qa_score DOUBLE PRECISION,
            terrain_area_total_sqm DOUBLE PRECISION DEFAULT 0,
            built_area_total_sqm DOUBLE PRECISION DEFAULT 0,
            total_buildings INTEGER DEFAULT 0,
            total_terrain_patches INTEGER DEFAULT 0,
            error_message TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_measurement_runs_project_id "
        "ON public.ai_measurement_runs(project_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_measurement_runs_status ON public.ai_measurement_runs(status)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_measurement_runs_started_at "
        "ON public.ai_measurement_runs(started_at DESC)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.ai_building_measurements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES public.ai_measurement_runs(id) ON DELETE CASCADE,
            detection_id UUID REFERENCES public.ai_detections(id) ON DELETE SET NULL,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            area_sqm DOUBLE PRECISION NOT NULL DEFAULT 0,
            perimeter_m DOUBLE PRECISION,
            height_m DOUBLE PRECISION,
            floors_estimate INTEGER,
            building_type VARCHAR(80),
            orientation_deg DOUBLE PRECISION,
            compactness DOUBLE PRECISION,
            quality_score DOUBLE PRECISION,
            properties JSONB DEFAULT '{}',
            polygon geometry(Polygon, 4326),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_building_measurements_run_id "
        "ON public.ai_building_measurements(run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_building_measurements_detection_id "
        "ON public.ai_building_measurements(detection_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_building_measurements_area "
        "ON public.ai_building_measurements(area_sqm)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_building_measurements_polygon "
        "ON public.ai_building_measurements USING GIST(polygon)"
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS public.ai_terrain_measurements (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id UUID NOT NULL REFERENCES public.ai_measurement_runs(id) ON DELETE CASCADE,
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            area_sqm DOUBLE PRECISION NOT NULL DEFAULT 0,
            perimeter_m DOUBLE PRECISION,
            compactness DOUBLE PRECISION,
            surface_type VARCHAR(80) DEFAULT 'terrain',
            properties JSONB DEFAULT '{}',
            polygon geometry(Polygon, 4326),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_terrain_measurements_run_id "
        "ON public.ai_terrain_measurements(run_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_terrain_measurements_area "
        "ON public.ai_terrain_measurements(area_sqm)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_terrain_measurements_polygon "
        "ON public.ai_terrain_measurements USING GIST(polygon)"
    )

    # ── updated_at trigger function + triggers on the new tables ─────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION public.trigger_set_updated_at()
        RETURNS TRIGGER AS $f$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $f$ LANGUAGE plpgsql
    """)
    for table in (
        "ai_measurement_runs",
        "ai_building_measurements",
        "ai_terrain_measurements",
    ):
        op.execute(f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger WHERE tgname = 'set_updated_at_{table}'
                ) THEN
                    CREATE TRIGGER set_updated_at_{table}
                    BEFORE UPDATE ON public.{table}
                    FOR EACH ROW EXECUTE FUNCTION public.trigger_set_updated_at();
                END IF;
            END;
            $$
        """)

    # ── projects: backfill NULL/empty codes with sequential values ───────────
    op.execute(r"""
        DO $$
        DECLARE
            next_num INTEGER;
            rec RECORD;
        BEGIN
            SELECT COALESCE(MAX(NULLIF(regexp_replace(code, '\D', '', 'g'), '')::int), 0) + 1
            INTO next_num
            FROM public.projects
            WHERE code LIKE 'PRJ-%';

            FOR rec IN
                SELECT id FROM public.projects
                WHERE code IS NULL OR btrim(code) = ''
                ORDER BY created_at
            LOOP
                UPDATE public.projects
                SET code = 'PRJ-' || lpad(next_num::text, 3, '0')
                WHERE id = rec.id;
                next_num := next_num + 1;
            END LOOP;
        END;
        $$
    """)


def downgrade() -> None:
    # Convergence of divergent field schemas is intentionally not reversible:
    # there is no single "previous" state to return to.
    pass
