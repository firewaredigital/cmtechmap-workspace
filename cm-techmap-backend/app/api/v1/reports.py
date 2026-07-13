"""
CM TECHMAP — Reports API
Endpoints for generating, listing, and downloading reports.
"""

import uuid
import logging
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.storage import get_minio_client
from app.config import get_settings
from app.dependencies import require_super_admin
from app.schemas.report import (
    ReportGenerateRequest,
    ReportRead,
    ReportListResponse,
    ReportConfigPresetResponse,
    ReportConfigPreset,
    ReportConfigPresetUpsertRequest,
)

logger = logging.getLogger("cm_techmap.api.reports")
settings = get_settings()

router = APIRouter(prefix="/reports", tags=["Reports"])


REPORT_CONFIG_PRESETS: dict[str, dict[str, object]] = {
    "5208707": {
        "municipality_name": "Goiania",
        "iptu_rate_per_sqm": 12.0,
        "assumed_irregular_share": 0.25,
        "qa_threshold": 0.80,
        "notes": "Preset inicial para validacao com equipe fiscal municipal.",
    },
    "3550308": {
        "municipality_name": "Sao Paulo",
        "iptu_rate_per_sqm": 19.5,
        "assumed_irregular_share": 0.21,
        "qa_threshold": 0.84,
        "notes": "Zona densa urbana: calibracao conservadora para arrecadacao.",
    },
    "3304557": {
        "municipality_name": "Rio de Janeiro",
        "iptu_rate_per_sqm": 17.2,
        "assumed_irregular_share": 0.24,
        "qa_threshold": 0.82,
        "notes": "Uso misto: pondera ocupacoes formais e expansoes nao registradas.",
    },
}


SUPPORTED_REPORT_PROFILES = [
    "project_summary",
    "comparison",
    "flight_detail",
    "custom",
    "property_appraisal",
    "project_consolidated",
    "fiscal_revenue",
    "technical_qa",
]


async def _load_presets_from_db(session: AsyncSession) -> list[ReportConfigPreset]:
    """Load latest active preset version per municipality from database."""
    rows = await session.execute(text("""
        SELECT DISTINCT ON (municipality_code)
            municipality_code,
            municipality_name,
            iptu_rate_per_sqm,
            assumed_irregular_share,
            qa_threshold,
            notes
        FROM public.report_config_presets
        WHERE is_active = true
        ORDER BY municipality_code, version DESC, created_at DESC
    """))

    return [
        ReportConfigPreset(
            municipality_code=str(r["municipality_code"]),
            municipality_name=str(r["municipality_name"]),
            iptu_rate_per_sqm=float(r["iptu_rate_per_sqm"]),
            assumed_irregular_share=float(r["assumed_irregular_share"]),
            qa_threshold=float(r["qa_threshold"]),
            notes=str(r["notes"]) if r["notes"] else None,
        )
        for r in rows.mappings().all()
    ]


async def _effective_presets(session: AsyncSession) -> dict[str, dict[str, object]]:
    """Return DB presets when available, otherwise fallback to static defaults."""
    try:
        presets = await _load_presets_from_db(session)
        if presets:
            return {
                p.municipality_code: {
                    "municipality_name": p.municipality_name,
                    "iptu_rate_per_sqm": p.iptu_rate_per_sqm,
                    "assumed_irregular_share": p.assumed_irregular_share,
                    "qa_threshold": p.qa_threshold,
                    "notes": p.notes,
                }
                for p in presets
            }
    except Exception as exc:
        logger.warning("Preset table unavailable, using static defaults: %s", exc)

    return REPORT_CONFIG_PRESETS


def _resolve_report_config(
    project: dict,
    report_config: dict | None,
    report_type: str,
    preset_map: dict[str, dict[str, object]],
) -> dict:
    resolved = dict(report_config or {})

    # Accept explicit code first; fallback to common city-based defaults.
    municipality_code = str(resolved.get("municipality_code") or "").strip()
    if not municipality_code:
        city = str(project.get("city") or "").strip().lower()
        city_map = {
            "goiania": "5208707",
            "sao paulo": "3550308",
            "rio de janeiro": "3304557",
        }
        municipality_code = city_map.get(city, "5208707")

    default_preset = preset_map.get("5208707") or next(iter(preset_map.values()))
    preset = preset_map.get(municipality_code, default_preset)

    # Set defaults only when caller did not provide explicit overrides.
    resolved.setdefault("municipality_code", municipality_code)
    resolved.setdefault("iptu_rate_per_sqm", preset["iptu_rate_per_sqm"])
    resolved.setdefault("assumed_irregular_share", preset["assumed_irregular_share"])
    resolved.setdefault("qa_threshold", preset["qa_threshold"])
    resolved.setdefault("report_profile", report_type)

    return resolved


@router.get("/config/presets", response_model=ReportConfigPresetResponse)
async def list_report_config_presets(
    session: AsyncSession = Depends(get_db_session),
):
    """Expose supported fiscal/QA presets used by advanced report generation."""
    preset_map = await _effective_presets(session)
    presets = [
        ReportConfigPreset(
            municipality_code=code,
            municipality_name=str(meta["municipality_name"]),
            iptu_rate_per_sqm=float(meta["iptu_rate_per_sqm"]),
            assumed_irregular_share=float(meta["assumed_irregular_share"]),
            qa_threshold=float(meta["qa_threshold"]),
            notes=str(meta.get("notes")) if meta.get("notes") else None,
        )
        for code, meta in preset_map.items()
    ]

    return ReportConfigPresetResponse(
        presets=presets,
        supported_profiles=SUPPORTED_REPORT_PROFILES,
    )


@router.post("/config/presets", response_model=ReportConfigPreset)
async def upsert_report_config_preset(
    request: ReportConfigPresetUpsertRequest,
    session: AsyncSession = Depends(get_db_session),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Create a new active version for one municipality report preset."""
    try:
        latest = await session.execute(text("""
            SELECT COALESCE(MAX(version), 0) AS max_version
            FROM public.report_config_presets
            WHERE municipality_code = :code
        """), {"code": request.municipality_code})
        current_version = int(latest.scalar() or 0)
        next_version = current_version + 1

        await session.execute(text("""
            UPDATE public.report_config_presets
            SET is_active = false, updated_at = NOW(), updated_by = :updated_by
            WHERE municipality_code = :code AND is_active = true
        """), {
            "code": request.municipality_code,
            "updated_by": str(user.get("email") or user.get("sub") or "unknown"),
        })

        result = await session.execute(text("""
            INSERT INTO public.report_config_presets (
                municipality_code,
                municipality_name,
                iptu_rate_per_sqm,
                assumed_irregular_share,
                qa_threshold,
                version,
                is_active,
                notes,
                created_by,
                updated_by
            )
            VALUES (
                :code,
                :name,
                :iptu,
                :share,
                :qa,
                :version,
                true,
                :notes,
                :actor,
                :actor
            )
            RETURNING municipality_code, municipality_name,
                      iptu_rate_per_sqm, assumed_irregular_share,
                      qa_threshold, notes
        """), {
            "code": request.municipality_code,
            "name": request.municipality_name,
            "iptu": request.iptu_rate_per_sqm,
            "share": request.assumed_irregular_share,
            "qa": request.qa_threshold,
            "version": next_version,
            "notes": request.notes,
            "actor": str(user.get("email") or user.get("sub") or "unknown"),
        })
        await session.commit()

        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=500, detail="Falha ao persistir preset")

        return ReportConfigPreset(
            municipality_code=str(row["municipality_code"]),
            municipality_name=str(row["municipality_name"]),
            iptu_rate_per_sqm=float(row["iptu_rate_per_sqm"]),
            assumed_irregular_share=float(row["assumed_irregular_share"]),
            qa_threshold=float(row["qa_threshold"]),
            notes=str(row["notes"]) if row.get("notes") else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to upsert report preset: %s", exc)
        raise HTTPException(status_code=500, detail="Falha ao salvar preset")


@router.delete("/config/presets/{municipality_code}", status_code=204)
async def deactivate_report_config_preset(
    municipality_code: str,
    session: AsyncSession = Depends(get_db_session),
    user: dict[str, Any] = Depends(require_super_admin),
):
    """Deactivate active preset for a municipality (soft delete)."""
    result = await session.execute(text("""
        UPDATE public.report_config_presets
        SET is_active = false,
            updated_at = NOW(),
            updated_by = :actor
        WHERE municipality_code = :code AND is_active = true
        RETURNING id
    """), {
        "code": municipality_code,
        "actor": str(user.get("email") or user.get("sub") or "unknown"),
    })

    if not result.mappings().first():
        raise HTTPException(status_code=404, detail="Preset não encontrado")

    await session.commit()


@router.post("", response_model=ReportRead, status_code=202)
async def generate_report(
    request: ReportGenerateRequest,
    session: AsyncSession = Depends(get_db_session),
):
    """
    Request generation of a new report.
    The report is generated asynchronously via Celery.
    Returns immediately with the report record (status: pending).
    """
    report_id = str(uuid.uuid4())

    # Verify project exists
    result = await session.execute(
        text("SELECT id, name FROM public.projects WHERE id = :id"),
        {"id": str(request.project_id)},
    )
    project = result.mappings().first()
    if not project:
        raise HTTPException(status_code=404, detail="Projeto não encontrado")

    # Create report record
    await session.execute(
        text("""
            INSERT INTO public.reports (id, project_id, title, report_type, output_format, status)
            VALUES (:id, :pid, :title, :rtype, :fmt, 'pending')
        """),
        {
            "id": report_id,
            "pid": str(request.project_id),
            "title": request.title,
            "rtype": request.report_type,
            "fmt": request.output_format,
        },
    )
    await session.commit()

    preset_map = await _effective_presets(session)
    resolved_config = _resolve_report_config(
        project=dict(project),
        report_config=request.config,
        report_type=request.report_type,
        preset_map=preset_map,
    )
    if request.groq is not None:
        resolved_config["groq"] = request.groq.model_dump(exclude_none=True)

    # Dispatch Celery task
    from app.tasks.report_tasks import generate_project_report
    task = generate_project_report.delay(
        report_id=report_id,
        project_id=str(request.project_id),
        report_type=request.report_type,
        output_format=request.output_format,
        report_config=resolved_config,
    )

    # Update celery_task_id
    await session.execute(
        text("UPDATE public.reports SET celery_task_id = :tid WHERE id = :id"),
        {"tid": task.id, "id": report_id},
    )
    await session.commit()

    logger.info(f"Report {report_id} queued (celery={task.id})")

    return ReportRead(
        id=uuid.UUID(report_id),
        project_id=request.project_id,
        title=request.title,
        report_type=request.report_type,
        output_format=request.output_format,
        status="pending",
        celery_task_id=task.id,
        file_key=None,
        file_size_bytes=None,
        requested_by=None,
        error_message=None,
        download_url=None,
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        updated_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


@router.get("", response_model=ReportListResponse)
async def list_reports(
    project_id: uuid.UUID | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """List reports with optional filtering by project and status."""
    conditions = []
    params: dict = {}

    if project_id:
        conditions.append("project_id = :pid")
        params["pid"] = str(project_id)
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Count
    count_result = await session.execute(
        text(f"SELECT COUNT(*) FROM public.reports {where_clause}"),
        params,
    )
    total = count_result.scalar() or 0

    # Paginate
    offset = (page - 1) * page_size
    result = await session.execute(
        text(f"""
            SELECT * FROM public.reports {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": page_size, "offset": offset},
    )
    rows = result.mappings().all()

    items = []
    for row in rows:
        download_url = None
        if row.get("file_key") and row.get("status") == "completed":
            try:
                parts = row["file_key"].split("/", 1)
                if len(parts) == 2:
                    client = get_minio_client()
                    download_url = client.presigned_get_object(
                        parts[0], parts[1], expires=timedelta(hours=1),
                    )
            except Exception:
                pass

        items.append(ReportRead(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            report_type=row["report_type"],
            output_format=row["output_format"],
            status=row["status"],
            celery_task_id=row.get("celery_task_id"),
            file_key=row.get("file_key"),
            file_size_bytes=row.get("file_size_bytes"),
            requested_by=row.get("requested_by"),
            error_message=row.get("error_message"),
            download_url=download_url,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        ))

    return ReportListResponse(total=total, page=page, page_size=page_size, items=items)


@router.get("/{report_id}", response_model=ReportRead)
async def get_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
):
    """Get report details with download URL (if completed)."""
    result = await session.execute(
        text("SELECT * FROM public.reports WHERE id = :id"),
        {"id": str(report_id)},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")

    download_url = None
    if row.get("file_key") and row["status"] == "completed":
        try:
            parts = row["file_key"].split("/", 1)
            if len(parts) == 2:
                client = get_minio_client()
                download_url = client.presigned_get_object(
                    parts[0], parts[1], expires=timedelta(hours=1),
                )
        except Exception:
            pass

    return ReportRead(
        id=row["id"],
        project_id=row["project_id"],
        title=row["title"],
        report_type=row["report_type"],
        output_format=row["output_format"],
        status=row["status"],
        celery_task_id=row.get("celery_task_id"),
        file_key=row.get("file_key"),
        file_size_bytes=row.get("file_size_bytes"),
        requested_by=row.get("requested_by"),
        error_message=row.get("error_message"),
        download_url=download_url,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.delete("/{report_id}", status_code=204)
async def delete_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
):
    """Delete a report and its file from MinIO."""
    result = await session.execute(
        text("SELECT file_key FROM public.reports WHERE id = :id"),
        {"id": str(report_id)},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Relatório não encontrado")

    # Delete from MinIO
    if row.get("file_key"):
        try:
            parts = row["file_key"].split("/", 1)
            if len(parts) == 2:
                client = get_minio_client()
                client.remove_object(parts[0], parts[1])
        except Exception as e:
            logger.warning(f"Failed to delete file from MinIO: {e}")

    # Delete from database
    await session.execute(
        text("DELETE FROM public.reports WHERE id = :id"),
        {"id": str(report_id)},
    )
    await session.commit()
