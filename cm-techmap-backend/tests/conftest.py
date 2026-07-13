"""
CM TECHMAP — Backend Test Suite Configuration
Pytest fixtures, tenant factory, and shared test infrastructure.
Supports both unit tests (mock DB) and integration tests (real DB via Docker).
"""

import asyncio
import uuid
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, MagicMock, patch

from app.main import app
from app.config import get_settings


# ══════════════════════════════════════════════════════════════════════════════
# Event Loop
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create an event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ══════════════════════════════════════════════════════════════════════════════
# HTTP Client
# ══════════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for testing FastAPI endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def auth_client() -> AsyncGenerator[AsyncClient, None]:
    """Authenticated HTTP client with mock JWT token."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={
            "Authorization": "Bearer test-token-super-admin",
            "X-Tenant-ID": "test-municipality",
        },
    ) as ac:
        yield ac


# ══════════════════════════════════════════════════════════════════════════════
# Settings
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def settings():
    """Application settings fixture."""
    return get_settings()


# ══════════════════════════════════════════════════════════════════════════════
# Mock User Payloads
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def super_admin_user() -> dict:
    """Mock super_admin user payload."""
    return {
        "sub": str(uuid.uuid4()),
        "email": "admin@cmtechmap.com",
        "name": "Super Admin",
        "roles": ["super_admin"],
        "tenant_id": "platform",
    }


@pytest.fixture
def tenant_admin_user() -> dict:
    """Mock tenant_admin user payload."""
    return {
        "sub": str(uuid.uuid4()),
        "email": "admin@goiania.gov.br",
        "name": "Admin Goiânia",
        "roles": ["tenant_admin"],
        "tenant_id": "goiania",
    }


@pytest.fixture
def gestor_user() -> dict:
    """Mock gestor user payload."""
    return {
        "sub": str(uuid.uuid4()),
        "email": "gestor@goiania.gov.br",
        "name": "Gestor Goiânia",
        "roles": ["gestor"],
        "tenant_id": "goiania",
    }


@pytest.fixture
def viewer_user() -> dict:
    """Mock viewer user payload."""
    return {
        "sub": str(uuid.uuid4()),
        "email": "viewer@goiania.gov.br",
        "name": "Viewer Goiânia",
        "roles": ["viewer"],
        "tenant_id": "goiania",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Tenant Test Data Factory
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def tenant_goiania() -> dict:
    """Test tenant: Goiânia."""
    return {
        "name": "Prefeitura de Goiânia",
        "slug": "goiania",
        "cnpj": "01.610.560/0001-24",
        "city": "Goiânia",
        "state": "GO",
        "contact_email": "fiscal@goiania.gov.br",
    }


@pytest.fixture
def tenant_anapolis() -> dict:
    """Test tenant: Anápolis."""
    return {
        "name": "Prefeitura de Anápolis",
        "slug": "anapolis",
        "cnpj": "02.457.730/0001-14",
        "city": "Anápolis",
        "state": "GO",
        "contact_email": "fiscal@anapolis.gov.br",
    }


@pytest.fixture
def sample_project() -> dict:
    """Sample project data."""
    return {
        "code": "PRJ-2026-001",
        "name": "Levantamento Setor Bueno",
        "description": "Levantamento aerofotogramétrico do Setor Bueno - Goiânia/GO",
        "status": "pendente",
        "city": "Goiânia",
        "state": "GO",
        "bbox_min_lon": -49.29,
        "bbox_min_lat": -16.72,
        "bbox_max_lon": -49.25,
        "bbox_max_lat": -16.68,
    }


@pytest.fixture
def sample_flight() -> dict:
    """Sample flight data."""
    return {
        "flight_date": "2026-05-15",
        "altitude_m": 120.0,
        "overlap_pct": 80.0,
        "sidelap_pct": 70.0,
        "images_count": 450,
        "camera_model": "DJI Mavic 3E",
        "gsd_cm": 2.5,
    }


@pytest.fixture
def sample_discrepancy() -> dict:
    """Sample discrepancy data."""
    return {
        "discrepancy_type": "construction_expansion",
        "severity": "high",
        "status": "pending",
        "cadastral_code": "GOI-SB-00123",
        "address": "Rua T-37, 1234, Setor Bueno",
        "neighborhood": "Setor Bueno",
        "owner_name": "João da Silva",
        "registered_area_sqm": 200.0,
        "detected_area_sqm": 350.0,
        "difference_sqm": 150.0,
        "difference_pct": 75.0,
        "confidence": 0.95,
        "iptu_current_brl": 2500.0,
        "iptu_proposed_brl": 4375.0,
        "estimated_iptu_gap_brl": 1875.0,
    }


@pytest.fixture
def sample_iptu_rule() -> dict:
    """Sample IPTU rule data."""
    return {
        "municipality_code": "5208707",
        "municipality_name": "Goiânia",
        "year": 2026,
        "base_rate_per_sqm": 12.50,
        "built_rate_per_sqm": 25.00,
        "zone_name": "Zona Residencial 1",
        "zone_aliquot": 1.0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Mock DB Session
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def mock_db_session():
    """Mock async database session for unit tests."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.close = AsyncMock()
    return session


# ══════════════════════════════════════════════════════════════════════════════
# Auth Mock Helper
# ══════════════════════════════════════════════════════════════════════════════

def mock_auth(user_payload: dict):
    """
    Context manager that mocks authentication to return a specific user.
    Usage:
        with mock_auth(super_admin_user):
            response = await client.get("/api/v1/admin/tenants")
    """
    return patch(
        "app.core.security.get_current_user_from_request",
        return_value=user_payload,
    )
