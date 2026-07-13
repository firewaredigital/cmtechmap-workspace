"""
CM TECHMAP — Configuration Tests
Tests that the settings module loads correctly and validates critical settings.
"""

import pytest
from app.config import get_settings, Settings


def test_settings_load():
    """Settings should load without errors."""
    settings = get_settings()
    assert settings is not None
    assert settings.app_name == "CM TECHMAP"


def test_settings_cors_list():
    """CORS origins should parse into a list."""
    settings = get_settings()
    origins = settings.cors_origins_list
    assert isinstance(origins, list)
    assert len(origins) > 0


def test_settings_keycloak_admin_exists():
    """Keycloak admin credentials should be configurable."""
    settings = get_settings()
    assert hasattr(settings, "keycloak_admin_username")
    assert hasattr(settings, "keycloak_admin_password")
    assert settings.keycloak_admin_username  # Not empty


def test_settings_is_production():
    """is_production property should work correctly."""
    settings = get_settings()
    # In test, we're always in development
    assert settings.is_production is False


def test_settings_database_urls():
    """Database URLs should be configured."""
    settings = get_settings()
    assert "postgresql" in settings.database_url
    assert "postgresql" in settings.database_url_sync


def test_settings_minio_buckets():
    """All MinIO bucket names should be configured."""
    settings = get_settings()
    assert settings.minio_bucket_raw_uploads
    assert settings.minio_bucket_orthomosaics
    assert settings.minio_bucket_reports
    assert settings.minio_bucket_elevation_models


def test_settings_groq_fields_present():
    """Groq integration settings should exist for hybrid orchestration."""
    settings = get_settings()
    assert hasattr(settings, "groq_enabled")
    assert hasattr(settings, "ai_provider")
    assert hasattr(settings, "groq_vision_model")
    assert hasattr(settings, "groq_report_model")


def test_settings_groq_safe_defaults():
    """Defaults should be conservative and backward-compatible."""
    settings = get_settings()
    assert settings.ai_provider in {"local", "groq", "hybrid"}
    assert settings.groq_rpm_limit_free >= 1
    assert settings.groq_tpm_limit_free >= 100


def test_settings_groq_adaptive_and_breaker_defaults():
    """Adaptive/breaker controls must be present with sane values."""
    settings = get_settings()
    assert 0 < settings.groq_safety_factor <= 1
    assert 0 < settings.groq_min_utilization_factor <= 1
    assert settings.groq_circuit_breaker_failures >= 1
    assert settings.groq_circuit_breaker_open_seconds >= 1


def test_settings_groq_tiling_fairness_and_telemetry_defaults():
    """Advanced Groq controls for tiling/fairness/telemetry must be available."""
    settings = get_settings()
    assert settings.groq_vision_tile_size_px >= 256
    assert settings.groq_vision_max_tiles >= 1
    assert settings.groq_vision_tile_max_completion_tokens >= 64
    assert 0 < settings.groq_fairness_entity_share <= 1
    assert settings.groq_fairness_entity_min_rpm >= 1
    assert settings.groq_fairness_entity_min_tpm >= 100
    assert isinstance(settings.groq_telemetry_prefix, str)
    assert settings.groq_telemetry_prefix
