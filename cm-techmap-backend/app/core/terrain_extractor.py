"""
CM TECHMAP - Terrain Footprint Extractor
Extracts terrain polygons from georeferenced orthophotos, excluding
building footprints and obvious non-terrain classes (water/shadow).

Output is a GeoJSON FeatureCollection suitable for metrology pipelines.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from pyproj import Geod
from rasterio.features import rasterize, shapes
from scipy.ndimage import binary_closing, binary_opening, binary_fill_holes
from shapely.geometry import shape, Polygon, MultiPolygon, mapping
from shapely.ops import unary_union

logger = logging.getLogger(__name__)
GEOD = Geod(ellps="WGS84")


def _geodesic_area_perimeter(poly: Polygon) -> tuple[float, float]:
    """Calculate area/perimeter in meters using geodesic math (WGS84)."""
    if poly.is_empty:
        return 0.0, 0.0

    lons, lats = poly.exterior.xy
    area, perimeter = GEOD.polygon_area_perimeter(lons, lats)
    return abs(float(area)), float(perimeter)


def _classify_terrain_mask(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> np.ndarray:
    """Classify likely terrain pixels from RGB image."""
    value = np.maximum(np.maximum(red, green), blue)
    min_rgb = np.minimum(np.minimum(red, green), blue)
    range_rgb = value - min_rgb
    saturation = np.where(value > 0.01, range_rgb / (value + 1e-6), 0)
    greenness = (green - red) / (green + red + 1e-6)
    blueness = (blue - np.maximum(red, green)) / (blue + np.maximum(red, green) + 1e-6)

    water = (value < 0.35) & (blueness > 0.05) & (saturation > 0.1)
    shadow = value < 0.10
    high_density_canopy = (greenness > 0.18) & (value > 0.2)

    # Terrain classes: open ground + roads + low/medium vegetation.
    terrain = (~water) & (~shadow) & (~high_density_canopy)

    terrain = binary_closing(terrain, structure=np.ones((5, 5)), iterations=2)
    terrain = binary_opening(terrain, structure=np.ones((3, 3)), iterations=1)
    terrain = binary_fill_holes(terrain)
    return terrain.astype(np.uint8)


def _load_building_polygons(buildings_geojson_path: str | Path | None) -> list[Polygon]:
    if not buildings_geojson_path:
        return []

    path = Path(buildings_geojson_path)
    if not path.exists():
        return []

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    polygons: list[Polygon] = []
    for feature in data.get("features", []):
        try:
            geom = shape(feature.get("geometry", {}))
            if isinstance(geom, Polygon) and geom.is_valid and not geom.is_empty:
                polygons.append(geom)
            elif isinstance(geom, MultiPolygon):
                polygons.extend([p for p in geom.geoms if p.is_valid and not p.is_empty])
        except Exception:
            continue

    return polygons


def extract_terrain_footprints(
    orthophoto_path: str | Path,
    output_path: str | Path | None = None,
    buildings_geojson_path: str | Path | None = None,
    min_patch_area_sqm: float = 80.0,
) -> Path:
    """
    Extract terrain polygons from orthophoto and save as GeoJSON.

    Args:
        orthophoto_path: Path to orthophoto GeoTIFF.
        output_path: Output GeoJSON path.
        buildings_geojson_path: Optional path to building footprints to subtract.
        min_patch_area_sqm: Minimum area for a terrain polygon.
    """
    orthophoto_path = Path(orthophoto_path)
    if output_path is None:
        output_path = orthophoto_path.parent / "terrain.geojson"
    output_path = Path(output_path)

    with rasterio.open(orthophoto_path) as src:
        transform = src.transform
        crs = src.crs
        red = src.read(1).astype(np.float32)
        green = src.read(2).astype(np.float32) if src.count >= 2 else red.copy()
        blue = src.read(3).astype(np.float32) if src.count >= 3 else red.copy()

    max_val = max(float(red.max()), float(green.max()), float(blue.max()), 1.0)
    red_n, green_n, blue_n = red / max_val, green / max_val, blue / max_val

    terrain_mask = _classify_terrain_mask(red_n, green_n, blue_n)

    building_polygons = _load_building_polygons(buildings_geojson_path)
    if building_polygons:
        building_mask = rasterize(
            [(poly, 1) for poly in building_polygons],
            out_shape=terrain_mask.shape,
            transform=transform,
            fill=0,
            dtype="uint8",
        )
        terrain_mask = np.where(building_mask == 1, 0, terrain_mask).astype(np.uint8)

    features: list[dict[str, Any]] = []
    union_polys: list[Polygon] = []

    for geom, val in shapes(terrain_mask, mask=terrain_mask == 1, transform=transform):
        if val != 1:
            continue

        try:
            poly = shape(geom)
            if poly.is_empty:
                continue
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty:
                continue

            area_sqm, perimeter_m = _geodesic_area_perimeter(poly)
            if area_sqm < min_patch_area_sqm:
                continue

            compactness = 0.0
            if perimeter_m > 0:
                compactness = float((4.0 * np.pi * area_sqm) / (perimeter_m * perimeter_m))

            props = {
                "area_sqm": round(area_sqm, 2),
                "perimeter_m": round(perimeter_m, 2),
                "compactness": round(compactness, 4),
                "confidence": 0.78,
                "surface_type": "terrain",
            }
            features.append({"type": "Feature", "geometry": mapping(poly), "properties": props})
            union_polys.append(poly)
        except Exception:
            continue

    total_area = 0.0
    if union_polys:
        merged = unary_union(union_polys)
        if isinstance(merged, Polygon):
            total_area = _geodesic_area_perimeter(merged)[0]
        elif isinstance(merged, MultiPolygon):
            total_area = sum(_geodesic_area_perimeter(p)[0] for p in merged.geoms)

    fc = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "source": "terrain_extractor_v1",
            "orthophoto": orthophoto_path.name,
            "total_patches": len(features),
            "total_area_sqm": round(total_area, 2),
            "crs": str(crs) if crs else "EPSG:4326",
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(fc, f)

    logger.info(
        "[TERRAIN] Extracted %s terrain patches, total_area=%.2fm2",
        len(features),
        total_area,
    )
    return output_path
