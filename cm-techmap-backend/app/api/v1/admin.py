"""CM TECHMAP — Admin Routes (Enhanced with Lifecycle + Quota Management)"""

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.tenant_lifecycle import (
    provision_tenant, migrate_tenant_schema, migrate_all_tenants,
    deactivate_tenant, reactivate_tenant, get_tenant_stats, SCHEMA_VERSION,
)
from app.services.tenant_quota import get_tenant_quota
from app.dependencies import get_public_db, get_current_user, require_super_admin, require_tenant_admin
from app.schemas.tenant import TenantCreate, TenantRead, SubscriptionRead

router = APIRouter(prefix="/admin", tags=["Admin"])


# ══════════════════════════════════════════════════════════════════════════════
# TENANT CRUD
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tenants", response_model=list[TenantRead])
async def list_tenants(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """List all registered tenants (super_admin only)."""
    result = await db.execute(text(
        "SELECT id, name, slug, city, state, is_active, created_at "
        "FROM public.tenants ORDER BY created_at DESC"
    ))
    return [TenantRead(id=r[0], name=r[1], slug=r[2], city=r[3],
                       state=r[4], is_active=r[5], created_at=r[6])
            for r in result.fetchall()]


@router.post("/tenants", response_model=dict, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreate,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """
    Provision a new tenant (municipality).
    Creates the database record, PostgreSQL schema, tables, indexes, and RLS.
    """
    # Check for duplicate slug
    existing = await db.execute(text(
        "SELECT 1 FROM public.tenants WHERE slug = :slug"
    ), {"slug": body.slug})
    if existing.scalar():
        raise HTTPException(status_code=409, detail=f"Tenant '{body.slug}' already exists")

    # Insert tenant record
    result = await db.execute(text(
        "INSERT INTO public.tenants (name, slug, cnpj, city, state, contact_email) "
        "VALUES (:name, :slug, :cnpj, :city, :state, :email) RETURNING id, name, slug, city, state, is_active, created_at"
    ), {"name": body.name, "slug": body.slug, "cnpj": body.cnpj,
        "city": body.city, "state": body.state, "email": body.contact_email})
    row = result.fetchone()
    await db.commit()

    # Create a default subscription
    await db.execute(text(
        "INSERT INTO public.subscriptions (tenant_id, plan, status, max_users, "
        "max_storage_tb, max_projects, monthly_price_brl) "
        "VALUES (:tid, 'starter', 'active', 5, 1.0, 10, 0.0)"
    ), {"tid": str(row[0])})
    await db.commit()

    # Full tenant provisioning: schema + tables + indexes + RLS
    report = await provision_tenant(
        db, body.slug,
        municipality_name=body.name,
        city=body.city or "",
        state=body.state or "",
    )

    return {
        "tenant": TenantRead(
            id=row[0], name=row[1], slug=row[2], city=row[3],
            state=row[4], is_active=row[5], created_at=row[6],
        ).model_dump(),
        "provisioning": report,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TENANT LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/tenants/{slug}/deactivate")
async def deactivate_tenant_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Deactivate a tenant (revoke access, keep data)."""
    return await deactivate_tenant(db, slug)


@router.post("/tenants/{slug}/reactivate")
async def reactivate_tenant_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Reactivate a previously deactivated tenant."""
    return await reactivate_tenant(db, slug)


@router.post("/tenants/{slug}/migrate")
async def migrate_tenant_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Migrate a single tenant schema to the latest version."""
    return await migrate_tenant_schema(db, slug)


@router.post("/tenants/migrate-all")
async def migrate_all_tenants_endpoint(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Migrate ALL tenant schemas to the latest version. Use after upgrades."""
    reports = await migrate_all_tenants(db)
    return {
        "total": len(reports),
        "target_version": SCHEMA_VERSION,
        "results": reports,
    }


@router.get("/tenants/{slug}/stats")
async def tenant_stats_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Get comprehensive stats for a tenant (usage, disk, schema version)."""
    return await get_tenant_stats(db, slug)


# ══════════════════════════════════════════════════════════════════════════════
# QUOTA MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/tenants/{slug}/quota")
async def tenant_quota_endpoint(
    slug: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Get quota usage vs. limits for a tenant."""
    quota = await get_tenant_quota(db, slug)
    if not quota:
        raise HTTPException(status_code=404, detail=f"Tenant '{slug}' not found")
    return quota.to_dict()


@router.get("/tenants/{slug}/quota/summary")
async def tenant_quota_summary(
    slug: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """
    Quota summary for the current tenant admin.
    Lighter endpoint accessible by tenant admins (not just super_admin).
    """
    quota = await get_tenant_quota(db, slug)
    if not quota:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "plan": quota.plan,
        "users": {"used": quota.usage.get("users", 0), "limit": quota.limits["max_users"]},
        "projects": {"used": quota.usage.get("projects", 0), "limit": quota.limits["max_projects"]},
        "storage": {
            "used_gb": quota.usage.get("storage_gb", 0),
            "limit_tb": quota.limits["max_storage_tb"],
        },
        "flights": quota.usage.get("flights", 0),
        "discrepancies": quota.usage.get("discrepancies", 0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/schemas")
async def list_schemas(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """List all tenant schemas in the database."""
    result = await db.execute(text(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name LIKE 'tenant_%' ORDER BY schema_name"
    ))
    schemas = [row[0] for row in result.fetchall()]
    return {
        "schemas": schemas,
        "count": len(schemas),
        "current_schema_version": SCHEMA_VERSION,
    }


@router.get("/schemas/{schema_name}/tables")
async def list_schema_tables(
    schema_name: str,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """List tables and their row counts for a specific schema."""
    if not schema_name.startswith("tenant_"):
        raise HTTPException(status_code=400, detail="Only tenant schemas allowed")

    result = await db.execute(text("""
        SELECT table_name,
               pg_size_pretty(pg_total_relation_size(quote_ident(:schema) || '.' || quote_ident(table_name))) as size
        FROM information_schema.tables
        WHERE table_schema = :schema AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """), {"schema": schema_name})

    tables = []
    for row in result.fetchall():
        try:
            count_result = await db.execute(text(
                f'SELECT COUNT(*) FROM "{schema_name}"."{row[0]}"'
            ))
            count = count_result.scalar() or 0
        except Exception:
            count = -1
        tables.append({"name": row[0], "size": row[1], "row_count": count})

    return {"schema": schema_name, "tables": tables}


# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/subscriptions", response_model=list[SubscriptionRead])
async def list_subscriptions(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_tenant_admin),
):
    """List all subscriptions."""
    result = await db.execute(text(
        "SELECT id, tenant_id, plan, status, max_users, max_storage_tb, "
        "monthly_price_brl, payment_status, is_active, created_at "
        "FROM public.subscriptions ORDER BY created_at DESC"
    ))
    return [SubscriptionRead(
        id=r[0], tenant_id=r[1], plan=r[2], status=r[3], max_users=r[4],
        max_storage_tb=r[5], monthly_price_brl=r[6], payment_status=r[7],
        is_active=r[8], created_at=r[9],
    ) for r in result.fetchall()]


@router.put("/subscriptions/{tenant_slug}/upgrade")
async def upgrade_subscription(
    tenant_slug: str,
    plan: str = Query(..., pattern="^(starter|professional|enterprise)$"),
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Upgrade a tenant's subscription plan."""
    PLAN_LIMITS = {
        "starter": {"max_users": 5, "max_storage_tb": 1.0, "max_projects": 10, "price": 0},
        "professional": {"max_users": 25, "max_storage_tb": 5.0, "max_projects": 50, "price": 2990},
        "enterprise": {"max_users": 999, "max_storage_tb": 50.0, "max_projects": 999, "price": 9990},
    }
    limits = PLAN_LIMITS.get(plan)
    if not limits:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {plan}")

    # Get tenant_id
    result = await db.execute(text(
        "SELECT id FROM public.tenants WHERE slug = :slug"
    ), {"slug": tenant_slug})
    tenant_id = result.scalar()
    if not tenant_id:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Update subscription
    await db.execute(text("""
        UPDATE public.subscriptions
        SET plan = :plan, max_users = :max_users, max_storage_tb = :storage,
            max_projects = :projects, monthly_price_brl = :price,
            updated_at = NOW()
        WHERE tenant_id = :tid AND is_active = true
    """), {
        "plan": plan, "max_users": limits["max_users"],
        "storage": limits["max_storage_tb"], "projects": limits["max_projects"],
        "price": limits["price"], "tid": str(tenant_id),
    })
    await db.commit()

    return {
        "tenant": tenant_slug,
        "plan": plan,
        "limits": limits,
        "status": "upgraded",
    }
