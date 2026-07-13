"""
CM TECHMAP — Dashboard Analytics API
Aggregated KPIs, activity timeline, and platform-wide metrics.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, require_viewer

logger = logging.getLogger("cm_techmap.api.dashboard")

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/analytics")
async def get_dashboard_analytics(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_viewer),
):
    """
    Comprehensive dashboard analytics endpoint.
    Returns KPIs, project breakdown, activity timeline, and infrastructure status.
    """
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)
    seven_days_ago = now - timedelta(days=7)

    # ── KPIs ──────────────────────────────────────────────────────────────
    kpis: dict[str, Any] = {}

    # Total projects
    try:
        r = await db.execute(text("SELECT COUNT(*) FROM projects"))
        kpis["total_projects"] = r.scalar() or 0
    except Exception:
        kpis["total_projects"] = 0

    # Projects by status
    try:
        r = await db.execute(text(
            "SELECT status, COUNT(*) FROM projects GROUP BY status"
        ))
        status_map = {row[0]: row[1] for row in r.fetchall()}
        kpis["projects_by_status"] = status_map
    except Exception:
        kpis["projects_by_status"] = {}

    # Total flights
    try:
        r = await db.execute(text("SELECT COUNT(*) FROM flights WHERE is_active = true"))
        kpis["total_flights"] = r.scalar() or 0
    except Exception:
        kpis["total_flights"] = 0

    # Total images
    try:
        r = await db.execute(text("SELECT COALESCE(SUM(images_count), 0) FROM flights WHERE is_active = true"))
        kpis["total_images"] = int(r.scalar() or 0)
    except Exception:
        kpis["total_images"] = 0

    # Total area mapped (m² → km²)
    try:
        r = await db.execute(text("SELECT COALESCE(SUM(area_coverage_sqm), 0) FROM flights WHERE is_active = true"))
        area_sqm = float(r.scalar() or 0)
        kpis["total_area_sqm"] = area_sqm
        kpis["total_area_km2"] = round(area_sqm / 1_000_000, 2)
    except Exception:
        kpis["total_area_sqm"] = 0
        kpis["total_area_km2"] = 0

    # Total assets
    try:
        r = await db.execute(text("SELECT COUNT(*) FROM flight_assets WHERE is_active = true"))
        kpis["total_assets"] = r.scalar() or 0
    except Exception:
        kpis["total_assets"] = 0

    # Total storage (bytes → GB)
    try:
        r = await db.execute(text("SELECT COALESCE(SUM(file_size_bytes), 0) FROM flight_assets WHERE is_active = true"))
        storage_bytes = int(r.scalar() or 0)
        kpis["storage_used_bytes"] = storage_bytes
        kpis["storage_used_gb"] = round(storage_bytes / (1024 ** 3), 2)
    except Exception:
        kpis["storage_used_bytes"] = 0
        kpis["storage_used_gb"] = 0

    # Reports
    try:
        r = await db.execute(text("SELECT COUNT(*) FROM reports"))
        kpis["total_reports"] = r.scalar() or 0
        r = await db.execute(text("SELECT COUNT(*) FROM reports WHERE status = 'completed'"))
        kpis["completed_reports"] = r.scalar() or 0
    except Exception:
        kpis["total_reports"] = 0
        kpis["completed_reports"] = 0

    # Processing jobs
    try:
        r = await db.execute(text("SELECT COUNT(*) FROM processing_jobs WHERE status = 'running'"))
        kpis["active_processing_jobs"] = r.scalar() or 0
        r = await db.execute(text("SELECT COUNT(*) FROM processing_jobs WHERE status = 'queued'"))
        kpis["queued_processing_jobs"] = r.scalar() or 0
    except Exception:
        kpis["active_processing_jobs"] = 0
        kpis["queued_processing_jobs"] = 0

    # ── Recent Activity (last 30 days) ─────────────────────────────────────
    activity: list[dict] = []

    try:
        r = await db.execute(text("""
            SELECT 'project' as type, name as title, status, created_at
            FROM projects
            WHERE created_at >= :since
            ORDER BY created_at DESC LIMIT 10
        """), {"since": thirty_days_ago})
        for row in r.mappings().all():
            activity.append({
                "type": "project",
                "title": row["title"],
                "status": row["status"],
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
            })
    except Exception:
        pass

    try:
        r = await db.execute(text("""
            SELECT 'flight' as type, CONCAT('Voo #', ROW_NUMBER() OVER(ORDER BY created_at)) as title,
                   status, created_at
            FROM flights
            WHERE is_active = true AND created_at >= :since
            ORDER BY created_at DESC LIMIT 10
        """), {"since": thirty_days_ago})
        for row in r.mappings().all():
            activity.append({
                "type": "flight",
                "title": row["title"],
                "status": row["status"],
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
            })
    except Exception:
        pass

    try:
        r = await db.execute(text("""
            SELECT 'report' as type, title, status, created_at
            FROM reports
            WHERE created_at >= :since
            ORDER BY created_at DESC LIMIT 10
        """), {"since": thirty_days_ago})
        for row in r.mappings().all():
            activity.append({
                "type": "report",
                "title": row["title"],
                "status": row["status"],
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
            })
    except Exception:
        pass

    # Sort by timestamp
    activity.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
    activity = activity[:20]

    # ── Projects for Map (with bbox) ───────────────────────────────────────
    project_locations: list[dict] = []
    try:
        r = await db.execute(text("""
            SELECT id, code, name, city, state, status,
                   bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat,
                   area_sqm, flight_count, image_count
            FROM projects
            WHERE is_active = true
            ORDER BY created_at DESC LIMIT 50
        """))
        for row in r.mappings().all():
            loc: dict = {
                "id": str(row["id"]),
                "code": row["code"],
                "name": row["name"],
                "city": row["city"],
                "state": row["state"],
                "status": row["status"],
                "area_sqm": row.get("area_sqm"),
                "flight_count": row.get("flight_count", 0),
                "image_count": row.get("image_count", 0),
            }
            if row.get("bbox_min_lon") and row.get("bbox_max_lon"):
                loc["center_lon"] = (row["bbox_min_lon"] + row["bbox_max_lon"]) / 2
                loc["center_lat"] = (row["bbox_min_lat"] + row["bbox_max_lat"]) / 2
                loc["bbox"] = [
                    row["bbox_min_lon"], row["bbox_min_lat"],
                    row["bbox_max_lon"], row["bbox_max_lat"],
                ]
            project_locations.append(loc)
    except Exception:
        pass

    # ── Monthly Statistics (last 6 months) ──────────────────────────────────
    monthly_stats: list[dict] = []
    try:
        r = await db.execute(text("""
            SELECT
                TO_CHAR(DATE_TRUNC('month', created_at), 'Mon') as month_label,
                DATE_TRUNC('month', created_at) as month_date,
                COUNT(*) as flight_count,
                COALESCE(SUM(area_coverage_sqm), 0) / 1000000.0 as area_km2
            FROM flights
            WHERE is_active = true
                AND created_at >= DATE_TRUNC('month', NOW()) - INTERVAL '5 months'
            GROUP BY DATE_TRUNC('month', created_at)
            ORDER BY month_date ASC
        """))
        for row in r.mappings().all():
            monthly_stats.append({
                "month": row["month_label"],
                "flights": row["flight_count"],
                "area_km2": round(float(row["area_km2"]), 2),
            })
    except Exception:
        pass

    # Fill missing months with zeros
    if len(monthly_stats) < 6:
        import calendar
        existing_months = {s["month"] for s in monthly_stats}
        for i in range(6):
            m = (now - timedelta(days=30 * (5 - i)))
            label = calendar.month_abbr[m.month][:3]
            if label not in existing_months:
                monthly_stats.insert(i, {"month": label, "flights": 0, "area_km2": 0})
        monthly_stats = monthly_stats[:6]

    # ── Notification count ─────────────────────────────────────────────────
    notification_count = 0
    try:
        user_id = user.get("sub", "")
        if user_id:
            r = await db.execute(text(
                "SELECT COUNT(*) FROM public.notifications WHERE user_id = :uid AND is_read = false"
            ), {"uid": user_id})
            notification_count = r.scalar() or 0
    except Exception:
        pass

    return {
        "kpis": kpis,
        "recent_activity": activity,
        "project_locations": project_locations,
        "monthly_stats": monthly_stats,
        "unread_notifications": notification_count,
        "generated_at": now.isoformat(),
    }
