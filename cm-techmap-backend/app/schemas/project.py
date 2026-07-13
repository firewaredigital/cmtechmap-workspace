"""CM TECHMAP — Project Schemas"""

import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=3, max_length=500)
    description: str | None = None
    city: str | None = None
    state: str | None = Field(None, max_length=2)


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    city: str | None = None
    state: str | None = None
    status: str | None = None


class ProjectRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    status: str
    city: str | None
    state: str | None
    area_sqm: float | None
    flight_count: int
    image_count: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ProjectRead]
