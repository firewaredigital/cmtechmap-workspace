"""
CM TECHMAP — Analysis Run Model
Tracks each execution of the IPTU Malha Fina analysis,
pool detection, or temporal comparison. Groups discrepancies
into a single audit-traceable run.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AnalysisRun(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A single execution of an urban analysis pipeline.
    Groups all resulting discrepancies for traceability.
    """
    __tablename__ = "analysis_runs"
    __table_args__ = (
        Index("ix_analysis_runs_project_id", "project_id"),
        Index("ix_analysis_runs_status", "status"),
        Index("ix_analysis_runs_run_type", "run_type"),
        {"schema": "public"},
    )

    # ── Foreign Key ───────────────────────────────────────────────────────────
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Run Metadata ──────────────────────────────────────────────────────────
    run_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="iptu_malha_fina|pool_detection|temporal_comparison|full_analysis",
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="running",
        comment="running|completed|failed|cancelled",
    )
    triggered_by: Mapped[str | None] = mapped_column(
        String(320), comment="User ID that triggered the analysis",
    )

    # ── Parameters ────────────────────────────────────────────────────────────
    parameters: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Input parameters: area_tolerance_pct, min_overlap_pct, etc.",
    )

    # ── Results ───────────────────────────────────────────────────────────────
    summary: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Result summary: totals, breakdowns, etc.",
    )
    total_discrepancies: Mapped[int] = mapped_column(Integer, default=0)
    estimated_total_gap_brl: Mapped[float] = mapped_column(Float, default=0.0)

    # ── Timing ────────────────────────────────────────────────────────────────
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)

    # ── Relationships ─────────────────────────────────────────────────────────
    project: Mapped["Project"] = relationship("Project", lazy="selectin")  # noqa: F821
    discrepancies: Mapped[list["Discrepancy"]] = relationship(  # noqa: F821
        "Discrepancy", back_populates="analysis_run", lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<AnalysisRun {self.run_type} [{self.status}] "
            f"discrepancies={self.total_discrepancies}>"
        )
