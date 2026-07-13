"""CM TECHMAP — Projects Routes"""

import uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, require_gestor, require_viewer
from app.schemas.project import ProjectCreate, ProjectRead, ProjectUpdate, ProjectListResponse

router = APIRouter(prefix="/projects", tags=["Projects"])


@router.get("", response_model=ProjectListResponse)
async def list_projects(
    page: int = 1,
    page_size: int = 20,
    search: str = "",
    status_filter: str = "",
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """List all projects for the current tenant with pagination and search."""
    offset = (page - 1) * page_size
    where_clauses = []
    params: dict[str, Any] = {"limit": page_size, "offset": offset}

    if search:
        where_clauses.append("(name ILIKE :search OR code ILIKE :search OR city ILIKE :search)")
        params["search"] = f"%{search}%"
    if status_filter:
        where_clauses.append("status = :status_filter")
        params["status_filter"] = status_filter

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    count_result = await db.execute(text(f"SELECT COUNT(*) FROM projects {where_sql}"), params)
    total = count_result.scalar() or 0

    result = await db.execute(text(
        f"SELECT id, code, name, description, status, city, state, area_sqm, "
        f"flight_count, image_count, created_at, updated_at "
        f"FROM projects {where_sql} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    ), params)

    items = [ProjectRead(
        id=r[0], code=r[1], name=r[2], description=r[3], status=r[4],
        city=r[5], state=r[6], area_sqm=r[7], flight_count=r[8],
        image_count=r[9], created_at=r[10], updated_at=r[11],
    ) for r in result.fetchall()]

    return ProjectListResponse(total=total, page=page, page_size=page_size, items=items)


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Create a new mapping project."""
    # Generate sequential code: PRJ-001, PRJ-002, ...
    code_result = await db.execute(text(
        "SELECT COUNT(*) FROM projects"
    ))
    count = (code_result.scalar() or 0) + 1
    code = f"PRJ-{count:03d}"

    result = await db.execute(text(
        "INSERT INTO projects (code, name, description, city, state, status) "
        "VALUES (:code, :name, :desc, :city, :state, 'pendente') "
        "RETURNING id, code, name, description, status, city, state, "
        "area_sqm, flight_count, image_count, created_at, updated_at"
    ), {"code": code, "name": body.name, "desc": body.description,
        "city": body.city, "state": body.state})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=500, detail="Failed to create project")

    await db.commit()
    return ProjectRead(
        id=row[0], code=row[1], name=row[2], description=row[3], status=row[4],
        city=row[5], state=row[6], area_sqm=row[7], flight_count=row[8] or 0,
        image_count=row[9] or 0, created_at=row[10], updated_at=row[11],
    )


@router.get("/{project_id}", response_model=ProjectRead)
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """Get a single project by ID."""
    result = await db.execute(text(
        "SELECT id, code, name, description, status, city, state, area_sqm, "
        "flight_count, image_count, created_at, updated_at "
        "FROM projects WHERE id = :id"
    ), {"id": str(project_id)})

    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    return ProjectRead(
        id=row[0], code=row[1], name=row[2], description=row[3], status=row[4],
        city=row[5], state=row[6], area_sqm=row[7], flight_count=row[8],
        image_count=row[9], created_at=row[10], updated_at=row[11],
    )


@router.patch("/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Update a project's metadata."""
    updates = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = str(project_id)
    updates["now"] = "NOW()"

    await db.execute(text(
        f"UPDATE projects SET {set_clause}, updated_at = NOW() WHERE id = :id"
    ), updates)
    await db.commit()

    return await get_project(project_id, db, user)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Delete a project and all associated data."""
    result = await db.execute(text("DELETE FROM projects WHERE id = :id"), {"id": str(project_id)})
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    await db.commit()
