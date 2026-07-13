"""
CM TECHMAP — Rate Limiting Tests
Tests that the rate limiter correctly rejects excessive requests.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_rate_limit_login(client: AsyncClient):
    """Login endpoint should be rate limited after excessive attempts."""
    # Send 15 rapid login attempts (limit is 10/min)
    responses = []
    for i in range(15):
        resp = await client.post("/api/v1/auth/login", json={
            "email": f"ratelimit-test-{i}@test.com",
            "password": "test",
        })
        responses.append(resp)

    # At least one should be rate limited (429)
    status_codes = [r.status_code for r in responses]
    assert 429 in status_codes, (
        f"Expected at least one 429 response, got: {status_codes}"
    )


@pytest.mark.asyncio
async def test_rate_limit_headers(client: AsyncClient):
    """Successful requests should include rate limit headers."""
    resp = await client.post("/api/v1/auth/login", json={
        "email": "test-headers@test.com",
        "password": "test",
    })
    # Response may be 401 (bad creds) or 503 (KC down), but headers should be present
    if resp.status_code != 429:
        assert "X-RateLimit-Limit" in resp.headers
        assert "X-RateLimit-Remaining" in resp.headers


@pytest.mark.asyncio
async def test_rate_limit_register(client: AsyncClient):
    """Register endpoint has stricter rate limits (5/5min)."""
    responses = []
    for i in range(8):
        resp = await client.post("/api/v1/auth/register", json={
            "email": f"ratelimit-reg-{i}@test.com",
            "name": f"Test User {i}",
            "password": "TestPass123!",
        })
        responses.append(resp)

    status_codes = [r.status_code for r in responses]
    assert 429 in status_codes, (
        f"Expected register rate limit (5/5min), got: {status_codes}"
    )
