"""CM TECHMAP — Flight Asset Model (public schema)"""

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Index
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FlightAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A generated asset (orthomosaic, DSM, DTM, point cloud) from a processed flight.
    Each flight can produce multiple assets of different types.
    """
    __tablename__ = "flight_assets"
    __table_args__ = (
        Index("ix_flight_assets_flight_id", "flight_id"),
        Index("ix_flight_assets_asset_type", "asset_type"),
        {"schema": "public"},
    )

    # ── Parent ────────────────────────────────────────────────────────────────
    flight_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("public.flights.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Asset identification ──────────────────────────────────────────────────
    asset_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="orthomosaic | dsm | dtm | point_cloud | 3d_model | thumbnail | report",
    )

    # ── Storage ───────────────────────────────────────────────────────────────
    file_key: Mapped[str] = mapped_column(
        String(1000), nullable=False,
        comment="MinIO object key (bucket/path/filename)",
    )
    bucket_name: Mapped[str | None] = mapped_column(String(255))
    file_size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(100))
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))

    # ── Quality metrics ───────────────────────────────────────────────────────
    resolution_cm: Mapped[float | None] = mapped_column(
        Float, comment="Spatial resolution in cm/pixel",
    )
    cog_validated: Mapped[bool] = mapped_column(
        Boolean, default=False,
        comment="Whether the file is a valid Cloud-Optimized GeoTIFF",
    )

    # ── Geospatial metadata ───────────────────────────────────────────────────
    crs_epsg: Mapped[int | None] = mapped_column(
        Integer, comment="Coordinate Reference System EPSG code",
    )
    bbox_min_lon: Mapped[float | None] = mapped_column(Float)
    bbox_min_lat: Mapped[float | None] = mapped_column(Float)
    bbox_max_lon: Mapped[float | None] = mapped_column(Float)
    bbox_max_lat: Mapped[float | None] = mapped_column(Float)

    # ── Extended metadata ─────────────────────────────────────────────────────
    metadata_json: Mapped[dict | None] = mapped_column(
        JSONB, comment="Full geospatial metadata (bands, stats, etc.)",
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    flight: Mapped["Flight"] = relationship(  # noqa: F821
        "Flight", back_populates="assets",
    )

    def __repr__(self) -> str:
        return f"<FlightAsset {self.asset_type} flight={self.flight_id}>"
