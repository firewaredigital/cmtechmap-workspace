"""
CM TECHMAP — Report Profile and Preset Tests
Covers advanced report profile contracts, config presets, and QA section synthesis.
"""

import uuid
import sys
import types

import pytest

from app.api.v1.reports import REPORT_CONFIG_PRESETS, _resolve_report_config
from app.schemas.report import ReportGenerateRequest
from app.services.report_generator import ReportGeneratorService
from app.tasks.processing import run_ai_pipeline_groq


class TestReportSchemas:
    """Validate request schema supports all advanced report profiles."""

    @pytest.mark.parametrize(
        "profile",
        [
            "project_summary",
            "comparison",
            "flight_detail",
            "custom",
            "property_appraisal",
            "project_consolidated",
            "fiscal_revenue",
            "technical_qa",
        ],
    )
    def test_report_generate_request_accepts_profile(self, profile: str):
        payload = ReportGenerateRequest(
            project_id=uuid.uuid4(),
            title="Relatorio de Validacao",
            report_type=profile,
            output_format="pdf",
        )
        assert payload.report_type == profile

    def test_report_generate_request_accepts_groq_overrides(self):
        payload = ReportGenerateRequest(
            project_id=uuid.uuid4(),
            title="Relatorio com Groq",
            report_type="project_summary",
            output_format="pdf",
            groq={
                "enable_narrative": True,
                "model_override": "openai/gpt-oss-120b",
                "max_completion_tokens": 700,
            },
        )

        assert payload.groq is not None
        assert payload.groq.enable_narrative is True
        assert payload.groq.model_override == "openai/gpt-oss-120b"
        assert payload.groq.max_completion_tokens == 700


class TestGroqQueueTask:
    """Validate dedicated Groq Celery task is properly declared."""

    def test_run_ai_pipeline_groq_declared_on_ai_llm_queue(self):
        assert run_ai_pipeline_groq.name == "app.tasks.processing.run_ai_pipeline_groq"
        assert run_ai_pipeline_groq.queue == "ai_llm"


class TestReportPresetResolution:
    """Validate fiscal/QA preset merge logic for report generation."""

    def test_resolve_config_uses_city_fallback(self):
        project = {"city": "goiania", "state": "GO"}
        resolved = _resolve_report_config(project, None, "fiscal_revenue", REPORT_CONFIG_PRESETS)

        assert resolved["municipality_code"] == "5208707"
        assert resolved["report_profile"] == "fiscal_revenue"
        assert resolved["iptu_rate_per_sqm"] == REPORT_CONFIG_PRESETS["5208707"]["iptu_rate_per_sqm"]

    def test_resolve_config_preserves_explicit_override(self):
        project = {"city": "goiania", "state": "GO"}
        resolved = _resolve_report_config(
            project,
            {
                "municipality_code": "3550308",
                "iptu_rate_per_sqm": 21.3,
                "assumed_irregular_share": 0.33,
                "qa_threshold": 0.91,
            },
            "technical_qa",
            REPORT_CONFIG_PRESETS,
        )

        assert resolved["municipality_code"] == "3550308"
        assert resolved["iptu_rate_per_sqm"] == 21.3
        assert resolved["assumed_irregular_share"] == 0.33
        assert resolved["qa_threshold"] == 0.91
        assert resolved["report_profile"] == "technical_qa"


class TestReportMandatorySections:
    """Validate generated mandatory section payloads used by PDF renderer."""

    def test_mandatory_sections_include_qa_compliance(self):
        svc = ReportGeneratorService()
        sections = svc._build_mandatory_sections(
            report_profile="technical_qa",
            project_data={"code": "PRJ-001", "area_sqm": 12000},
            flights_data=[{"id": "f1"}],
            urban_analytics={"total_buildings": 2, "total_built_area_sqm": 400},
            analytics_data={
                "qa_score": 0.88,
                "measurement_run_id": "run-1",
                "completed_at": "2026-07-13T10:00:00+00:00",
                "total_buildings": 2,
                "total_terrain_patches": 3,
                "built_area_total_sqm": 400,
                "terrain_area_total_sqm": 1200,
            },
            buildings=[
                {"area_sqm": 250, "confidence": 0.92, "building_type": "residential"},
                {"area_sqm": 150, "confidence": 0.89, "building_type": "commercial"},
            ],
            terrain_data=[
                {"area_sqm": 800, "compactness": 0.76},
                {"area_sqm": 400, "compactness": 0.71},
            ],
            config={"qa_threshold": 0.80, "iptu_rate_per_sqm": 12.0, "assumed_irregular_share": 0.25},
        )

        qa_metrics = sections["technical_qa"]["metrics"]
        assert sections["technical_qa"]["enabled"] is True
        assert qa_metrics["qa_score"] == 0.88
        assert qa_metrics["qa_threshold"] == 0.80
        assert qa_metrics["qa_compliant"] is True
        assert qa_metrics["measurement_run_id"] == "run-1"

    def test_fiscal_section_uses_configured_rate(self):
        svc = ReportGeneratorService()
        sections = svc._build_mandatory_sections(
            report_profile="fiscal_revenue",
            project_data={"code": "PRJ-002", "area_sqm": 20000},
            flights_data=[],
            urban_analytics={"total_buildings": 1, "total_built_area_sqm": 500},
            analytics_data={},
            buildings=[{"area_sqm": 500, "confidence": 0.85}],
            terrain_data=[{"area_sqm": 1000, "compactness": 0.7}],
            config={"iptu_rate_per_sqm": 18.2, "assumed_irregular_share": 0.4},
        )

        fiscal = sections["fiscal_revenue"]
        assert fiscal["enabled"] is True
        assert fiscal["metrics"]["iptu_rate_per_sqm"] == 18.2
        assert fiscal["metrics"]["assumed_irregular_share_pct"] == 40.0
        assert fiscal["metrics"]["annual_tax_gain_estimate"] > 0

    def test_generate_pdf_renders_mandatory_sections_end_to_end(self, monkeypatch):
        pytest.importorskip("jinja2")

        class FakeHTML:
            last_html = ""

            def __init__(self, string: str):
                self.string = string
                FakeHTML.last_html = string

            def write_pdf(self):
                return self.string.encode("utf-8")

        fake_weasyprint = types.SimpleNamespace(HTML=FakeHTML)
        monkeypatch.setitem(sys.modules, "weasyprint", fake_weasyprint)

        svc = ReportGeneratorService()
        pdf_bytes = svc.generate_pdf(
            project_data={"code": "PRJ-003", "name": "Projeto Teste", "city": "Goiania"},
            flights_data=[{"images_count": 120, "area_coverage_sqm": 1800}],
            assets_data=[{"asset_type": "orthomosaic", "file_key": "a.tif", "file_size_bytes": 1024}],
            report_config={
                "report_profile": "project_summary",
                "iptu_rate_per_sqm": 15.5,
                "assumed_irregular_share": 0.28,
                "qa_threshold": 0.81,
            },
            buildings_data=[
                {"area_sqm": 300, "perimeter_m": 80, "height_m": 8, "confidence": 0.92, "building_type": "residential"},
                {"area_sqm": 190, "perimeter_m": 56, "height_m": 6, "confidence": 0.88, "building_type": "commercial"},
            ],
            analytics_data={
                "qa_score": 0.87,
                "total_buildings": 2,
                "total_terrain_patches": 2,
                "built_area_total_sqm": 490,
                "terrain_area_total_sqm": 1800,
                "measurement_run_id": "run-e2e-001",
            },
            terrain_data=[
                {"area_sqm": 1000, "compactness": 0.75},
                {"area_sqm": 800, "compactness": 0.71},
            ],
        )

        rendered = FakeHTML.last_html
        assert len(pdf_bytes) > 0
        assert "Laudo por Imóvel" in rendered
        assert "Consolidado do Projeto" in rendered
        assert "Relatório Fiscal de Arrecadação" in rendered
        assert "QA Técnico" in rendered
        assert "Threshold QA" in rendered

    def test_generate_pdf_includes_ai_narrative_block(self, monkeypatch):
        pytest.importorskip("jinja2")

        class FakeHTML:
            last_html = ""

            def __init__(self, string: str):
                self.string = string
                FakeHTML.last_html = string

            def write_pdf(self):
                return self.string.encode("utf-8")

        fake_weasyprint = types.SimpleNamespace(HTML=FakeHTML)
        monkeypatch.setitem(sys.modules, "weasyprint", fake_weasyprint)

        svc = ReportGeneratorService()
        svc.generate_pdf(
            project_data={"code": "PRJ-004", "name": "Projeto Narrativo", "city": "Goiania"},
            flights_data=[],
            assets_data=[],
            report_config={
                "report_profile": "project_summary",
                "ai_narrative": {
                    "executive_summary": "Resumo executivo de teste.",
                    "fiscal_analysis": "Análise fiscal de teste.",
                    "qa_analysis": "Análise QA de teste.",
                    "recommendations": "Recomendações de teste.",
                },
            },
            buildings_data=[],
            analytics_data={},
            terrain_data=[],
        )

        rendered = FakeHTML.last_html
        assert "Análises Narrativas por IA" in rendered
        assert "Resumo executivo de teste." in rendered
