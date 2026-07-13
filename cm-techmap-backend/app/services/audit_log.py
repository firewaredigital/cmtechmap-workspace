"""
CM TECHMAP — Audit Log Service
Writes structured activity records to the tenant's activity_logs table.
Used to track all significant user and system actions for compliance.
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("cm_techmap.audit")


class AuditAction:
    """Constants for audit log action types."""
    # Auth
    LOGIN = "auth.login"
    LOGIN_FAILED = "auth.login_failed"
    LOGOUT = "auth.logout"
    REGISTER = "auth.register"
    PASSWORD_RESET = "auth.password_reset"

    # Projects
    PROJECT_CREATE = "project.create"
    PROJECT_UPDATE = "project.update"
    PROJECT_DELETE = "project.delete"

    # Flights
    FLIGHT_CREATE = "flight.create"
    FLIGHT_UPDATE = "flight.update"
    FLIGHT_DELETE = "flight.delete"

    # Uploads
    UPLOAD_START = "upload.start"
    UPLOAD_COMPLETE = "upload.complete"
    UPLOAD_FAILED = "upload.failed"

    # Processing
    PROCESSING_START = "processing.start"
    PROCESSING_COMPLETE = "processing.complete"
    PROCESSING_FAILED = "processing.failed"

    # Reports
    REPORT_GENERATE = "report.generate"
    REPORT_DOWNLOAD = "report.download"

    # Admin
    USER_CREATE = "admin.user_create"
    USER_UPDATE = "admin.user_update"
    USER_DISABLE = "admin.user_disable"
    USER_ENABLE = "admin.user_enable"
    USER_ROLE_CHANGE = "admin.user_role_change"
    USER_SESSIONS_LOGOUT = "admin.user_sessions_logout"

    # Subscription
    SUBSCRIPTION_CREATE = "subscription.create"
    SUBSCRIPTION_UPDATE = "subscription.update"
    SUBSCRIPTION_CANCEL = "subscription.cancel"


async def write_audit_log(
    session: AsyncSession,
    *,
    action: str,
    user_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    schema: str | None = None,
) -> None:
    """
    Write an audit log entry to the activity_logs table.

    If schema is provided, writes to that tenant schema.
    Otherwise writes to public schema (for platform-level events).
    """
    import json

    target_schema = schema or "public"
    details_json = json.dumps(details) if details else "{}"

    try:
        await session.execute(
            text(f"""
                INSERT INTO "{target_schema}".activity_logs
                    (action, user_id, entity_type, entity_id, details, ip_address, user_agent)
                VALUES
                    (:action, :user_id, :entity_type, :entity_id,
                     CAST(:details AS jsonb), :ip_address, :user_agent)
            """),
            {
                "action": action,
                "user_id": user_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "details": details_json,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        logger.debug(
            f"Audit: {action} | user={user_id} | entity={entity_type}:{entity_id}"
        )
    except Exception as e:
        # Audit log failure should NEVER break the main operation.
        # Log the error and continue silently.
        logger.warning(f"Failed to write audit log: {e} (action={action})")


def write_audit_log_sync(
    *,
    action: str,
    user_id: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    schema: str | None = None,
) -> None:
    """
    Synchronous version for Celery workers.
    Uses a sync SQLAlchemy engine to write audit entries.
    """
    import json
    from sqlalchemy import create_engine, text as sa_text
    from app.config import get_settings

    settings = get_settings()
    target_schema = schema or "public"
    details_json = json.dumps(details) if details else "{}"

    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            conn.execute(
                sa_text(f"""
                    INSERT INTO "{target_schema}".activity_logs
                        (action, user_id, entity_type, entity_id, details, ip_address, user_agent)
                    VALUES
                        (:action, :user_id, :entity_type, :entity_id,
                         CAST(:details AS jsonb), :ip_address, :user_agent)
                """),
                {
                    "action": action,
                    "user_id": user_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "details": details_json,
                    "ip_address": ip_address,
                    "user_agent": user_agent,
                },
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"Failed to write sync audit log: {e} (action={action})")
    finally:
        engine.dispose()


def extract_request_context(request: Any) -> dict[str, str | None]:
    """Extract IP and user-agent from a Starlette/FastAPI request."""
    ip = None
    ua = None
    try:
        ip = request.headers.get("X-Real-IP") or (
            request.client.host if request.client else None
        )
        ua = request.headers.get("User-Agent", "")[:500]  # Truncate long UAs
    except Exception:
        pass
    return {"ip_address": ip, "user_agent": ua}
