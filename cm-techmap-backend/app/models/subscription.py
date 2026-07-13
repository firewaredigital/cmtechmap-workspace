"""CM TECHMAP — Subscription Model (public schema)"""

from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Subscription(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Subscription/billing records. Lives in the PUBLIC schema."""
    __tablename__ = "subscriptions"
    __table_args__ = {"schema": "public"}

    tenant_id: Mapped[str] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="starter")
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    max_users: Mapped[int] = mapped_column(Integer, default=5)
    max_storage_tb: Mapped[float] = mapped_column(Float, default=1.0)
    max_projects: Mapped[int] = mapped_column(Integer, default=10)
    monthly_price_brl: Mapped[float] = mapped_column(Float, default=0.0)
    payment_status: Mapped[str] = mapped_column(String(50), default="em_dia")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
