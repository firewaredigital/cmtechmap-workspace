"""CM TECHMAP — Processing Schemas"""

from datetime import datetime
from pydantic import BaseModel


class ProcessingJobRead(BaseModel):
    task_id: str
    celery_task_id: str | None = None
    odm_uuid: str | None = None
    status: str
    stage: str | None = None
    progress: int = 0
    message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ProcessingProgress(BaseModel):
    task_id: str
    stage: str
    progress: int
    message: str = ""
    odm_status: str | None = None
    processing_time: int | None = None
    images_count: int | None = None


class ProcessingOptionsRead(BaseModel):
    """Available ODM processing options."""
    dsm: bool = True
    dtm: bool = True
    orthophoto_resolution: float = 2.0
    pc_las: bool = True
    skip_3dmodel: bool = False
    mesh_octree_depth: int = 12
    mesh_size: int = 300000
    min_num_features: int = 10000
