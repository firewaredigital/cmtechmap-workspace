"""
CM TECHMAP — Notifications API
Endpoints for listing, reading, and managing in-app notifications.
"""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user
from app.schemas.notification import NotificationRead, NotificationStats
from app.services.notification_service import NotificationService

logger = logging.getLogger("cm_techmap.api.notifications")

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", response_model=list[NotificationRead])
async def list_notifications(
    unread_only: bool = Query(False, description="Only unread notifications"),
    category: str | None = Query(None, description="Filter by category"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List notifications for the current user."""
    user_id = user.get("sub") or user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    notifications = await NotificationService.list_for_user(
        db,
        user_id=user_id,
        limit=limit,
        offset=offset,
        unread_only=unread_only,
        category=category,
    )
    return notifications


@router.get("/stats", response_model=NotificationStats)
async def get_notification_stats(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Get notification count summary for the current user."""
    user_id = user.get("sub") or user.get("id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found")

    stats = await NotificationService.get_stats(db, user_id=user_id)
    return NotificationStats(**stats)


@router.patch("/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Mark a single notification as read."""
    user_id = user.get("sub") or user.get("id")
    success = await NotificationService.mark_read(db, notification_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notificação não encontrada")
    return {"status": "ok"}


@router.patch("/read-all")
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Mark all notifications as read for the current user."""
    user_id = user.get("sub") or user.get("id")
    count = await NotificationService.mark_all_read(db, user_id)
    return {"status": "ok", "marked_count": count}
