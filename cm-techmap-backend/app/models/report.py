"""CM TECHMAP — Report Model (public schema)"""

import uuid

from sqlalchemy import Float, ForeignKey, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Report(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Generated report (PDF or Excel) for a project.
    Reports are generated asynchronously via Celery and stored in MinIO.
    """
    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_project_id", "project_id"),
        Index("ix_reports_status", "status"),
        {"schema": "public"},
    )

    # ── Parent ────────────────────────────────────────────────────────────────
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Report metadata ───────────────────────────────────────────────────────
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    report_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="project_summary",
        comment=(
            "project_summary | comparison | flight_detail | custom | "
            "property_appraisal | project_consolidated | fiscal_revenue | technical_qa"
        ),
    )
    output_format: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pdf",
        comment="pdf | xlsx",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        comment="pending | generating | completed | failed",
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)

    # ── Storage ───────────────────────────────────────────────────────────────
    file_key: Mapped[str | None] = mapped_column(
        String(1000), comment="MinIO object key for the generated file",
    )
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)

    # ── Content configuration ─────────────────────────────────────────────────
    config: Mapped[dict | None] = mapped_column(
        JSONB, comment="Report generation parameters",
    )

    # ── Ownership ─────────────────────────────────────────────────────────────
    requested_by: Mapped[str | None] = mapped_column(
        String(320), comment="Email of the user who requested the report",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    project: Mapped["Project"] = relationship(  # noqa: F821
        "Project", back_populates="reports",
    )
    sections: Mapped[list["ReportSection"]] = relationship(
        "ReportSection", back_populates="report", cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Report '{self.title}' [{self.status}] ({self.output_format})>"


class ReportSection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Individual section within a report.
    Allows modular report composition with different content types.
    """
    __tablename__ = "report_sections"
    __table_args__ = (
        Index("ix_report_sections_report_id", "report_id"),
        {"schema": "public"},
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.reports.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Section content ───────────────────────────────────────────────────────
    section_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="cover | summary | flight_table | area_analysis | map_preview | "
                "comparison | measurements | custom",
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content: Mapped[dict | None] = mapped_column(
        JSONB, comment="Section data payload",
    )

    # ── Relationship ──────────────────────────────────────────────────────────
    report: Mapped["Report"] = relationship(
        "Report", back_populates="sections",
    )

    def __repr__(self) -> str:
        return f"<ReportSection '{self.title}' type={self.section_type}>"
