"""
Unanimidade medida por detecção + certificados de autoauditoria.

- consensus_votes: voto de cada juiz independente (rede@3 thresholds,
  altura 3D, planaridade) — a unanimidade vira um FATO consultável.
- is_unanimous: todos os juízes concordaram.
- audit_certificates: cada execução da autoprova recomputa medições por um
  caminho de código INDEPENDENTE e registra se bateram bit a bit — o
  sistema carrega a própria prova de idoneidade.

Revision ID: 010
Revises: 009
"""

from collections.abc import Sequence

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.ai_detections "
        "ADD COLUMN IF NOT EXISTS consensus_votes JSONB"
    )
    op.execute(
        "ALTER TABLE public.ai_detections "
        "ADD COLUMN IF NOT EXISTS is_unanimous BOOLEAN"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.audit_certificates (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            flight_id     UUID NOT NULL,
            run_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            checks_total  INTEGER NOT NULL,
            checks_passed INTEGER NOT NULL,
            passed        BOOLEAN NOT NULL,
            details       JSONB NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_certificates_flight "
        "ON public.audit_certificates (flight_id, run_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.audit_certificates")
    op.execute("ALTER TABLE public.ai_detections DROP COLUMN IF EXISTS consensus_votes")
    op.execute("ALTER TABLE public.ai_detections DROP COLUMN IF EXISTS is_unanimous")
