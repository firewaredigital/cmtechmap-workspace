"""
CM TECHMAP — User Management Routes
Full CRUD, role assignment, session management, and statistics.
All operations go through the Keycloak Admin REST API.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status, Query

from app.dependencies import get_current_user, require_tenant_admin, require_super_admin
from app.services.keycloak_admin import keycloak_admin, KeycloakAdminError
from app.schemas.user_management import (
    UserCreateRequest, UserUpdateRequest, UserRoleChangeRequest,
    PasswordResetRequest, UserResponse, UserListResponse,
    UserStatsResponse, RoleResponse,
)
from app.schemas.auth import UserInfo

logger = logging.getLogger("cm_techmap.api.users")

router = APIRouter(prefix="/users", tags=["Users"])


# ── Current User ──────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserInfo)
async def get_current_user_profile(
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get the currently authenticated user's profile from JWT claims."""
    return UserInfo(**user)


@router.put("/me")
async def update_my_profile(
    body: UserUpdateRequest,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Update the current user's own profile (name, department, phone)."""
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    try:
        updated = await keycloak_admin.update_user(
            user_id=user_id,
            first_name=body.first_name,
            last_name=body.last_name,
            department=body.department,
        )
        return updated
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/me/activity")
async def get_my_activity(
    limit: int = Query(20, ge=1, le=100),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get the current user's recent activity history from audit logs."""
    from sqlalchemy import text
    from app.core.database import get_public_db_session

    user_id = user.get("sub") or user.get("email", "")

    try:
        async for session in get_public_db_session():
            result = await session.execute(
                text("""
                    SELECT action, entity_type, entity_id, details,
                           ip_address, created_at
                    FROM public.activity_logs
                    WHERE user_id = :uid
                    ORDER BY created_at DESC
                    LIMIT :lim
                """),
                {"uid": user_id, "lim": limit},
            )
            activities = []
            for r in result.mappings().all():
                activities.append({
                    "action": r["action"],
                    "entity_type": r.get("entity_type"),
                    "entity_id": r.get("entity_id"),
                    "details": r.get("details", {}),
                    "ip_address": r.get("ip_address"),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                })
            return {"activities": activities, "count": len(activities)}
    except Exception as e:
        logger.warning(f"Failed to get activity history: {e}")
        return {"activities": [], "count": 0}


@router.get("/me/notifications")
async def get_my_notification_count(
    user: dict[str, Any] = Depends(get_current_user),
):
    """Quick endpoint to get unread notification count for the current user."""
    from sqlalchemy import text
    from app.core.database import get_public_db_session

    user_id = user.get("sub", "")
    try:
        async for session in get_public_db_session():
            result = await session.execute(
                text("SELECT COUNT(*) FROM public.notifications WHERE user_id = :uid AND is_read = false"),
                {"uid": user_id},
            )
            count = result.scalar() or 0
            return {"unread_count": count}
    except Exception:
        return {"unread_count": 0}


@router.get("/me/permissions")
async def get_current_user_permissions(
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get the current user's role and detailed permissions."""
    roles = user.get("roles", [])
    role = _primary_role(roles)

    permissions = _compute_permissions(role)
    return {
        "user_id": user.get("sub"),
        "email": user.get("email"),
        "role": role,
        "all_roles": roles,
        "permissions": permissions,
        "portal": "admin" if role == "super_admin" else "prefeitura",
    }


# ── User CRUD (tenant_admin+) ────────────────────────────────────────────────

@router.get("", response_model=UserListResponse)
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(""),
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """List all users with pagination and search (tenant_admin+)."""
    try:
        first = (page - 1) * page_size
        users, total = await keycloak_admin.list_users(
            search=search, first=first, max_results=page_size,
        )
        return UserListResponse(
            items=[UserResponse(**u) for u in users],
            total=total,
            page=page,
            page_size=page_size,
        )
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/stats", response_model=UserStatsResponse)
async def get_user_stats(
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Get aggregate user statistics (tenant_admin+)."""
    try:
        stats = await keycloak_admin.get_user_stats()
        return UserStatsResponse(**stats)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/roles", response_model=list[RoleResponse])
async def get_available_roles(
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Get all available platform roles with descriptions (tenant_admin+)."""
    try:
        roles = await keycloak_admin.get_available_roles()
        return [RoleResponse(**r) for r in roles]
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Get a single user with full details (tenant_admin+)."""
    try:
        user = await keycloak_admin.get_user(user_id)
        return UserResponse(**user)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """
    Create a new user with role and tenant assignment (tenant_admin+).
    The user is created directly in Keycloak with the specified role.
    """
    # tenant_admin can only create users with lower-priority roles
    admin_role = _primary_role(admin.get("roles", []))
    if not _can_assign_role(admin_role, body.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign role '{body.role}' with your role '{admin_role}'",
        )

    try:
        user = await keycloak_admin.create_user(
            email=body.email,
            first_name=body.first_name,
            last_name=body.last_name,
            password=body.password,
            role=body.role,
            tenant_id=body.tenant_id,
            department=body.department,
        )
        return UserResponse(**user)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Update user attributes (tenant_admin+)."""
    try:
        user = await keycloak_admin.update_user(
            user_id=user_id,
            first_name=body.first_name,
            last_name=body.last_name,
            email=body.email,
            enabled=body.enabled,
            department=body.department,
            tenant_id=body.tenant_id,
        )
        return UserResponse(**user)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_super_admin),
):
    """Delete a user permanently (super_admin only)."""
    # Prevent self-deletion
    if admin.get("sub") == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )
    try:
        await keycloak_admin.delete_user(user_id)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ── Role Management ──────────────────────────────────────────────────────────

@router.put("/{user_id}/role", response_model=UserResponse)
async def change_user_role(
    user_id: str,
    body: UserRoleChangeRequest,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Change a user's platform role (tenant_admin+)."""
    admin_role = _primary_role(admin.get("roles", []))
    if not _can_assign_role(admin_role, body.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You cannot assign role '{body.role}' with your role '{admin_role}'",
        )

    # Prevent self-demotion for safety
    if admin.get("sub") == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change your own role",
        )

    try:
        user = await keycloak_admin.change_user_role(user_id, body.role)
        return UserResponse(**user)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ── Session & Password Management ────────────────────────────────────────────

@router.post("/{user_id}/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_user(
    user_id: str,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Terminate all active sessions for a user (tenant_admin+)."""
    try:
        await keycloak_admin.logout_user(user_id)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.put("/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: str,
    body: PasswordResetRequest,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Reset a user's password (tenant_admin+)."""
    try:
        await keycloak_admin.reset_password(user_id, body.new_password, body.temporary)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.put("/{user_id}/toggle-status", response_model=UserResponse)
async def toggle_user_status(
    user_id: str,
    admin: dict[str, Any] = Depends(require_tenant_admin),
):
    """Enable/disable a user account (tenant_admin+)."""
    if admin.get("sub") == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot toggle your own account status",
        )

    try:
        current = await keycloak_admin.get_user(user_id)
        new_status = not current.get("enabled", True)
        user = await keycloak_admin.update_user(user_id, enabled=new_status)

        # If disabling, also logout
        if not new_status:
            try:
                await keycloak_admin.logout_user(user_id)
            except KeycloakAdminError:
                pass  # Best-effort

        return UserResponse(**user)
    except KeycloakAdminError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


# ── Helpers ──────────────────────────────────────────────────────────────────

ROLE_HIERARCHY = {
    "super_admin": 0,
    "tenant_admin": 1,
    "gestor": 2,
    "operador": 3,
    "viewer": 4,
}


def _primary_role(roles: list[str]) -> str:
    """Get the highest-priority platform role from a list."""
    platform = [r for r in roles if r in ROLE_HIERARCHY]
    if not platform:
        return "viewer"
    return min(platform, key=lambda r: ROLE_HIERARCHY.get(r, 99))


def _can_assign_role(admin_role: str, target_role: str) -> bool:
    """Check if an admin can assign a given role (must be higher priority)."""
    admin_level = ROLE_HIERARCHY.get(admin_role, 99)
    target_level = ROLE_HIERARCHY.get(target_role, 99)
    # Can only assign roles at same level or lower
    return admin_level <= target_level


def _compute_permissions(role: str) -> dict[str, bool]:
    """Compute granular permissions based on role."""
    level = ROLE_HIERARCHY.get(role, 99)
    return {
        # Platform
        "manage_tenants": level <= 0,
        "view_all_tenants": level <= 0,
        # Users
        "manage_users": level <= 1,
        "create_users": level <= 1,
        "delete_users": level <= 0,
        "change_roles": level <= 1,
        "reset_passwords": level <= 1,
        # Projects
        "create_projects": level <= 2,
        "delete_projects": level <= 2,
        "manage_projects": level <= 2,
        # Operations
        "upload_images": level <= 3,
        "create_flights": level <= 3,
        "run_processing": level <= 3,
        # Reports
        "generate_reports": level <= 2,
        "export_data": level <= 2,
        # Viewing
        "view_projects": level <= 4,
        "view_maps": level <= 4,
        "view_reports": level <= 4,
        "view_dashboard": level <= 4,
        "view_public_data": level <= 4,
        # Admin
        "view_admin_panel": level <= 1,
        "view_monitoring": level <= 1,
        "view_subscriptions": level <= 1,
    }
