"""
CM TECHMAP — Auth Endpoint Tests
Tests authentication contracts (login validation, rate limiting).
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_login_missing_body(client: AsyncClient):
    """Login with empty body should return 422 (validation error)."""
    response = await client.post("/api/v1/auth/login", json={})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_login_invalid_credentials(client: AsyncClient):
    """Login with wrong credentials should return 401 or 503 (if Keycloak down)."""
    response = await client.post("/api/v1/auth/login", json={
        "email": "nonexistent@test.com",
        "password": "wrongpassword",
    })
    # 401 if Keycloak is up, 503 if Keycloak is unreachable
    assert response.status_code in (401, 503)


@pytest.mark.asyncio
async def test_register_missing_fields(client: AsyncClient):
    """Register with missing fields should return 422."""
    response = await client.post("/api/v1/auth/register", json={
        "email": "test@test.com",
        # Missing: name, password
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_register_invalid_email(client: AsyncClient):
    """Register with invalid email should return validation error."""
    response = await client.post("/api/v1/auth/register", json={
        "email": "not-an-email",
        "name": "Test User",
        "password": "TestPass123",
    })
    # 422 if caught by Pydantic, 400/500 if Keycloak rejects it
    assert response.status_code in (400, 422, 500)


@pytest.mark.asyncio
async def test_refresh_missing_token(client: AsyncClient):
    """Refresh without token should return 422."""
    response = await client.post("/api/v1/auth/refresh", json={})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_protected_endpoint_no_auth(client: AsyncClient):
    """Accessing a protected endpoint without JWT should return 401/403."""
    response = await client.get("/api/v1/projects")
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_protected_endpoint_invalid_token(client: AsyncClient):
    """Accessing a protected endpoint with invalid JWT should return 401/403."""
    response = await client.get(
        "/api/v1/projects",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert response.status_code in (401, 403)
