"""Add activity_logs and uploads tables to public schema

Revision ID: 002
Revises: 001
Create Date: 2026-05-27

Adds:
- uploads table (tracks file uploads before processing)
- activity_logs table (audit trail for all user/system actions)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Uploads ───────────────────────────────────────────────────────────────
    op.create_table(
        "uploads",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("public.projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("flight_id", UUID(as_uuid=True), sa.ForeignKey("public.flights.id", ondelete="SET NULL")),
        sa.Column("filename", sa.String(1024), nullable=False),
        sa.Column("original_filename", sa.String(1024), nullable=False),
        sa.Column("file_key", sa.String(2048), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("content_type", sa.String(255)),
        sa.Column("chunk_count", sa.Integer(), server_default="0"),
        sa.Column("chunks_received", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(50), server_default="uploading"),
        sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("processing_job_id", sa.String(255)),
        sa.Column("uploaded_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        schema="public",
    )
    op.create_index("ix_uploads_project_id", "uploads", ["project_id"], schema="public")
    op.create_index("ix_uploads_status", "uploads", ["status"], schema="public")

    # ── Activity Logs (Audit Trail) ───────────────────────────────────────────
    op.create_table(
        "activity_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.String(320), comment="Keycloak sub or email"),
        sa.Column("action", sa.String(100), nullable=False, comment="e.g. auth.login, project.create"),
        sa.Column("entity_type", sa.String(100), comment="e.g. project, flight, user"),
        sa.Column("entity_id", sa.String(255), comment="UUID of the affected entity"),
        sa.Column("details", JSONB, server_default="{}"),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("user_agent", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_activity_logs_user_id", "activity_logs", ["user_id"], schema="public")
    op.create_index("ix_activity_logs_action", "activity_logs", ["action"], schema="public")
    op.create_index("ix_activity_logs_created_at", "activity_logs", ["created_at"], schema="public")
    op.create_index("ix_activity_logs_entity", "activity_logs", ["entity_type", "entity_id"], schema="public")


def downgrade() -> None:
    op.drop_table("activity_logs", schema="public")
    op.drop_table("uploads", schema="public")
