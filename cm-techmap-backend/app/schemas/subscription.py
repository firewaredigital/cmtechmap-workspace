"""
CM TECHMAP — Subscription & Billing Schemas
Pydantic models for subscription CRUD, plan management, and usage tracking.
"""

import uuid
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class PlanTier(str, Enum):
    """Available subscription plan tiers."""
    STARTER = "starter"
    PROFESSIONAL = "professional"
    ENTERPRISE = "enterprise"
    CUSTOM = "custom"


class PaymentStatus(str, Enum):
    """Payment status options."""
    EM_DIA = "em_dia"
    PENDENTE = "pendente"
    ATRASADO = "atrasado"
    CANCELADO = "cancelado"


class SubscriptionStatus(str, Enum):
    """Subscription lifecycle status."""
    ACTIVE = "active"
    TRIAL = "trial"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# ── Plan Definitions ──────────────────────────────────────────────────────────

PLAN_DEFINITIONS: dict[str, dict] = {
    "starter": {
        "name": "Starter",
        "max_users": 5,
        "max_storage_tb": 0.5,
        "max_projects": 10,
        "max_flights_per_month": 20,
        "features": ["ortomosaico", "dsm", "dtm", "relatorios_basicos"],
        "monthly_price_brl": 0.0,
        "description": "Plano gratuito para avaliação da plataforma",
    },
    "professional": {
        "name": "Professional",
        "max_users": 25,
        "max_storage_tb": 5.0,
        "max_projects": 100,
        "max_flights_per_month": 100,
        "features": [
            "ortomosaico", "dsm", "dtm", "nuvem_pontos",
            "relatorios_completos", "building_extraction",
            "api_dados_publicos", "exportacao_avancada",
        ],
        "monthly_price_brl": 2990.0,
        "description": "Para prefeituras de pequeno e médio porte",
    },
    "enterprise": {
        "name": "Enterprise",
        "max_users": -1,  # unlimited
        "max_storage_tb": 50.0,
        "max_projects": -1,  # unlimited
        "max_flights_per_month": -1,  # unlimited
        "features": [
            "ortomosaico", "dsm", "dtm", "nuvem_pontos",
            "relatorios_completos", "building_extraction",
            "api_dados_publicos", "exportacao_avancada",
            "ia_segmentacao", "iptu_malha_fina",
            "prevencao_desastres", "combate_dengue",
            "planejamento_urbano", "suporte_prioritario",
        ],
        "monthly_price_brl": 9990.0,
        "description": "Para grandes prefeituras e consórcios municipais",
    },
}


# ── Request/Response Schemas ──────────────────────────────────────────────────

class SubscriptionCreate(BaseModel):
    """Create a new subscription for a tenant."""
    tenant_id: uuid.UUID
    plan: PlanTier = PlanTier.STARTER
    monthly_price_brl: float | None = None  # Override default price if custom


class SubscriptionUpdate(BaseModel):
    """Update subscription details."""
    plan: PlanTier | None = None
    status: SubscriptionStatus | None = None
    max_users: int | None = None
    max_storage_tb: float | None = None
    max_projects: int | None = None
    monthly_price_brl: float | None = None
    payment_status: PaymentStatus | None = None


class SubscriptionDetailRead(BaseModel):
    """Extended subscription read with plan details and usage."""
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str | None = None
    plan: str
    plan_name: str | None = None
    status: str
    max_users: int
    max_storage_tb: float
    max_projects: int
    monthly_price_brl: float
    payment_status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime | None = None
    # Usage metrics
    current_users: int | None = None
    current_projects: int | None = None
    current_storage_gb: float | None = None
    features: list[str] | None = None

    model_config = {"from_attributes": True}


class PlanRead(BaseModel):
    """Plan definition for display."""
    key: str
    name: str
    max_users: int
    max_storage_tb: float
    max_projects: int
    max_flights_per_month: int
    features: list[str]
    monthly_price_brl: float
    description: str


class UsageReport(BaseModel):
    """Current resource usage for a tenant."""
    tenant_id: uuid.UUID
    tenant_name: str | None = None
    plan: str
    users: dict = Field(default_factory=lambda: {"current": 0, "limit": 0, "pct": 0.0})
    storage: dict = Field(default_factory=lambda: {"current_gb": 0.0, "limit_tb": 0.0, "pct": 0.0})
    projects: dict = Field(default_factory=lambda: {"current": 0, "limit": 0, "pct": 0.0})
