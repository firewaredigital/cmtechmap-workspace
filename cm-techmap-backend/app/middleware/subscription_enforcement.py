"""
CM TECHMAP — Subscription Enforcement Middleware
Checks subscription limits before allowing resource creation.
"""

import logging
from typing import Any

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_public_db, get_current_user

logger = logging.getLogger("cm_techmap.subscription")


class SubscriptionLimitExceeded(HTTPException):
    """Raised when a subscription limit is reached."""
    def __init__(self, resource: str, current: int, limit: int):
        detail = (
            f"Limite do plano atingido para {resource}: "
            f"{current}/{limit}. Faça upgrade do plano para continuar."
        )
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=detail,
            headers={"X-Limit-Resource": resource, "X-Limit-Current": str(current), "X-Limit-Max": str(limit)},
        )


async def _get_tenant_subscription(db: AsyncSession, tenant_id: str | None) -> dict | None:
    """Fetch the active subscription for the current tenant."""
    if not tenant_id:
        return None

    try:
        result = await db.execute(text("""
            SELECT s.id, s.plan, s.status, s.max_users, s.max_storage_tb,
                   s.max_projects, s.payment_status, s.is_active
            FROM public.subscriptions s
            JOIN public.tenants t ON t.id = s.tenant_id
            WHERE t.slug = :slug AND s.is_active = true
            LIMIT 1
        """), {"slug": tenant_id})
        row = result.mappings().first()
        if row:
            return dict(row)
    except Exception as e:
        logger.warning(f"Failed to check subscription: {e}")

    return None


async def enforce_project_limit(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Check if the tenant can create a new project."""
    tenant_id = user.get("tenant_id")
    sub = await _get_tenant_subscription(db, tenant_id)
    if not sub:
        return  # No subscription → allow (graceful degradation)

    if sub["payment_status"] in ("atrasado", "cancelado"):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Pagamento pendente. Regularize sua assinatura para criar novos projetos.",
        )

    max_projects = sub["max_projects"]
    if max_projects <= 0:  # -1 = unlimited
        return

    try:
        r = await db.execute(text("SELECT COUNT(*) FROM public.projects WHERE is_active = true"))
        current = r.scalar() or 0
        if current >= max_projects:
            raise SubscriptionLimitExceeded("projetos", current, max_projects)
    except SubscriptionLimitExceeded:
        raise
    except Exception as e:
        logger.warning(f"Failed to check project limit: {e}")


async def enforce_storage_limit(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Check if the tenant has storage capacity."""
    tenant_id = user.get("tenant_id")
    sub = await _get_tenant_subscription(db, tenant_id)
    if not sub:
        return

    max_storage_tb = sub["max_storage_tb"]
    if max_storage_tb <= 0:
        return

    try:
        r = await db.execute(text(
            "SELECT COALESCE(SUM(file_size_bytes), 0) FROM public.flight_assets WHERE is_active = true"
        ))
        current_bytes = int(r.scalar() or 0)
        limit_bytes = int(max_storage_tb * 1024 ** 4)  # TB → bytes
        if current_bytes >= limit_bytes:
            current_gb = round(current_bytes / (1024 ** 3), 2)
            limit_gb = round(max_storage_tb * 1024, 2)
            raise SubscriptionLimitExceeded("armazenamento (GB)", int(current_gb), int(limit_gb))
    except SubscriptionLimitExceeded:
        raise
    except Exception as e:
        logger.warning(f"Failed to check storage limit: {e}")


async def enforce_payment_status(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(get_current_user),
) -> None:
    """Block operations if payment is overdue."""
    tenant_id = user.get("tenant_id")
    sub = await _get_tenant_subscription(db, tenant_id)
    if not sub:
        return

    if sub["payment_status"] == "cancelado":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Assinatura cancelada. Entre em contato com o suporte.",
        )

    if sub["payment_status"] == "atrasado":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Pagamento atrasado. Regularize para continuar utilizando a plataforma.",
        )
