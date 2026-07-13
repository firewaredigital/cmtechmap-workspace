"""
CM TECHMAP — Discrepancy Model
Represents a tax discrepancy found by the IPTU Malha Fina analysis.
Each discrepancy links a cadastral parcel to an AI detection and tracks
the municipal employee's decision (approve/reject/schedule inspection).
"""

import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DiscrepancyStatus:
    """Valid status values for a discrepancy."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INSPECTION_SCHEDULED = "inspection_scheduled"
    INSPECTED = "inspected"
    ARCHIVED = "archived"


class DiscrepancyType:
    """Types of IPTU discrepancies."""
    UNREGISTERED = "unregistered"
    AREA_UNDER_DECLARED = "area_under_declared"
    AREA_OVER_DECLARED = "area_over_declared"
    DEMOLISHED = "demolished"
    HEIGHT_MISMATCH = "height_mismatch"
    POOL_DETECTED = "pool_detected"


class Discrepancy(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A tax discrepancy found by cross-referencing AI detections with
    the cadastral registry. This is the core entity of the "Fiscal Virtual"
    workflow — municipal employees review and decide on each discrepancy.
    """
    __tablename__ = "discrepancies"
    __table_args__ = (
        Index("ix_discrepancies_status", "status"),
        Index("ix_discrepancies_type", "discrepancy_type"),
        Index("ix_discrepancies_severity", "severity"),
        Index("ix_discrepancies_project_id", "project_id"),
        Index("ix_discrepancies_analysis_run_id", "analysis_run_id"),
        Index("ix_discrepancies_polygon", "polygon", postgresql_using="gist"),
        {"schema": "public"},
    )

    # ── Foreign Keys ──────────────────────────────────────────────────────────
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    parcel_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.parcels.id", ondelete="SET NULL"),
        nullable=True,
    )
    detection_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.ai_detections.id", ondelete="SET NULL"),
        nullable=True,
    )
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.analysis_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Discrepancy Classification ────────────────────────────────────────────
    discrepancy_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="unregistered|area_under_declared|area_over_declared|demolished|height_mismatch|pool_detected",
    )
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium",
        comment="low|medium|high|critical",
    )

    # ── Parcel Data (denormalized for fast queries) ───────────────────────────
    cadastral_code: Mapped[str | None] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(500))
    neighborhood: Mapped[str | None] = mapped_column(String(200))
    owner_name: Mapped[str | None] = mapped_column(String(500))

    # ── Area Comparison ───────────────────────────────────────────────────────
    registered_area_sqm: Mapped[float | None] = mapped_column(Float)
    detected_area_sqm: Mapped[float | None] = mapped_column(Float)
    difference_sqm: Mapped[float | None] = mapped_column(Float)
    difference_pct: Mapped[float | None] = mapped_column(Float)
    overlap_pct: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)

    # ── Height Comparison ─────────────────────────────────────────────────────
    detected_height_m: Mapped[float | None] = mapped_column(Float)
    registered_floors: Mapped[int | None] = mapped_column(Integer)

    # ── IPTU Calculation ──────────────────────────────────────────────────────
    iptu_current_brl: Mapped[float | None] = mapped_column(
        Float, comment="Current annual IPTU value (BRL)",
    )
    iptu_proposed_brl: Mapped[float | None] = mapped_column(
        Float, comment="Proposed new IPTU value after correction (BRL)",
    )
    estimated_iptu_gap_brl: Mapped[float] = mapped_column(
        Float, default=0.0,
        comment="Estimated annual revenue gap (BRL)",
    )
    calculation_details: Mapped[dict | None] = mapped_column(
        JSONB, comment="Breakdown of IPTU calculation",
    )

    # ── Decision / Workflow ───────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=DiscrepancyStatus.PENDING,
        comment="pending|approved|rejected|inspection_scheduled|inspected|archived",
    )
    reviewed_by: Mapped[str | None] = mapped_column(
        String(320), comment="User ID who reviewed",
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(
        String(200), comment="shadow|ai_error|already_regularized|other",
    )

    # ── Inspection ────────────────────────────────────────────────────────────
    inspection_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspector_name: Mapped[str | None] = mapped_column(String(300))
    inspector_report: Mapped[str | None] = mapped_column(Text)
    inspection_result: Mapped[str | None] = mapped_column(
        String(50), comment="confirmed|not_confirmed|partial",
    )

    # ── Geometry ──────────────────────────────────────────────────────────────
    polygon: Mapped[object | None] = mapped_column(
        Geometry("POLYGON", srid=4326), nullable=True,
        comment="Discrepancy area polygon (the difference geometry)",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    project: Mapped["Project"] = relationship(  # noqa: F821
        "Project", lazy="selectin",
    )
    parcel: Mapped["Parcel | None"] = relationship(  # noqa: F821
        "Parcel", back_populates="discrepancies", lazy="selectin",
    )
    detection: Mapped["AIDetection | None"] = relationship(  # noqa: F821
        "AIDetection", back_populates="discrepancies", lazy="selectin",
    )
    analysis_run: Mapped["AnalysisRun | None"] = relationship(  # noqa: F821
        "AnalysisRun", back_populates="discrepancies", lazy="selectin",
    )

    def __repr__(self) -> str:
        return (
            f"<Discrepancy {self.discrepancy_type} [{self.status}] "
            f"gap=R${self.estimated_iptu_gap_brl:.2f}>"
        )
