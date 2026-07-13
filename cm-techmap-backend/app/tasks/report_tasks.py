"""
CM TECHMAP — Report Celery Tasks
Async report generation with database integration and MinIO storage.
"""

import logging
import uuid
from datetime import datetime, timezone

from celery import shared_task

from app.config import get_settings

logger = logging.getLogger("cm_techmap.tasks.reports")
settings = get_settings()


def _build_fallback_narrative(project_data: dict, analytics_data: dict) -> dict[str, str]:
    """Deterministic narrative fallback when Groq is unavailable."""
    total_buildings = int(analytics_data.get("total_buildings", 0) or 0)
    built_area = float(analytics_data.get("built_area_total_sqm", 0) or 0)
    terrain_area = float(analytics_data.get("terrain_area_total_sqm", 0) or 0)
    qa_score = float(analytics_data.get("qa_score", 0) or 0)

    return {
        "executive_summary": (
            f"Projeto {project_data.get('name', 'sem nome')} com {total_buildings} edificações "
            f"detectadas e área construída total de {built_area:.2f} m2."
        ),
        "fiscal_analysis": (
            f"A análise fiscal deve considerar a área construída ({built_area:.2f} m2) em relação "
            f"à área de terreno ({terrain_area:.2f} m2) conforme parâmetros municipais vigentes."
        ),
        "qa_analysis": (
            f"QA score consolidado do último run: {qa_score:.4f}. Recomenda-se revisão amostral "
            "para os casos de menor confiança."
        ),
        "recommendations": (
            "Priorizar auditoria em clusters de maior densidade construtiva e manter rotina de "
            "reprocessamento para estabilidade temporal das medições."
        ),
    }


@shared_task(
    name="app.tasks.report_tasks.generate_project_report",
    bind=True,
    max_retries=2,
    default_retry_delay=30,
)
def generate_project_report(
    self,
    report_id: str,
    project_id: str,
    report_type: str = "project_summary",
    output_format: str = "pdf",
    report_config: dict | None = None,
) -> dict:
    """
    Generate a complete project report (PDF or Excel).

    Steps:
    1. Fetch project, flights, and assets from database
    2. Generate report content via ReportGeneratorService
    3. Upload to MinIO
    4. Update report record in database
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    logger.info(f"[REPORT] Starting report generation: %s (%s, type=%s)", report_id, output_format, report_type)

    engine = create_engine(settings.database_url_sync)

    try:
        with Session(engine) as session:
            # Update status to generating
            session.execute(
                text("UPDATE public.reports SET status = 'generating' WHERE id = :id"),
                {"id": report_id},
            )
            session.commit()

            # ── 1. Fetch project data ──────────────────────────────────────
            result = session.execute(
                text("SELECT * FROM public.projects WHERE id = :id"),
                {"id": project_id},
            )
            project_row = result.mappings().first()
            if not project_row:
                raise ValueError(f"Project {project_id} not found")

            project_data = dict(project_row)

            # ── 2. Fetch flights ───────────────────────────────────────────
            result = session.execute(
                text("""
                    SELECT * FROM public.flights
                    WHERE project_id = :pid AND is_active = true
                    ORDER BY flight_date DESC
                """),
                {"pid": project_id},
            )
            flights_data = [dict(r) for r in result.mappings().all()]

            # ── 3. Fetch assets ────────────────────────────────────────────
            flight_ids = [str(f["id"]) for f in flights_data]
            assets_data = []
            if flight_ids:
                # Build parameterized query for flight IDs
                placeholders = ", ".join(f":fid_{i}" for i in range(len(flight_ids)))
                params = {f"fid_{i}": fid for i, fid in enumerate(flight_ids)}
                result = session.execute(
                    text(f"""
                        SELECT * FROM public.flight_assets
                        WHERE flight_id IN ({placeholders}) AND is_active = true
                        ORDER BY asset_type
                    """),
                    params,
                )
                assets_data = [dict(r) for r in result.mappings().all()]

            # ── 4. Fetch latest AI measurement run + buildings ─────────────
            analytics_data = {}
            buildings_data = []
            terrain_data = []
            try:
                run_row = session.execute(
                    text("""
                        SELECT id, qa_score, terrain_area_total_sqm, built_area_total_sqm,
                               total_buildings, total_terrain_patches, completed_at
                        FROM ai_measurement_runs
                        WHERE project_id = :pid AND status = 'completed'
                        ORDER BY completed_at DESC NULLS LAST
                        LIMIT 1
                    """),
                    {"pid": project_id},
                ).mappings().first()

                if run_row:
                    analytics_data = {
                        "measurement_run_id": str(run_row["id"]),
                        "qa_score": float(run_row["qa_score"] or 0),
                        "terrain_area_total_sqm": float(run_row["terrain_area_total_sqm"] or 0),
                        "built_area_total_sqm": float(run_row["built_area_total_sqm"] or 0),
                        "total_buildings": int(run_row["total_buildings"] or 0),
                        "total_terrain_patches": int(run_row["total_terrain_patches"] or 0),
                        "completed_at": run_row["completed_at"].isoformat() if run_row.get("completed_at") else None,
                    }

                    b_rows = session.execute(
                        text("""
                            SELECT area_sqm, perimeter_m, height_m, confidence,
                                   building_type, floors_estimate, quality_score
                            FROM ai_building_measurements
                            WHERE run_id = :rid
                            ORDER BY area_sqm DESC
                            LIMIT 3000
                        """),
                        {"rid": str(run_row["id"])},
                    ).mappings().all()

                    buildings_data = [dict(r) for r in b_rows]

                    t_rows = session.execute(
                        text("""
                            SELECT area_sqm, perimeter_m, confidence, compactness, surface_type
                            FROM ai_terrain_measurements
                            WHERE run_id = :rid
                            ORDER BY area_sqm DESC
                            LIMIT 3000
                        """),
                        {"rid": str(run_row["id"])},
                    ).mappings().all()
                    terrain_data = [dict(r) for r in t_rows]
            except Exception:
                # Optional runtime tables may not exist yet in all environments.
                analytics_data = {}
                buildings_data = []
                terrain_data = []

            # ── 5. Generate report ─────────────────────────────────────────
            from app.services.report_generator import ReportGeneratorService
            generator = ReportGeneratorService()
            config = dict(report_config or {})
            config["report_profile"] = report_type
            groq_cfg = dict(config.get("groq") or {})
            groq_narrative_enabled = bool(groq_cfg.get("enable_narrative", True))
            groq_model_override = groq_cfg.get("model_override")
            groq_tokens_override = groq_cfg.get("max_completion_tokens")

            narrative = _build_fallback_narrative(project_data, analytics_data)
            if settings.groq_enabled and settings.groq_report_enabled and groq_narrative_enabled:
                try:
                    from app.services.groq_inference import GroqInferenceService

                    groq = GroqInferenceService()
                    narrative = groq.generate_report_narrative(
                        report_profile=report_type,
                        project_data=project_data,
                        analytics_data=analytics_data,
                        config=config,
                        model_override=str(groq_model_override) if groq_model_override else None,
                        max_completion_tokens_override=int(groq_tokens_override) if groq_tokens_override else None,
                    )
                except Exception as exc:
                    logger.warning("[REPORT] Groq narrative failed, fallback enabled: %s", exc)
            config["ai_narrative"] = narrative

            if output_format == "pdf":
                report_bytes = generator.generate_pdf(
                    project_data,
                    flights_data,
                    assets_data,
                    config,
                    buildings_data=buildings_data,
                    analytics_data=analytics_data,
                    terrain_data=terrain_data,
                )
                content_type = "application/pdf"
                extension = "pdf"
            else:
                report_bytes = generator.generate_excel(
                    project_data, flights_data, assets_data, config,
                )
                content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                extension = "xlsx"

            # ── 5. Upload to MinIO ─────────────────────────────────────────
            from app.core.storage import get_minio_client
            import io

            minio_client = get_minio_client()
            bucket = settings.minio_bucket_reports
            project_code = project_data.get("code", "unknown")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_key = f"reports/{project_code}/{timestamp}_report.{extension}"

            # Ensure bucket exists
            if not minio_client.bucket_exists(bucket):
                minio_client.make_bucket(bucket)

            minio_client.put_object(
                bucket,
                file_key,
                io.BytesIO(report_bytes),
                length=len(report_bytes),
                content_type=content_type,
            )
            logger.info(f"[REPORT] Uploaded to MinIO: {bucket}/{file_key}")

            # ── 6. Update database ─────────────────────────────────────────
            session.execute(
                text("""
                    UPDATE public.reports SET
                        status = 'completed',
                        file_key = :file_key,
                        file_size_bytes = :size
                    WHERE id = :id
                """),
                {
                    "id": report_id,
                    "file_key": f"{bucket}/{file_key}",
                    "size": len(report_bytes),
                },
            )
            session.commit()

            logger.info(f"[REPORT] Report {report_id} completed: {len(report_bytes)} bytes")

            return {
                "report_id": report_id,
                "status": "completed",
                "file_key": f"{bucket}/{file_key}",
                "file_size_bytes": len(report_bytes),
                "format": output_format,
                "report_type": report_type,
            }

    except Exception as exc:
        logger.error(f"[REPORT] Report {report_id} failed: {exc}")
        # Update status to failed
        try:
            with Session(engine) as session:
                session.execute(
                    text("UPDATE public.reports SET status = 'failed', error_message = :err WHERE id = :id"),
                    {"id": report_id, "err": str(exc)[:1000]},
                )
                session.commit()
        except Exception:
            pass
        raise self.retry(exc=exc)
    finally:
        engine.dispose()


@shared_task(
    name="app.tasks.report_tasks.generate_comparison_report",
    bind=True,
    max_retries=2,
)
def generate_comparison_report(
    self,
    report_id: str,
    project_id: str,
    flight_ids: list[str],
    output_format: str = "pdf",
) -> dict:
    """
    Generate a temporal comparison report between multiple flights.
    Shows changes over time in the mapped area.
    """
    logger.info(f"[REPORT] Starting comparison report: {report_id}")

    # Delegate to project report with comparison config
    return generate_project_report.apply(
        kwargs={
            "report_id": report_id,
            "project_id": project_id,
            "report_type": "comparison",
            "output_format": output_format,
            "report_config": {"comparison_flights": flight_ids},
        },
    ).get()
