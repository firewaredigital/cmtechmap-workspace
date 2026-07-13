"""CM TECHMAP — Flight Model (public schema)"""

import uuid
from datetime import date

from sqlalchemy import Boolean, Date, Float, ForeignKey, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Flight(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A single drone flight within a project.
    Contains metadata about the flight and references to its processed assets.
    """
    __tablename__ = "flights"
    __table_args__ = (
        Index("ix_flights_project_id", "project_id"),
        Index("ix_flights_status", "status"),
        {"schema": "public"},
    )

    # ── Parent ────────────────────────────────────────────────────────────────
    project_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # ── Flight metadata ───────────────────────────────────────────────────────
    flight_date: Mapped[date] = mapped_column(Date, nullable=False)
    altitude_m: Mapped[float | None] = mapped_column(
        Float, comment="Flight altitude in meters AGL",
    )
    overlap_pct: Mapped[float | None] = mapped_column(
        Float, comment="Image overlap percentage (30-95%)",
    )
    sidelap_pct: Mapped[float | None] = mapped_column(
        Float, comment="Image sidelap percentage",
    )
    images_count: Mapped[int] = mapped_column(Integer, default=0)
    camera_model: Mapped[str | None] = mapped_column(String(200))
    sensor_width_mm: Mapped[float | None] = mapped_column(Float)
    focal_length_mm: Mapped[float | None] = mapped_column(Float)

    # ── Computed metrics ──────────────────────────────────────────────────────
    gsd_cm: Mapped[float | None] = mapped_column(
        Float, comment="Ground Sample Distance in cm/pixel",
    )
    area_coverage_sqm: Mapped[float | None] = mapped_column(
        Float, comment="Approximate area covered in square meters",
    )

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        comment="pending | uploaded | processing | completed | failed",
    )
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    project: Mapped["Project"] = relationship(  # noqa: F821
        "Project", back_populates="flights",
    )
    assets: Mapped[list["FlightAsset"]] = relationship(  # noqa: F821
        "FlightAsset", back_populates="flight", cascade="all, delete-orphan",
        lazy="selectin",
    )
    processing_jobs: Mapped[list["ProcessingJob"]] = relationship(  # noqa: F821
        "ProcessingJob", back_populates="flight", cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Flight {self.id} date={self.flight_date} [{self.status}]>"
