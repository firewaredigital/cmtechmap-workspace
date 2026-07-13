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
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_config_presets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("municipality_code", sa.String(length=10), nullable=False),
        sa.Column("municipality_name", sa.String(length=120), nullable=False),
        sa.Column("iptu_rate_per_sqm", sa.Float(), nullable=False),
        sa.Column("assumed_irregular_share", sa.Float(), nullable=False, server_default="0.25"),
        sa.Column("qa_threshold", sa.Float(), nullable=False, server_default="0.80"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=320), nullable=True),
        sa.Column("updated_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )

    op.create_index(
        "ix_report_config_presets_municipality",
        "report_config_presets",
        ["municipality_code", "version"],
        schema="public",
    )
    op.create_index(
        "ix_report_config_presets_active",
        "report_config_presets",
        ["is_active"],
        schema="public",
    )

    op.execute(
        sa.text(
            """
            INSERT INTO public.report_config_presets (
                municipality_code, municipality_name,
                iptu_rate_per_sqm, assumed_irregular_share, qa_threshold,
                version, is_active, notes, created_by, updated_by
            ) VALUES
                ('5208707', 'Goiania', 12.0, 0.25, 0.80, 1, true,
                 'Preset inicial para validacao com equipe fiscal municipal.',
                 'system', 'system'),
                ('3550308', 'Sao Paulo', 19.5, 0.21, 0.84, 1, true,
                 'Zona densa urbana: calibracao conservadora para arrecadacao.',
                 'system', 'system'),
                ('3304557', 'Rio de Janeiro', 17.2, 0.24, 0.82, 1, true,
                 'Uso misto: pondera ocupacoes formais e expansoes nao registradas.',
                 'system', 'system')
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_report_config_presets_active", table_name="report_config_presets", schema="public")
    op.drop_index("ix_report_config_presets_municipality", table_name="report_config_presets", schema="public")
    op.drop_table("report_config_presets", schema="public")
