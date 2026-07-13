"""
CM TECHMAP — Public Data API
Endpoints for IBGE, INMET, and CEMADEN government data integration.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from app.services.public_data import IBGEClient, INMETClient, CEMADENClient
from app.schemas.public_data import (
    EstadoRead,
    MunicipioListResponse,
    MunicipioRead,
    EstacaoListResponse,
    EstacaoMeteorologica,
    DadosEstacaoResponse,
    ObservacaoMeteorologica,
    AlertaResponse,
    PublicDataSummary,
)

logger = logging.getLogger("cm_techmap.api.public_data")

router = APIRouter(prefix="/public-data", tags=["Public Data"])

# Singleton clients
_ibge = IBGEClient()
_inmet = INMETClient()
_cemaden = CEMADENClient()


@router.get("/providers", response_model=PublicDataSummary)
async def list_providers():
    """List available public data providers and their capabilities."""
    return PublicDataSummary(
        total_providers=3,
        providers=[
            {
                "id": "ibge",
                "name": "IBGE — Instituto Brasileiro de Geografia e Estatística",
                "description": "Dados geográficos, malhas censitárias, divisões territoriais",
                "endpoints": [
                    "/public-data/ibge/estados",
                    "/public-data/ibge/municipios?uf=XX",
                    "/public-data/ibge/municipio/{codigo}/malha",
                    "/public-data/ibge/municipio/{codigo}/setores",
                ],
                "status": "active",
            },
            {
                "id": "inmet",
                "name": "INMET — Instituto Nacional de Meteorologia",
                "description": "Dados meteorológicos, estações, condições atuais",
                "endpoints": [
                    "/public-data/inmet/estacoes?uf=XX",
                    "/public-data/inmet/estacao/{codigo}/dados",
                    "/public-data/inmet/estacao/{codigo}/atual",
                    "/public-data/inmet/proxima?lat=X&lon=Y",
                ],
                "status": "active",
            },
            {
                "id": "cemaden",
                "name": "CEMADEN — Centro Nacional de Monitoramento de Desastres",
                "description": "Alertas de risco, pluviômetros, monitoramento",
                "endpoints": [
                    "/public-data/cemaden/alertas?uf=XX",
                    "/public-data/cemaden/pluviometros?lat=X&lon=Y",
                ],
                "status": "active",
            },
        ],
    )


# =============================================================================
# IBGE Endpoints
# =============================================================================

@router.get("/ibge/estados", response_model=list[EstadoRead])
async def ibge_list_estados():
    """List all Brazilian states (UFs) from IBGE."""
    try:
        return await _ibge.get_estados()
    except Exception as e:
        logger.error(f"IBGE estados error: {e}")
        raise HTTPException(status_code=502, detail=f"IBGE API indisponível: {e}")


@router.get("/ibge/municipios", response_model=MunicipioListResponse)
async def ibge_list_municipios(uf: str = Query(..., min_length=2, max_length=2, description="Sigla do estado (ex: DF, SP)")):
    """List all municipalities for a given state from IBGE."""
    try:
        municipios = await _ibge.get_municipios(uf)
        return MunicipioListResponse(
            uf=uf.upper(),
            total=len(municipios),
            items=[MunicipioRead(**m) for m in municipios],
        )
    except Exception as e:
        logger.error(f"IBGE municipios error: {e}")
        raise HTTPException(status_code=502, detail=f"IBGE API indisponível: {e}")


@router.get("/ibge/municipio/{codigo}/malha")
async def ibge_get_malha(
    codigo: int,
    resolucao: int = Query(2, ge=0, le=3, description="Resolução: 0=nenhuma, 1=mínima, 2=inter, 3=máxima"),
):
    """Get municipality boundary as GeoJSON from IBGE."""
    try:
        return await _ibge.get_malha_geojson(codigo, resolucao)
    except Exception as e:
        logger.error(f"IBGE malha error: {e}")
        raise HTTPException(status_code=502, detail=f"IBGE API indisponível: {e}")


@router.get("/ibge/municipio/{codigo}/setores")
async def ibge_get_setores(codigo: int):
    """Get census sectors (setores censitários) for a municipality as GeoJSON."""
    try:
        return await _ibge.get_setores_censitarios(codigo)
    except Exception as e:
        logger.error(f"IBGE setores error: {e}")
        raise HTTPException(status_code=502, detail=f"IBGE API indisponível: {e}")


@router.get("/ibge/nomes")
async def ibge_nomes_geograficos(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    raio_km: float = Query(10, ge=1, le=100, description="Raio de busca em km"),
):
    """Search geographic names (BNGB) near coordinates."""
    try:
        return await _ibge.get_nomes_geograficos(lat, lon, raio_km)
    except Exception as e:
        logger.error(f"IBGE nomes error: {e}")
        raise HTTPException(status_code=502, detail=f"IBGE API indisponível: {e}")


# =============================================================================
# INMET Endpoints
# =============================================================================

@router.get("/inmet/estacoes", response_model=EstacaoListResponse)
async def inmet_list_estacoes(
    uf: str = Query(..., min_length=2, max_length=2, description="Sigla do estado"),
):
    """List INMET weather stations for a state."""
    try:
        estacoes = await _inmet.get_estacoes_por_uf(uf)
        return EstacaoListResponse(
            uf=uf.upper(),
            total=len(estacoes),
            items=[EstacaoMeteorologica(**e) for e in estacoes],
        )
    except Exception as e:
        logger.error(f"INMET estacoes error: {e}")
        raise HTTPException(status_code=502, detail=f"INMET API indisponível: {e}")


@router.get("/inmet/estacao/{codigo}/dados", response_model=DadosEstacaoResponse)
async def inmet_dados_estacao(
    codigo: str,
    dias: int = Query(1, ge=1, le=30, description="Últimos N dias"),
):
    """Get observation data from a weather station for the last N days."""
    try:
        data_fim = datetime.now().strftime("%Y-%m-%d")
        data_inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
        dados = await _inmet.get_dados_estacao(codigo, data_inicio, data_fim)
        return DadosEstacaoResponse(
            codigo=codigo,
            total=len(dados),
            items=[ObservacaoMeteorologica(**d) for d in dados],
        )
    except Exception as e:
        logger.error(f"INMET dados error: {e}")
        raise HTTPException(status_code=502, detail=f"INMET API indisponível: {e}")


@router.get("/inmet/estacao/{codigo}/atual")
async def inmet_condicoes_atuais(codigo: str):
    """Get current weather conditions from a station."""
    try:
        condicao = await _inmet.get_condicoes_atuais(codigo)
        if not condicao:
            raise HTTPException(status_code=404, detail="Sem dados recentes para esta estação")
        return condicao
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"INMET atual error: {e}")
        raise HTTPException(status_code=502, detail=f"INMET API indisponível: {e}")


@router.get("/inmet/proxima", response_model=EstacaoMeteorologica | None)
async def inmet_estacao_proxima(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """Find the nearest INMET weather station to given coordinates."""
    try:
        estacao = await _inmet.get_estacao_mais_proxima(lat, lon)
        if not estacao:
            raise HTTPException(status_code=404, detail="Nenhuma estação encontrada")
        return EstacaoMeteorologica(**estacao)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"INMET proxima error: {e}")
        raise HTTPException(status_code=502, detail=f"INMET API indisponível: {e}")


# =============================================================================
# CEMADEN Endpoints
# =============================================================================

@router.get("/cemaden/alertas", response_model=AlertaResponse)
async def cemaden_alertas(
    uf: str = Query(..., min_length=2, max_length=2, description="Sigla do estado"),
):
    """Get active disaster alerts for a state from CEMADEN."""
    try:
        alertas = await _cemaden.get_alertas_estado(uf)
        return AlertaResponse(uf=uf.upper(), total=len(alertas), items=alertas)
    except Exception as e:
        logger.error(f"CEMADEN alertas error: {e}")
        raise HTTPException(status_code=502, detail=f"CEMADEN API indisponível: {e}")


@router.get("/cemaden/pluviometros")
async def cemaden_pluviometros(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
):
    """Get CEMADEN rain gauges near coordinates."""
    try:
        return await _cemaden.get_pluviometros_proximos(lat, lon)
    except Exception as e:
        logger.error(f"CEMADEN pluviometros error: {e}")
        raise HTTPException(status_code=502, detail=f"CEMADEN API indisponível: {e}")
