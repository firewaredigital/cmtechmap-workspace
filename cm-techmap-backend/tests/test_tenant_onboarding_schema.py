"""Regression tests for the onboarding TenantCreate contract.

Production incident: the wizard let users submit free-typed slugs
("Prefeitura-Teste") that failed the ^[a-z0-9_]+$ pattern with a 422, and the
raw Pydantic detail array crashed the frontend (minified React #31). The
wizard also posted IPTU rules to /iptu/rules with a JSON body the endpoint
never accepted — those now travel inside TenantCreate.iptu and are seeded by
the backend into the new tenant schema. These tests pin that contract.
"""

import pytest
from pydantic import ValidationError

from app.schemas.tenant import TenantCreate


def _wizard_payload(**overrides):
    payload = {
        "name": "Prefeitura Teste",
        "slug": "prefeitura_teste",
        "cnpj": None,
        "city": "Goiânia",
        "state": "GO",
        "contact_email": "contato@teste.gov.br",
        "iptu": {
            "municipality_code": "5208707",
            "base_year": 2026,
            "zones": [
                {
                    "zone_name": "Zona Urbana",
                    "land_value_per_sqm_brl": 50.0,
                    "built_value_per_sqm_brl": 800.0,
                    "aliquot_pct": 1.0,
                }
            ],
        },
        "initial_project": {"name": "Mapeamento Piloto", "description": None},
    }
    payload.update(overrides)
    return payload


class TestTenantCreateOnboarding:
    def test_accepts_full_wizard_payload(self):
        body = TenantCreate(**_wizard_payload())
        assert body.iptu is not None
        assert body.iptu.municipality_code == "5208707"
        assert body.iptu.base_year == 2026
        assert body.iptu.zones[0].zone_name == "Zona Urbana"
        assert body.initial_project is not None
        assert body.initial_project.name == "Mapeamento Piloto"

    def test_iptu_and_project_are_optional(self):
        body = TenantCreate(**_wizard_payload(iptu=None, initial_project=None))
        assert body.iptu is None
        assert body.initial_project is None

    def test_rejects_free_typed_slug(self):
        # The exact input shape from the production 422 (hyphen + uppercase).
        with pytest.raises(ValidationError):
            TenantCreate(**_wizard_payload(slug="Prefeitura-Teste"))

    def test_rejects_single_letter_slug(self):
        # The old auto-slug froze at the first typed letter of the name.
        with pytest.raises(ValidationError):
            TenantCreate(**_wizard_payload(slug="p"))

    def test_rejects_short_municipality_code(self):
        payload = _wizard_payload()
        payload["iptu"]["municipality_code"] = "123"
        with pytest.raises(ValidationError):
            TenantCreate(**payload)

    def test_rejects_aliquot_above_100(self):
        payload = _wizard_payload()
        payload["iptu"]["zones"][0]["aliquot_pct"] = 150.0
        with pytest.raises(ValidationError):
            TenantCreate(**payload)

    def test_rejects_short_project_name(self):
        payload = _wizard_payload()
        payload["initial_project"]["name"] = "ab"
        with pytest.raises(ValidationError):
            TenantCreate(**payload)

    def test_legacy_extra_keys_are_ignored(self):
        # Older frontend builds sent municipality_name/plan alongside the body.
        body = TenantCreate(
            **_wizard_payload(), municipality_name="X", plan="professional"
        )
        assert body.slug == "prefeitura_teste"
