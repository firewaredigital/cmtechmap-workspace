"""
CM TECHMAP — Maintenance Tasks
Scheduled Celery tasks for system maintenance and health monitoring.
"""

import contextlib
import glob
import logging
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit

from app.celery_app import celery_app
from app.config import get_settings

logger = logging.getLogger("cm_techmap.maintenance")
settings = get_settings()


def _database_connection_params() -> dict[str, str]:
    """
    Extract host/port/user/password/dbname from the configured database URL.

    The Settings model only exposes URLs (database_url_sync), never the
    individual components, so shelling out to pg_dump requires parsing.
    """
    parsed = urlsplit(settings.database_url_sync)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "user": unquote(parsed.username or "postgres"),
        "password": unquote(parsed.password or ""),
        "dbname": (parsed.path or "/postgres").lstrip("/") or "postgres",
    }


@celery_app.task(name="app.tasks.maintenance.backup_database", bind=True, max_retries=2)
def backup_database(self):
    """Create a compressed database backup and upload to MinIO."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_file = f"/tmp/cm_techmap_backup_{timestamp}.sql.gz"

    if shutil.which("pg_dump") is None:
        logger.error(
            "[BACKUP] pg_dump is not installed in this container — "
            "database backups are NOT being taken"
        )
        return {"status": "skipped", "reason": "pg_dump not installed"}

    conn = _database_connection_params()

    try:
        # pg_dump (plain SQL) → gzip; plain format restores with `gunzip | psql`
        cmd = (
            f"pg_dump -h {conn['host']} -p {conn['port']} "
            f"-U {conn['user']} -d {conn['dbname']} "
            f"--format=plain --no-owner --no-privileges | gzip > {backup_file}"
        )
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=1800,
            env={**os.environ, "PGPASSWORD": conn["password"]},
        )

        if result.returncode != 0:
            logger.error(f"[BACKUP] pg_dump failed: {result.stderr}")
            raise self.retry(countdown=300, exc=Exception(result.stderr))

        file_size = os.path.getsize(backup_file)
        logger.info(f"[BACKUP] Created: {backup_file} ({file_size / 1024 / 1024:.1f} MB)")

        # Upload via the MinIO SDK (no mc/aws CLI dependency)
        try:
            from app.core.storage import upload_file

            object_name = os.path.basename(backup_file)
            with open(backup_file, "rb") as fh:
                upload_file(
                    settings.minio_bucket_backups,
                    f"postgres/{object_name}",
                    fh,
                    file_size,
                    content_type="application/gzip",
                )
            logger.info(
                f"[BACKUP] Uploaded to MinIO: "
                f"{settings.minio_bucket_backups}/postgres/{object_name}"
            )
        except Exception as e:
            logger.warning(f"[BACKUP] MinIO upload failed (backup still on disk): {e}")

        # Cleanup old local backups (keep last 5)
        local_backups = sorted(
            glob.glob("/tmp/cm_techmap_backup_*.sql.gz"),
            key=os.path.getmtime,
            reverse=True,
        )
        for stale in local_backups[5:]:
            with contextlib.suppress(OSError):
                os.remove(stale)

        return {"status": "success", "file": backup_file, "size_mb": round(file_size / 1024 / 1024, 1)}

    except subprocess.TimeoutExpired:
        logger.error("[BACKUP] Timeout after 30 minutes")
        raise self.retry(countdown=600)
    except Exception as e:
        logger.error(f"[BACKUP] Failed: {e}")
        raise self.retry(countdown=300, exc=e)


@celery_app.task(name="app.tasks.maintenance.cleanup_expired_uploads")
def cleanup_expired_uploads():
    """Remove incomplete uploads older than 24 hours."""
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            cutoff = datetime.now(UTC) - timedelta(hours=24)
            # public first (chunked uploads live there), then tenant schemas
            result = conn.execute(text(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%'"
            ))
            schemas = ["public"] + [row[0] for row in result.fetchall()]
            total_cleaned = 0
            for schema in schemas:
                try:
                    res = conn.execute(text(
                        f'DELETE FROM "{schema}".uploads '
                        f"WHERE status = 'uploading' AND created_at < :cutoff "
                        f"RETURNING id"
                    ), {"cutoff": cutoff})
                    count = len(res.fetchall())
                    total_cleaned += count
                except Exception as e:
                    logger.warning(f"[CLEANUP] Error in {schema}: {e}")
            conn.commit()
            logger.info(f"[CLEANUP] Removed {total_cleaned} expired uploads")
            return {"cleaned": total_cleaned}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.maintenance.tenant_health_check")
def tenant_health_check():
    """Verify all active tenant schemas are accessible."""
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT slug FROM public.tenants WHERE is_active = true"
            ))
            slugs = [row[0] for row in result.fetchall()]
            report = {"total": len(slugs), "healthy": 0, "unhealthy": []}
            for slug in slugs:
                schema = f"tenant_{slug}"
                try:
                    conn.execute(text(f'SELECT 1 FROM "{schema}"._schema_metadata LIMIT 1'))
                    report["healthy"] += 1
                except Exception:
                    report["unhealthy"].append(slug)
            if report["unhealthy"]:
                logger.error(f"[HEALTH] Unhealthy tenants: {report['unhealthy']}")
            else:
                logger.info(f"[HEALTH] All {report['total']} tenants healthy")
            return report
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.maintenance.cleanup_old_notifications")
def cleanup_old_notifications():
    """Remove read notifications older than 30 days."""
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            cutoff = datetime.now(UTC) - timedelta(days=30)
            # Notifications live in public.notifications (see Alembic 003);
            # tenant schemas intentionally have no notifications table.
            res = conn.execute(text(
                "DELETE FROM public.notifications "
                "WHERE is_read = true AND created_at < :cutoff "
                "RETURNING id"
            ), {"cutoff": cutoff})
            total = len(res.fetchall())
            conn.commit()
            logger.info(f"[NOTIF-CLEANUP] Removed {total} old notifications")
            return {"cleaned": total}
    finally:
        engine.dispose()


@celery_app.task(name="app.tasks.maintenance.metrics_snapshot")
def metrics_snapshot():
    """Capture tenant usage metrics for monitoring."""
    from sqlalchemy import create_engine, text

    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT slug FROM public.tenants WHERE is_active = true"
            ))
            slugs = [row[0] for row in result.fetchall()]
            metrics = {}
            for slug in slugs:
                schema = f"tenant_{slug}"
                counts = {}
                for table in ["projects", "flights", "parcels", "discrepancies"]:
                    try:
                        res = conn.execute(text(f'SELECT COUNT(*) FROM "{schema}"."{table}"'))
                        counts[table] = res.scalar() or 0
                    except Exception:
                        counts[table] = -1
                metrics[slug] = counts
            logger.info(f"[METRICS] Snapshot for {len(metrics)} tenants")
            return metrics
    finally:
        engine.dispose()

@celery_app.task(name="app.tasks.maintenance.requeue_stuck_reports")
def requeue_stuck_reports(max_age_minutes: int = 10) -> dict:
    """
    AUTOCURA: relatório 'pending' há mais de N minutos sem tarefa viva é
    redespachado. Cobre worker substituído em atualização, queda de
    container e mensagem perdida — o gestor nunca fica com "pendente"
    eterno sem saber por quê.
    """
    from sqlalchemy import create_engine, text as sa_text

    from app.celery_app import celery_app as _app

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    requeued, checked = 0, 0
    try:
        insp = _app.control.inspect(timeout=3)
        live = set()
        for group in (insp.active() or {}, insp.reserved() or {}, insp.scheduled() or {}):
            for tasks in group.values():
                for t in tasks:
                    tid = t.get("id") if isinstance(t, dict) else None
                    if tid:
                        live.add(tid)
        with engine.connect() as conn:
            rows = conn.execute(sa_text(
                "SELECT id, project_id, report_type, output_format, celery_task_id "
                "FROM public.reports WHERE status = 'pending' "
                "AND created_at < now() - make_interval(mins => :age)"
            ), {"age": max_age_minutes}).fetchall()
            checked = len(rows)
            from app.tasks.report_tasks import generate_project_report
            for rid, pid, rtype, fmt, ctid in rows:
                if ctid and str(ctid) in live:
                    continue  # está rodando de verdade — não duplicar
                task = generate_project_report.apply_async(kwargs={
                    "report_id": str(rid), "project_id": str(pid),
                    "report_type": rtype or "project_summary",
                    "output_format": fmt or "pdf",
                    "report_config": None, "tenant_schema": None,
                })
                conn.execute(sa_text(
                    "UPDATE public.reports SET celery_task_id = :t WHERE id = :id"
                ), {"t": task.id, "id": rid})
                requeued += 1
            conn.commit()
    except Exception as e:
        logger.warning(f"[MAINT] requeue_stuck_reports: {e}")
    finally:
        engine.dispose()
    if requeued:
        logger.info(f"[MAINT] {requeued} relatório(s) órfão(s) redespachado(s) de {checked}")
    return {"checked": checked, "requeued": requeued}

@celery_app.task(name="app.tasks.maintenance.reanalyze_stale_detections")
def reanalyze_stale_detections(max_flights: int = 1) -> dict:
    """
    AUTO-REPROCESSAMENTO após atualização do sistema.

    O instalador atualiza o CÓDIGO, mas os dados já processados ficam como
    estavam — foi assim que uma máquina atualizada continuou exibindo
    sombras como imóveis: as detecções eram da geração anterior e a análise
    de telhados nunca tinha rodado nelas.

    Este vigia (beat, a cada 15 min) encontra voos cujas detecções ativas
    não têm roof_analysis e dispara a cadeia completa (validação 3D →
    adjudicação → telhados) para UM voo por vez — a máquina se corrige
    sozinha depois de atualizar, sem ninguém precisar reprocessar à mão.
    """
    from sqlalchemy import create_engine, text as sa_text

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    dispatched = []
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa_text(
                "SELECT fa.flight_id, count(*) AS pend "
                "FROM ai_detections d "
                "JOIN flight_assets fa ON d.flight_asset_id = fa.id "
                "WHERE fa.is_active AND d.polygon IS NOT NULL "
                "AND d.roof_analysis IS NULL "
                "GROUP BY fa.flight_id ORDER BY pend DESC LIMIT :n"
            ), {"n": max_flights}).fetchall()
        from app.tasks.post_processing import validate_detections_elevation
        for flight_id, pend in rows:
            validate_detections_elevation.apply_async(
                kwargs={"flight_id": str(flight_id), "tenant_schema": None}
            )
            dispatched.append({"flight_id": str(flight_id), "pending": int(pend)})
            logger.info(
                f"[MAINT] voo {flight_id}: {pend} detecção(ões) sem análise de "
                f"telhado — reprocessamento automático disparado"
            )
    except Exception as e:
        logger.warning(f"[MAINT] reanalyze_stale_detections: {e}")
    finally:
        engine.dispose()
    return {"dispatched": dispatched}

