"""
CM TECHMAP — Celery Application
Async task queue for heavy processing (photogrammetry, AI, reports).
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "cm_techmap",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/Sao_Paulo",
    enable_utc=True,

    # Task execution
    task_track_started=True,
    task_time_limit=7200,           # 2 hours hard limit
    task_soft_time_limit=6900,      # 1h55m soft limit (for cleanup)
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks (memory leak protection)
    worker_prefetch_multiplier=1,   # Fair scheduling for long tasks

    # Result management
    result_expires=86400,           # Results expire after 24h

    # Task routing — separate queues for different workloads
    task_routes={
        "app.tasks.processing.process_drone_upload": {"queue": "processing"},
        "app.tasks.processing.run_ai_pipeline": {"queue": "processing"},
        "app.tasks.processing.run_ai_pipeline_groq": {"queue": "ai_llm"},
        "app.tasks.processing.generate_report": {"queue": "reports"},
        "app.tasks.post_processing.convert_orthomosaic_to_cog": {"queue": "processing"},
        "app.tasks.post_processing.generate_dsm_and_buildings": {"queue": "processing"},
        "app.tasks.post_processing.extract_buildings_from_real_dsm": {"queue": "processing"},
        "app.tasks.post_processing.extract_and_store_metadata": {"queue": "default"},
        "app.tasks.post_processing.generate_thumbnail": {"queue": "default"},
        "app.tasks.report_tasks.generate_project_report": {"queue": "reports"},
        "app.tasks.report_tasks.generate_comparison_report": {"queue": "reports"},
    },
    task_default_queue="default",

    # Retry policy
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)

# Explicitly register all task modules so Celery discovers them reliably
celery_app.conf.update(include=[
    "app.tasks.processing",
    "app.tasks.post_processing",
    "app.tasks.report_tasks",
    "app.tasks.maintenance",
])

# ── Celery Beat — Scheduled Tasks ─────────────────────────────────────────────
from celery.schedules import crontab

celery_app.conf.beat_schedule = {
    # Backup database every 6 hours
    "backup-database": {
        "task": "app.tasks.maintenance.backup_database",
        "schedule": crontab(minute=0, hour="*/6"),
        "options": {"queue": "default"},
    },
    # Clean expired/incomplete uploads every hour
    "cleanup-expired-uploads": {
        "task": "app.tasks.maintenance.cleanup_expired_uploads",
        "schedule": crontab(minute=30),
        "options": {"queue": "default"},
    },
    # Tenant schema health check every 30 minutes
    "tenant-health-check": {
        "task": "app.tasks.maintenance.tenant_health_check",
        "schedule": 1800.0,  # 30 minutes
        "options": {"queue": "default"},
    },
    # Clean old read notifications daily at 3 AM
    "notification-cleanup": {
        "task": "app.tasks.maintenance.cleanup_old_notifications",
        "schedule": crontab(minute=0, hour=3),
        "options": {"queue": "default"},
    },
    # Metrics snapshot every 5 minutes
    "metrics-snapshot": {
        "task": "app.tasks.maintenance.metrics_snapshot",
        "schedule": 300.0,  # 5 minutes
        "options": {"queue": "default"},
    },
}

