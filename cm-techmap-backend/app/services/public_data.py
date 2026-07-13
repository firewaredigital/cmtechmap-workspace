"""
CM TECHMAP — Public Data Integration Service
Clients for IBGE, INMET, and CEMADEN Brazilian government APIs.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger("cm_techmap.services.public_data")

# ── Cache simples em memória (TTL-based) ─────────────────────────────────────
_cache: dict[str, tuple[datetime, Any]] = {}
CACHE_TTL = timedelta(minutes=30)


def _get_cached(key: str) -> Any | None:
    if key in _cache:
        ts, data = _cache[key]
        if datetime.now() - ts < CACHE_TTL:
            return data
        del _cache[key]
    return None


def _set_cached(key: str, data: Any) -> None:
    _cache[key] = (datetime.now(), data)


# =============================================================================
# IBGE — Instituto Brasileiro de Geografia e Estatística
# https://servicodados.ibge.gov.br/api/docs
# =============================================================================
class IBGEClient:
    """Client for IBGE geographic and demographic data APIs."""

    BASE_URL = "https://servicodados.ibge.gov.br/api/v1"
    BASE_URL_V3 = "https://servicodados.ibge.gov.br/api/v3"

    def __init__(self, timeout: float = 15.0):
        self._timeout = timeout

    async def get_estados(self) -> list[dict]:
        """List all Brazilian states (UFs)."""
        cache_key = "ibge:estados"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.BASE_URL}/localidades/estados?orderBy=nome")
            resp.raise_for_status()
            data = resp.json()

        result = [
            {
                "id": e["id"],
                "sigla": e["sigla"],
                "nome": e["nome"],
                "regiao": e.get("regiao", {}).get("nome", ""),
            }
            for e in data
        ]
        _set_cached(cache_key, result)
        return result

    async def get_municipios(self, uf: str) -> list[dict]:
        """List municipalities for a given state (UF sigla)."""
        cache_key = f"ibge:municipios:{uf.upper()}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self.BASE_URL}/localidades/estados/{uf.upper()}/municipios?orderBy=nome"
            )
            resp.raise_for_status()
            data = resp.json()

        result = [
            {
                "id": m["id"],
                "nome": m["nome"],
                "microrregiao": m.get("microrregiao", {}).get("nome", ""),
                "mesorregiao": m.get("microrregiao", {}).get("mesorregiao", {}).get("nome", ""),
            }
            for m in data
        ]
        _set_cached(cache_key, result)
        return result

    async def get_municipio_malha(self, codigo_ibge: int, formato: str = "application/json") -> dict:
        """
        Get the geospatial mesh (malha) of a municipality as GeoJSON.
        """
        cache_key = f"ibge:malha:{codigo_ibge}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self.BASE_URL}/localidades/municipios/{codigo_ibge}",
                headers={"Accept": formato},
            )
            resp.raise_for_status()
            data = resp.json()

        _set_cached(cache_key, data)
        return data

    async def get_malha_geojson(self, codigo_ibge: int, resolucao: int = 2) -> dict:
        """
        Get municipality boundary as GeoJSON.
        Resolution: 0=nenhuma, 1=mínima, 2=intermediária, 3=máxima
        """
        cache_key = f"ibge:malha_geo:{codigo_ibge}:{resolucao}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        url = (
            f"https://servicodados.ibge.gov.br/api/v3/malhas/municipios/{codigo_ibge}"
            f"?formato=application/vnd.geo+json&qualidade={resolucao}"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        _set_cached(cache_key, data)
        return data

    async def get_setores_censitarios(self, codigo_ibge: int) -> dict:
        """
        Get census sectors (setores censitários) for a municipality.
        """
        cache_key = f"ibge:setores:{codigo_ibge}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        url = (
            f"https://servicodados.ibge.gov.br/api/v3/malhas/municipios/{codigo_ibge}"
            f"?formato=application/vnd.geo+json&qualidade=3&intrarregiao=setor"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        _set_cached(cache_key, data)
        return data

    async def get_nomes_geograficos(self, lat: float, lon: float, raio_km: float = 10) -> list[dict]:
        """
        Search for geographic names (BNGB) near a coordinate.
        """
        cache_key = f"ibge:nomes:{lat:.4f}:{lon:.4f}:{raio_km}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        url = (
            f"https://servicodados.ibge.gov.br/api/v1/bngb"
            f"?lat={lat}&lon={lon}&raio={raio_km}"
        )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
            else:
                data = []

        _set_cached(cache_key, data)
        return data


# =============================================================================
# INMET — Instituto Nacional de Meteorologia
# https://portal.inmet.gov.br/manual/manual-de-uso-da-api-esta%C3%A7%C3%B5es
# =============================================================================
class INMETClient:
    """Client for INMET weather station data API."""

    BASE_URL = "https://apitempo.inmet.gov.br"

    def __init__(self, timeout: float = 15.0):
        self._timeout = timeout

    async def get_estacoes(self, tipo: str = "T") -> list[dict]:
        """
        List weather stations.
        tipo: T=automática, M=convencional
        """
        cache_key = f"inmet:estacoes:{tipo}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{self.BASE_URL}/estacoes/{tipo}")
            resp.raise_for_status()
            data = resp.json()

        result = [
            {
                "codigo": e.get("CD_ESTACAO", ""),
                "nome": e.get("DC_NOME", ""),
                "uf": e.get("SG_ESTADO", ""),
                "latitude": float(e.get("VL_LATITUDE", 0) or 0),
                "longitude": float(e.get("VL_LONGITUDE", 0) or 0),
                "altitude": float(e.get("VL_ALTITUDE", 0) or 0),
                "situacao": e.get("CD_SITUACAO", ""),
            }
            for e in data
        ]
        _set_cached(cache_key, result)
        return result

    async def get_estacoes_por_uf(self, uf: str) -> list[dict]:
        """Get weather stations filtered by UF."""
        all_stations = await self.get_estacoes()
        return [s for s in all_stations if s["uf"].upper() == uf.upper()]

    async def get_dados_estacao(self, codigo: str, data_inicio: str, data_fim: str) -> list[dict]:
        """
        Get station observation data for a date range.
        data_inicio/data_fim format: YYYY-MM-DD
        """
        cache_key = f"inmet:dados:{codigo}:{data_inicio}:{data_fim}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{self.BASE_URL}/estacao/dados/{data_inicio}/{data_fim}/{codigo}"
            )
            if resp.status_code == 200:
                data = resp.json()
            else:
                data = []

        result = []
        if isinstance(data, list):
            for obs in data:
                result.append({
                    "data_hora": obs.get("DT_MEDICAO", ""),
                    "hora": obs.get("HR_MEDICAO", ""),
                    "temperatura": _safe_float(obs.get("TEM_INS")),
                    "temperatura_max": _safe_float(obs.get("TEM_MAX")),
                    "temperatura_min": _safe_float(obs.get("TEM_MIN")),
                    "umidade": _safe_float(obs.get("UMD_INS")),
                    "pressao": _safe_float(obs.get("PRE_INS")),
                    "vento_velocidade": _safe_float(obs.get("VEN_VEL")),
                    "vento_direcao": _safe_float(obs.get("VEN_DIR")),
                    "chuva": _safe_float(obs.get("CHUVA")),
                    "radiacao": _safe_float(obs.get("RAD_GLO")),
                })
        _set_cached(cache_key, result)
        return result

    async def get_condicoes_atuais(self, codigo: str) -> dict | None:
        """Get current weather conditions from a station."""
        hoje = datetime.now().strftime("%Y-%m-%d")
        dados = await self.get_dados_estacao(codigo, hoje, hoje)
        return dados[-1] if dados else None

    async def get_estacao_mais_proxima(self, lat: float, lon: float) -> dict | None:
        """Find the nearest weather station to given coordinates."""
        estacoes = await self.get_estacoes()
        if not estacoes:
            return None

        def dist(e: dict) -> float:
            return ((e["latitude"] - lat) ** 2 + (e["longitude"] - lon) ** 2) ** 0.5

        return min(estacoes, key=dist)


# =============================================================================
# CEMADEN — Centro Nacional de Monitoramento e Alertas de Desastres Naturais
# http://www2.cemaden.gov.br/mapainterativo/
# =============================================================================
class CEMADENClient:
    """Client for CEMADEN disaster alerts and monitoring data."""

    BASE_URL = "http://resources.cemaden.gov.br/graficos/json"

    def __init__(self, timeout: float = 15.0):
        self._timeout = timeout

    async def get_municipios_monitorados(self) -> list[dict]:
        """Get list of municipalities monitored by CEMADEN."""
        cache_key = "cemaden:municipios"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(f"{self.BASE_URL}/listaMunicipios.json")
                if resp.status_code == 200:
                    data = resp.json()
                else:
                    data = []
            except Exception:
                data = []

        _set_cached(cache_key, data)
        return data if isinstance(data, list) else []

    async def get_alertas_estado(self, uf: str) -> list[dict]:
        """
        Get active disaster alerts for a state.
        Returns alerts with risk level, type, and affected areas.
        """
        cache_key = f"cemaden:alertas:{uf}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        # CEMADEN doesn't have a clean public API, so we use available endpoints
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"http://www2.cemaden.gov.br/wp-content/themes/flavor/getEstados.php",
                    params={"uf": uf.upper()},
                )
                if resp.status_code == 200:
                    data = resp.json()
                else:
                    data = []
            except Exception:
                # CEMADEN API may be unavailable — return empty
                logger.warning(f"CEMADEN API unavailable for UF={uf}")
                data = []

        _set_cached(cache_key, data)
        return data if isinstance(data, list) else []

    async def get_pluviometros_proximos(self, lat: float, lon: float) -> list[dict]:
        """
        Get rain gauges near coordinates from CEMADEN network.
        """
        cache_key = f"cemaden:pluviometros:{lat:.3f}:{lon:.3f}"
        cached = _get_cached(cache_key)
        if cached:
            return cached

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/listaPluviometros.json",
                )
                if resp.status_code == 200:
                    all_data = resp.json()
                    # Filter by proximity (rough ~50km radius)
                    result = []
                    for p in (all_data if isinstance(all_data, list) else []):
                        try:
                            plat = float(p.get("latitude", 0))
                            plon = float(p.get("longitude", 0))
                            dist = ((plat - lat) ** 2 + (plon - lon) ** 2) ** 0.5
                            if dist < 0.5:  # ~50km
                                p["distancia_graus"] = round(dist, 4)
                                result.append(p)
                        except (ValueError, TypeError):
                            continue
                    data = sorted(result, key=lambda x: x.get("distancia_graus", 999))[:20]
                else:
                    data = []
            except Exception:
                logger.warning("CEMADEN pluviometers API unavailable")
                data = []

        _set_cached(cache_key, data)
        return data


# ── Utility ───────────────────────────────────────────────────────────────────
def _safe_float(val: Any) -> float | None:
    """Safely convert a value to float, returning None if impossible."""
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
