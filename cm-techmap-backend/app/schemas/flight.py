"""CM TECHMAP — Flight Schemas"""

import uuid
from datetime import date, datetime
from pydantic import BaseModel, Field


class FlightCreate(BaseModel):
    flight_date: date
    altitude_m: float | None = Field(None, ge=10, le=500)
    overlap_pct: float | None = Field(None, ge=30, le=95)
    camera_model: str | None = None
    notes: str | None = None


class FlightRead(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    flight_date: date
    altitude_m: float | None = None
    overlap_pct: float | None = None
    images_count: int | None = 0
    camera_model: str | None = None
    status: str | None = "pending"
    notes: str | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class FlightAsset(BaseModel):
    asset_id: uuid.UUID | None = None  # flight_assets.id for tile requests
    asset_type: str   # orthomosaic, dsm, dtm, point_cloud, 3d_model
    file_key: str
    file_size_bytes: int | None = None
    resolution_cm: float | None = None
    download_url: str | None = None


class FlightAssetsRead(BaseModel):
    flight_id: uuid.UUID
    project_id: uuid.UUID
    status: str
    assets: list[FlightAsset]
    processing_job_id: str | None = None


class FlightProcessRequest(BaseModel):
    options: dict | None = None  # ODM options override
