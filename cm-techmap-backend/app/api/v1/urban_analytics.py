"""
CM TECHMAP — Urban Analytics API
Endpoints for parcels (cadastral), AI detections, IPTU analysis, and measurements.
"""

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_db, get_current_user, require_gestor, require_operador

logger = logging.getLogger("cm_techmap.api.urban")

router = APIRouter(prefix="/urban", tags=["Urban Analytics"])


# ══════════════════════════════════════════════════════════════════════════════
# PARCELS (Cadastral Data)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/parcels")
async def list_parcels(
    project_id: UUID | None = Query(None, description="Filter by project bounding box"),
    neighborhood: str | None = Query(None),
    land_use: str | None = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """List cadastral parcels with optional spatial and attribute filters."""
    conditions = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if project_id:
        conditions.append("""
            ST_Intersects(p.polygon,
                (SELECT ST_MakeEnvelope(bbox_min_lon, bbox_min_lat, bbox_max_lon, bbox_max_lat, 4326)
                 FROM projects WHERE id = :pid))
        """)
        params["pid"] = str(project_id)

    if neighborhood:
        conditions.append("p.neighborhood ILIKE :neigh")
        params["neigh"] = f"%{neighborhood}%"

    if land_use:
        conditions.append("p.land_use = :land_use")
        params["land_use"] = land_use

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    try:
        result = await db.execute(text(f"""
            SELECT p.id, p.cadastral_code, p.address, p.neighborhood,
                   p.registered_area_sqm, p.registered_built_area_sqm,
                   p.land_use, p.iptu_zone, p.owner_name,
                   ST_AsGeoJSON(p.polygon) as geojson,
                   p.imported_at
            FROM parcels p
            {where}
            ORDER BY p.cadastral_code
            LIMIT :limit OFFSET :offset
        """), params)

        parcels = []
        for r in result.mappings().all():
            parcel = dict(r)
            parcel["id"] = str(parcel["id"])
            if parcel.get("geojson"):
                parcel["geometry"] = json.loads(parcel.pop("geojson"))
            if parcel.get("imported_at"):
                parcel["imported_at"] = parcel["imported_at"].isoformat()
            parcels.append(parcel)

        return {"parcels": parcels, "count": len(parcels)}
    except Exception as e:
        logger.warning(f"Parcels query failed: {e}")
        raise HTTPException(status_code=500, detail="Falha ao consultar parcelas")


@router.get("/parcels/stats")
async def get_parcel_stats(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Get aggregate parcel statistics."""
    try:
        result = await db.execute(text("""
            SELECT
                COUNT(*) as total_parcels,
                COALESCE(SUM(registered_area_sqm), 0) as total_area_sqm,
                COALESCE(SUM(registered_built_area_sqm), 0) as total_built_area_sqm,
                COUNT(DISTINCT neighborhood) as neighborhoods,
                COUNT(DISTINCT land_use) as land_use_types,
                (SELECT json_object_agg(land_use, cnt) FROM (
                    SELECT land_use, COUNT(*) as cnt FROM parcels
                    WHERE land_use IS NOT NULL GROUP BY land_use
                ) sub) as land_use_breakdown
            FROM parcels
        """))
        r = result.mappings().first()
        return dict(r) if r else {}
    except Exception as e:
        logger.warning(f"Parcel stats failed: {e}")
        return {"total_parcels": 0}


@router.post("/parcels/import")
async def import_parcels(
    file: UploadFile = File(..., description="GeoJSON file with parcel features"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """Import cadastral parcels from a GeoJSON file."""
    if not file.filename or not file.filename.endswith((".geojson", ".json")):
        raise HTTPException(status_code=400, detail="Arquivo deve ser GeoJSON (.geojson ou .json)")

    content = await file.read()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="JSON inválido")

    features = data.get("features", [])
    if not features:
        raise HTTPException(status_code=400, detail="Nenhuma feature encontrada no GeoJSON")

    imported = 0
    errors = 0
    for feature in features:
        props = feature.get("properties", {})
        geom = json.dumps(feature.get("geometry", {}))

        try:
            await db.execute(text("""
                INSERT INTO parcels (cadastral_code, address, neighborhood, polygon,
                    registered_area_sqm, registered_built_area_sqm, land_use, iptu_zone, owner_name)
                VALUES (:code, :addr, :neigh, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326),
                    :area, :built_area, :land_use, :zone, :owner)
                ON CONFLICT (cadastral_code) DO UPDATE SET
                    address = EXCLUDED.address,
                    neighborhood = EXCLUDED.neighborhood,
                    polygon = EXCLUDED.polygon,
                    registered_area_sqm = EXCLUDED.registered_area_sqm,
                    registered_built_area_sqm = EXCLUDED.registered_built_area_sqm,
                    land_use = EXCLUDED.land_use,
                    iptu_zone = EXCLUDED.iptu_zone,
                    updated_at = NOW()
            """), {
                "code": props.get("cadastral_code") or props.get("inscricao_imobiliaria") or f"auto_{imported}",
                "addr": props.get("address") or props.get("endereco"),
                "neigh": props.get("neighborhood") or props.get("bairro"),
                "geom": geom,
                "area": props.get("registered_area_sqm") or props.get("area_terreno"),
                "built_area": props.get("registered_built_area_sqm") or props.get("area_construida"),
                "land_use": props.get("land_use") or props.get("uso_solo"),
                "zone": props.get("iptu_zone") or props.get("zona_fiscal"),
                "owner": props.get("owner_name") or props.get("proprietario"),
            })
            imported += 1
        except Exception as e:
            logger.warning(f"Failed to import parcel: {e}")
            errors += 1

    await db.commit()
    return {"imported": imported, "errors": errors, "total": len(features)}


# ══════════════════════════════════════════════════════════════════════════════
# AI DETECTIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/detections")
async def list_detections(
    project_id: UUID | None = Query(None),
    detection_class: str = Query("building"),
    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(200, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """List AI detections with filters."""
    conditions = ["ad.detection_class = :cls"]
    params: dict[str, Any] = {"cls": detection_class, "limit": limit}

    if project_id:
        conditions.append("f.project_id = :pid")
        params["pid"] = str(project_id)

    if min_confidence > 0:
        conditions.append("ad.confidence >= :min_conf")
        params["min_conf"] = min_confidence

    where = " AND ".join(conditions)

    try:
        result = await db.execute(text(f"""
            SELECT ad.id, ad.detection_class, ad.confidence,
                   ad.area_sqm, ad.perimeter_m, ad.properties,
                   ad.model_version, ad.created_at,
                   ST_AsGeoJSON(ad.polygon) as geojson,
                   f.project_id
            FROM ai_detections ad
            JOIN flight_assets fa ON fa.id = ad.flight_asset_id
            JOIN flights f ON f.id = fa.flight_id
            WHERE {where}
            ORDER BY ad.confidence DESC
            LIMIT :limit
        """), params)

        detections = []
        for r in result.mappings().all():
            det = dict(r)
            det["id"] = str(det["id"])
            det["project_id"] = str(det["project_id"])
            if det.get("geojson"):
                det["geometry"] = json.loads(det.pop("geojson"))
            if det.get("created_at"):
                det["created_at"] = det["created_at"].isoformat()
            detections.append(det)

        return {"detections": detections, "count": len(detections)}
    except Exception as e:
        logger.warning(f"Detection query failed: {e}")
        raise HTTPException(status_code=500, detail="Falha ao consultar detecções")


@router.get("/detections/stats")
async def get_detection_stats(
    project_id: UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Get aggregate detection statistics."""
    project_filter = "AND f.project_id = :pid" if project_id else ""
    params = {"pid": str(project_id)} if project_id else {}

    try:
        result = await db.execute(text(f"""
            SELECT
                ad.detection_class,
                COUNT(*) as count,
                AVG(ad.confidence) as avg_confidence,
                COALESCE(SUM(ad.area_sqm), 0) as total_area_sqm,
                AVG(ad.area_sqm) as avg_area_sqm,
                MIN(ad.area_sqm) as min_area_sqm,
                MAX(ad.area_sqm) as max_area_sqm
            FROM ai_detections ad
            JOIN flight_assets fa ON fa.id = ad.flight_asset_id
            JOIN flights f ON f.id = fa.flight_id
            WHERE 1=1 {project_filter}
            GROUP BY ad.detection_class
        """), params)

        stats = []
        for r in result.mappings().all():
            stats.append({
                "detection_class": r["detection_class"],
                "count": r["count"],
                "avg_confidence": round(float(r["avg_confidence"] or 0), 3),
                "total_area_sqm": round(float(r["total_area_sqm"] or 0), 2),
                "avg_area_sqm": round(float(r["avg_area_sqm"] or 0), 2),
                "min_area_sqm": round(float(r["min_area_sqm"] or 0), 2),
                "max_area_sqm": round(float(r["max_area_sqm"] or 0), 2),
            })

        return {"stats": stats}
    except Exception as e:
        logger.warning(f"Detection stats failed: {e}")
        return {"stats": []}


@router.get("/measurements/ai/runs")
async def list_ai_measurement_runs(
    project_id: UUID,
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """List recent AI measurement runs (terrain + buildings) for a project."""
    try:
        result = await db.execute(text("""
            SELECT id, status, model_version, terrain_model_version,
                   qa_score, terrain_area_total_sqm, built_area_total_sqm,
                   total_buildings, total_terrain_patches,
                   orthomosaic_file_key, dsm_file_key,
                   started_at, completed_at, error_message
            FROM ai_measurement_runs
            WHERE project_id = :pid
            ORDER BY started_at DESC
            LIMIT :limit
        """), {"pid": str(project_id), "limit": limit})

        runs = []
        for row in result.mappings().all():
            item = dict(row)
            item["id"] = str(item["id"])
            if item.get("started_at"):
                item["started_at"] = item["started_at"].isoformat()
            if item.get("completed_at"):
                item["completed_at"] = item["completed_at"].isoformat()
            runs.append(item)

        return {"runs": runs, "count": len(runs)}
    except Exception as e:
        logger.warning(f"AI measurement runs query failed: {e}")
        return {"runs": [], "count": 0}


@router.get("/measurements/ai/summary")
async def get_ai_measurement_summary(
    project_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Get aggregate terrain/building measurement metrics for a project."""
    try:
        summary = await db.execute(text("""
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(SUM(total_buildings), 0) AS total_buildings,
                COALESCE(SUM(total_terrain_patches), 0) AS total_terrain_patches,
                COALESCE(SUM(built_area_total_sqm), 0) AS built_area_total_sqm,
                COALESCE(SUM(terrain_area_total_sqm), 0) AS terrain_area_total_sqm,
                AVG(qa_score) AS avg_qa_score,
                MAX(completed_at) AS last_completed_at
            FROM ai_measurement_runs
            WHERE project_id = :pid AND status = 'completed'
        """), {"pid": str(project_id)})
        row = summary.mappings().first()

        latest_buildings = await db.execute(text("""
            SELECT id, area_sqm, perimeter_m, height_m, floors_estimate,
                   building_type, quality_score,
                   ST_AsGeoJSON(polygon) AS geojson
            FROM ai_building_measurements
            WHERE run_id = (
                SELECT id FROM ai_measurement_runs
                WHERE project_id = :pid AND status = 'completed'
                ORDER BY completed_at DESC NULLS LAST
                LIMIT 1
            )
            ORDER BY area_sqm DESC
            LIMIT 100
        """), {"pid": str(project_id)})

        buildings = []
        for b in latest_buildings.mappings().all():
            item = dict(b)
            item["id"] = str(item["id"])
            if item.get("geojson"):
                item["geometry"] = json.loads(item.pop("geojson"))
            buildings.append(item)

        payload = dict(row) if row else {}
        if payload.get("last_completed_at"):
            payload["last_completed_at"] = payload["last_completed_at"].isoformat()

        return {
            "summary": payload,
            "latest_buildings": buildings,
            "latest_buildings_count": len(buildings),
        }
    except Exception as e:
        logger.warning(f"AI measurement summary failed: {e}")
        return {
            "summary": {
                "total_runs": 0,
                "total_buildings": 0,
                "total_terrain_patches": 0,
                "built_area_total_sqm": 0,
                "terrain_area_total_sqm": 0,
            },
            "latest_buildings": [],
            "latest_buildings_count": 0,
        }


# ══════════════════════════════════════════════════════════════════════════════
# IPTU MALHA FINA
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/iptu/analysis")
async def run_iptu_analysis(
    project_id: UUID,
    area_tolerance: float = Query(15.0, ge=1.0, le=50.0, description="Tolerância de área (%)"),
    municipality_code: str | None = Query(None, description="Código IBGE do município"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Run IPTU "Malha Fina" analysis for a project.
    Cross-references AI building detections with cadastral parcels.
    Results are persisted to the discrepancies table.
    """
    from app.services.iptu_malha_fina import IPTUMalhaFinaService

    result = await IPTUMalhaFinaService.run_analysis(
        db,
        str(project_id),
        triggered_by=user.get("sub", "unknown"),
        area_tolerance_pct=area_tolerance,
        municipality_code=municipality_code,
    )
    return result


@router.post("/pool-detection")
async def run_pool_detection(
    project_id: UUID,
    flight_asset_id: UUID,
    min_area: float = Query(6.0, ge=1.0, le=100.0, description="Área mínima da piscina (m²)"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Run pool detection analysis on an orthomosaic asset.
    Uses spectral analysis (NDWI) and shape filtering.
    """
    from app.services.pool_detection import PoolDetectionService

    result = await PoolDetectionService.detect_pools(
        db,
        str(project_id),
        str(flight_asset_id),
        triggered_by=user.get("sub", "unknown"),
        min_area_sqm=min_area,
    )
    return result


# ══════════════════════════════════════════════════════════════════════════════
# MEASUREMENTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/measurements")
async def list_measurements(
    project_id: UUID,
    measurement_type: str | None = Query(None, description="distance, area, volume, profile"),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """List measurements for a project."""
    conditions = ["m.project_id = :pid"]
    params: dict[str, Any] = {"pid": str(project_id), "limit": limit}

    if measurement_type:
        conditions.append("m.measurement_type = :mtype")
        params["mtype"] = measurement_type

    where = " AND ".join(conditions)

    try:
        result = await db.execute(text(f"""
            SELECT m.id, m.measurement_type, m.value, m.unit, m.label, m.notes,
                   ST_AsGeoJSON(m.geometry) as geojson, m.created_at
            FROM measurements m
            WHERE {where}
            ORDER BY m.created_at DESC
            LIMIT :limit
        """), params)

        measurements = []
        for r in result.mappings().all():
            m = dict(r)
            m["id"] = str(m["id"])
            if m.get("geojson"):
                m["geometry"] = json.loads(m.pop("geojson"))
            if m.get("created_at"):
                m["created_at"] = m["created_at"].isoformat()
            measurements.append(m)

        return {"measurements": measurements, "count": len(measurements)}
    except Exception as e:
        logger.warning(f"Measurements query failed: {e}")
        return {"measurements": [], "count": 0}


@router.post("/measurements")
async def create_measurement(
    project_id: UUID,
    measurement_type: str = Query(..., description="distance, area, volume, profile"),
    value: float = Query(...),
    unit: str = Query("m"),
    label: str | None = Query(None),
    notes: str | None = Query(None),
    geometry: str = Query(..., description="GeoJSON geometry string"),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Create a new measurement on the map."""
    try:
        geom_data = json.loads(geometry)
        geom_str = json.dumps(geom_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="GeoJSON inválido")

    valid_types = {"distance", "area", "volume", "profile", "elevation", "angle"}
    if measurement_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Tipo inválido. Use: {', '.join(valid_types)}")

    try:
        user_id = user.get("sub")
        result = await db.execute(text("""
            INSERT INTO measurements (project_id, measurement_type, geometry, value, unit, label, notes, measured_by)
            VALUES (:pid, :mtype, ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326), :val, :unit, :label, :notes, :uid)
            RETURNING id, created_at
        """), {
            "pid": str(project_id),
            "mtype": measurement_type,
            "geom": geom_str,
            "val": value,
            "unit": unit,
            "label": label,
            "notes": notes,
            "uid": user_id,
        })
        row = result.fetchone()
        await db.commit()

        return {
            "id": str(row[0]),
            "measurement_type": measurement_type,
            "value": value,
            "unit": unit,
            "label": label,
            "created_at": row[1].isoformat() if row[1] else None,
        }
    except Exception as e:
        logger.warning(f"Measurement creation failed: {e}")
        raise HTTPException(status_code=500, detail="Falha ao salvar medição")


@router.delete("/measurements/{measurement_id}")
async def delete_measurement(
    measurement_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(require_operador),
):
    """Delete a measurement."""
    try:
        await db.execute(text("DELETE FROM measurements WHERE id = :mid"), {"mid": str(measurement_id)})
        await db.commit()
        return {"status": "ok"}
    except Exception as e:
        logger.warning(f"Measurement delete failed: {e}")
        raise HTTPException(status_code=500, detail="Falha ao excluir medição")


# ══════════════════════════════════════════════════════════════════════════════
# DISASTER PREVENTION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/disaster/slope-analysis")
async def analyze_slope_risk(
    dsm_file_key: str = Query(..., description="MinIO key for DSM raster"),
    cell_size: float = Query(1.0, ge=0.01, le=10.0),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Run slope-based landslide risk analysis on a DSM raster.
    Downloads the DSM from MinIO, computes slope gradients, and classifies risk zones.
    """
    import tempfile
    from pathlib import Path
    from minio import Minio
    from app.config import get_settings
    from app.services.disaster_prevention import DisasterPreventionService

    settings = get_settings()
    minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    workdir = Path(tempfile.mkdtemp(prefix="disaster_"))
    dsm_local = workdir / "dsm.tif"

    try:
        minio_client.fget_object(settings.minio_bucket_elevation_models, dsm_file_key, str(dsm_local))
        result = DisasterPreventionService.analyze_slope_risk(str(dsm_local), cell_size_m=cell_size)
        return result
    except Exception as e:
        logger.error(f"Slope analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Análise de declividade falhou: {str(e)[:200]}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


@router.post("/disaster/flood-analysis")
async def analyze_flood_risk(
    dsm_file_key: str = Query(..., description="MinIO key for DSM raster"),
    base_flood_elevation: float | None = Query(None, description="Base flood elevation (m)"),
    cell_size: float = Query(1.0, ge=0.01, le=10.0),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Run flood risk analysis on a DSM raster.
    Identifies low-elevation zones susceptible to flooding.
    """
    import tempfile
    from pathlib import Path
    from minio import Minio
    from app.config import get_settings
    from app.services.disaster_prevention import DisasterPreventionService

    settings = get_settings()
    minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    workdir = Path(tempfile.mkdtemp(prefix="flood_"))
    dsm_local = workdir / "dsm.tif"

    try:
        minio_client.fget_object(settings.minio_bucket_elevation_models, dsm_file_key, str(dsm_local))
        result = DisasterPreventionService.analyze_flood_risk(
            str(dsm_local),
            base_flood_elevation=base_flood_elevation,
            cell_size_m=cell_size,
        )
        return result
    except Exception as e:
        logger.error(f"Flood analysis failed: {e}")
        raise HTTPException(status_code=500, detail=f"Análise de enchente falhou: {str(e)[:200]}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# DENGUE HOTSPOT DETECTION
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/dengue/water-detection")
async def detect_dengue_water(
    orthomosaic_file_key: str = Query(..., description="MinIO key for orthomosaic"),
    ndwi_threshold: float = Query(0.1, ge=0.0, le=0.5),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Detect standing water from orthomosaic imagery using spectral analysis.
    Identifies potential Aedes aegypti breeding sites.
    """
    import tempfile
    from pathlib import Path
    from minio import Minio
    from app.config import get_settings
    from app.services.dengue_detection import DengueHotspotService

    settings = get_settings()
    minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    workdir = Path(tempfile.mkdtemp(prefix="dengue_"))
    ortho_local = workdir / "ortho.tif"

    try:
        minio_client.fget_object(settings.minio_bucket_orthomosaics, orthomosaic_file_key, str(ortho_local))
        result = DengueHotspotService.detect_water_bodies(str(ortho_local), ndwi_threshold=ndwi_threshold)
        return result
    except Exception as e:
        logger.error(f"Dengue water detection failed: {e}")
        raise HTTPException(status_code=500, detail=f"Detecção de água falhou: {str(e)[:200]}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


@router.post("/dengue/terrain-depressions")
async def detect_dengue_depressions(
    dsm_file_key: str = Query(..., description="MinIO key for DSM raster"),
    depression_threshold: float = Query(0.3, ge=0.1, le=2.0, description="Depth threshold (m)"),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Detect terrain depressions from DSM that may accumulate rainwater.
    """
    import tempfile
    from pathlib import Path
    from minio import Minio
    from app.config import get_settings
    from app.services.dengue_detection import DengueHotspotService

    settings = get_settings()
    minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    workdir = Path(tempfile.mkdtemp(prefix="dengue_dep_"))
    dsm_local = workdir / "dsm.tif"

    try:
        minio_client.fget_object(settings.minio_bucket_elevation_models, dsm_file_key, str(dsm_local))
        result = DengueHotspotService.detect_terrain_depressions(
            str(dsm_local), depression_threshold_m=depression_threshold,
        )
        return result
    except Exception as e:
        logger.error(f"Depression detection failed: {e}")
        raise HTTPException(status_code=500, detail=f"Detecção de depressões falhou: {str(e)[:200]}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════════
# TEMPORAL COMPARISON
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/temporal/dsm-comparison")
async def compare_dsm_epochs(
    dsm_epoch1_key: str = Query(..., description="MinIO key for earlier DSM"),
    dsm_epoch2_key: str = Query(..., description="MinIO key for later DSM"),
    change_threshold: float = Query(2.0, ge=0.5, le=10.0, description="Min height change (m)"),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Compare two DSM epochs to detect new constructions and demolitions.
    """
    import tempfile
    from pathlib import Path
    from minio import Minio
    from app.config import get_settings
    from app.services.temporal_comparison import TemporalComparisonService

    settings = get_settings()
    minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    workdir = Path(tempfile.mkdtemp(prefix="temporal_"))
    dsm1 = workdir / "epoch1.tif"
    dsm2 = workdir / "epoch2.tif"

    try:
        minio_client.fget_object(settings.minio_bucket_elevation_models, dsm_epoch1_key, str(dsm1))
        minio_client.fget_object(settings.minio_bucket_elevation_models, dsm_epoch2_key, str(dsm2))
        result = TemporalComparisonService.compare_dsm_epochs(
            str(dsm1), str(dsm2), change_threshold_m=change_threshold,
        )
        return result
    except Exception as e:
        logger.error(f"Temporal DSM comparison failed: {e}")
        raise HTTPException(status_code=500, detail=f"Comparação temporal falhou: {str(e)[:200]}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


@router.post("/temporal/vegetation-comparison")
async def compare_vegetation_epochs(
    ortho_epoch1_key: str = Query(..., description="MinIO key for earlier orthomosaic"),
    ortho_epoch2_key: str = Query(..., description="MinIO key for later orthomosaic"),
    user: dict[str, Any] = Depends(require_gestor),
):
    """
    Compare vegetation (NDVI) between two epochs.
    Detects deforestation and vegetation health changes.
    """
    import tempfile
    from pathlib import Path
    from minio import Minio
    from app.config import get_settings
    from app.services.temporal_comparison import TemporalComparisonService

    settings = get_settings()
    minio_client = Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )

    workdir = Path(tempfile.mkdtemp(prefix="vegetation_"))
    ortho1 = workdir / "epoch1.tif"
    ortho2 = workdir / "epoch2.tif"

    try:
        minio_client.fget_object(settings.minio_bucket_orthomosaics, ortho_epoch1_key, str(ortho1))
        minio_client.fget_object(settings.minio_bucket_orthomosaics, ortho_epoch2_key, str(ortho2))
        result = TemporalComparisonService.compare_vegetation(str(ortho1), str(ortho2))
        return result
    except Exception as e:
        logger.error(f"Vegetation comparison failed: {e}")
        raise HTTPException(status_code=500, detail=f"Comparação de vegetação falhou: {str(e)[:200]}")
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

