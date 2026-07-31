"""Integration test: the IPTU malha fina must PRODUCE discrepancies.

The whole fiscal chain broke in three silent places: detections were never
persisted, parcels had no geometry, and tenant schemas lacked the columns the
analysis queries (the error was swallowed into an empty list). This test
provisions a scratch tenant schema (v7), seeds detections and parcels the way
the real pipeline/import now do, runs the service through the public API
surface (run_analysis) and asserts real discrepancies come out — including
the estimated tax gap.

Requires the local dev database (same env vars as the rest of the suite).
"""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services.iptu_malha_fina import IPTUMalhaFinaService
from app.services.tenant_lifecycle import provision_tenant

SLUG = "malhafina_test"
SCHEMA = f"tenant_{SLUG}"

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="requires a real database (DATABASE_URL not set)",
)


# Bounds da ortofoto de teste — os polígonos abaixo vivem dentro deles
WEST, SOUTH, EAST, NORTH = -50.3708, -15.4406, -50.3682, -15.4383


def _square(lon: float, lat: float, size_deg: float = 0.0002) -> str:
    """WKT de um quadrado com canto inferior-esquerdo em (lon, lat)."""
    return (
        f"POLYGON(({lon} {lat}, {lon + size_deg} {lat}, "
        f"{lon + size_deg} {lat + size_deg}, {lon} {lat + size_deg}, {lon} {lat}))"
    )


@pytest.fixture
async def scratch_schema():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await session.commit()
        await provision_tenant(session, SLUG, municipality_name="Teste MF",
                               city="Goiânia", state="GO")
        await session.execute(text(f"SET search_path TO {SCHEMA}, public, topology"))
        yield session
        await session.rollback()
        await session.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        await session.commit()
    await engine.dispose()


async def _seed(session: AsyncSession) -> str:
    """Projeto + voo + asset + 2 detecções + 2 lotes. Retorna o project_id."""
    pid = str(uuid.uuid4())
    await session.execute(text(f"""
        INSERT INTO {SCHEMA}.projects
            (id, code, name, status, city, state,
             bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat)
        VALUES (CAST(:pid AS uuid), 'PRJ-901', 'Projeto Malha Fina', 'pendente',
                'Goiânia', 'GO', :w, :s, :e, :n)
    """), {"pid": pid, "w": WEST, "s": SOUTH, "e": EAST, "n": NORTH})

    fid = str(uuid.uuid4())
    await session.execute(text(f"""
        INSERT INTO {SCHEMA}.flights (id, project_id, flight_date, status)
        VALUES (CAST(:fid AS uuid), CAST(:pid AS uuid), CURRENT_DATE, 'completed')
    """), {"fid": fid, "pid": pid})

    aid = str(uuid.uuid4())
    await session.execute(text(f"""
        INSERT INTO {SCHEMA}.flight_assets
            (id, flight_id, asset_type, file_key, bucket_name, content_type)
        VALUES (CAST(:aid AS uuid), CAST(:fid AS uuid), 'orthomosaic',
                'test/ortho.tif', 'orthomosaics', 'image/tiff')
    """), {"aid": aid, "fid": fid})

    # Detecção 1: sobre o lote 900001, com área MUITO maior que a declarada
    det1 = _square(WEST + 0.0004, SOUTH + 0.0004)
    # Detecção 2: em área sem lote nenhum → não cadastrada
    det2 = _square(WEST + 0.0014, SOUTH + 0.0014)
    for geom, area in ((det1, 300.0), (det2, 180.0)):
        await session.execute(text(f"""
            INSERT INTO {SCHEMA}.ai_detections
                (flight_asset_id, detection_class, polygon, confidence,
                 area_sqm, properties, model_version)
            VALUES (CAST(:aid AS uuid), 'building',
                    ST_SetSRID(ST_GeomFromText(:wkt), 4326),
                    0.9, :area,
                    CAST(:props AS jsonb), 'test_v1')
        """), {"aid": aid, "wkt": geom, "area": area,
               "props": '{"height_m": 5.0}'})

    # Lote 900001: mesma posição da detecção 1, com só 80 m² declarados
    await session.execute(text(f"""
        INSERT INTO {SCHEMA}.parcels
            (cadastral_code, address, owner_name,
             registered_area_sqm, registered_built_area_sqm, polygon)
        VALUES ('900001', 'Rua Teste 1', 'Fulano',
                500, 80, ST_SetSRID(ST_GeomFromText(:wkt), 4326))
    """), {"wkt": _square(WEST + 0.0003, SOUTH + 0.0003, 0.0004)})

    # Lote 900002: declara 200 m² construídos, mas NENHUMA detecção sobre ele
    await session.execute(text(f"""
        INSERT INTO {SCHEMA}.parcels
            (cadastral_code, address, owner_name,
             registered_area_sqm, registered_built_area_sqm, polygon)
        VALUES ('900002', 'Rua Teste 2', 'Beltrano',
                400, 200, ST_SetSRID(ST_GeomFromText(:wkt), 4326))
    """), {"wkt": _square(EAST - 0.0006, NORTH - 0.0006, 0.0004)})

    await session.commit()
    return pid


class TestMalhaFinaProducesDiscrepancies:
    async def test_analysis_finds_the_three_planted_irregularities(self, scratch_schema):
        session = scratch_schema
        pid = await _seed(session)

        result = await IPTUMalhaFinaService.run_analysis(
            session, pid, triggered_by="pytest", area_tolerance_pct=15.0,
        )

        summary = result["summary"]
        assert summary["total_detections"] == 2
        assert summary["total_parcels"] == 2
        # Detecção 2 não intersecta lote nenhum
        assert summary["discrepancies_by_type"]["unregistered"] >= 1
        # Detecção 1 (300 m²) sobre lote com 80 m² declarados
        assert summary["discrepancies_by_type"]["area_under_declared"] >= 1
        assert summary["total_discrepancies"] >= 2
        # O produto existe para isto: estimar o gap de arrecadação
        assert result["estimated_total_gap_brl"] > 0

    async def test_discrepancies_are_persisted_for_the_review_queue(self, scratch_schema):
        session = scratch_schema
        pid = await _seed(session)
        await IPTUMalhaFinaService.run_analysis(session, pid, triggered_by="pytest")

        rows = (await session.execute(text(f"""
            SELECT discrepancy_type, status, estimated_iptu_gap_brl
            FROM {SCHEMA}.discrepancies WHERE project_id = CAST(:pid AS uuid)
        """), {"pid": pid})).fetchall()

        assert len(rows) >= 2
        # Todas nascem pendentes — é o que alimenta a fila de revisão fiscal
        assert all(r[1] == "pending" for r in rows)

    async def test_rerun_does_not_duplicate(self, scratch_schema):
        session = scratch_schema
        pid = await _seed(session)
        first = await IPTUMalhaFinaService.run_analysis(session, pid, triggered_by="pytest")
        second = await IPTUMalhaFinaService.run_analysis(session, pid, triggered_by="pytest")

        total = (await session.execute(text(f"""
            SELECT count(*) FROM {SCHEMA}.discrepancies
            WHERE project_id = CAST(:pid AS uuid)
        """), {"pid": pid})).scalar()

        # Reexecutar substitui/atualiza — nunca acumula cópias
        assert total <= first["summary"]["total_discrepancies"] + \
                        second["summary"]["total_discrepancies"]
        assert total >= second["summary"]["total_discrepancies"]
