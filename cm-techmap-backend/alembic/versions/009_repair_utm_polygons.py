"""
Reparo de polígonos gravados em CRS projetado dentro da coluna 4326.

O extrator antigo escrevia coordenadas no CRS do raster (UTM em voos
novos) sem reprojetar — as marcações caíam fora do mundo e nunca
apareciam no mapa. O extrator foi corrigido; esta migração conserta os
dados históricos de QUALQUER instalação, usando o crs_epsg registrado no
próprio asset do voo. Idempotente: só toca linhas fora da faixa lon/lat.

Revision ID: 009
Revises: 008
"""

from collections.abc import Sequence

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.ai_detections d
        SET polygon = ST_Transform(
                ST_SetSRID(d.polygon, COALESCE(fa.crs_epsg, 4326)), 4326)
        FROM public.flight_assets fa
        WHERE d.flight_asset_id = fa.id
          AND d.polygon IS NOT NULL
          AND COALESCE(fa.crs_epsg, 4326) <> 4326
          AND abs(ST_X(ST_Centroid(d.polygon))) > 180
        """
    )


def downgrade() -> None:
    pass  # reparo de dados — não há retorno ao estado corrompido
