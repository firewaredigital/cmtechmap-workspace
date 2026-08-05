"""
CM TECHMAP — Telemetria de erros de uso

Duas metades com públicos opostos:

1. POST /telemetry/errors — captação. Chamada pelo frontend sempre que
   QUALQUER erro acontece durante o uso (erro de renderização React, chamada
   de API que falhou, exceção JS não tratada, promise rejeitada). Funciona
   COM ou SEM login: erro na tela de login também precisa ser registrado.
   A identidade gravada é resolvida do token quando ele existe (fonte
   confiável); o que o cliente declara só entra rotulado como não verificado.

2. GET/DELETE /admin/error-logs — leitura e limpeza, EXCLUSIVAS do
   super_admin da plataforma. Os técnicos das prefeituras não têm acesso:
   a tabela é compartilhada entre todos os municípios (decisão de produto —
   não é multi-tenant) e os registros carregam a jornada completa de uso.

O registro nunca pode derrubar a aplicação que ele observa: a captação
engole falhas próprias e responde 202 mesmo assim.
"""

import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user_from_request
from app.dependencies import get_public_db, require_super_admin

logger = logging.getLogger(__name__)

telemetry_router = APIRouter(prefix="/telemetry", tags=["Telemetry"])
error_logs_router = APIRouter(prefix="/admin/error-logs", tags=["Admin — Error Logs"])

# Tetos de sanidade: a jornada é um buffer circular no cliente, mas o
# servidor não confia — payload gigante não pode virar vetor de abuso.
MAX_JOURNEY_EVENTS = 300
MAX_MESSAGE_CHARS = 4_000
MAX_STACK_CHARS = 20_000
MAX_EXTRA_CHARS = 8_000


class JourneyEvent(BaseModel):
    t: str = Field(max_length=40)          # ISO timestamp do evento
    kind: str = Field(max_length=20)       # nav | click | api | auth | info
    label: str = Field(max_length=300)     # "clicou no botão 'Download'"
    detail: str | None = Field(default=None, max_length=500)


class ErrorReport(BaseModel):
    error_type: str = Field(max_length=40)   # react_error | api_error | js_error | unhandled_rejection
    message: str
    stack: str | None = None
    page_url: str | None = Field(default=None, max_length=2000)
    page_route: str | None = Field(default=None, max_length=500)
    app_version: str | None = Field(default=None, max_length=40)
    http: dict[str, Any] | None = None       # {method, url, status, body}
    journey: list[JourneyEvent] = Field(default_factory=list)
    # Declarado pelo cliente — usado apenas quando não há token válido
    user_name: str | None = Field(default=None, max_length=200)
    user_email: str | None = Field(default=None, max_length=320)
    tenant_slug: str | None = Field(default=None, max_length=100)
    extra: dict[str, Any] | None = None


@telemetry_router.post("/errors", status_code=202)
async def register_usage_error(
    report: ErrorReport,
    request: Request,
    db: AsyncSession = Depends(get_public_db),
):
    """Registra um erro de uso vindo do frontend (autenticado ou não)."""
    # Identidade: o token é a fonte da verdade; a declaração do cliente só
    # vale rotulada quando não há sessão (ex.: erro na tela de login).
    user_id = user_name = user_email = None
    try:
        user = await get_current_user_from_request(request)
        user_id = str(user.get("sub") or user.get("id") or "")
        user_name = user.get("name") or user.get("preferred_username")
        user_email = user.get("email")
    except Exception:
        if report.user_email:
            user_email = f"{report.user_email} (não verificado)"
            user_name = report.user_name
    try:
        journey = [e.model_dump() for e in report.journey[:MAX_JOURNEY_EVENTS]]
        http_payload = None
        if report.http:
            http_payload = json.dumps(report.http)[:MAX_EXTRA_CHARS]
        extra_payload = None
        if report.extra:
            extra_payload = json.dumps(report.extra)[:MAX_EXTRA_CHARS]

        await db.execute(text(
            "INSERT INTO public.usage_error_logs "
            "(error_type, message, stack, user_id, user_name, user_email, "
            " tenant_slug, page_url, page_route, user_agent, app_version, "
            " http, journey, extra) "
            "VALUES (:etype, :msg, :stack, :uid, :uname, :uemail, :tslug, "
            " :purl, :proute, :ua, :ver, CAST(:http AS jsonb), "
            " CAST(:journey AS jsonb), CAST(:extra AS jsonb))"
        ), {
            "etype": report.error_type[:40],
            "msg": report.message[:MAX_MESSAGE_CHARS],
            "stack": (report.stack or "")[:MAX_STACK_CHARS] or None,
            "uid": user_id,
            "uname": user_name,
            "uemail": user_email,
            "tslug": report.tenant_slug,
            "purl": report.page_url,
            "proute": report.page_route,
            "ua": (request.headers.get("user-agent") or "")[:400],
            "ver": report.app_version,
            "http": http_payload,
            "journey": json.dumps(journey),
            "extra": extra_payload,
        })
        await db.commit()
    except Exception as e:
        # Telemetria jamais derruba o uso — registra a própria falha e segue.
        logger.error(f"[TELEMETRY] Falha ao gravar erro de uso: {e}")
    return {"status": "registered"}


# ═══════════════════════════════════════════════════════════════════════════
# Leitura — EXCLUSIVA do super_admin da plataforma
# ═══════════════════════════════════════════════════════════════════════════

@error_logs_router.get("")
async def list_error_logs(
    q: str | None = None,
    error_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Lista paginada dos erros de uso de TODOS os usuários (mais novos primeiro)."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    where, params = ["TRUE"], {}
    if q:
        where.append("(message ILIKE :q OR user_email ILIKE :q OR user_name ILIKE :q OR page_route ILIKE :q)")
        params["q"] = f"%{q}%"
    if error_type:
        where.append("error_type = :etype")
        params["etype"] = error_type
    where_sql = " AND ".join(where)

    total = (await db.execute(text(
        f"SELECT count(*) FROM public.usage_error_logs WHERE {where_sql}"  # noqa: S608
    ), params)).scalar() or 0

    rows = (await db.execute(text(
        "SELECT id, occurred_at, error_type, message, user_name, user_email, "
        "tenant_slug, page_route, jsonb_array_length(journey) "
        f"FROM public.usage_error_logs WHERE {where_sql} "  # noqa: S608
        "ORDER BY occurred_at DESC LIMIT :limit OFFSET :offset"
    ), {**params, "limit": limit, "offset": offset})).fetchall()

    return {
        "total": total,
        "items": [{
            "id": str(r[0]),
            "occurred_at": r[1].isoformat(),
            "error_type": r[2],
            "message": r[3][:300],
            "user_name": r[4],
            "user_email": r[5],
            "tenant_slug": r[6],
            "page_route": r[7],
            "journey_steps": r[8] or 0,
        } for r in rows],
    }


@error_logs_router.get("/{log_id}")
async def get_error_log(
    log_id: uuid.UUID,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Registro completo: mensagem, stack, requisição HTTP e a jornada passo a passo."""
    row = (await db.execute(text(
        "SELECT id, occurred_at, error_type, message, stack, user_id, "
        "user_name, user_email, tenant_slug, page_url, page_route, "
        "user_agent, app_version, http, journey, extra "
        "FROM public.usage_error_logs WHERE id = :id"
    ), {"id": str(log_id)})).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Registro de erro não encontrado")
    return {
        "id": str(row[0]), "occurred_at": row[1].isoformat(),
        "error_type": row[2], "message": row[3], "stack": row[4],
        "user_id": row[5], "user_name": row[6], "user_email": row[7],
        "tenant_slug": row[8], "page_url": row[9], "page_route": row[10],
        "user_agent": row[11], "app_version": row[12],
        "http": row[13], "journey": row[14] or [], "extra": row[15],
    }


@error_logs_router.delete("/{log_id}", status_code=204)
async def delete_error_log(
    log_id: uuid.UUID,
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Remove um registro específico."""
    await db.execute(text(
        "DELETE FROM public.usage_error_logs WHERE id = :id"
    ), {"id": str(log_id)})
    await db.commit()


@error_logs_router.delete("", status_code=204)
async def clear_error_logs(
    db: AsyncSession = Depends(get_public_db),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Limpa TODOS os registros (ação do administrador da plataforma)."""
    await db.execute(text("DELETE FROM public.usage_error_logs"))
    await db.commit()
