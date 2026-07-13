"""Initial schema — all tables

Revision ID: 001
Revises:
Create Date: 2026-05-22

Creates the complete CM TECHMAP database schema:
- tenants (multi-tenant registry)
- subscriptions (billing/plans)
- projects (mapping projects)
- flights (drone flights)
- processing_jobs (pipeline tracking)
- flight_assets (generated outputs)
- reports (PDF/Excel reports)
- report_sections (modular report composition)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Tenants ───────────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("slug", sa.String(100), unique=True, nullable=False),
        sa.Column("cnpj", sa.String(18)),
        sa.Column("city", sa.String(255)),
        sa.Column("state", sa.String(2)),
        sa.Column("contact_email", sa.String(320)),
        sa.Column("contact_phone", sa.String(50)),
        sa.Column("logo_url", sa.Text()),
        sa.Column("config", JSONB, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], schema="public")

    # ── Subscriptions ─────────────────────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("plan", sa.String(50), nullable=False, server_default="starter"),
        sa.Column("status", sa.String(50), nullable=False, server_default="active"),
        sa.Column("max_users", sa.Integer(), server_default="5"),
        sa.Column("max_storage_tb", sa.Float(), server_default="1.0"),
        sa.Column("max_projects", sa.Integer(), server_default="10"),
        sa.Column("monthly_price_brl", sa.Float(), server_default="0.0"),
        sa.Column("payment_status", sa.String(50), server_default="em_dia"),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"], schema="public")

    # ── Projects ──────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.String(20), unique=True, nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(50), nullable=False, server_default="pendente"),
        sa.Column("city", sa.String(255)),
        sa.Column("state", sa.String(2)),
        sa.Column("area_sqm", sa.Float()),
        sa.Column("flight_count", sa.Integer(), server_default="0"),
        sa.Column("image_count", sa.Integer(), server_default="0"),
        sa.Column("bbox_min_lon", sa.Float()),
        sa.Column("bbox_min_lat", sa.Float()),
        sa.Column("bbox_max_lon", sa.Float()),
        sa.Column("bbox_max_lat", sa.Float()),
        sa.Column("created_by", sa.String(320)),
        sa.Column("responsible", sa.String(500)),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_projects_code", "projects", ["code"], schema="public")
    op.create_index("ix_projects_status", "projects", ["status"], schema="public")
    op.create_index("ix_projects_city", "projects", ["city"], schema="public")

    # ── Flights ───────────────────────────────────────────────────────────────
    op.create_table(
        "flights",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("public.projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("flight_date", sa.Date(), nullable=False),
        sa.Column("altitude_m", sa.Float()),
        sa.Column("overlap_pct", sa.Float()),
        sa.Column("sidelap_pct", sa.Float()),
        sa.Column("images_count", sa.Integer(), server_default="0"),
        sa.Column("camera_model", sa.String(200)),
        sa.Column("sensor_width_mm", sa.Float()),
        sa.Column("focal_length_mm", sa.Float()),
        sa.Column("gsd_cm", sa.Float()),
        sa.Column("area_coverage_sqm", sa.Float()),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text()),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_flights_project_id", "flights", ["project_id"], schema="public")
    op.create_index("ix_flights_status", "flights", ["status"], schema="public")

    # ── Processing Jobs ───────────────────────────────────────────────────────
    op.create_table(
        "processing_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("flight_id", UUID(as_uuid=True), sa.ForeignKey("public.flights.id", ondelete="CASCADE"), nullable=False),
        sa.Column("celery_task_id", sa.String(255), unique=True),
        sa.Column("stage", sa.String(50), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), server_default="0"),
        sa.Column("status_message", sa.Text()),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("error_message", sa.Text()),
        sa.Column("odm_task_uuid", sa.String(255)),
        sa.Column("odm_options", JSONB),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("processing_time_sec", sa.Float()),
        sa.Column("result_metadata", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_processing_jobs_flight_id", "processing_jobs", ["flight_id"], schema="public")
    op.create_index("ix_processing_jobs_status", "processing_jobs", ["status"], schema="public")
    op.create_index("ix_processing_jobs_celery_task_id", "processing_jobs", ["celery_task_id"], schema="public")

    # ── Flight Assets ─────────────────────────────────────────────────────────
    op.create_table(
        "flight_assets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("flight_id", UUID(as_uuid=True), sa.ForeignKey("public.flights.id", ondelete="CASCADE"), nullable=False),
        sa.Column("asset_type", sa.String(50), nullable=False),
        sa.Column("file_key", sa.String(1000), nullable=False),
        sa.Column("bucket_name", sa.String(255)),
        sa.Column("file_size_bytes", sa.Integer()),
        sa.Column("content_type", sa.String(100)),
        sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("resolution_cm", sa.Float()),
        sa.Column("cog_validated", sa.Boolean(), server_default="false"),
        sa.Column("crs_epsg", sa.Integer()),
        sa.Column("bbox_min_lon", sa.Float()),
        sa.Column("bbox_min_lat", sa.Float()),
        sa.Column("bbox_max_lon", sa.Float()),
        sa.Column("bbox_max_lat", sa.Float()),
        sa.Column("metadata_json", JSONB),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_flight_assets_flight_id", "flight_assets", ["flight_id"], schema="public")
    op.create_index("ix_flight_assets_asset_type", "flight_assets", ["asset_type"], schema="public")

    # ── Reports ───────────────────────────────────────────────────────────────
    op.create_table(
        "reports",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("public.projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("report_type", sa.String(50), nullable=False, server_default="project_summary"),
        sa.Column("output_format", sa.String(10), nullable=False, server_default="pdf"),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column("celery_task_id", sa.String(255)),
        sa.Column("error_message", sa.Text()),
        sa.Column("file_key", sa.String(1000)),
        sa.Column("file_size_bytes", sa.Integer()),
        sa.Column("config", JSONB),
        sa.Column("requested_by", sa.String(320)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_reports_project_id", "reports", ["project_id"], schema="public")
    op.create_index("ix_reports_status", "reports", ["status"], schema="public")

    # ── Report Sections ───────────────────────────────────────────────────────
    op.create_table(
        "report_sections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("report_id", UUID(as_uuid=True), sa.ForeignKey("public.reports.id", ondelete="CASCADE"), nullable=False),
        sa.Column("section_type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema="public",
    )
    op.create_index("ix_report_sections_report_id", "report_sections", ["report_id"], schema="public")

    # ── Updated_at trigger function ───────────────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION public.trigger_set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # Apply trigger to all tables with updated_at
    for table in ["tenants", "subscriptions", "projects", "flights",
                   "processing_jobs", "flight_assets", "reports", "report_sections"]:
        op.execute(f"""
            CREATE TRIGGER set_updated_at
            BEFORE UPDATE ON public.{table}
            FOR EACH ROW
            EXECUTE FUNCTION public.trigger_set_updated_at();
        """)


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("report_sections", schema="public")
    op.drop_table("reports", schema="public")
    op.drop_table("flight_assets", schema="public")
    op.drop_table("processing_jobs", schema="public")
    op.drop_table("flights", schema="public")
    op.drop_table("projects", schema="public")
    op.drop_table("subscriptions", schema="public")
    op.drop_table("tenants", schema="public")

    op.execute("DROP FUNCTION IF EXISTS public.trigger_set_updated_at() CASCADE;")
