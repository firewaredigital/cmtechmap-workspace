"""
Tipologia de telhado, material e ÁREA REAL por detecção.

A projeção vertical do ortomosaico subestima a superfície de um telhado
inclinado; area_real_sqm guarda a metragem verdadeira (integral do fator
de esticamento). shadow_rejected marca o que foi descartado por ser sombra.

Revision ID: 011
Revises: 010
"""

from collections.abc import Sequence

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLS = [
    ("roof_type", "TEXT"),
    ("roof_design", "TEXT"),
    ("roof_waters", "INTEGER"),
    ("roof_type_confidence", "DOUBLE PRECISION"),
    ("roof_material", "TEXT"),
    ("roof_material_confidence", "DOUBLE PRECISION"),
    ("roof_slope_pct", "DOUBLE PRECISION"),
    ("roof_slope_deg", "DOUBLE PRECISION"),
    ("area_projected_sqm", "DOUBLE PRECISION"),
    ("area_real_sqm", "DOUBLE PRECISION"),
    ("area_gain_pct", "DOUBLE PRECISION"),
    ("luminance", "DOUBLE PRECISION"),
    ("shadow_rejected", "BOOLEAN DEFAULT FALSE"),
    ("roof_analysis", "JSONB"),
]


def upgrade() -> None:
    for name, typ in COLS:
        op.execute(f"ALTER TABLE public.ai_detections ADD COLUMN IF NOT EXISTS {name} {typ}")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_detections_roof_type "
        "ON public.ai_detections (roof_type)"
    )


def downgrade() -> None:
    for name, _ in COLS:
        op.execute(f"ALTER TABLE public.ai_detections DROP COLUMN IF EXISTS {name}")
