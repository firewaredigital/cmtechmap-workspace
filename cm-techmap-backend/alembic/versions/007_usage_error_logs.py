"""
Tabela COMPARTILHADA de erros de uso do sistema.

Por decisão de produto ela NÃO é multi-tenant: vive em public e recebe os
erros de absolutamente todos os usuários de todos os municípios — a página
de logs é exclusiva do administrador da plataforma (super_admin), invisível
para os técnicos das prefeituras.

`journey` guarda a trilha completa de uso que levou ao erro (navegações,
botões clicados, chamadas de API), gravada pelo frontend em buffer circular
e enviada junto com cada erro.

Revision ID: 007
Revises: 006
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.usage_error_logs (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            error_type    TEXT NOT NULL,
            message       TEXT NOT NULL,
            stack         TEXT,
            user_id       TEXT,
            user_name     TEXT,
            user_email    TEXT,
            tenant_slug   TEXT,
            page_url      TEXT,
            page_route    TEXT,
            user_agent    TEXT,
            app_version   TEXT,
            http          JSONB,
            journey       JSONB NOT NULL DEFAULT '[]'::jsonb,
            extra         JSONB
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_error_logs_occurred "
        "ON public.usage_error_logs (occurred_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_error_logs_email "
        "ON public.usage_error_logs (user_email)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_usage_error_logs_type "
        "ON public.usage_error_logs (error_type)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.usage_error_logs")
