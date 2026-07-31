"""Add height_m to public.ai_detections.

The canonical tenant DDL (tenant_lifecycle) has carried `height_m` since v6,
and the DSM pipeline now writes it when persisting building detections. The
public schema — where single-tenant installs and demo users without a
provisioned tenant schema actually operate — was created by 005 WITHOUT the
column, so every detection INSERT failed with UndefinedColumn and the fiscal
analysis saw nothing.

Idempotent: ADD COLUMN IF NOT EXISTS is safe on databases that already
converged.

Revision ID: 006
Revises: 005
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE public.ai_detections "
        "ADD COLUMN IF NOT EXISTS height_m DOUBLE PRECISION"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE public.ai_detections DROP COLUMN IF EXISTS height_m"
    )
