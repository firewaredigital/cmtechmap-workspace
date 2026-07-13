"""
CM TECHMAP — Tenant Resolution Middleware
Extracts tenant_id from JWT and sets the schema context for the request.
"""

import logging
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.database import current_tenant_schema, engine
from app.core.security import decode_jwt_token

logger = logging.getLogger(__name__)

# Routes that don't require tenant resolution
PUBLIC_PATHS = {"/api/v1/health", "/api/v1/health/ready", "/api/v1/auth/login",
                "/api/v1/auth/register", "/api/v1/subscriptions/plans",
                "/docs", "/openapi.json", "/redoc"}

# Simple cache for user → tenant mapping (cleared on restart)
_user_tenant_cache: dict[str, str] = {}


class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip tenant resolution for WebSocket connections.
        # BaseHTTPMiddleware is incompatible with the WebSocket protocol
        # upgrade — it wraps the connection as a regular HTTP request and
        # closes it before the handshake completes.
        if path.startswith("/ws/"):
            token = current_tenant_schema.set(None)
            try:
                return await call_next(request)
            finally:
                current_tenant_schema.reset(token)

        # Skip tenant resolution for public endpoints and metrics
        if path in PUBLIC_PATHS or path.startswith("/docs") or path.startswith("/redoc") or path == "/metrics":
            token = current_tenant_schema.set(None)
            try:
                return await call_next(request)
            finally:
                current_tenant_schema.reset(token)

        # Extract tenant from JWT
        auth_header = request.headers.get("Authorization", "")
        tenant_slug = None
        user_sub = None

        if auth_header.startswith("Bearer "):
            try:
                jwt_token = auth_header.split("Bearer ", 1)[1]
                payload = await decode_jwt_token(jwt_token)
                tenant_slug = payload.get("tenant_id")
                user_sub = payload.get("sub")

                # If tenant_id not in JWT, resolve from cache or DB
                if not tenant_slug and user_sub:
                    tenant_slug = await self._resolve_tenant_for_user(user_sub)
            except Exception:
                pass  # Let the route-level auth handle errors

        # Also allow X-Tenant-ID header (for super_admin operations)
        if not tenant_slug:
            tenant_slug = request.headers.get("X-Tenant-ID")

        schema_name = f"tenant_{tenant_slug}" if tenant_slug and tenant_slug != "platform" else None
        token = current_tenant_schema.set(schema_name)

        try:
            response = await call_next(request)
            return response
        finally:
            current_tenant_schema.reset(token)

    async def _resolve_tenant_for_user(self, keycloak_sub: str) -> str | None:
        """Resolve tenant slug for a user by searching all tenant schemas."""
        # Check cache first
        if keycloak_sub in _user_tenant_cache:
            return _user_tenant_cache[keycloak_sub]

        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        try:
            async with session_factory() as session:
                # Get all tenant schemas
                result = await session.execute(text(
                    "SELECT slug FROM public.tenants WHERE is_active = true"
                ))
                tenants = [row[0] for row in result.fetchall()]

                # Search for the user in each tenant schema
                for slug in tenants:
                    schema = f"tenant_{slug}"
                    try:
                        result = await session.execute(text(
                            f'SELECT 1 FROM "{schema}".users WHERE keycloak_id = :sub LIMIT 1'
                        ), {"sub": keycloak_sub})
                        if result.fetchone():
                            _user_tenant_cache[keycloak_sub] = slug
                            logger.info(f"Resolved tenant '{slug}' for user {keycloak_sub}")
                            return slug
                    except Exception:
                        continue

                # Fallback: if only one tenant exists, use it
                if len(tenants) == 1:
                    _user_tenant_cache[keycloak_sub] = tenants[0]
                    return tenants[0]

        except Exception as e:
            logger.warning(f"Failed to resolve tenant for user {keycloak_sub}: {e}")

        return None
