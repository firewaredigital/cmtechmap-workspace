"""Add report config presets table

Revision ID: 004
Revises: 003
Create Date: 2026-07-13

Adds:
- report_config_presets table with versioned municipal fiscal/QA defaults
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent on purpose: several databases in the field already have this
    # table from migrations/003_report_config_presets.sql (raw-SQL path), with
    # alembic_version still at 003.
    op.execute("""
        CREATE TABLE IF NOT EXISTS public.report_config_presets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            municipality_code VARCHAR(10) NOT NULL,
            municipality_name VARCHAR(120) NOT NULL,
            iptu_rate_per_sqm FLOAT NOT NULL,
            assumed_irregular_share FLOAT NOT NULL DEFAULT 0.25,
            qa_threshold FLOAT NOT NULL DEFAULT 0.80,
            version INTEGER NOT NULL DEFAULT 1,
            is_active BOOLEAN NOT NULL DEFAULT true,
            notes TEXT,
            created_by VARCHAR(320),
            updated_by VARCHAR(320),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_report_config_presets_municipality "
        "ON public.report_config_presets(municipality_code, version)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_report_config_presets_active "
        "ON public.report_config_presets(is_active)"
    )

    op.execute(
        sa.text(
            """
            INSERT INTO public.report_config_presets (
                municipality_code, municipality_name,
                iptu_rate_per_sqm, assumed_irregular_share, qa_threshold,
                version, is_active, notes, created_by, updated_by
            )
            SELECT v.code, v.name, v.rate, v.share, v.qa, 1, true, v.notes, 'system', 'system'
            FROM (VALUES
                ('5208707', 'Goiania', 12.0, 0.25, 0.80,
                 'Preset inicial para validacao com equipe fiscal municipal.'),
                ('3550308', 'Sao Paulo', 19.5, 0.21, 0.84,
                 'Zona densa urbana: calibracao conservadora para arrecadacao.'),
                ('3304557', 'Rio de Janeiro', 17.2, 0.24, 0.82,
                 'Uso misto: pondera ocupacoes formais e expansoes nao registradas.')
            ) AS v(code, name, rate, share, qa, notes)
            WHERE NOT EXISTS (
                SELECT 1 FROM public.report_config_presets p
                WHERE p.municipality_code = v.code
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_report_config_presets_active", table_name="report_config_presets", schema="public")
    op.drop_index("ix_report_config_presets_municipality", table_name="report_config_presets", schema="public")
    op.drop_table("report_config_presets", schema="public")
