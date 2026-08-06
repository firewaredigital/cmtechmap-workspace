"""
Medições fotogramétricas + veredito de validação cruzada por detecção.

A rede neural (RGB) propõe; o par DSM/DTM (3D medido) confirma, enfraquece
ou contradiz — com incerteza declarada em cada número.

Revision ID: 008
Revises: 007
"""

from collections.abc import Sequence

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLS = [
    ("height_measured_m", "DOUBLE PRECISION"),
    ("height_std_m", "DOUBLE PRECISION"),
    ("volume_m3", "DOUBLE PRECISION"),
    ("area_uncertainty_sqm", "DOUBLE PRECISION"),
    ("planarity", "DOUBLE PRECISION"),
    ("evidence_score", "DOUBLE PRECISION"),
    ("validation_status", "TEXT"),
    ("validated_at", "TIMESTAMPTZ"),
]


def upgrade() -> None:
    for name, typ in COLS:
        op.execute(
            f"ALTER TABLE public.ai_detections ADD COLUMN IF NOT EXISTS {name} {typ}"
        )


def downgrade() -> None:
    for name, _ in COLS:
        op.execute(f"ALTER TABLE public.ai_detections DROP COLUMN IF EXISTS {name}")
