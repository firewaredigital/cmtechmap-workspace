"""CM TECHMAP — Project Model (public schema)"""

from sqlalchemy import Boolean, Float, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Project(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """
    Geospatial mapping project.
    A project groups flights, assets, and reports for a specific area.
    Lives in the PUBLIC schema (cross-tenant for now, tenant isolation via middleware).
    """
    __tablename__ = "projects"
    __table_args__ = (
        Index("ix_projects_status", "status"),
        Index("ix_projects_city", "city"),
        {"schema": "public"},
    )

    # ── Identification ────────────────────────────────────────────────────────
    code: Mapped[str] = mapped_column(
        String(20), unique=True, nullable=False, index=True,
        comment="Human-readable project code (e.g., PRJ-001)",
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # ── Status ────────────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pendente",
        comment="pendente | processando | concluido | erro",
    )

    # ── Location ──────────────────────────────────────────────────────────────
    city: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str | None] = mapped_column(String(2))

    # ── Metrics ───────────────────────────────────────────────────────────────
    area_sqm: Mapped[float | None] = mapped_column(
        Float, comment="Total mapped area in square meters",
    )
    flight_count: Mapped[int] = mapped_column(Integer, default=0)
    image_count: Mapped[int] = mapped_column(Integer, default=0)

    # ── Geospatial bounds (EPSG:4326) ─────────────────────────────────────────
    bbox_min_lon: Mapped[float | None] = mapped_column(Float)
    bbox_min_lat: Mapped[float | None] = mapped_column(Float)
    bbox_max_lon: Mapped[float | None] = mapped_column(Float)
    bbox_max_lat: Mapped[float | None] = mapped_column(Float)

    # ── Ownership ─────────────────────────────────────────────────────────────
    created_by: Mapped[str | None] = mapped_column(
        String(320), comment="Email of the user who created the project",
    )
    responsible: Mapped[str | None] = mapped_column(
        String(500), comment="Name of the responsible person",
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Relationships ─────────────────────────────────────────────────────────
    flights: Mapped[list["Flight"]] = relationship(  # noqa: F821
        "Flight", back_populates="project", cascade="all, delete-orphan",
        lazy="selectin",
    )
    reports: Mapped[list["Report"]] = relationship(  # noqa: F821
        "Report", back_populates="project", cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<Project {self.code} '{self.name}' [{self.status}]>"
