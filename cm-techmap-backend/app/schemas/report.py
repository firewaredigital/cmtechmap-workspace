"""CM TECHMAP — Report Schemas"""

import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Literal


class ReportGroqConfig(BaseModel):
    """Optional Groq narrative controls for report generation."""
    enable_narrative: bool = True
    model_override: str | None = None
    max_completion_tokens: int | None = Field(None, ge=64, le=4096)


class ReportGenerateRequest(BaseModel):
    """Request to generate a new report."""
    project_id: uuid.UUID
    title: str = Field(..., min_length=3, max_length=500)
    report_type: Literal[
        "project_summary",
        "comparison",
        "flight_detail",
        "custom",
        "property_appraisal",
        "project_consolidated",
        "fiscal_revenue",
        "technical_qa",
    ] = "project_summary"
    output_format: Literal["pdf", "xlsx"] = "pdf"
    config: dict | None = Field(
        None,
        description="Optional configuration: flight_ids, date_range, sections, etc.",
    )
    groq: ReportGroqConfig | None = Field(
        None,
        description="Optional Groq narrative overrides for this report request.",
    )


class ReportConfigPreset(BaseModel):
    """Operational defaults for fiscal/QA report configuration by municipality."""
    municipality_code: str
    municipality_name: str
    iptu_rate_per_sqm: float
    assumed_irregular_share: float
    qa_threshold: float
    notes: str | None = None


class ReportConfigPresetResponse(BaseModel):
    """List of known report configuration presets and profile metadata."""
    presets: list[ReportConfigPreset]
    supported_profiles: list[str]


class ReportConfigPresetUpsertRequest(BaseModel):
    """Create or update an operational report preset for one municipality."""
    municipality_code: str = Field(..., min_length=5, max_length=10)
    municipality_name: str = Field(..., min_length=2, max_length=120)
    iptu_rate_per_sqm: float = Field(..., ge=0)
    assumed_irregular_share: float = Field(..., ge=0, le=1)
    qa_threshold: float = Field(..., ge=0, le=1)
    notes: str | None = Field(None, max_length=1000)


class ReportRead(BaseModel):
    """Report response."""
    id: uuid.UUID
    project_id: uuid.UUID
    title: str
    report_type: str
    output_format: str
    status: str
    celery_task_id: str | None
    file_key: str | None
    file_size_bytes: int | None
    requested_by: str | None
    error_message: str | None
    download_url: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ReportListResponse(BaseModel):
    """Paginated report list."""
    total: int
    page: int
    page_size: int
    items: list[ReportRead]


class ReportSectionRead(BaseModel):
    """Report section response."""
    id: uuid.UUID
    section_type: str
    title: str
    order: int
    content: dict | None

    model_config = {"from_attributes": True}
