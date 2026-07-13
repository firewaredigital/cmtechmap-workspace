-- ============================================================================
-- CM TECHMAP — Fiscal Virtual Bootstrap Migration
-- Creates all tables required for the IPTU Malha Fina / Fiscal Virtual system.
-- Safe to run multiple times (uses IF NOT EXISTS).
-- ============================================================================

-- Ensure PostGIS extension is available
CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================================
-- 1. PARCELS (Cadastral Lots)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.parcels (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cadastral_code  VARCHAR(100) NOT NULL UNIQUE,
    address         VARCHAR(500),
    neighborhood    VARCHAR(200),
    owner_name      VARCHAR(500),
    owner_cpf_cnpj  VARCHAR(20),
    registered_area_sqm       DOUBLE PRECISION,
    registered_built_area_sqm DOUBLE PRECISION,
    land_use        VARCHAR(50),
    iptu_zone       VARCHAR(50),
    iptu_value_current_brl    DOUBLE PRECISION,
    polygon         geometry(Polygon, 4326),
    properties      JSONB DEFAULT '{}',
    imported_at     TIMESTAMPTZ DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_parcels_cadastral_code ON public.parcels(cadastral_code);
CREATE INDEX IF NOT EXISTS ix_parcels_neighborhood ON public.parcels(neighborhood);
CREATE INDEX IF NOT EXISTS ix_parcels_land_use ON public.parcels(land_use);
CREATE INDEX IF NOT EXISTS ix_parcels_iptu_zone ON public.parcels(iptu_zone);
CREATE INDEX IF NOT EXISTS ix_parcels_polygon ON public.parcels USING GIST(polygon);

-- ============================================================================
-- 2. AI DETECTIONS
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.ai_detections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    flight_asset_id UUID NOT NULL,
    detection_class VARCHAR(50) NOT NULL DEFAULT 'building',
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    area_sqm        DOUBLE PRECISION,
    perimeter_m     DOUBLE PRECISION,
    polygon         geometry(Polygon, 4326),
    properties      JSONB DEFAULT '{}',
    model_version   VARCHAR(100),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_ai_detections_flight_asset_id ON public.ai_detections(flight_asset_id);
CREATE INDEX IF NOT EXISTS ix_ai_detections_class ON public.ai_detections(detection_class);
CREATE INDEX IF NOT EXISTS ix_ai_detections_confidence ON public.ai_detections(confidence);
CREATE INDEX IF NOT EXISTS ix_ai_detections_polygon ON public.ai_detections USING GIST(polygon);

-- ============================================================================
-- 3. ANALYSIS RUNS
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.analysis_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    run_type        VARCHAR(50) NOT NULL,
    status          VARCHAR(30) NOT NULL DEFAULT 'running',
    triggered_by    VARCHAR(320),
    parameters      JSONB DEFAULT '{}',
    summary         JSONB DEFAULT '{}',
    total_discrepancies INTEGER DEFAULT 0,
    estimated_total_gap_brl DOUBLE PRECISION DEFAULT 0.0,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    elapsed_seconds DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_analysis_runs_project_id ON public.analysis_runs(project_id);
CREATE INDEX IF NOT EXISTS ix_analysis_runs_status ON public.analysis_runs(status);
CREATE INDEX IF NOT EXISTS ix_analysis_runs_run_type ON public.analysis_runs(run_type);

-- ============================================================================
-- 4. DISCREPANCIES
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.discrepancies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES public.projects(id) ON DELETE CASCADE,
    parcel_id       UUID REFERENCES public.parcels(id) ON DELETE SET NULL,
    detection_id    UUID REFERENCES public.ai_detections(id) ON DELETE SET NULL,
    analysis_run_id UUID REFERENCES public.analysis_runs(id) ON DELETE SET NULL,
    -- Classification
    discrepancy_type VARCHAR(50) NOT NULL,
    severity         VARCHAR(20) NOT NULL DEFAULT 'medium',
    -- Denormalized parcel data
    cadastral_code   VARCHAR(100),
    address          VARCHAR(500),
    neighborhood     VARCHAR(200),
    owner_name       VARCHAR(500),
    -- Area comparison
    registered_area_sqm DOUBLE PRECISION,
    detected_area_sqm   DOUBLE PRECISION,
    difference_sqm      DOUBLE PRECISION,
    difference_pct      DOUBLE PRECISION,
    overlap_pct         DOUBLE PRECISION,
    confidence          DOUBLE PRECISION,
    -- Height comparison
    detected_height_m   DOUBLE PRECISION,
    registered_floors   INTEGER,
    -- IPTU calculation
    iptu_current_brl    DOUBLE PRECISION,
    iptu_proposed_brl   DOUBLE PRECISION,
    estimated_iptu_gap_brl DOUBLE PRECISION DEFAULT 0.0,
    calculation_details JSONB,
    -- Decision
    status           VARCHAR(30) NOT NULL DEFAULT 'pending',
    reviewed_by      VARCHAR(320),
    reviewed_at      TIMESTAMPTZ,
    reviewer_notes   TEXT,
    rejection_reason VARCHAR(200),
    -- Inspection
    inspection_date  TIMESTAMPTZ,
    inspector_name   VARCHAR(300),
    inspector_report TEXT,
    inspection_result VARCHAR(50),
    -- Geometry
    polygon          geometry(Polygon, 4326),
    -- Timestamps
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_discrepancies_status ON public.discrepancies(status);
CREATE INDEX IF NOT EXISTS ix_discrepancies_type ON public.discrepancies(discrepancy_type);
CREATE INDEX IF NOT EXISTS ix_discrepancies_severity ON public.discrepancies(severity);
CREATE INDEX IF NOT EXISTS ix_discrepancies_project_id ON public.discrepancies(project_id);
CREATE INDEX IF NOT EXISTS ix_discrepancies_analysis_run_id ON public.discrepancies(analysis_run_id);
CREATE INDEX IF NOT EXISTS ix_discrepancies_polygon ON public.discrepancies USING GIST(polygon);

-- ============================================================================
-- 5. IPTU RULE SETS
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.iptu_rule_sets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    municipality_name VARCHAR(200) NOT NULL,
    municipality_code VARCHAR(10) NOT NULL UNIQUE,
    state           VARCHAR(2) NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    base_year       INTEGER NOT NULL,
    default_land_value_per_sqm   DOUBLE PRECISION DEFAULT 50.0,
    default_built_value_per_sqm  DOUBLE PRECISION DEFAULT 800.0,
    default_aliquot_pct          DOUBLE PRECISION DEFAULT 1.0,
    pool_surcharge_pct           DOUBLE PRECISION DEFAULT 20.0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_iptu_rule_sets_municipality ON public.iptu_rule_sets(municipality_code);

-- ============================================================================
-- 6. IPTU RULES (Zone-specific)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.iptu_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_set_id     UUID NOT NULL REFERENCES public.iptu_rule_sets(id) ON DELETE CASCADE,
    zone_name       VARCHAR(100) NOT NULL,
    land_value_per_sqm_brl   DOUBLE PRECISION NOT NULL,
    built_value_per_sqm_brl  DOUBLE PRECISION NOT NULL,
    aliquot_pct              DOUBLE PRECISION NOT NULL,
    depreciation_rate_per_year DOUBLE PRECISION DEFAULT 0.01,
    min_area_sqm             DOUBLE PRECISION DEFAULT 0.0,
    max_depreciation_pct     DOUBLE PRECISION DEFAULT 50.0,
    exemption_rules          JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(rule_set_id, zone_name)
);

CREATE INDEX IF NOT EXISTS ix_iptu_rules_rule_set_zone ON public.iptu_rules(rule_set_id, zone_name);

-- ============================================================================
-- 7. MEASUREMENTS (ensure exists)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.measurements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL,
    measurement_type VARCHAR(30) NOT NULL,
    geometry        geometry(Geometry, 4326),
    value           DOUBLE PRECISION NOT NULL,
    unit            VARCHAR(20) DEFAULT 'm',
    label           VARCHAR(200),
    notes           TEXT,
    measured_by     VARCHAR(320),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 8. NOTIFICATIONS (ensure exists)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    title           VARCHAR(300) NOT NULL,
    message         TEXT,
    type            VARCHAR(50) DEFAULT 'info',
    category        VARCHAR(50),
    link            VARCHAR(500),
    is_read         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_notifications_user_id ON public.notifications(user_id);

-- ============================================================================
-- 9. REPORTS (ensure exists)
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES public.projects(id) ON DELETE SET NULL,
    title           VARCHAR(500) NOT NULL,
    report_type     VARCHAR(50) NOT NULL DEFAULT 'general',
    status          VARCHAR(30) NOT NULL DEFAULT 'pending',
    format          VARCHAR(20) DEFAULT 'pdf',
    parameters      JSONB DEFAULT '{}',
    file_path       VARCHAR(1000),
    file_size_bytes BIGINT,
    generated_by    VARCHAR(320),
    generated_at    TIMESTAMPTZ,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS public.report_sections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id       UUID NOT NULL REFERENCES public.reports(id) ON DELETE CASCADE,
    title           VARCHAR(300),
    content         TEXT,
    section_order   INTEGER DEFAULT 0,
    section_type    VARCHAR(50) DEFAULT 'text',
    data            JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- DONE
-- ============================================================================
SELECT 'Fiscal Virtual migration completed successfully' AS result;
