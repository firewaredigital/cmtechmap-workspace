"""CM TECHMAP — Tenant & User & Subscription Schemas"""

import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=3)
    slug: str = Field(..., min_length=3, max_length=100, pattern=r"^[a-z0-9_]+$")
    cnpj: str | None = None
    city: str | None = None
    state: str | None = Field(None, max_length=2)
    contact_email: str | None = None


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
