"""
CM TECHMAP — Maintenance Tasks
Scheduled Celery tasks for system maintenance and health monitoring.
"""

import logging
import subprocess
from datetime import datetime, timezone, timedelta

from app.celery_app import celery_app
from app.config import get_settings

logger = logging.getLogger("cm_techmap.maintenance")
settings = get_settings()


@celery_app.task(name="app.tasks.maintenance.backup_database", bind=True, max_retries=2)
def backup_database(self):
    """Create a compressed database backup and upload to MinIO."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = f"/tmp/cm_techmap_backup_{timestamp}.sql.gz"

    try:
        # pg_dump → gzip
        cmd = (
            f"PGPASSWORD={settings.database_password} "
            f"pg_dump -h {settings.database_host} -p {settings.database_port} "
            f"-U {settings.database_user} -d {settings.database_name} "
            f"--format=plain --no-owner --no-privileges | gzip > {backup_file}"
        )
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=1800)

        if result.returncode != 0:
            logger.error(f"[BACKUP] pg_dump failed: {result.stderr}")
            raise self.retry(countdown=300, exc=Exception(result.stderr))

        # Upload to MinIO via mc or boto3
        import os
        file_size = os.path.getsize(backup_file)
        logger.info(f"[BACKUP] Created: {backup_file} ({file_size / 1024 / 1024:.1f} MB)")

        # Try uploading via MinIO client
        try:
            upload_cmd = (
                f"mc cp {backup_file} minio/cm-techmap-backups/ 2>/dev/null || "
                f"aws --endpoint-url http://{settings.minio_endpoint} "
                f"s3 cp {backup_file} s3://cm-techmap-backups/"
            )
            subprocess.run(upload_cmd, shell=True, timeout=600)
            logger.info(f"[BACKUP] Uploaded to MinIO: cm-techmap-backups/{os.path.basename(backup_file)}")
        except Exception as e:
            logger.warning(f"[BACKUP] MinIO upload failed (backup still on disk): {e}")

        # Cleanup old local backups (keep last 5)
        cleanup_cmd = "ls -t /tmp/cm_techmap_backup_*.sql.gz | tail -n +6 | xargs rm -f 2>/dev/null"
        subprocess.run(cleanup_cmd, shell=True)

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
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            # Find all tenant schemas
            result = conn.execute(text(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%'"
            ))
            schemas = [row[0] for row in result.fetchall()]
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
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            result = conn.execute(text(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%'"
            ))
            schemas = [row[0] for row in result.fetchall()]
            total = 0
            for schema in schemas:
                try:
                    res = conn.execute(text(
                        f'DELETE FROM "{schema}".activity_logs '
                        f"WHERE action = 'notification' AND created_at < :cutoff "
                        f"RETURNING id"
                    ), {"cutoff": cutoff})
                    total += len(res.fetchall())
                except Exception:
                    pass
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
