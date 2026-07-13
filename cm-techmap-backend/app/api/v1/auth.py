"""CM TECHMAP — Auth Routes (Keycloak token exchange)"""

import logging
from fastapi import APIRouter, HTTPException, Request, status
import httpx

from app.config import get_settings
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, TokenRefreshRequest

router = APIRouter(prefix="/auth", tags=["Authentication"])
settings = get_settings()
logger = logging.getLogger(__name__)

TOKEN_URL = (
    f"{settings.keycloak_server_url}/realms/{settings.keycloak_realm}"
    f"/protocol/openid-connect/token"
)


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT payload without signature verification (for extracting user info from a freshly-obtained token)."""
    import base64
    import json
    try:
        payload_b64 = token.split(".")[1]
        # Fix base64 padding
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return {}


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request):
    """Authenticate via Keycloak using Resource Owner Password Credentials grant."""
    from app.services.audit_log import AuditAction, extract_request_context
    req_ctx = extract_request_context(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(TOKEN_URL, data={
                "grant_type": "password",
                "client_id": settings.keycloak_client_id,
                "client_secret": settings.keycloak_client_secret,
                "username": body.email,
                "password": body.password,
                "scope": "openid",
            })
        except httpx.HTTPError as e:
            logger.error(f"Keycloak connection failed: {e}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Serviço de autenticação indisponível")

    if resp.status_code != 200:
        kc_error = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        error_desc = kc_error.get("error_description", "")

        # If Keycloak itself crashed (5xx), report as service unavailable — NOT as auth failure
        if resp.status_code >= 500:
            logger.error(
                f"Keycloak internal error on login for {body.email}: "
                f"HTTP {resp.status_code} — {error_desc or resp.text[:200]}"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Serviço de autenticação instável. Tente novamente em alguns segundos.",
            )

        # Translate common Keycloak 4xx errors to Portuguese
        if "invalid" in error_desc.lower() or "credentials" in error_desc.lower():
            detail = "Email ou senha incorretos"
        elif "disabled" in error_desc.lower():
            detail = "Conta desativada. Contate o administrador."
        elif "locked" in error_desc.lower():
            detail = "Conta bloqueada temporariamente. Tente novamente mais tarde."
        else:
            detail = error_desc or "Falha na autenticação"

        # Audit: log failed login attempt
        logger.info(f"Login failed for {body.email}: {detail}")

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    data = resp.json()

    # Extract user info from the JWT to return to the frontend
    payload = _decode_jwt_payload(data["access_token"])
    user_info = None
    if payload:
        realm_roles = payload.get("realm_access", {}).get("roles", [])
        # Pick the most relevant role (priority order)
        role = "viewer"
        for r in ["super_admin", "tenant_admin", "gestor", "operador", "viewer"]:
            if r in realm_roles:
                role = r
                break

        user_info = {
            "id": payload.get("sub", ""),
            "name": payload.get("name", payload.get("preferred_username", "")),
            "email": payload.get("email", ""),
            "role": role,
            "portal": "admin" if role == "super_admin" else "prefeitura",
        }

    # Audit: log successful login
    logger.info(f"Login success for {body.email} (sub={payload.get('sub', '?')})")

    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type="bearer",
        expires_in=data.get("expires_in", 900),
        user=user_info,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(body: TokenRefreshRequest):
    """Refresh an expired access token via Keycloak."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(TOKEN_URL, data={
                "grant_type": "refresh_token",
                "client_id": settings.keycloak_client_id,
                "client_secret": settings.keycloak_client_secret,
                "refresh_token": body.refresh_token,
            })
        except httpx.HTTPError as e:
            logger.error(f"Keycloak refresh failed: {e}")
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Serviço de autenticação indisponível")

    if resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Sessão expirada. Faça login novamente.")

    data = resp.json()
    payload = _decode_jwt_payload(data["access_token"])
    user_info = None
    if payload:
        user_info = {
            "id": payload.get("sub", ""),
            "name": payload.get("name", ""),
            "email": payload.get("email", ""),
            "role": "viewer",
        }

    return TokenResponse(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        token_type="bearer",
        expires_in=data.get("expires_in", 900),
        user=user_info,
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """
    Register a new user in Keycloak.
    Creates the user, sets their password, and assigns the 'viewer' role.
    """
    # Get admin token for Keycloak API
    admin_token = await _get_admin_token()
    if not admin_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Serviço de identidade indisponível. Tente novamente.")

    users_url = (
        f"{settings.keycloak_server_url}/admin/realms/{settings.keycloak_realm}/users"
    )

    # Build user payload for Keycloak
    name_parts = body.name.strip().split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else first_name

    user_payload = {
        "username": body.email,
        "email": body.email,
        "firstName": first_name,
        "lastName": last_name,
        "enabled": True,
        "emailVerified": True,  # Skip email verification for dev
        "credentials": [{"type": "password", "value": body.password, "temporary": False}],
        "attributes": {"tenant_id": [body.tenant_slug or "default"]},
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: Create the user
        resp = await client.post(users_url, json=user_payload,
                                 headers={"Authorization": f"Bearer {admin_token}"})

    if resp.status_code == 409:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Este email já está cadastrado. Tente fazer login.")
    if resp.status_code not in (200, 201):
        logger.error(f"Keycloak user creation failed: {resp.status_code} {resp.text}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Erro ao criar conta. Tente novamente.")

    # Step 2: Assign the 'viewer' role to the new user
    try:
        await _assign_viewer_role(admin_token, body.email)
    except Exception as e:
        logger.warning(f"Could not assign viewer role: {e}")
        # Not fatal — user was created, just without explicit role

    return {"message": "Conta criada com sucesso!", "email": body.email}


async def _assign_viewer_role(admin_token: str, username: str) -> None:
    """Find the new user by username and assign the 'viewer' realm role."""
    base_url = f"{settings.keycloak_server_url}/admin/realms/{settings.keycloak_realm}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # Find user by username
        resp = await client.get(f"{base_url}/users", params={"username": username, "exact": "true"},
                                headers=headers)
        if resp.status_code != 200 or not resp.json():
            return
        user_id = resp.json()[0]["id"]

        # Get the viewer role
        resp = await client.get(f"{base_url}/roles/viewer", headers=headers)
        if resp.status_code != 200:
            # Role might not exist — create it
            await client.post(f"{base_url}/roles", json={"name": "viewer"}, headers=headers)
            resp = await client.get(f"{base_url}/roles/viewer", headers=headers)
            if resp.status_code != 200:
                return
        viewer_role = resp.json()

        # Assign role to user
        await client.post(
            f"{base_url}/users/{user_id}/role-mappings/realm",
            json=[viewer_role],
            headers=headers,
        )


async def _get_admin_token() -> str | None:
    """Get a Keycloak admin access token for user management operations."""
    admin_token_url = f"{settings.keycloak_server_url}/realms/master/protocol/openid-connect/token"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(admin_token_url, data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": settings.keycloak_admin_username,
                "password": settings.keycloak_admin_password,
            })
            if resp.status_code == 200:
                return resp.json()["access_token"]
    except httpx.HTTPError:
        pass
    return None
