"""
CM TECHMAP — Health & Root Endpoint Tests
Tests the most basic API contracts: root, health, ready.
"""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_root_endpoint(client: AsyncClient):
    """Root should return service info."""
    response = await client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "CM TECHMAP API"
    assert "version" in data
    assert data["docs"] == "/docs"


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    """Health endpoint should return status even if DB is down."""
    response = await client.get("/api/v1/health")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data


@pytest.mark.asyncio
async def test_health_ready_endpoint(client: AsyncClient):
    """Ready endpoint returns full service health."""
    response = await client.get("/api/v1/health/ready")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data
    assert "services" in data
    assert "version" in data


@pytest.mark.asyncio
async def test_metrics_endpoint(client: AsyncClient):
    """Prometheus metrics endpoint should return text metrics."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "cm_techmap" in response.text
