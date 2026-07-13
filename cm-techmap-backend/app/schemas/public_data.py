"""CM TECHMAP — Public Data Schemas"""

from pydantic import BaseModel


# ── IBGE ──────────────────────────────────────────────────────────────────────
class EstadoRead(BaseModel):
    id: int
    sigla: str
    nome: str
    regiao: str


class MunicipioRead(BaseModel):
    id: int
    nome: str
    microrregiao: str
    mesorregiao: str


class MunicipioListResponse(BaseModel):
    uf: str
    total: int
    items: list[MunicipioRead]


# ── INMET ─────────────────────────────────────────────────────────────────────
class EstacaoMeteorologica(BaseModel):
    codigo: str
    nome: str
    uf: str
    latitude: float
    longitude: float
    altitude: float
    situacao: str


class ObservacaoMeteorologica(BaseModel):
    data_hora: str
    hora: str
    temperatura: float | None
    temperatura_max: float | None
    temperatura_min: float | None
    umidade: float | None
    pressao: float | None
    vento_velocidade: float | None
    vento_direcao: float | None
    chuva: float | None
    radiacao: float | None


class EstacaoListResponse(BaseModel):
    uf: str
    total: int
    items: list[EstacaoMeteorologica]


class DadosEstacaoResponse(BaseModel):
    codigo: str
    total: int
    items: list[ObservacaoMeteorologica]


# ── CEMADEN ───────────────────────────────────────────────────────────────────
class AlertaResponse(BaseModel):
    uf: str
    total: int
    items: list[dict]


# ── Generic ───────────────────────────────────────────────────────────────────
class PublicDataSummary(BaseModel):
    """Summary of available public data providers."""
    providers: list[dict]
    total_providers: int
