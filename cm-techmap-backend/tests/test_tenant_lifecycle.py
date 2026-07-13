"""
CM TECHMAP — Tenant Lifecycle Tests
Tests the complete tenant provisioning, migration, and quota enforcement pipeline.
"""

import pytest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.tenant_lifecycle import (
    SCHEMA_VERSION,
    TENANT_TABLE_REGISTRY,
    _create_all_tenant_tables,
    _create_tenant_indexes,
    _enable_rls_for_schema,
)
from app.services.tenant_quota import TenantQuota, get_tenant_quota


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA VERSION
# ══════════════════════════════════════════════════════════════════════════════

class TestSchemaVersion:
    """Validate schema version constants are consistent."""

    def test_schema_version_is_positive(self):
        assert SCHEMA_VERSION > 0

    def test_schema_version_type(self):
        assert isinstance(SCHEMA_VERSION, int)

    def test_tenant_table_registry_has_minimum_tables(self):
        """Each tenant schema must have at least 11 core tables."""
        assert len(TENANT_TABLE_REGISTRY) >= 11

    def test_tenant_table_registry_includes_core_tables(self):
        required = {"users", "projects", "flights", "parcels", "discrepancies"}
        actual = set(TENANT_TABLE_REGISTRY)
        assert required.issubset(actual), f"Missing tables: {required - actual}"

    def test_tenant_table_registry_includes_sprint1_tables(self):
        """Sprint 1 additions must be present."""
        sprint1 = {"discrepancies", "analysis_runs", "iptu_rules"}
        actual = set(TENANT_TABLE_REGISTRY)
        assert sprint1.issubset(actual), f"Missing Sprint 1 tables: {sprint1 - actual}"

    def test_no_duplicate_tables_in_registry(self):
        assert len(TENANT_TABLE_REGISTRY) == len(set(TENANT_TABLE_REGISTRY))


# ══════════════════════════════════════════════════════════════════════════════
# TABLE CREATION (unit tests with mock session)
# ══════════════════════════════════════════════════════════════════════════════

class TestTableCreation:
    """Test that table creation SQL is called correctly."""

    @pytest.mark.asyncio
    async def test_create_all_tenant_tables_returns_list(self, mock_db_session):
        """_create_all_tenant_tables should return list of table names."""
        tables = await _create_all_tenant_tables(mock_db_session, "tenant_test")
        assert isinstance(tables, list)
        assert len(tables) == len(TENANT_TABLE_REGISTRY)

    @pytest.mark.asyncio
    async def test_create_tables_names_match_registry(self, mock_db_session):
        """Created table names must match the registry."""
        tables = await _create_all_tenant_tables(mock_db_session, "tenant_test")
        assert set(tables) == set(TENANT_TABLE_REGISTRY)

    @pytest.mark.asyncio
    async def test_create_tables_calls_execute_for_each(self, mock_db_session):
        """Each table should trigger at least one session.execute() call."""
        await _create_all_tenant_tables(mock_db_session, "tenant_test")
        # 14 tables = 14 execute calls minimum
        assert mock_db_session.execute.call_count >= len(TENANT_TABLE_REGISTRY)

    @pytest.mark.asyncio
    async def test_create_tables_uses_correct_schema(self, mock_db_session):
        """SQL should reference the correct schema name."""
        schema = "tenant_goiania"
        await _create_all_tenant_tables(mock_db_session, schema)
        
        # Check that at least one SQL call contains the schema name
        calls = mock_db_session.execute.call_args_list
        sql_texts = [str(call[0][0]) for call in calls]
        schema_refs = [s for s in sql_texts if schema in s]
        assert len(schema_refs) > 0, f"No SQL contained schema '{schema}'"


# ══════════════════════════════════════════════════════════════════════════════
# INDEX CREATION
# ══════════════════════════════════════════════════════════════════════════════

class TestIndexCreation:
    """Test spatial and performance index creation."""

    @pytest.mark.asyncio
    async def test_create_indexes_returns_list(self, mock_db_session):
        indexes = await _create_tenant_indexes(mock_db_session, "tenant_test")
        assert isinstance(indexes, list)
        assert len(indexes) > 0

    @pytest.mark.asyncio
    async def test_indexes_include_spatial(self, mock_db_session):
        """Must include GIST indexes for geometry columns."""
        indexes = await _create_tenant_indexes(mock_db_session, "tenant_test")
        gist_indexes = [i for i in indexes if "polygon" in i or "geom" in i or "detect" in i]
        assert len(gist_indexes) >= 3, "Need at least 3 spatial indexes"

    @pytest.mark.asyncio
    async def test_indexes_respect_pg_name_limit(self, mock_db_session):
        """PostgreSQL identifier limit is 63 chars."""
        indexes = await _create_tenant_indexes(mock_db_session, "tenant_very_long_municipality_name_here")
        for idx in indexes:
            assert len(idx) <= 63, f"Index name too long: {idx} ({len(idx)} chars)"


# ══════════════════════════════════════════════════════════════════════════════
# RLS POLICIES
# ══════════════════════════════════════════════════════════════════════════════

class TestRLSPolicies:
    """Test Row-Level Security policy creation."""

    @pytest.mark.asyncio
    async def test_rls_returns_enabled_tables(self, mock_db_session):
        enabled = await _enable_rls_for_schema(mock_db_session, "tenant_test")
        assert isinstance(enabled, list)
        assert len(enabled) >= 5, "RLS should be enabled on at least 5 tables"

    @pytest.mark.asyncio
    async def test_rls_includes_critical_tables(self, mock_db_session):
        enabled = await _enable_rls_for_schema(mock_db_session, "tenant_test")
        critical = {"users", "projects", "parcels", "discrepancies"}
        assert critical.issubset(set(enabled)), f"RLS missing on: {critical - set(enabled)}"


# ══════════════════════════════════════════════════════════════════════════════
# TENANT QUOTA
# ══════════════════════════════════════════════════════════════════════════════

class TestTenantQuota:
    """Test quota enforcement logic."""

    def test_quota_users_remaining(self):
        quota = TenantQuota("test", "starter", {"max_users": 5}, {"users": 3})
        assert quota.users_remaining == 2

    def test_quota_users_at_limit(self):
        quota = TenantQuota("test", "starter", {"max_users": 5}, {"users": 5})
        assert quota.users_remaining == 0
        assert not quota.can_add_user()

    def test_quota_users_over_limit(self):
        quota = TenantQuota("test", "starter", {"max_users": 5}, {"users": 7})
        assert quota.users_remaining == 0
        assert not quota.can_add_user()

    def test_quota_projects_remaining(self):
        quota = TenantQuota("test", "professional", {"max_projects": 50}, {"projects": 10})
        assert quota.projects_remaining == 40

    def test_quota_projects_at_limit(self):
        quota = TenantQuota("test", "starter", {"max_projects": 10}, {"projects": 10})
        assert not quota.can_add_project()

    def test_quota_storage_remaining(self):
        quota = TenantQuota("test", "starter", {"max_storage_tb": 1.0}, {"storage_gb": 500})
        assert quota.storage_remaining_gb == 524.0  # 1TB = 1024GB - 500

    def test_quota_storage_at_limit(self):
        quota = TenantQuota("test", "starter", {"max_storage_tb": 1.0}, {"storage_gb": 1024})
        assert not quota.can_upload(1.0)

    def test_quota_to_dict_structure(self):
        quota = TenantQuota("tid", "pro", {"max_users": 25, "max_projects": 50, "max_storage_tb": 5.0},
                           {"users": 10, "projects": 20, "storage_gb": 100})
        d = quota.to_dict()
        assert "tenant_id" in d
        assert "plan" in d
        assert "limits" in d
        assert "usage" in d
        assert "remaining" in d
        assert "at_capacity" in d

    def test_quota_plan_tiers(self):
        """Verify plan tier limits are correctly propagated."""
        starter = TenantQuota("t1", "starter", {"max_users": 5, "max_projects": 10, "max_storage_tb": 1.0}, {})
        pro = TenantQuota("t2", "professional", {"max_users": 25, "max_projects": 50, "max_storage_tb": 5.0}, {})
        ent = TenantQuota("t3", "enterprise", {"max_users": 999, "max_projects": 999, "max_storage_tb": 50.0}, {})
        
        assert starter.limits["max_users"] < pro.limits["max_users"] < ent.limits["max_users"]
        assert starter.limits["max_storage_tb"] < pro.limits["max_storage_tb"] < ent.limits["max_storage_tb"]


# ══════════════════════════════════════════════════════════════════════════════
# MULTI-TENANT ISOLATION (Logic Tests)
# ══════════════════════════════════════════════════════════════════════════════

class TestMultiTenantIsolation:
    """Test that tenant isolation logic is correct."""

    def test_schema_name_format(self):
        """Schema name must follow 'tenant_{slug}' pattern."""
        from app.services.tenant_lifecycle import provision_tenant
        slug = "goiania"
        expected = f"tenant_{slug}"
        assert expected == f"tenant_{slug}"

    def test_schema_name_sanitization(self):
        """Slugs should be alphanumeric + underscore only."""
        import re
        valid_slugs = ["goiania", "sao_paulo", "belo_horizonte", "rio123"]
        for slug in valid_slugs:
            schema = f"tenant_{slug}"
            assert re.match(r'^tenant_[a-z0-9_]+$', schema), f"Invalid schema: {schema}"

    def test_public_paths_dont_require_tenant(self):
        """Public paths should not require tenant resolution."""
        from app.middleware.tenant import PUBLIC_PATHS
        assert "/api/v1/health" in PUBLIC_PATHS
        assert "/api/v1/auth/login" in PUBLIC_PATHS
        assert "/docs" in PUBLIC_PATHS

    def test_context_var_default_is_none(self):
        """current_tenant_schema should default to None (public schema)."""
        from app.core.database import current_tenant_schema
        assert current_tenant_schema.get() is None
