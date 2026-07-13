"""
CM TECHMAP — Reports API Presets Endpoint Tests
Ensures report config presets are exposed and stable for frontend consumption.
"""

import pytest


class TestReportConfigPresetsEndpoint:
    @pytest.mark.asyncio
    async def test_list_presets_contract(self, client):
        response = await client.get("/api/v1/reports/config/presets")
        assert response.status_code == 200

        data = response.json()
        assert "presets" in data
        assert "supported_profiles" in data
        assert isinstance(data["presets"], list)
        assert isinstance(data["supported_profiles"], list)
        assert len(data["presets"]) >= 1

        first = data["presets"][0]
        assert "municipality_code" in first
        assert "municipality_name" in first
        assert "iptu_rate_per_sqm" in first
        assert "assumed_irregular_share" in first
        assert "qa_threshold" in first

    @pytest.mark.asyncio
    async def test_profiles_include_advanced_types(self, client):
        response = await client.get("/api/v1/reports/config/presets")
        assert response.status_code == 200
        profiles = response.json().get("supported_profiles", [])

        assert "property_appraisal" in profiles
        assert "project_consolidated" in profiles
        assert "fiscal_revenue" in profiles
        assert "technical_qa" in profiles

    @pytest.mark.asyncio
    async def test_upsert_preset_requires_super_admin(self, client):
        response = await client.post(
            "/api/v1/reports/config/presets",
            json={
                "municipality_code": "9999999",
                "municipality_name": "Cidade Teste",
                "iptu_rate_per_sqm": 11.0,
                "assumed_irregular_share": 0.2,
                "qa_threshold": 0.8,
            },
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_delete_preset_requires_super_admin(self, client):
        response = await client.delete("/api/v1/reports/config/presets/5208707")
        assert response.status_code in (401, 403)
