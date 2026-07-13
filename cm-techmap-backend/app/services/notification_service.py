"""
CM TECHMAP — Notification Service
Manages in-app notifications with DB persistence and Redis PubSub for real-time delivery.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("cm_techmap.notifications")


class NotificationService:
    """Service for creating, reading, and managing notifications."""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        user_id: str,
        title: str,
        message: str,
        notification_type: str = "info",
        category: str = "system",
        link: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        schema: str | None = None,
    ) -> dict | None:
        """Create a notification and optionally push via Redis PubSub."""
        target_schema = schema or "public"

        try:
            result = await session.execute(
                text(f"""
                    INSERT INTO "{target_schema}".notifications
                        (user_id, title, message, type, category, link, entity_type, entity_id)
                    VALUES
                        (:user_id, :title, :message, :type, :category, :link, :entity_type, :entity_id)
                    RETURNING id, created_at
                """),
                {
                    "user_id": user_id,
                    "title": title,
                    "message": message,
                    "type": notification_type,
                    "category": category,
                    "link": link,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                },
            )
            row = result.fetchone()
            await session.commit()

            notification = {
                "id": str(row[0]),
                "user_id": user_id,
                "title": title,
                "message": message,
                "type": notification_type,
                "category": category,
                "link": link,
                "is_read": False,
                "created_at": row[1].isoformat() if row[1] else datetime.now(timezone.utc).isoformat(),
            }

            # Push to Redis PubSub for real-time delivery
            await _publish_notification(user_id, notification)

            logger.debug(f"Notification created for user {user_id}: {title}")
            return notification

        except Exception as e:
            logger.warning(f"Failed to create notification: {e}")
            return None

    @staticmethod
    async def list_for_user(
        session: AsyncSession,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        unread_only: bool = False,
        category: str | None = None,
        schema: str | None = None,
    ) -> list[dict]:
        """List notifications for a user."""
        target_schema = schema or "public"
        conditions = ["user_id = :user_id"]
        params: dict[str, Any] = {"user_id": user_id, "limit": limit, "offset": offset}

        if unread_only:
            conditions.append("is_read = false")
        if category:
            conditions.append("category = :category")
            params["category"] = category

        where = " AND ".join(conditions)

        try:
            result = await session.execute(
                text(f"""
                    SELECT id, user_id, title, message, type, category,
                           link, is_read, created_at
                    FROM "{target_schema}".notifications
                    WHERE {where}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """),
                params,
            )
            return [
                {
                    "id": str(r[0]),
                    "user_id": r[1],
                    "title": r[2],
                    "message": r[3],
                    "type": r[4],
                    "category": r[5],
                    "link": r[6],
                    "is_read": r[7],
                    "created_at": r[8].isoformat() if r[8] else None,
                }
                for r in result.fetchall()
            ]
        except Exception as e:
            logger.warning(f"Failed to list notifications: {e}")
            return []

    @staticmethod
    async def get_stats(
        session: AsyncSession,
        user_id: str,
        *,
        schema: str | None = None,
    ) -> dict:
        """Get notification counts for a user."""
        target_schema = schema or "public"
        try:
            total_r = await session.execute(
                text(f'SELECT COUNT(*) FROM "{target_schema}".notifications WHERE user_id = :uid'),
                {"uid": user_id},
            )
            unread_r = await session.execute(
                text(f'SELECT COUNT(*) FROM "{target_schema}".notifications WHERE user_id = :uid AND is_read = false'),
                {"uid": user_id},
            )
            cat_r = await session.execute(
                text(f"""
                    SELECT category, COUNT(*) FROM "{target_schema}".notifications
                    WHERE user_id = :uid AND is_read = false
                    GROUP BY category
                """),
                {"uid": user_id},
            )

            return {
                "total": total_r.scalar() or 0,
                "unread": unread_r.scalar() or 0,
                "by_category": {row[0]: row[1] for row in cat_r.fetchall()},
            }
        except Exception as e:
            logger.warning(f"Failed to get notification stats: {e}")
            return {"total": 0, "unread": 0, "by_category": {}}

    @staticmethod
    async def mark_read(
        session: AsyncSession,
        notification_id: str,
        user_id: str,
        *,
        schema: str | None = None,
    ) -> bool:
        """Mark a single notification as read."""
        target_schema = schema or "public"
        try:
            await session.execute(
                text(f"""
                    UPDATE "{target_schema}".notifications
                    SET is_read = true
                    WHERE id = :nid AND user_id = :uid
                """),
                {"nid": notification_id, "uid": user_id},
            )
            await session.commit()
            return True
        except Exception as e:
            logger.warning(f"Failed to mark notification as read: {e}")
            return False

    @staticmethod
    async def mark_all_read(
        session: AsyncSession,
        user_id: str,
        *,
        schema: str | None = None,
    ) -> int:
        """Mark all notifications as read for a user."""
        target_schema = schema or "public"
        try:
            result = await session.execute(
                text(f"""
                    UPDATE "{target_schema}".notifications
                    SET is_read = true
                    WHERE user_id = :uid AND is_read = false
                """),
                {"uid": user_id},
            )
            await session.commit()
            return result.rowcount or 0
        except Exception as e:
            logger.warning(f"Failed to mark all as read: {e}")
            return 0


async def _publish_notification(user_id: str, notification: dict) -> None:
    """Publish notification via Redis PubSub for WebSocket delivery."""
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings

        settings = get_settings()
        r = aioredis.from_url(settings.redis_url)
        channel = f"notifications:{user_id}"
        await r.publish(channel, json.dumps(notification))
        await r.aclose()
    except Exception as e:
        logger.debug(f"Redis publish failed (non-critical): {e}")


# ── Convenience functions for common notification patterns ────────────────────

async def notify_processing_complete(
    session: AsyncSession,
    user_id: str,
    project_name: str,
    flight_id: str,
    schema: str | None = None,
) -> None:
    """Notify user that photogrammetric processing is complete."""
    await NotificationService.create(
        session,
        user_id=user_id,
        title="Processamento concluído",
        message=f"O voo do projeto '{project_name}' foi processado com sucesso. Os resultados já estão disponíveis no mapa.",
        notification_type="success",
        category="processing",
        link=f"/projects/{flight_id}",
        entity_type="flight",
        entity_id=flight_id,
        schema=schema,
    )


async def notify_processing_failed(
    session: AsyncSession,
    user_id: str,
    project_name: str,
    error: str,
    schema: str | None = None,
) -> None:
    """Notify user of processing failure."""
    await NotificationService.create(
        session,
        user_id=user_id,
        title="Falha no processamento",
        message=f"O processamento do projeto '{project_name}' falhou: {error[:200]}",
        notification_type="error",
        category="processing",
        schema=schema,
    )


async def notify_report_ready(
    session: AsyncSession,
    user_id: str,
    report_title: str,
    report_id: str,
    schema: str | None = None,
) -> None:
    """Notify user that a report is ready for download."""
    await NotificationService.create(
        session,
        user_id=user_id,
        title="Relatório pronto",
        message=f"O relatório '{report_title}' foi gerado e está disponível para download.",
        notification_type="success",
        category="report",
        link=f"/reports/{report_id}",
        entity_type="report",
        entity_id=report_id,
        schema=schema,
    )


async def notify_subscription_expiring(
    session: AsyncSession,
    user_id: str,
    days_left: int,
    schema: str | None = None,
) -> None:
    """Notify tenant admin that subscription is expiring."""
    await NotificationService.create(
        session,
        user_id=user_id,
        title="Assinatura expirando",
        message=f"Sua assinatura expira em {days_left} dias. Renove para continuar utilizando todos os recursos.",
        notification_type="warning",
        category="subscription",
        link="/admin/billing",
        schema=schema,
    )
