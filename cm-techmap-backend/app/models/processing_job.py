"""CM TECHMAP — Processing Job Model (public schema)"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProcessingJob(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Tracks a photogrammetry processing job through the 7-stage pipeline.
    Each flight can have multiple processing jobs (re-processing).
    """
    __tablename__ = "processing_jobs"
    __table_args__ = (
        Index("ix_processing_jobs_flight_id", "flight_id"),
        Index("ix_processing_jobs_status", "status"),
        Index("ix_processing_jobs_celery_task_id", "celery_task_id"),
        {"schema": "public"},
    )

    # ── Parent ────────────────────────────────────────────────────────────────
    flight_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.flights.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Celery Integration ────────────────────────────────────────────────────
    celery_task_id: Mapped[str | None] = mapped_column(
        String(255), unique=True,
        comment="Celery task ID for tracking",
    )

    # ── Pipeline Stage ────────────────────────────────────────────────────────
    stage: Mapped[str] = mapped_column(
        String(50), nullable=False, default="queued",
        comment="queued | validation | preparation | odm_submission | "
                "odm_processing | odm_download | post_processing | publication | "
                "completed | failed | canceled",
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, comment="0-100")
    status_message: Mapped[str | None] = mapped_column(Text)

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending",
        comment="pending | running | completed | failed | canceled",
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    # ── NodeODM ───────────────────────────────────────────────────────────────
    odm_task_uuid: Mapped[str | None] = mapped_column(
        String(255), comment="NodeODM task UUID",
    )
    odm_options: Mapped[dict | None] = mapped_column(
        JSONB, comment="ODM processing options used",
    )

    # ── Timing ────────────────────────────────────────────────────────────────
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_time_sec: Mapped[float | None] = mapped_column(
        Float, comment="Total processing time in seconds",
    )

    # ── Results ───────────────────────────────────────────────────────────────
    result_metadata: Mapped[dict | None] = mapped_column(
        JSONB, comment="Processing result metadata (bounds, resolution, etc.)",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    flight: Mapped["Flight"] = relationship(  # noqa: F821
        "Flight", back_populates="processing_jobs",
    )

    def __repr__(self) -> str:
        return f"<ProcessingJob {self.id} stage={self.stage} [{self.status}]>"
