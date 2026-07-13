"""
CM TECHMAP — Tenant Quota Enforcement Middleware
Enforces subscription limits: max_users, max_storage_tb, max_projects.
Runs after TenantMiddleware and before route handlers.
"""

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("cm_techmap.quota")


class TenantQuota:
    """Encapsulates quota limits and current usage for a tenant."""

    def __init__(self, tenant_id: str, plan: str, limits: dict, usage: dict):
        self.tenant_id = tenant_id
        self.plan = plan
        self.limits = limits
        self.usage = usage

    @property
    def users_remaining(self) -> int:
        return max(0, self.limits.get("max_users", 0) - self.usage.get("users", 0))

    @property
    def projects_remaining(self) -> int:
        return max(0, self.limits.get("max_projects", 0) - self.usage.get("projects", 0))

    @property
    def storage_remaining_gb(self) -> float:
        max_gb = self.limits.get("max_storage_tb", 0) * 1024
        used_gb = self.usage.get("storage_gb", 0)
        return max(0.0, max_gb - used_gb)

    def can_add_user(self) -> bool:
        return self.users_remaining > 0

    def can_add_project(self) -> bool:
        return self.projects_remaining > 0

    def can_upload(self, file_size_gb: float = 0) -> bool:
        return self.storage_remaining_gb >= file_size_gb

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "plan": self.plan,
            "limits": self.limits,
            "usage": self.usage,
            "remaining": {
                "users": self.users_remaining,
                "projects": self.projects_remaining,
                "storage_gb": round(self.storage_remaining_gb, 2),
            },
            "at_capacity": {
                "users": not self.can_add_user(),
                "projects": not self.can_add_project(),
                "storage": not self.can_upload(1.0),
            },
        }


async def get_tenant_quota(session: AsyncSession, tenant_slug: str) -> TenantQuota | None:
    """
    Fetch the subscription limits and current usage for a tenant.
    Returns None if tenant or subscription not found.
    """
    schema = f"tenant_{tenant_slug}"

    # ── Get subscription limits ───────────────────────────────────────────
    result = await session.execute(text("""
        SELECT t.id, s.plan, s.max_users, s.max_storage_tb, s.max_projects
        FROM public.tenants t
        LEFT JOIN public.subscriptions s ON s.tenant_id = t.id AND s.is_active = true
        WHERE t.slug = :slug AND t.is_active = true
        LIMIT 1
    """), {"slug": tenant_slug})
    row = result.fetchone()
    if not row:
        return None

    tenant_id, plan, max_users, max_storage_tb, max_projects = row
    limits = {
        "max_users": max_users or 5,
        "max_storage_tb": float(max_storage_tb or 1.0),
        "max_projects": max_projects or 10,
    }

    # ── Count current usage ───────────────────────────────────────────────
    usage: dict[str, Any] = {}

    # Active users
    try:
        r = await session.execute(text(
            f'SELECT COUNT(*) FROM "{schema}".users WHERE is_active = true'
        ))
        usage["users"] = r.scalar() or 0
    except Exception:
        usage["users"] = 0

    # Active projects
    try:
        r = await session.execute(text(
            f'SELECT COUNT(*) FROM "{schema}".projects WHERE is_active = true'
        ))
        usage["projects"] = r.scalar() or 0
    except Exception:
        usage["projects"] = 0

    # Storage: sum of flight_assets file sizes in bytes → convert to GB
    try:
        r = await session.execute(text(
            f'SELECT COALESCE(SUM(file_size_bytes), 0) FROM "{schema}".flight_assets WHERE is_active = true'
        ))
        usage["storage_bytes"] = r.scalar() or 0
        usage["storage_gb"] = round(usage["storage_bytes"] / (1024 ** 3), 2)
    except Exception:
        usage["storage_bytes"] = 0
        usage["storage_gb"] = 0.0

    # Discrepancies (informational)
    try:
        r = await session.execute(text(
            f'SELECT COUNT(*) FROM "{schema}".discrepancies'
        ))
        usage["discrepancies"] = r.scalar() or 0
    except Exception:
        usage["discrepancies"] = 0

    # Flights count
    try:
        r = await session.execute(text(
            f'SELECT COUNT(*) FROM "{schema}".flights WHERE is_active = true'
        ))
        usage["flights"] = r.scalar() or 0
    except Exception:
        usage["flights"] = 0

    return TenantQuota(
        tenant_id=str(tenant_id),
        plan=plan or "starter",
        limits=limits,
        usage=usage,
    )


async def enforce_user_quota(session: AsyncSession, tenant_slug: str) -> None:
    """Raise exception if user quota exceeded."""
    from fastapi import HTTPException
    quota = await get_tenant_quota(session, tenant_slug)
    if quota and not quota.can_add_user():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "quota_exceeded",
                "resource": "users",
                "limit": quota.limits["max_users"],
                "current": quota.usage["users"],
                "message": f"Limite de {quota.limits['max_users']} usuários atingido. "
                           f"Faça upgrade do plano para adicionar mais usuários.",
            },
        )


async def enforce_project_quota(session: AsyncSession, tenant_slug: str) -> None:
    """Raise exception if project quota exceeded."""
    from fastapi import HTTPException
    quota = await get_tenant_quota(session, tenant_slug)
    if quota and not quota.can_add_project():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "quota_exceeded",
                "resource": "projects",
                "limit": quota.limits["max_projects"],
                "current": quota.usage["projects"],
                "message": f"Limite de {quota.limits['max_projects']} projetos atingido. "
                           f"Faça upgrade do plano para criar mais projetos.",
            },
        )


async def enforce_storage_quota(
    session: AsyncSession, tenant_slug: str, additional_bytes: int = 0
) -> None:
    """Raise exception if storage quota would be exceeded."""
    from fastapi import HTTPException
    quota = await get_tenant_quota(session, tenant_slug)
    additional_gb = additional_bytes / (1024 ** 3)
    if quota and not quota.can_upload(additional_gb):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "quota_exceeded",
                "resource": "storage",
                "limit_tb": quota.limits["max_storage_tb"],
                "current_gb": quota.usage["storage_gb"],
                "message": f"Armazenamento de {quota.limits['max_storage_tb']}TB atingido. "
                           f"Usado: {quota.usage['storage_gb']:.1f}GB. "
                           f"Faça upgrade do plano para mais espaço.",
            },
        )
