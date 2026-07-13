"""
CM TECHMAP — Keycloak User Management Service
Full lifecycle management of users via Keycloak Admin REST API.
Handles CRUD, role assignment, status management, and audit logging.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger("cm_techmap.services.keycloak_admin")
settings = get_settings()

# ── Constants ─────────────────────────────────────────────────────────────────
KC_BASE = settings.keycloak_server_url
KC_REALM = settings.keycloak_realm
KC_ADMIN_URL = f"{KC_BASE}/admin/realms/{KC_REALM}"
KC_TOKEN_URL = f"{KC_BASE}/realms/master/protocol/openid-connect/token"

PLATFORM_ROLES = ["super_admin", "tenant_admin", "gestor", "operador", "viewer"]

ROLE_HIERARCHY = {
    "super_admin": 0,
    "tenant_admin": 1,
    "gestor": 2,
    "operador": 3,
    "viewer": 4,
}


class KeycloakAdminError(Exception):
    """Exception raised for Keycloak Admin API errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class KeycloakAdminService:
    """
    Comprehensive Keycloak Admin API client.
    Manages users, roles, and sessions for the CM TECHMAP platform.
    """

    def __init__(self):
        self._admin_token: str | None = None
        self._token_expires: datetime | None = None

    # ── Token Management ──────────────────────────────────────────────────

    async def _get_admin_token(self) -> str:
        """
        Obtain an admin access token from Keycloak master realm.
        Caches token until near expiry.
        """
        now = datetime.now(timezone.utc)
        if self._admin_token and self._token_expires and now < self._token_expires:
            return self._admin_token

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(KC_TOKEN_URL, data={
                    "grant_type": "password",
                    "client_id": "admin-cli",
                    "username": "admin",
                    "password": "admin_dev_2026",
                })
                if resp.status_code != 200:
                    raise KeycloakAdminError(
                        f"Failed to obtain admin token: {resp.status_code}",
                        status_code=503,
                    )
                data = resp.json()
                self._admin_token = data["access_token"]
                # Cache with 30s margin
                from datetime import timedelta
                self._token_expires = now + timedelta(seconds=data.get("expires_in", 60) - 30)
                return self._admin_token
        except httpx.HTTPError as e:
            raise KeycloakAdminError(f"Keycloak unreachable: {e}", status_code=503)

    async def _headers(self) -> dict[str, str]:
        token = await self._get_admin_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    # ── User CRUD ─────────────────────────────────────────────────────────

    async def list_users(
        self,
        search: str = "",
        first: int = 0,
        max_results: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """
        List users with pagination and optional search.
        Returns (users, total_count).
        """
        headers = await self._headers()
        params: dict[str, Any] = {"first": first, "max": max_results, "briefRepresentation": False}
        if search:
            params["search"] = search

        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get users
            resp = await client.get(f"{KC_ADMIN_URL}/users", headers=headers, params=params)
            if resp.status_code != 200:
                raise KeycloakAdminError(f"Failed to list users: {resp.text}", resp.status_code)
            users = resp.json()

            # Get total count
            count_resp = await client.get(
                f"{KC_ADMIN_URL}/users/count",
                headers=headers,
                params={"search": search} if search else {},
            )
            total = count_resp.json() if count_resp.status_code == 200 else len(users)

        # Enrich with role mappings
        enriched = []
        for user in users:
            user_roles = await self._get_user_realm_roles(user["id"], headers)
            platform_role = self._extract_platform_role(user_roles)
            enriched.append(self._format_user(user, platform_role, user_roles))

        return enriched, total

    async def get_user(self, user_id: str) -> dict[str, Any]:
        """Get a single user by ID with full details."""
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{KC_ADMIN_URL}/users/{user_id}", headers=headers)
            if resp.status_code == 404:
                raise KeycloakAdminError("User not found", 404)
            if resp.status_code != 200:
                raise KeycloakAdminError(f"Failed to get user: {resp.text}", resp.status_code)
            user = resp.json()

        user_roles = await self._get_user_realm_roles(user_id, headers)
        platform_role = self._extract_platform_role(user_roles)
        result = self._format_user(user, platform_role, user_roles)

        # Add session info
        sessions = await self._get_user_sessions(user_id, headers)
        result["active_sessions"] = len(sessions)
        result["sessions"] = sessions

        return result

    async def create_user(
        self,
        email: str,
        first_name: str,
        last_name: str,
        password: str,
        role: str = "viewer",
        tenant_id: str = "",
        department: str = "",
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Create a new user in Keycloak with role and tenant assignment."""
        if role not in PLATFORM_ROLES:
            raise KeycloakAdminError(f"Invalid role: {role}. Must be one of {PLATFORM_ROLES}", 400)

        headers = await self._headers()
        user_payload = {
            "username": email,
            "email": email,
            "firstName": first_name,
            "lastName": last_name,
            "enabled": enabled,
            "emailVerified": True,
            "credentials": [{"type": "password", "value": password, "temporary": False}],
            "attributes": {
                "tenant_id": [tenant_id] if tenant_id else [],
                "department": [department] if department else [],
                "created_by": ["api"],
            },
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{KC_ADMIN_URL}/users",
                headers=headers,
                json=user_payload,
            )
            if resp.status_code == 409:
                raise KeycloakAdminError("User with this email already exists", 409)
            if resp.status_code not in (200, 201):
                raise KeycloakAdminError(f"Failed to create user: {resp.text}", resp.status_code)

            # Extract user ID from Location header
            location = resp.headers.get("Location", "")
            user_id = location.rsplit("/", 1)[-1] if location else ""

        if not user_id:
            # Fallback: search by email
            users = await self._search_by_email(email, headers)
            if users:
                user_id = users[0]["id"]

        # Assign role
        if user_id and role:
            await self._assign_realm_role(user_id, role, headers)

        logger.info(f"User created: {email} (role={role}, tenant={tenant_id})")
        return await self.get_user(user_id)

    async def update_user(
        self,
        user_id: str,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        enabled: bool | None = None,
        department: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Update user attributes in Keycloak."""
        headers = await self._headers()

        # Fetch current user
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{KC_ADMIN_URL}/users/{user_id}", headers=headers)
            if resp.status_code == 404:
                raise KeycloakAdminError("User not found", 404)
            current = resp.json()

        # Build update payload
        update: dict[str, Any] = {}
        if first_name is not None:
            update["firstName"] = first_name
        if last_name is not None:
            update["lastName"] = last_name
        if email is not None:
            update["email"] = email
            update["username"] = email
        if enabled is not None:
            update["enabled"] = enabled

        # Merge attributes
        attrs = current.get("attributes", {})
        if department is not None:
            attrs["department"] = [department]
        if tenant_id is not None:
            attrs["tenant_id"] = [tenant_id]
        if attrs:
            update["attributes"] = attrs

        if update:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.put(
                    f"{KC_ADMIN_URL}/users/{user_id}",
                    headers=headers,
                    json=update,
                )
                if resp.status_code not in (200, 204):
                    raise KeycloakAdminError(f"Failed to update user: {resp.text}", resp.status_code)

        logger.info(f"User updated: {user_id}")
        return await self.get_user(user_id)

    async def delete_user(self, user_id: str) -> None:
        """Delete a user from Keycloak permanently."""
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{KC_ADMIN_URL}/users/{user_id}", headers=headers)
            if resp.status_code == 404:
                raise KeycloakAdminError("User not found", 404)
            if resp.status_code not in (200, 204):
                raise KeycloakAdminError(f"Failed to delete user: {resp.text}", resp.status_code)
        logger.info(f"User deleted: {user_id}")

    # ── Role Management ──────────────────────────────────────────────────

    async def change_user_role(self, user_id: str, new_role: str) -> dict[str, Any]:
        """
        Change a user's platform role.
        Removes all existing platform roles and assigns the new one.
        """
        if new_role not in PLATFORM_ROLES:
            raise KeycloakAdminError(f"Invalid role: {new_role}", 400)

        headers = await self._headers()

        # Remove existing platform roles
        current_roles = await self._get_user_realm_roles(user_id, headers)
        platform_roles_to_remove = [
            r for r in current_roles if r["name"] in PLATFORM_ROLES
        ]
        if platform_roles_to_remove:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{KC_ADMIN_URL}/users/{user_id}/role-mappings/realm",
                    headers=headers,
                    json=platform_roles_to_remove,
                )

        # Assign new role
        await self._assign_realm_role(user_id, new_role, headers)
        logger.info(f"User {user_id} role changed to {new_role}")
        return await self.get_user(user_id)

    async def get_available_roles(self) -> list[dict[str, Any]]:
        """Get all platform roles with descriptions."""
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{KC_ADMIN_URL}/roles", headers=headers)
            if resp.status_code != 200:
                raise KeycloakAdminError(f"Failed to list roles: {resp.text}", resp.status_code)
            all_roles = resp.json()

        return [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description", ""),
                "hierarchy": ROLE_HIERARCHY.get(r["name"], 99),
            }
            for r in all_roles
            if r["name"] in PLATFORM_ROLES
        ]

    # ── Session Management ───────────────────────────────────────────────

    async def logout_user(self, user_id: str) -> None:
        """Terminate all active sessions for a user."""
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{KC_ADMIN_URL}/users/{user_id}/logout",
                headers=headers,
            )
            if resp.status_code not in (200, 204):
                raise KeycloakAdminError(f"Failed to logout user: {resp.text}", resp.status_code)
        logger.info(f"User {user_id} sessions terminated")

    async def reset_password(self, user_id: str, new_password: str, temporary: bool = False) -> None:
        """Reset a user's password."""
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.put(
                f"{KC_ADMIN_URL}/users/{user_id}/reset-password",
                headers=headers,
                json={"type": "password", "value": new_password, "temporary": temporary},
            )
            if resp.status_code not in (200, 204):
                raise KeycloakAdminError(f"Failed to reset password: {resp.text}", resp.status_code)
        logger.info(f"Password reset for user {user_id}")

    # ── Statistics ────────────────────────────────────────────────────────

    async def get_user_stats(self) -> dict[str, Any]:
        """Get aggregate user statistics."""
        headers = await self._headers()
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Total users
            count_resp = await client.get(f"{KC_ADMIN_URL}/users/count", headers=headers)
            total = count_resp.json() if count_resp.status_code == 200 else 0

            # Active sessions
            sessions_resp = await client.get(
                f"{KC_ADMIN_URL}/client-session-stats",
                headers=headers,
            )
            active_sessions = 0
            if sessions_resp.status_code == 200:
                for s in sessions_resp.json():
                    active_sessions += s.get("active", 0)

        # Role breakdown (sample first 200 users)
        users, _ = await self.list_users(max_results=200)
        role_counts: dict[str, int] = {role: 0 for role in PLATFORM_ROLES}
        enabled_count = 0
        disabled_count = 0
        for u in users:
            role = u.get("role", "viewer")
            if role in role_counts:
                role_counts[role] += 1
            if u.get("enabled"):
                enabled_count += 1
            else:
                disabled_count += 1

        return {
            "total_users": total,
            "active_sessions": active_sessions,
            "enabled_users": enabled_count,
            "disabled_users": disabled_count,
            "role_breakdown": role_counts,
        }

    # ── Private Helpers ──────────────────────────────────────────────────

    async def _get_user_realm_roles(
        self, user_id: str, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Get realm role mappings for a user."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KC_ADMIN_URL}/users/{user_id}/role-mappings/realm",
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            return resp.json()

    async def _assign_realm_role(
        self, user_id: str, role_name: str, headers: dict[str, str]
    ) -> None:
        """Assign a realm role to a user."""
        # First, get the role representation
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{KC_ADMIN_URL}/roles/{role_name}", headers=headers)
            if resp.status_code != 200:
                raise KeycloakAdminError(f"Role '{role_name}' not found in Keycloak", 404)
            role_data = resp.json()

            # Assign
            resp = await client.post(
                f"{KC_ADMIN_URL}/users/{user_id}/role-mappings/realm",
                headers=headers,
                json=[{"id": role_data["id"], "name": role_data["name"]}],
            )
            if resp.status_code not in (200, 204):
                raise KeycloakAdminError(
                    f"Failed to assign role {role_name}: {resp.text}", resp.status_code
                )

    async def _search_by_email(
        self, email: str, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Search users by exact email."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KC_ADMIN_URL}/users",
                headers=headers,
                params={"email": email, "exact": True},
            )
            return resp.json() if resp.status_code == 200 else []

    async def _get_user_sessions(
        self, user_id: str, headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Get active sessions for a user."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{KC_ADMIN_URL}/users/{user_id}/sessions",
                headers=headers,
            )
            if resp.status_code != 200:
                return []
            return [
                {
                    "id": s.get("id"),
                    "ip_address": s.get("ipAddress"),
                    "started": s.get("start"),
                    "last_access": s.get("lastAccess"),
                    "clients": s.get("clients", {}),
                }
                for s in resp.json()
            ]

    def _extract_platform_role(self, roles: list[dict[str, Any]]) -> str:
        """Extract the highest-priority platform role from a list of realm roles."""
        user_platform_roles = [r["name"] for r in roles if r["name"] in PLATFORM_ROLES]
        if not user_platform_roles:
            return "viewer"
        # Return highest priority (lowest number)
        return min(user_platform_roles, key=lambda r: ROLE_HIERARCHY.get(r, 99))

    def _format_user(
        self, raw: dict[str, Any], platform_role: str, roles: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Format a Keycloak user into a standardized API response."""
        attrs = raw.get("attributes", {})
        return {
            "id": raw["id"],
            "username": raw.get("username", ""),
            "email": raw.get("email", ""),
            "first_name": raw.get("firstName", ""),
            "last_name": raw.get("lastName", ""),
            "full_name": f"{raw.get('firstName', '')} {raw.get('lastName', '')}".strip(),
            "enabled": raw.get("enabled", False),
            "email_verified": raw.get("emailVerified", False),
            "role": platform_role,
            "all_roles": [r["name"] for r in roles],
            "tenant_id": (attrs.get("tenant_id") or [""])[0],
            "department": (attrs.get("department") or [""])[0],
            "created_at": raw.get("createdTimestamp"),
            "totp_enabled": raw.get("totp", False),
        }


# Singleton instance
keycloak_admin = KeycloakAdminService()
