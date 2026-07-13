"""
CM TECHMAP — Security & JWT Handling
Keycloak JWT validation, JWKS caching, and role-based access control.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.backends import RSAKey

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Security scheme ───────────────────────────────────────────────────────────
bearer_scheme = HTTPBearer(auto_error=True)

# ── JWKS cache ────────────────────────────────────────────────────────────────
_jwks_cache: dict[str, Any] | None = None
_jwks_cache_time: datetime | None = None
_JWKS_CACHE_TTL_SECONDS = 3600  # Re-fetch JWKS every hour


async def _get_keycloak_jwks() -> dict[str, Any]:
    """
    Fetch and cache the JSON Web Key Set (JWKS) from Keycloak.
    The JWKS contains the public keys used to verify JWT signatures.
    """
    global _jwks_cache, _jwks_cache_time

    now = datetime.now(timezone.utc)
    if _jwks_cache and _jwks_cache_time and (now - _jwks_cache_time).seconds < _JWKS_CACHE_TTL_SECONDS:
        return _jwks_cache

    jwks_url = (
        f"{settings.keycloak_server_url}/realms/{settings.keycloak_realm}"
        f"/protocol/openid-connect/certs"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(jwks_url)
            response.raise_for_status()
            _jwks_cache = response.json()
            _jwks_cache_time = now
            logger.info("JWKS refreshed from Keycloak")
            return _jwks_cache
    except httpx.HTTPError as e:
        logger.error(f"Failed to fetch JWKS from Keycloak: {e}")
        if _jwks_cache:
            logger.warning("Using stale JWKS cache")
            return _jwks_cache
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )


def _find_rsa_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    """Find the RSA key matching the JWT's Key ID (kid)."""
    for key in jwks.get("keys", []):
        if key.get("kid") == kid and key.get("kty") == "RSA":
            return key
    return None


async def decode_jwt_token(token: str) -> dict[str, Any]:
    """
    Decode and validate a JWT token issued by Keycloak.

    Performs:
    1. Fetch/cache the JWKS from Keycloak
    2. Match the token's `kid` to a public key
    3. Verify signature, expiration, and issuer
    4. Return the decoded claims
    """
    try:
        # Extract header to get the Key ID
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token missing key ID (kid)",
            )

        # Get JWKS and find matching key
        jwks = await _get_keycloak_jwks()
        rsa_key_data = _find_rsa_key(jwks, kid)

        if not rsa_key_data:
            # Key not found — maybe Keycloak rotated keys. Force refresh.
            _jwks_cache_time_reset()
            jwks = await _get_keycloak_jwks()
            rsa_key_data = _find_rsa_key(jwks, kid)
            if not rsa_key_data:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unable to find matching signing key",
                )

        # Build the RSA public key
        rsa_key = RSAKey(rsa_key_data, algorithm="RS256")

        # Decode and verify — first try without audience check, then validate manually.
        # Keycloak tokens may have aud="account" instead of the client_id, depending
        # on the realm configuration. The authorized party (azp) claim always matches
        # the client_id, so we use that as the primary validation.
        issuer = f"{settings.keycloak_external_url}/realms/{settings.keycloak_realm}"
        payload: dict[str, Any] = jwt.decode(
            token,
            rsa_key.to_dict(),
            algorithms=["RS256"],
            issuer=issuer,
            options={
                "verify_aud": False,  # We verify manually below
                "verify_iss": True,
                "verify_exp": True,
            },
        )

        # Manual audience/azp validation
        token_aud = payload.get("aud", "")
        token_azp = payload.get("azp", "")
        client_id = settings.keycloak_client_id

        # Accept if: aud matches, OR azp matches, OR aud is "account" (Keycloak default)
        aud_list = [token_aud] if isinstance(token_aud, str) else (token_aud or [])
        is_valid_audience = (
            client_id in aud_list
            or token_azp == client_id
            or "account" in aud_list
        )

        if not is_valid_audience:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token audience: {token_aud}",
            )

        return payload

    except JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )


def _jwks_cache_time_reset() -> None:
    """Force JWKS cache refresh on next call."""
    global _jwks_cache_time
    _jwks_cache_time = None


def extract_user_info(token_payload: dict[str, Any]) -> dict[str, Any]:
    """
    Extract standardized user information from a decoded Keycloak JWT.

    Returns:
        dict with keys: sub, email, name, preferred_username, roles, tenant_id
    """
    # Keycloak stores realm roles in realm_access.roles
    realm_access = token_payload.get("realm_access", {})
    roles = realm_access.get("roles", [])

    # Also check our custom claim for roles (set by protocol mapper)
    custom_roles = token_payload.get("roles", [])
    if custom_roles:
        roles = list(set(roles + custom_roles))

    return {
        "sub": token_payload.get("sub", ""),
        "email": token_payload.get("email", ""),
        "name": token_payload.get("name", ""),
        "preferred_username": token_payload.get("preferred_username", ""),
        "roles": roles,
        "tenant_id": token_payload.get("tenant_id", ""),
        "email_verified": token_payload.get("email_verified", False),
    }


def check_roles(user_roles: list[str], required_roles: list[str]) -> bool:
    """
    Check if the user has at least one of the required roles.
    super_admin always passes.
    """
    if "super_admin" in user_roles:
        return True
    return bool(set(user_roles) & set(required_roles))


async def get_current_user_from_request(request: Request) -> dict[str, Any]:
    """
    Extract and validate the current user from the request's Authorization header.
    This is the main dependency used in route handlers.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header.split("Bearer ", 1)[1]
    payload = await decode_jwt_token(token)
    return extract_user_info(payload)


def get_user_portal(roles: list[str]) -> str:
    """
    Determine which portal the user belongs to based on their roles.

    Returns:
        'admin' for super_admin users (platform operators)
        'prefeitura' for all other roles (municipality users)
    """
    if "super_admin" in roles:
        return "admin"
    return "prefeitura"

