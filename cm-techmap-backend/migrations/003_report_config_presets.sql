-- ============================================================================
-- CM TECHMAP - Report Config Presets (runtime SQL migration)
-- Creates versioned municipal fiscal/QA defaults used by report generation.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.report_config_presets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    municipality_code VARCHAR(10) NOT NULL,
    municipality_name VARCHAR(120) NOT NULL,
    iptu_rate_per_sqm DOUBLE PRECISION NOT NULL,
    assumed_irregular_share DOUBLE PRECISION NOT NULL DEFAULT 0.25,
    qa_threshold DOUBLE PRECISION NOT NULL DEFAULT 0.80,
    version INTEGER NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT true,
    notes TEXT,
    created_by VARCHAR(320),
    updated_by VARCHAR(320),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_report_config_presets_municipality
    ON public.report_config_presets (municipality_code, version);
CREATE INDEX IF NOT EXISTS ix_report_config_presets_active
    ON public.report_config_presets (is_active);
CREATE UNIQUE INDEX IF NOT EXISTS ux_report_config_presets_code_version
    ON public.report_config_presets (municipality_code, version);

INSERT INTO public.report_config_presets (
    municipality_code,
    municipality_name,
    iptu_rate_per_sqm,
    assumed_irregular_share,
    qa_threshold,
    version,
    is_active,
    notes,
    created_by,
    updated_by
)
SELECT
    '5208707', 'Goiania', 12.0, 0.25, 0.80, 1, true,
    'Preset inicial para validacao com equipe fiscal municipal.',
    'system', 'system'
WHERE NOT EXISTS (
    SELECT 1 FROM public.report_config_presets
    WHERE municipality_code = '5208707' AND version = 1
);

INSERT INTO public.report_config_presets (
    municipality_code,
    municipality_name,
    iptu_rate_per_sqm,
    assumed_irregular_share,
    qa_threshold,
    version,
    is_active,
    notes,
    created_by,
    updated_by
)
SELECT
    '3550308', 'Sao Paulo', 19.5, 0.21, 0.84, 1, true,
    'Zona densa urbana: calibracao conservadora para arrecadacao.',
    'system', 'system'
WHERE NOT EXISTS (
    SELECT 1 FROM public.report_config_presets
    WHERE municipality_code = '3550308' AND version = 1
);

INSERT INTO public.report_config_presets (
    municipality_code,
    municipality_name,
    iptu_rate_per_sqm,
    assumed_irregular_share,
    qa_threshold,
    version,
    is_active,
    notes,
    created_by,
    updated_by
)
SELECT
    '3304557', 'Rio de Janeiro', 17.2, 0.24, 0.82, 1, true,
    'Uso misto: pondera ocupacoes formais e expansoes nao registradas.',
    'system', 'system'
WHERE NOT EXISTS (
    SELECT 1 FROM public.report_config_presets
    WHERE municipality_code = '3304557' AND version = 1
);

SELECT 'report_config_presets ready' AS result;
