"""
CM TECHMAP — Parcel Model (Cadastral Lot)
Represents an official cadastral parcel from the municipal registry.
Used for IPTU Malha Fina cross-referencing against AI detections.
"""

from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Float, Index, Integer, String, Text, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Parcel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A cadastral parcel (lote) from the municipal property registry.
    Each parcel has a unique cadastral_code (inscrição imobiliária),
    a polygon geometry, and registered area/built-area data.
    """
    __tablename__ = "parcels"
    __table_args__ = (
        Index("ix_parcels_cadastral_code", "cadastral_code", unique=True),
        Index("ix_parcels_neighborhood", "neighborhood"),
        Index("ix_parcels_land_use", "land_use"),
        Index("ix_parcels_iptu_zone", "iptu_zone"),
        Index("ix_parcels_polygon", "polygon", postgresql_using="gist"),
        {"schema": "public"},
    )

    # ── Identification ────────────────────────────────────────────────────────
    cadastral_code: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False,
        comment="Inscrição imobiliária / código cadastral único",
    )
    address: Mapped[str | None] = mapped_column(String(500))
    neighborhood: Mapped[str | None] = mapped_column(String(200))
    owner_name: Mapped[str | None] = mapped_column(String(500))
    owner_cpf_cnpj: Mapped[str | None] = mapped_column(
        String(20), comment="CPF or CNPJ of the property owner",
    )

    # ── Areas ─────────────────────────────────────────────────────────────────
    registered_area_sqm: Mapped[float | None] = mapped_column(
        Float, comment="Total lot area in m² (área do terreno)",
    )
    registered_built_area_sqm: Mapped[float | None] = mapped_column(
        Float, comment="Registered built area in m² (área construída)",
    )

    # ── Classification ────────────────────────────────────────────────────────
    land_use: Mapped[str | None] = mapped_column(
        String(50), comment="residencial | comercial | industrial | mista | rural",
    )
    iptu_zone: Mapped[str | None] = mapped_column(
        String(50), comment="Zona fiscal para cálculo IPTU",
    )
    iptu_value_current_brl: Mapped[float | None] = mapped_column(
        Float, comment="Current annual IPTU value in BRL",
    )

    # ── Geometry (PostGIS) ────────────────────────────────────────────────────
    polygon: Mapped[object] = mapped_column(
        Geometry("POLYGON", srid=4326), nullable=True,
        comment="Parcel boundary polygon in WGS84",
    )

    # ── Metadata ──────────────────────────────────────────────────────────────
    properties: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Additional properties (registered_floors, year_built, etc.)",
    )
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    discrepancies: Mapped[list["Discrepancy"]] = relationship(  # noqa: F821
        "Discrepancy", back_populates="parcel", lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Parcel {self.cadastral_code} [{self.land_use}]>"
