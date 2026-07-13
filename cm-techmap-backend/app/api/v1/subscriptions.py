"""
CM TECHMAP — Subscription & Billing API
Full CRUD for subscriptions, plan management, usage tracking, and limit enforcement.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_public_db, require_super_admin, require_tenant_admin
from app.schemas.subscription import (
    PLAN_DEFINITIONS,
    PlanRead,
    SubscriptionCreate,
    SubscriptionDetailRead,
    SubscriptionUpdate,
    UsageReport,
)
from app.services.audit_log import AuditAction

logger = logging.getLogger("cm_techmap.api.subscriptions")

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions & Billing"])


# ── Plan Catalog ──────────────────────────────────────────────────────────────

@router.get("/plans", response_model=list[PlanRead])
async def list_plans():
    """List all available subscription plans (public endpoint)."""
    return [
        PlanRead(key=key, **plan)
        for key, plan in PLAN_DEFINITIONS.items()
    ]


@router.get("/plans/{plan_key}", response_model=PlanRead)
async def get_plan(plan_key: str):
    """Get details of a specific plan."""
    plan = PLAN_DEFINITIONS.get(plan_key)
    if not plan:
        raise HTTPException(
            status_code=404,
            detail=f"Plano '{plan_key}' não encontrado",
        )
    return PlanRead(key=plan_key, **plan)


# ── Subscription CRUD ─────────────────────────────────────────────────────────

@router.get("", response_model=list[SubscriptionDetailRead])
async def list_subscriptions(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """List all subscriptions with tenant info."""
    result = await db.execute(text("""
        SELECT s.id, s.tenant_id, t.name as tenant_name, s.plan, s.status,
               s.max_users, s.max_storage_tb, s.max_projects,
               s.monthly_price_brl, s.payment_status, s.is_active,
               s.created_at, s.updated_at
        FROM public.subscriptions s
        LEFT JOIN public.tenants t ON t.id = s.tenant_id
        ORDER BY s.created_at DESC
    """))

    subscriptions = []
    for r in result.mappings().all():
        plan_def = PLAN_DEFINITIONS.get(r["plan"], {})
        subscriptions.append(SubscriptionDetailRead(
            id=r["id"],
            tenant_id=r["tenant_id"],
            tenant_name=r.get("tenant_name"),
            plan=r["plan"],
            plan_name=plan_def.get("name", r["plan"]),
            status=r["status"],
            max_users=r["max_users"],
            max_storage_tb=r["max_storage_tb"],
            max_projects=r["max_projects"],
            monthly_price_brl=r["monthly_price_brl"],
            payment_status=r["payment_status"],
            is_active=r["is_active"],
            created_at=r["created_at"],
            updated_at=r.get("updated_at"),
            features=plan_def.get("features", []),
        ))

    return subscriptions


@router.get("/{subscription_id}", response_model=SubscriptionDetailRead)
async def get_subscription(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Get subscription details with usage metrics."""
    result = await db.execute(text("""
        SELECT s.id, s.tenant_id, t.name as tenant_name, t.slug as tenant_slug,
               s.plan, s.status, s.max_users, s.max_storage_tb, s.max_projects,
               s.monthly_price_brl, s.payment_status, s.is_active,
               s.created_at, s.updated_at
        FROM public.subscriptions s
        LEFT JOIN public.tenants t ON t.id = s.tenant_id
        WHERE s.id = :sid
    """), {"sid": str(subscription_id)})

    r = result.mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="Assinatura não encontrada")

    plan_def = PLAN_DEFINITIONS.get(r["plan"], {})

    # Get usage metrics
    usage = await _get_tenant_usage(db, r["tenant_id"], r.get("tenant_slug"))

    return SubscriptionDetailRead(
        id=r["id"],
        tenant_id=r["tenant_id"],
        tenant_name=r.get("tenant_name"),
        plan=r["plan"],
        plan_name=plan_def.get("name", r["plan"]),
        status=r["status"],
        max_users=r["max_users"],
        max_storage_tb=r["max_storage_tb"],
        max_projects=r["max_projects"],
        monthly_price_brl=r["monthly_price_brl"],
        payment_status=r["payment_status"],
        is_active=r["is_active"],
        created_at=r["created_at"],
        updated_at=r.get("updated_at"),
        current_users=usage.get("users", 0),
        current_projects=usage.get("projects", 0),
        current_storage_gb=usage.get("storage_gb", 0),
        features=plan_def.get("features", []),
    )


@router.post("", response_model=SubscriptionDetailRead, status_code=201)
async def create_subscription(
    body: SubscriptionCreate,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Create a new subscription for a tenant (super_admin only)."""
    plan_def = PLAN_DEFINITIONS.get(body.plan.value)
    if not plan_def:
        raise HTTPException(status_code=400, detail=f"Plano '{body.plan}' inválido")

    # Check tenant exists
    tenant = await db.execute(text(
        "SELECT id, name FROM public.tenants WHERE id = :tid"
    ), {"tid": str(body.tenant_id)})
    tenant_row = tenant.mappings().first()
    if not tenant_row:
        raise HTTPException(status_code=404, detail="Tenant não encontrado")

    # Check no active subscription exists
    existing = await db.execute(text(
        "SELECT 1 FROM public.subscriptions WHERE tenant_id = :tid AND is_active = true"
    ), {"tid": str(body.tenant_id)})
    if existing.scalar():
        raise HTTPException(status_code=409, detail="Este tenant já possui uma assinatura ativa")

    price = body.monthly_price_brl if body.monthly_price_brl is not None else plan_def["monthly_price_brl"]

    result = await db.execute(text("""
        INSERT INTO public.subscriptions
            (tenant_id, plan, status, max_users, max_storage_tb, max_projects, monthly_price_brl, payment_status)
        VALUES
            (:tid, :plan, 'active', :users, :storage, :projects, :price, 'em_dia')
        RETURNING id, tenant_id, plan, status, max_users, max_storage_tb, max_projects,
                  monthly_price_brl, payment_status, is_active, created_at, updated_at
    """), {
        "tid": str(body.tenant_id),
        "plan": body.plan.value,
        "users": plan_def["max_users"],
        "storage": plan_def["max_storage_tb"],
        "projects": plan_def["max_projects"],
        "price": price,
    })
    row = result.mappings().first()
    await db.commit()

    logger.info(f"Subscription created: tenant={body.tenant_id}, plan={body.plan.value}")

    return SubscriptionDetailRead(
        id=row["id"],
        tenant_id=row["tenant_id"],
        tenant_name=tenant_row["name"],
        plan=row["plan"],
        plan_name=plan_def["name"],
        status=row["status"],
        max_users=row["max_users"],
        max_storage_tb=row["max_storage_tb"],
        max_projects=row["max_projects"],
        monthly_price_brl=row["monthly_price_brl"],
        payment_status=row["payment_status"],
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
        features=plan_def.get("features", []),
    )


@router.patch("/{subscription_id}", response_model=SubscriptionDetailRead)
async def update_subscription(
    subscription_id: UUID,
    body: SubscriptionUpdate,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Update subscription (plan upgrade/downgrade, status change, etc.)."""
    # Build dynamic SET clause
    updates = {}
    set_parts = []

    if body.plan is not None:
        plan_def = PLAN_DEFINITIONS.get(body.plan.value)
        if not plan_def:
            raise HTTPException(status_code=400, detail=f"Plano '{body.plan}' inválido")
        updates["plan"] = body.plan.value
        set_parts.append("plan = :plan")
        # Apply plan limits unless overridden
        if body.max_users is None:
            updates["max_users"] = plan_def["max_users"]
            set_parts.append("max_users = :max_users")
        if body.max_storage_tb is None:
            updates["max_storage_tb"] = plan_def["max_storage_tb"]
            set_parts.append("max_storage_tb = :max_storage_tb")
        if body.max_projects is None:
            updates["max_projects"] = plan_def["max_projects"]
            set_parts.append("max_projects = :max_projects")
        if body.monthly_price_brl is None:
            updates["monthly_price_brl"] = plan_def["monthly_price_brl"]
            set_parts.append("monthly_price_brl = :monthly_price_brl")

    if body.status is not None:
        updates["status"] = body.status.value
        set_parts.append("status = :status")

    if body.max_users is not None:
        updates["max_users"] = body.max_users
        set_parts.append("max_users = :max_users")

    if body.max_storage_tb is not None:
        updates["max_storage_tb"] = body.max_storage_tb
        set_parts.append("max_storage_tb = :max_storage_tb")

    if body.max_projects is not None:
        updates["max_projects"] = body.max_projects
        set_parts.append("max_projects = :max_projects")

    if body.monthly_price_brl is not None:
        updates["monthly_price_brl"] = body.monthly_price_brl
        set_parts.append("monthly_price_brl = :monthly_price_brl")

    if body.payment_status is not None:
        updates["payment_status"] = body.payment_status.value
        set_parts.append("payment_status = :payment_status")

    if not set_parts:
        raise HTTPException(status_code=400, detail="Nenhum campo para atualizar")

    updates["sid"] = str(subscription_id)
    set_clause = ", ".join(set_parts)

    await db.execute(
        text(f"UPDATE public.subscriptions SET {set_clause} WHERE id = :sid"),
        updates,
    )
    await db.commit()

    logger.info(f"Subscription {subscription_id} updated: {list(updates.keys())}")

    # Return updated subscription
    return await get_subscription(subscription_id, db, user)


# ── Usage & Enforcement ───────────────────────────────────────────────────────

@router.get("/{subscription_id}/usage", response_model=UsageReport)
async def get_subscription_usage(
    subscription_id: UUID,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """Get current resource usage for a subscription."""
    result = await db.execute(text("""
        SELECT s.tenant_id, t.name, t.slug, s.plan,
               s.max_users, s.max_storage_tb, s.max_projects
        FROM public.subscriptions s
        LEFT JOIN public.tenants t ON t.id = s.tenant_id
        WHERE s.id = :sid
    """), {"sid": str(subscription_id)})

    r = result.mappings().first()
    if not r:
        raise HTTPException(status_code=404, detail="Assinatura não encontrada")

    usage = await _get_tenant_usage(db, r["tenant_id"], r.get("slug"))

    max_users = r["max_users"]
    max_projects = r["max_projects"]
    max_storage_tb = r["max_storage_tb"]

    return UsageReport(
        tenant_id=r["tenant_id"],
        tenant_name=r.get("name"),
        plan=r["plan"],
        users={
            "current": usage.get("users", 0),
            "limit": max_users if max_users > 0 else -1,
            "pct": round(usage.get("users", 0) / max_users * 100, 1) if max_users > 0 else 0,
        },
        storage={
            "current_gb": usage.get("storage_gb", 0),
            "limit_tb": max_storage_tb,
            "pct": round(usage.get("storage_gb", 0) / (max_storage_tb * 1024) * 100, 1) if max_storage_tb > 0 else 0,
        },
        projects={
            "current": usage.get("projects", 0),
            "limit": max_projects if max_projects > 0 else -1,
            "pct": round(usage.get("projects", 0) / max_projects * 100, 1) if max_projects > 0 else 0,
        },
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

async def _get_tenant_usage(db: AsyncSession, tenant_id, tenant_slug: str | None = None) -> dict:
    """Compute actual resource usage for a tenant."""
    usage = {"users": 0, "projects": 0, "storage_gb": 0.0}

    # Count users in tenant schema
    if tenant_slug:
        try:
            r = await db.execute(text(
                f'SELECT COUNT(*) FROM "tenant_{tenant_slug}".users WHERE is_active = true'
            ))
            usage["users"] = r.scalar() or 0
        except Exception:
            pass

    # Count projects
    try:
        r = await db.execute(text(
            "SELECT COUNT(*) FROM public.projects WHERE is_active = true"
        ))
        usage["projects"] = r.scalar() or 0
    except Exception:
        pass

    # Sum storage
    try:
        r = await db.execute(text(
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM public.flight_assets WHERE is_active = true"
        ))
        storage_bytes = int(r.scalar() or 0)
        usage["storage_gb"] = round(storage_bytes / (1024 ** 3), 2)
    except Exception:
        pass

    return usage
