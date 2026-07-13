"""CM TECHMAP — Report configuration presets model."""

from sqlalchemy import Boolean, Float, Index, Integer, String, Text

from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ReportConfigPreset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Versioned municipal defaults for fiscal and QA report calibration."""

    __tablename__ = "report_config_presets"
    __table_args__ = (
        Index("ix_report_config_presets_municipality", "municipality_code", "version"),
        Index("ix_report_config_presets_active", "is_active"),
        {"schema": "public"},
    )

    municipality_code: Mapped[str] = mapped_column(String(10), nullable=False)
    municipality_name: Mapped[str] = mapped_column(String(120), nullable=False)

    iptu_rate_per_sqm: Mapped[float] = mapped_column(Float, nullable=False)
    assumed_irregular_share: Mapped[float] = mapped_column(Float, nullable=False, default=0.25)
    qa_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.80)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(320))
    updated_by: Mapped[str | None] = mapped_column(String(320))

    def __repr__(self) -> str:
        return (
            f"<ReportConfigPreset {self.municipality_code} v{self.version} "
            f"active={self.is_active}>"
        )
