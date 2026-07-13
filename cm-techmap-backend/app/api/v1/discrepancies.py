"""
CM TECHMAP — Discrepancies API
The core "Fiscal Virtual" workflow: municipal employees review AI-detected
tax discrepancies and decide to approve, reject, or schedule inspections.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_gestor, require_operador

logger = logging.getLogger("cm_techmap.api.discrepancies")

router = APIRouter(prefix="/discrepancies", tags=["Discrepancies"])


# ══════════════════════════════════════════════════════════════════════════════
# LIST / QUERY
# ══════════════════════════════════════════════════════════════════════════════

@router.get("")
async def list_discrepancies(
    project_id: UUID | None = Query(None),
    status: str | None = Query(None, description="pending|approved|rejected|inspection_scheduled|inspected"),
    discrepancy_type: str | None = Query(None),
    severity: str | None = Query(None),
    neighborhood: str | None = Query(None),
    min_gap_brl: float | None = Query(None, ge=0),
    sort_by: str = Query("estimated_iptu_gap_brl", description="estimated_iptu_gap_brl|created_at|difference_pct"),
    sort_dir: str = Query("desc", description="asc|desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """List discrepancies with filters, sorting, and pagination."""
    conditions = []
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

    if project_id:
        conditions.append("d.project_id = :pid")
        params["pid"] = str(project_id)
    if status:
        conditions.append("d.status = :status")
        params["status"] = status
    if discrepancy_type:
        conditions.append("d.discrepancy_type = :dtype")
        params["dtype"] = discrepancy_type
    if severity:
        conditions.append("d.severity = :sev")
        params["sev"] = severity
    if neighborhood:
        conditions.append("d.neighborhood ILIKE :neigh")
        params["neigh"] = f"%{neighborhood}%"
    if min_gap_brl is not None:
        conditions.append("d.estimated_iptu_gap_brl >= :min_gap")
        params["min_gap"] = min_gap_brl

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    allowed_sorts = {"estimated_iptu_gap_brl", "created_at", "difference_pct", "severity", "status"}
    sort_col = sort_by if sort_by in allowed_sorts else "estimated_iptu_gap_brl"
    sort_direction = "ASC" if sort_dir.lower() == "asc" else "DESC"

    # Count
    count_result = await db.execute(text(f"SELECT COUNT(*) FROM discrepancies d {where}"), params)
    total = count_result.scalar() or 0

    # Fetch
    result = await db.execute(text(f"""
        SELECT d.id, d.project_id, d.parcel_id, d.detection_id, d.analysis_run_id,
               d.discrepancy_type, d.severity,
               d.cadastral_code, d.address, d.neighborhood, d.owner_name,
               d.registered_area_sqm, d.detected_area_sqm,
               d.difference_sqm, d.difference_pct, d.overlap_pct, d.confidence,
               d.detected_height_m, d.registered_floors,
               d.iptu_current_brl, d.iptu_proposed_brl, d.estimated_iptu_gap_brl,
               d.status, d.reviewed_by, d.reviewed_at, d.reviewer_notes,
               d.rejection_reason, d.inspection_date, d.inspector_name,
               d.created_at
        FROM discrepancies d
        {where}
        ORDER BY d.{sort_col} {sort_direction} NULLS LAST
        LIMIT :limit OFFSET :offset
    """), params)

    items = []
    for r in result.mappings().all():
        item = dict(r)
        for uid_field in ("id", "project_id", "parcel_id", "detection_id", "analysis_run_id"):
            if item.get(uid_field):
                item[uid_field] = str(item[uid_field])
        for ts_field in ("reviewed_at", "inspection_date", "created_at"):
            if item.get(ts_field):
                item[ts_field] = item[ts_field].isoformat()
        items.append(item)

    return {
        "discrepancies": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
    }


@router.get("/stats")
async def get_discrepancy_stats(
    project_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Get aggregate statistics for discrepancies."""
    project_filter = "WHERE d.project_id = :pid" if project_id else ""
    params = {"pid": str(project_id)} if project_id else {}

    result = await db.execute(text(f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE d.status = 'pending') as pending,
            COUNT(*) FILTER (WHERE d.status = 'approved') as approved,
            COUNT(*) FILTER (WHERE d.status = 'rejected') as rejected,
            COUNT(*) FILTER (WHERE d.status = 'inspection_scheduled') as inspection_scheduled,
            COUNT(*) FILTER (WHERE d.status = 'inspected') as inspected,
            COALESCE(SUM(d.estimated_iptu_gap_brl), 0) as total_estimated_gap_brl,
            COALESCE(SUM(d.estimated_iptu_gap_brl) FILTER (WHERE d.status = 'approved'), 0) as approved_gap_brl,
            COALESCE(SUM(d.estimated_iptu_gap_brl) FILTER (WHERE d.status = 'pending'), 0) as pending_gap_brl,
            COUNT(*) FILTER (WHERE d.discrepancy_type = 'unregistered') as unregistered,
            COUNT(*) FILTER (WHERE d.discrepancy_type = 'area_under_declared') as area_under_declared,
            COUNT(*) FILTER (WHERE d.discrepancy_type = 'pool_detected') as pool_detected,
            COUNT(*) FILTER (WHERE d.discrepancy_type = 'demolished') as demolished,
            COUNT(*) FILTER (WHERE d.severity = 'high' OR d.severity = 'critical') as high_severity
        FROM discrepancies d
        {project_filter}
    """), params)

    r = result.mappings().first()
    return dict(r) if r else {}


@router.get("/queue")
async def get_review_queue(
    project_id: UUID | None = Query(None),
    neighborhood: str | None = Query(None),
    min_severity: str | None = Query(None, description="low|medium|high|critical"),
    page: int = Query(1, ge=1),
    page_size: int = Query(1, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """
    Get the next discrepancy(ies) to review from the pending queue.
    Returns full detail including parcel and detection geometries.
    Ordered by estimated gap (highest value first).
    """
    conditions = ["d.status = 'pending'"]
    params: dict[str, Any] = {"limit": page_size, "offset": (page - 1) * page_size}

    if project_id:
        conditions.append("d.project_id = :pid")
        params["pid"] = str(project_id)
    if neighborhood:
        conditions.append("d.neighborhood ILIKE :neigh")
        params["neigh"] = f"%{neighborhood}%"
    if min_severity:
        severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        min_val = severity_order.get(min_severity, 0)
        sev_list = [k for k, v in severity_order.items() if v >= min_val]
        conditions.append(f"d.severity IN ({','.join(repr(s) for s in sev_list)})")

    where = " AND ".join(conditions)

    # Count total pending
    count_result = await db.execute(text(f"""
        SELECT COUNT(*) FROM discrepancies d WHERE {where}
    """), params)
    total_pending = count_result.scalar() or 0

    # Fetch with geometries
    result = await db.execute(text(f"""
        SELECT d.*,
               ST_AsGeoJSON(d.polygon) as discrepancy_geojson,
               ST_AsGeoJSON(p.polygon) as parcel_geojson,
               ST_AsGeoJSON(ad.polygon) as detection_geojson,
               p.owner_cpf_cnpj, p.iptu_value_current_brl as parcel_iptu_current,
               p.properties as parcel_properties,
               ad.detection_class, ad.model_version as detection_model,
               ad.properties as detection_properties
        FROM discrepancies d
        LEFT JOIN parcels p ON p.id = d.parcel_id
        LEFT JOIN ai_detections ad ON ad.id = d.detection_id
        WHERE {where}
        ORDER BY d.estimated_iptu_gap_brl DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """), params)

    items = []
    for r in result.mappings().all():
        item = dict(r)
        # Parse geometries
        for geo_field in ("discrepancy_geojson", "parcel_geojson", "detection_geojson"):
            if item.get(geo_field):
                item[geo_field.replace("_geojson", "_geometry")] = json.loads(item.pop(geo_field))
            else:
                item.pop(geo_field, None)
        # Convert UUIDs
        for uid_field in ("id", "project_id", "parcel_id", "detection_id", "analysis_run_id"):
            if item.get(uid_field):
                item[uid_field] = str(item[uid_field])
        # Convert timestamps
        for ts in ("created_at", "updated_at", "reviewed_at", "inspection_date"):
            if item.get(ts):
                item[ts] = item[ts].isoformat()
        # Remove raw polygon column
        item.pop("polygon", None)
        items.append(item)

    return {
        "queue": items,
        "total_pending": total_pending,
        "page": page,
        "page_size": page_size,
        "current_position": (page - 1) * page_size + 1 if items else 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# SINGLE DISCREPANCY DETAIL
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/{discrepancy_id}")
async def get_discrepancy(
    discrepancy_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Get full detail of a single discrepancy with geometries."""
    result = await db.execute(text("""
        SELECT d.*,
               ST_AsGeoJSON(d.polygon) as discrepancy_geojson,
               ST_AsGeoJSON(p.polygon) as parcel_geojson,
               ST_AsGeoJSON(ad.polygon) as detection_geojson,
               p.owner_cpf_cnpj, p.iptu_value_current_brl,
               p.properties as parcel_properties,
               ad.detection_class, ad.model_version,
               ad.properties as detection_properties
        FROM discrepancies d
        LEFT JOIN parcels p ON p.id = d.parcel_id
        LEFT JOIN ai_detections ad ON ad.id = d.detection_id
        WHERE d.id = :did
    """), {"did": str(discrepancy_id)})

    r = result.mappings().first()
    if not r:
        raise HTTPException(404, "Discrepância não encontrada")

    item = dict(r)
    for geo_field in ("discrepancy_geojson", "parcel_geojson", "detection_geojson"):
        if item.get(geo_field):
            item[geo_field.replace("_geojson", "_geometry")] = json.loads(item.pop(geo_field))
        else:
            item.pop(geo_field, None)
    for uid_field in ("id", "project_id", "parcel_id", "detection_id", "analysis_run_id"):
        if item.get(uid_field):
            item[uid_field] = str(item[uid_field])
    for ts in ("created_at", "updated_at", "reviewed_at", "inspection_date"):
        if item.get(ts):
            item[ts] = item[ts].isoformat()
    item.pop("polygon", None)
    return item


# ══════════════════════════════════════════════════════════════════════════════
# DECISION ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/{discrepancy_id}/approve")
async def approve_discrepancy(
    discrepancy_id: UUID,
    notes: str | None = Query(None, max_length=2000),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Approve a discrepancy — confirms the tax irregularity.
    Updates status, records reviewer, and generates a tax assessment entry.
    """
    user_id = user.get("sub", "unknown")
    now = datetime.now(timezone.utc)

    # Validate current status
    check = await db.execute(text(
        "SELECT status, estimated_iptu_gap_brl FROM discrepancies WHERE id = :id"
    ), {"id": str(discrepancy_id)})
    row = check.mappings().first()
    if not row:
        raise HTTPException(404, "Discrepância não encontrada")
    if row["status"] != "pending":
        raise HTTPException(400, f"Discrepância já está com status '{row['status']}'")

    # Update
    await db.execute(text("""
        UPDATE discrepancies
        SET status = 'approved',
            reviewed_by = :uid,
            reviewed_at = :now,
            reviewer_notes = :notes,
            updated_at = :now
        WHERE id = :id
    """), {"id": str(discrepancy_id), "uid": user_id, "now": now, "notes": notes})

    # Create notification
    try:
        await db.execute(text("""
            INSERT INTO notifications (user_id, title, message, type, category, link)
            VALUES (:uid::uuid, :title, :msg, 'success', 'fiscal',
                    '/fiscal/review/' || :did)
        """), {
            "uid": user_id,
            "title": "Discrepância Aprovada",
            "msg": f"Lançamento IPTU de R$ {row['estimated_iptu_gap_brl']:.2f} aprovado.",
            "did": str(discrepancy_id),
        })
    except Exception:
        pass  # Non-critical

    await db.commit()
    logger.info(f"Discrepancy {discrepancy_id} approved by {user_id}, gap=R${row['estimated_iptu_gap_brl']:.2f}")

    return {
        "status": "approved",
        "discrepancy_id": str(discrepancy_id),
        "reviewed_by": user_id,
        "reviewed_at": now.isoformat(),
    }


@router.post("/{discrepancy_id}/reject")
async def reject_discrepancy(
    discrepancy_id: UUID,
    reason: str = Query(..., description="shadow|ai_error|already_regularized|duplicate|other"),
    notes: str | None = Query(None, max_length=2000),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Reject a discrepancy — marks it as a false positive.
    Records the rejection reason for AI model improvement.
    """
    user_id = user.get("sub", "unknown")
    now = datetime.now(timezone.utc)

    valid_reasons = {"shadow", "ai_error", "already_regularized", "duplicate", "other"}
    if reason not in valid_reasons:
        raise HTTPException(400, f"Motivo inválido. Use: {', '.join(valid_reasons)}")

    check = await db.execute(text(
        "SELECT status FROM discrepancies WHERE id = :id"
    ), {"id": str(discrepancy_id)})
    row = check.mappings().first()
    if not row:
        raise HTTPException(404, "Discrepância não encontrada")
    if row["status"] != "pending":
        raise HTTPException(400, f"Discrepância já está com status '{row['status']}'")

    await db.execute(text("""
        UPDATE discrepancies
        SET status = 'rejected',
            reviewed_by = :uid,
            reviewed_at = :now,
            reviewer_notes = :notes,
            rejection_reason = :reason,
            updated_at = :now
        WHERE id = :id
    """), {
        "id": str(discrepancy_id), "uid": user_id,
        "now": now, "notes": notes, "reason": reason,
    })
    await db.commit()
    logger.info(f"Discrepancy {discrepancy_id} rejected by {user_id}, reason={reason}")

    return {
        "status": "rejected",
        "discrepancy_id": str(discrepancy_id),
        "rejection_reason": reason,
    }


@router.post("/{discrepancy_id}/schedule-inspection")
async def schedule_inspection(
    discrepancy_id: UUID,
    inspection_date: str = Query(..., description="ISO date YYYY-MM-DD"),
    inspector_name: str = Query(..., min_length=2),
    notes: str | None = Query(None, max_length=2000),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Schedule a field inspection for a discrepancy.
    A physical inspector will verify the construction on-site.
    """
    user_id = user.get("sub", "unknown")
    now = datetime.now(timezone.utc)

    try:
        insp_date = datetime.fromisoformat(inspection_date)
    except ValueError:
        raise HTTPException(400, "Data inválida. Use formato YYYY-MM-DD")

    check = await db.execute(text(
        "SELECT status FROM discrepancies WHERE id = :id"
    ), {"id": str(discrepancy_id)})
    row = check.mappings().first()
    if not row:
        raise HTTPException(404, "Discrepância não encontrada")
    if row["status"] not in ("pending", "inspection_scheduled"):
        raise HTTPException(400, f"Discrepância com status '{row['status']}' não pode agendar vistoria")

    await db.execute(text("""
        UPDATE discrepancies
        SET status = 'inspection_scheduled',
            reviewed_by = :uid,
            reviewed_at = :now,
            reviewer_notes = :notes,
            inspection_date = :insp_date,
            inspector_name = :inspector,
            updated_at = :now
        WHERE id = :id
    """), {
        "id": str(discrepancy_id), "uid": user_id,
        "now": now, "notes": notes,
        "insp_date": insp_date, "inspector": inspector_name,
    })
    await db.commit()

    return {
        "status": "inspection_scheduled",
        "discrepancy_id": str(discrepancy_id),
        "inspection_date": inspection_date,
        "inspector_name": inspector_name,
    }


@router.post("/{discrepancy_id}/inspection-result")
async def record_inspection_result(
    discrepancy_id: UUID,
    result_status: str = Query(..., description="confirmed|not_confirmed|partial"),
    report: str = Query(..., min_length=10, max_length=5000),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Record the result of a field inspection."""
    user_id = user.get("sub", "unknown")
    now = datetime.now(timezone.utc)

    valid_results = {"confirmed", "not_confirmed", "partial"}
    if result_status not in valid_results:
        raise HTTPException(400, f"Resultado inválido. Use: {', '.join(valid_results)}")

    # Map result to final status
    final_status = "approved" if result_status == "confirmed" else \
                   "rejected" if result_status == "not_confirmed" else "inspected"

    await db.execute(text("""
        UPDATE discrepancies
        SET status = :final_status,
            inspection_result = :result,
            inspector_report = :report,
            reviewed_by = :uid,
            reviewed_at = :now,
            updated_at = :now
        WHERE id = :id
    """), {
        "id": str(discrepancy_id), "final_status": final_status,
        "result": result_status, "report": report,
        "uid": user_id, "now": now,
    })
    await db.commit()

    return {
        "status": final_status,
        "inspection_result": result_status,
        "discrepancy_id": str(discrepancy_id),
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/export")
async def export_discrepancies(
    project_id: UUID | None = Query(None),
    status: str | None = Query(None),
    format: str = Query("json", description="json|csv"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Export filtered discrepancies as JSON or CSV."""
    conditions = []
    params: dict[str, Any] = {}

    if project_id:
        conditions.append("d.project_id = :pid")
        params["pid"] = str(project_id)
    if status:
        conditions.append("d.status = :status")
        params["status"] = status

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    result = await db.execute(text(f"""
        SELECT d.cadastral_code, d.address, d.neighborhood, d.owner_name,
               d.discrepancy_type, d.severity, d.status,
               d.registered_area_sqm, d.detected_area_sqm,
               d.difference_sqm, d.difference_pct,
               d.iptu_current_brl, d.iptu_proposed_brl, d.estimated_iptu_gap_brl,
               d.reviewed_by, d.reviewed_at, d.rejection_reason,
               d.inspection_date, d.inspector_name, d.inspection_result
        FROM discrepancies d
        {where}
        ORDER BY d.estimated_iptu_gap_brl DESC
    """), params)

    rows = [dict(r) for r in result.mappings().all()]
    for row in rows:
        for ts in ("reviewed_at", "inspection_date"):
            if row.get(ts):
                row[ts] = row[ts].isoformat()

    if format == "csv":
        if not rows:
            return {"csv": "", "count": 0}
        import csv
        import io
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        return {"csv": output.getvalue(), "count": len(rows)}

    return {"data": rows, "count": len(rows)}
