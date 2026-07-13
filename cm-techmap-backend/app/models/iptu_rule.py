"""
CM TECHMAP — IPTU Rule Models
Configurable per-municipality IPTU calculation rules.
Each municipality has a RuleSet containing zone-specific Rules
with rates, aliquots, and depreciation factors.
"""

import uuid

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class IPTURuleSet(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A collection of IPTU calculation rules for a specific municipality.
    Each municipality has one active rule set at a time.
    """
    __tablename__ = "iptu_rule_sets"
    __table_args__ = (
        Index("ix_iptu_rule_sets_municipality", "municipality_code", unique=True),
        {"schema": "public"},
    )

    # ── Municipality ──────────────────────────────────────────────────────────
    municipality_name: Mapped[str] = mapped_column(
        String(200), nullable=False,
        comment="Nome do município (e.g., 'Faina')",
    )
    municipality_code: Mapped[str] = mapped_column(
        String(10), unique=True, nullable=False,
        comment="Código IBGE do município (e.g., '5207600')",
    )
    state: Mapped[str] = mapped_column(
        String(2), nullable=False,
        comment="UF (e.g., 'GO')",
    )

    # ── Activation ────────────────────────────────────────────────────────────
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    base_year: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Reference year for the IPTU values (e.g., 2026)",
    )

    # ── Global Parameters ─────────────────────────────────────────────────────
    default_land_value_per_sqm: Mapped[float] = mapped_column(
        Float, default=50.0,
        comment="Default land value per m² if zone not matched (BRL)",
    )
    default_built_value_per_sqm: Mapped[float] = mapped_column(
        Float, default=800.0,
        comment="Default built area value per m² (BRL)",
    )
    default_aliquot_pct: Mapped[float] = mapped_column(
        Float, default=1.0,
        comment="Default IPTU aliquot percentage",
    )
    pool_surcharge_pct: Mapped[float] = mapped_column(
        Float, default=20.0,
        comment="Percentage surcharge for properties with pools",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    rules: Mapped[list["IPTURule"]] = relationship(
        "IPTURule", back_populates="rule_set",
        cascade="all, delete-orphan", lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<IPTURuleSet {self.municipality_name} ({self.state}) [{self.base_year}]>"


class IPTURule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    A zone-specific IPTU calculation rule within a municipality's rule set.
    Each zone (residencial, comercial, etc.) has its own rates and aliquots.
    """
    __tablename__ = "iptu_rules"
    __table_args__ = (
        Index("ix_iptu_rules_rule_set_zone", "rule_set_id", "zone_name", unique=True),
        {"schema": "public"},
    )

    # ── Foreign Key ───────────────────────────────────────────────────────────
    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public.iptu_rule_sets.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ── Zone Classification ───────────────────────────────────────────────────
    zone_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="residencial|comercial|industrial|mista|rural|central|periferica",
    )

    # ── Values per m² ─────────────────────────────────────────────────────────
    land_value_per_sqm_brl: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="Land value per m² for this zone (BRL)",
    )
    built_value_per_sqm_brl: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="Built area value per m² for this zone (BRL)",
    )

    # ── Tax Rate ──────────────────────────────────────────────────────────────
    aliquot_pct: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="IPTU aliquot as percentage (e.g., 1.0 = 1%)",
    )

    # ── Adjustments ───────────────────────────────────────────────────────────
    depreciation_rate_per_year: Mapped[float] = mapped_column(
        Float, default=0.01,
        comment="Annual depreciation rate (e.g., 0.01 = 1%/year)",
    )
    min_area_sqm: Mapped[float] = mapped_column(
        Float, default=0.0,
        comment="Minimum built area for IPTU to apply (exemption threshold)",
    )
    max_depreciation_pct: Mapped[float] = mapped_column(
        Float, default=50.0,
        comment="Maximum total depreciation percentage",
    )

    # ── Exemptions ────────────────────────────────────────────────────────────
    exemption_rules: Mapped[dict | None] = mapped_column(
        JSONB, default=dict,
        comment="Custom exemption rules (e.g., senior citizen, low income)",
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    rule_set: Mapped["IPTURuleSet"] = relationship(
        "IPTURuleSet", back_populates="rules",
    )

    def __repr__(self) -> str:
        return f"<IPTURule zone={self.zone_name} rate={self.aliquot_pct}%>"
