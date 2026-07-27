"""CM TECHMAP — Tenant & User & Subscription Schemas"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TenantIPTUZoneSpec(BaseModel):
    """One fiscal zone captured by the onboarding wizard (seeds tenant iptu_rules)."""

    zone_name: str = Field(..., min_length=2, max_length=100)
    land_value_per_sqm_brl: float = Field(..., ge=0)
    built_value_per_sqm_brl: float = Field(..., ge=0)
    aliquot_pct: float = Field(..., ge=0, le=100)


class TenantIPTUSpec(BaseModel):
    """IPTU rule set captured by the onboarding wizard (seeds tenant iptu_rule_sets)."""

    municipality_code: str = Field(..., min_length=5, max_length=10)
    base_year: int = Field(..., ge=2000, le=2100)
    default_land_value_per_sqm: float = Field(50.0, ge=0)
    default_built_value_per_sqm: float = Field(800.0, ge=0)
    default_aliquot_pct: float = Field(1.0, ge=0, le=100)
    zones: list[TenantIPTUZoneSpec] = Field(default_factory=list, max_length=50)


class TenantInitialProjectSpec(BaseModel):
    """First project captured by the onboarding wizard (seeds tenant projects)."""

    name: str = Field(..., min_length=3, max_length=500)
    description: str | None = None


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=3)
    slug: str = Field(..., min_length=3, max_length=100, pattern=r"^[a-z0-9_]+$")
    cnpj: str | None = None
    city: str | None = None
    state: str | None = Field(None, max_length=2)
    contact_email: str | None = None
    # Onboarding extras — written into the NEWLY provisioned schema. The wizard
    # used to call /iptu/rules and /projects afterwards, but those endpoints are
    # scoped to the CALLER's tenant, so the data landed in the wrong schema.
    iptu: TenantIPTUSpec | None = None
    initial_project: TenantInitialProjectSpec | None = None


class TenantRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    city: str | None
    state: str | None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserRead(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    department: str | None
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserUpdate(BaseModel):
    name: str | None = None
    department: str | None = None
    position: str | None = None
    phone: str | None = None


class SubscriptionRead(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    plan: str
    status: str
    max_users: int
    max_storage_tb: float
    monthly_price_brl: float
    payment_status: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
