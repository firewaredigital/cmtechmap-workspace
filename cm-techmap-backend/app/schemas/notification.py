"""
CM TECHMAP — Notification Schemas
Pydantic models for in-app notifications.
"""

import uuid
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class NotificationType(str, Enum):
    """Types of notifications."""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


class NotificationCategory(str, Enum):
    """Notification categories for filtering."""
    PROCESSING = "processing"
    REPORT = "report"
    SUBSCRIPTION = "subscription"
    SYSTEM = "system"
    ADMIN = "admin"
    ALERT = "alert"


class NotificationCreate(BaseModel):
    """Create a notification."""
    title: str = Field(..., max_length=200)
    message: str = Field(..., max_length=2000)
    type: NotificationType = NotificationType.INFO
    category: NotificationCategory = NotificationCategory.SYSTEM
    link: str | None = Field(None, max_length=500, description="Frontend route to navigate to")
    entity_type: str | None = None
    entity_id: str | None = None


class NotificationRead(BaseModel):
    """Read a notification."""
    id: uuid.UUID
    user_id: str
    title: str
    message: str
    type: str
    category: str
    link: str | None = None
    is_read: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationStats(BaseModel):
    """Notification count summary."""
    total: int = 0
    unread: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
