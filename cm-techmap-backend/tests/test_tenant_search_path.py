"""Regression tests for tenant isolation inside Celery tasks.

Production bug: Celery workers run outside the request cycle, so the
ContextVar the API uses to route queries to a tenant schema is empty there.
The pipeline "solved" it by listing EVERY tenant_* schema and concatenating
them into search_path — an unqualified INSERT then landed in whichever schema
Postgres resolved first, contaminating other municipalities' data. The fix
passes one schema explicitly; these tests pin that contract.
"""

import pytest

from app.tasks.processing import _apply_tenant_search_path


class _FakeConn:
    """Records the SQL a task would execute."""

    def __init__(self):
        self.statements: list[str] = []

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return None


class TestApplyTenantSearchPath:
    def test_sets_search_path_to_the_single_owning_schema(self):
        conn = _FakeConn()
        _apply_tenant_search_path(conn, "tenant_goiania")
        assert len(conn.statements) == 1
        assert conn.statements[0] == (
            'SET search_path TO "tenant_goiania", public, topology'
        )

    def test_never_includes_a_second_tenant_schema(self):
        conn = _FakeConn()
        _apply_tenant_search_path(conn, "tenant_goiania")
        # The old code produced "SET search_path TO tenant_a, tenant_b, ...".
        assert "tenant_" in conn.statements[0]
        assert conn.statements[0].count("tenant_") == 1

    def test_noop_without_a_schema(self):
        # Platform-level (non-tenant) work keeps the connection default.
        conn = _FakeConn()
        _apply_tenant_search_path(conn, None)
        assert conn.statements == []

    @pytest.mark.parametrize(
        "malicious",
        [
            'tenant_x"; DROP SCHEMA public CASCADE; --',
            "tenant_a, tenant_b",
            "public",
            "tenant_UPPER",
            "../etc",
            "tenant_x;",
        ],
    )
    def test_rejects_names_that_are_not_a_plain_tenant_schema(self, malicious):
        # The schema name cannot be a bind parameter in SET search_path, so it
        # is interpolated — validation is what keeps that safe.
        conn = _FakeConn()
        with pytest.raises(ValueError):
            _apply_tenant_search_path(conn, malicious)
        assert conn.statements == []


class TestPipelineTaskSignatures:
    """The schema only reaches the worker if the task accepts it."""

    def test_process_drone_upload_accepts_tenant_schema(self):
        import inspect

        from app.tasks.processing import process_drone_upload

        params = inspect.signature(process_drone_upload).parameters
        assert "tenant_schema" in params

    @pytest.mark.parametrize(
        "task_name",
        [
            "generate_dsm_and_buildings",
            "extract_buildings_from_real_dsm",
            "normalize_and_process_real_dsm",
        ],
    )
    def test_post_processing_tasks_accept_tenant_schema(self, task_name):
        import inspect

        import app.tasks.post_processing as pp

        params = inspect.signature(getattr(pp, task_name)).parameters
        assert "tenant_schema" in params
