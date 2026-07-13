"""
CM TECHMAP — FastAPI Dependencies
Reusable dependencies for authentication, authorization, and database access.
"""

from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session, get_public_db_session, get_direct_db_session
from app.core.security import get_current_user_from_request, check_roles


# ── Database dependencies ─────────────────────────────────────────────────────

async def get_db(session: AsyncSession = Depends(get_db_session)) -> AsyncSession:
    """Tenant-scoped database session."""
    return session


async def get_public_db(session: AsyncSession = Depends(get_public_db_session)) -> AsyncSession:
    """Public-schema database session for cross-tenant operations."""
    return session


async def get_direct_db(session: AsyncSession = Depends(get_direct_db_session)) -> AsyncSession:
    """Direct PostgreSQL session (bypasses PgBouncer). Use for DDL/schema operations."""
    return session


# ── Auth dependencies ─────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> dict[str, Any]:
    """Extract and validate the current authenticated user."""
    return await get_current_user_from_request(request)


def require_roles(*roles: str):
    """
    Dependency factory that enforces role-based access control.

    Usage:
        @router.get("/admin/users", dependencies=[Depends(require_roles("super_admin", "tenant_admin"))])
    """
    async def _check(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if not check_roles(user.get("roles", []), list(roles)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required roles: {', '.join(roles)}",
            )
        return user
    return _check


# ── Pre-built role checks ────────────────────────────────────────────────────

require_super_admin = require_roles("super_admin")
require_tenant_admin = require_roles("super_admin", "tenant_admin")
require_gestor = require_roles("super_admin", "tenant_admin", "gestor")
require_operador = require_roles("super_admin", "tenant_admin", "gestor", "operador")
require_viewer = require_roles("super_admin", "tenant_admin", "gestor", "operador", "viewer")


# ── Portal-level access guards ──────────────────────────────────────────────

def require_admin_portal():
    """Ensure the user has admin portal access (super_admin only)."""
    async def _check(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if "super_admin" not in user.get("roles", []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access restricted to admin portal",
            )
        return user
    return _check


def require_prefeitura_portal():
    """Ensure the user belongs to a municipality (not a platform-level admin-only)."""
    async def _check(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        roles = set(user.get("roles", []))
        allowed = {"tenant_admin", "admin", "gestor", "operador", "viewer"}
        if not (roles & allowed):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access restricted to municipality portal",
            )
        return user
    return _check

