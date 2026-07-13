"""
CM TECHMAP — AI Detection Model
Stores polygons detected by AI models (buildings, pools, vegetation, etc.)
linked to flight assets (orthomosaics, DSMs).
"""

from geoalchemy2 import Geometry
from sqlalchemy import Float, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

import uuid


class AIDetection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    An AI-detected feature from drone imagery.
    Detection classes: building, pool, vegetation, road, water, shadow.
    """
    __tablename__ = "ai_detections"
    __table_args__ = (
        Index("ix_ai_detections_flight_asset_id", "flight_asset_id"),
        Index("ix_ai_detections_class", "detection_class"),
        Index("ix_ai_detections_polygon", "polygon", postgresql_using="gist"),
        Index("ix_ai_detections_confidence", "confidence"),
        {"schema": "public"},
    )

    # ── Foreign Key ───────────────────────────────────────────────────────────
    flight_asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False,
        comment="FK to flight_assets.id (the source orthomosaic/DSM)",
    )

    # ── Detection Data ────────────────────────────────────────────────────────
    detection_class: Mapped[str] = mapped_column(
        String(50), nullable=False, default="building",
        comment="building | pool | vegetation | road | water | shadow",
    )
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0,
        comment="Model confidence score 0.0-1.0",
    )
    area_sqm: Mapped[float | None] = mapped_column(
        Float, comment="Detected footprint area in m²",
    )
    perimeter_m: Mapped[float | None] = mapped_column(
        Float, comment="Detected footprint perimeter in meters",
    )

    # ── Geometry (PostGIS) ────────────────────────────────────────────────────
    polygon: Mapped[object] = mapped_column(
        Geometry("POLYGON", srid=4326), nullable=True,
        comment="Detected feature polygon in WGS84",
    )

    # ── Extended Properties ───────────────────────────────────────────────────
    properties: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Extra metadata: height_m, floors, material, pool_type, etc.",
    )
    model_version: Mapped[str | None] = mapped_column(
        String(100), comment="AI model version that produced this detection",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    discrepancies: Mapped[list["Discrepancy"]] = relationship(  # noqa: F821
        "Discrepancy", back_populates="detection", lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<AIDetection {self.detection_class} conf={self.confidence:.2f} area={self.area_sqm}m²>"
