"""Add notifications table to public schema

Revision ID: 003
Revises: 002
Create Date: 2026-05-27

Adds:
- notifications table for in-app notification system
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Notifications ─────────────────────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.String(320), nullable=False, comment="Keycloak sub of the target user"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("type", sa.String(20), nullable=False, server_default="info",
                  comment="info, success, warning, error"),
        sa.Column("category", sa.String(50), nullable=False, server_default="system",
                  comment="processing, report, subscription, system, admin, alert"),
        sa.Column("link", sa.String(500), comment="Frontend route to navigate to"),
        sa.Column("entity_type", sa.String(100)),
        sa.Column("entity_id", sa.String(255)),
        sa.Column("is_read", sa.Boolean(), server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"], schema="public")
    op.create_index("ix_notifications_user_unread", "notifications", ["user_id", "is_read"], schema="public")
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"], schema="public")
    op.create_index("ix_notifications_category", "notifications", ["category"], schema="public")


def downgrade() -> None:
    op.drop_table("notifications", schema="public")
