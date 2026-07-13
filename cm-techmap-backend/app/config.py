"""
CM TECHMAP — Application Settings
Centralized configuration via Pydantic Settings with environment variable support.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    app_name: str = "CM TECHMAP"
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = True
    app_version: str = "0.1.0"
    app_secret_key: str = "CHANGE-ME-to-a-random-64-character-string-in-production"
    app_cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # ── Database ─────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://cm_techmap:cm_techmap_dev_2026@postgres:5432/cm_techmap"
    database_url_sync: str = "postgresql+psycopg2://cm_techmap:cm_techmap_dev_2026@postgres:5432/cm_techmap"
    # Direct connection (bypasses PgBouncer) — used for DDL/schema operations
    database_url_direct: str = ""
    database_pool_size: int = 20
    database_max_overflow: int = 10
    database_pool_timeout: int = 30
    database_echo: bool = False

    # ── Redis ────────────────────────────────────────────────────────────────
    redis_url: str = "redis://redis:6379/0"

    # ── Celery ───────────────────────────────────────────────────────────────
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"

    # ── MinIO ────────────────────────────────────────────────────────────────
    minio_endpoint: str = "minio:9000"
    minio_root_user: str = "cm_techmap_admin"
    minio_root_password: str = "cm_techmap_minio_2026"
    minio_use_ssl: bool = False
    minio_bucket_raw_uploads: str = "raw-uploads"
    minio_bucket_orthomosaics: str = "orthomosaics"
    minio_bucket_point_clouds: str = "point-clouds"
    minio_bucket_elevation_models: str = "elevation-models"
    minio_bucket_3d_models: str = "3d-models"
    minio_bucket_reports: str = "reports"
    minio_bucket_backups: str = "backups"

    # ── Keycloak ─────────────────────────────────────────────────────────────
    keycloak_server_url: str = "http://keycloak:8080"
    keycloak_external_url: str = "http://localhost:18080"
    keycloak_realm: str = "cm-techmap"
    keycloak_client_id: str = "cm-techmap-api"
    keycloak_client_secret: str = "cm-techmap-api-dev-secret-2026"
    keycloak_admin_username: str = "admin"
    keycloak_admin_password: str = "admin_dev_2026"

    # ── Upload ───────────────────────────────────────────────────────────────
    upload_chunk_size_mb: int = 50
    upload_max_file_size_gb: int = 100

    # ── NodeODM (Photogrammetry Engine) ──────────────────────────────────────
    nodeodm_host: str = "nodeodm"
    nodeodm_port: int = 3000
    nodeodm_timeout: int = 30
    nodeodm_poll_interval: int = 10
    nodeodm_default_options: str = (
        '{"dsm": true, "dtm": true, '
        '"orthophoto-resolution": 2, "dem-resolution": 2, '
        '"pc-quality": "high", "feature-quality": "high", '
        '"cog": true, '
        '"skip-3dmodel": false, '
        '"mesh-octree-depth": 11, '
        '"mesh-size": 300000, '
        '"texturing-data-term": "gmi", '
        '"texturing-outlier-removal-type": "gauss_clamping", '
        '"use-3dmesh": true}'
    )

    # ── TiTiler (Raster Tile Server) ─────────────────────────────────────────
    titiler_url: str = "http://titiler:80"
    titiler_external_url: str = "http://localhost:18888"

    # ── Martin (Vector Tile Server) ──────────────────────────────────────────
    martin_url: str = "http://martin:3000"
    martin_external_url: str = "http://localhost:13001"

    # ── Processing ───────────────────────────────────────────────────────────
    processing_temp_dir: str = "/tmp/cm-techmap-processing"
    processing_cog_blocksize: int = 512

    # ── Groq Inference (Hybrid AI + Narrative Reports) ─────────────────────
    groq_enabled: bool = False
    ai_provider: Literal["local", "groq", "hybrid"] = "local"
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Model selection
    groq_vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    groq_report_model: str = "openai/gpt-oss-120b"

    # Operational controls (safe defaults for free-tier style usage)
    groq_timeout_seconds: int = 90
    groq_max_retries: int = 3
    groq_backoff_base_seconds: float = 1.5
    groq_rpm_limit_free: int = 20
    groq_tpm_limit_free: int = 12000
    groq_enable_adaptive_rate_from_headers: bool = True
    groq_safety_factor: float = 0.70
    groq_min_utilization_factor: float = 0.70
    groq_circuit_breaker_failures: int = 5
    groq_circuit_breaker_open_seconds: int = 60
    groq_fairness_entity_share: float = 0.35
    groq_fairness_entity_min_rpm: int = 2
    groq_fairness_entity_min_tpm: int = 1200

    # Vision request shaping
    groq_vision_max_side_px: int = 1024
    groq_vision_image_quality: int = 80
    groq_vision_max_completion_tokens: int = 1400
    groq_vision_enable_tiling: bool = True
    groq_vision_tile_size_px: int = 1024
    groq_vision_tile_overlap_px: int = 96
    groq_vision_max_tiles: int = 6
    groq_vision_tile_max_completion_tokens: int = 900

    # Report narrative shaping
    groq_report_enabled: bool = True
    groq_report_max_completion_tokens: int = 1800

    # Operational telemetry
    groq_telemetry_enabled: bool = True
    groq_telemetry_prefix: str = "cm_techmap:groq:telemetry"

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse CORS origins string into a list."""
        return [origin.strip() for origin in self.app_cors_origins.split(",")]

    @property
    def minio_access_key(self) -> str:
        """Backward-compatible alias used by legacy modules."""
        return self.minio_root_user

    @property
    def minio_secret_key(self) -> str:
        """Backward-compatible alias used by legacy modules."""
        return self.minio_root_password

    @property
    def minio_secure(self) -> bool:
        """Backward-compatible alias used by legacy modules."""
        return self.minio_use_ssl

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton for application settings."""
    return Settings()
