"""
CM TECHMAP — Building Footprint Extractor
Extracts building footprints from an orthophoto + synthetic DSM.
Outputs a GeoJSON FeatureCollection with height attributes for 3D extrusion.
"""

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

logger = logging.getLogger(__name__)

# Minimum area in pixels² to be considered a building
MIN_BUILDING_AREA_PX = 200
# Maximum area in m² — anything larger is a terrain hull, not a building
MAX_BUILDING_AREA_M2 = 5000.0
# Maximum polygon simplification tolerance (in pixel units)
SIMPLIFY_TOLERANCE = 2.0


def extract_building_footprints(
    orthophoto_path: str | Path,
    dsm_path: str | Path,
    output_path: str | Path | None = None,
    min_height_m: float = 2.5,
    base_elevation: float = 800.0,
) -> Path:
    """
    Extract building footprints from orthophoto + DSM and save as GeoJSON.

    Args:
        orthophoto_path: Path to input orthophoto GeoTIFF
        dsm_path: Path to DSM GeoTIFF (synthetic or real)
        output_path: Path for output GeoJSON (default: footprints.geojson)
        min_height_m: Minimum height above ground to be considered a building
        base_elevation: Base terrain elevation for height calculation

    Returns:
        Path to the generated GeoJSON file
    """
    orthophoto_path = Path(orthophoto_path)
    dsm_path = Path(dsm_path)
    if output_path is None:
        output_path = orthophoto_path.parent / "footprints.geojson"
    output_path = Path(output_path)

    logger.info(f"[BUILDINGS] Extracting footprints from {orthophoto_path.name}")

    # Load orthophoto for color classification
    # Downsample large images to max 4096px to avoid OOM — the DSM generator
    # already operates at this resolution, so building detection accuracy
    # is not affected. Coordinates are remapped via adjusted transform.
    MAX_DIM = 4096
    with rasterio.open(orthophoto_path) as src:
        orig_transform = src.transform
        crs = src.crs
        width = src.width
        height = src.height
        band_count = min(src.count, 3)

        scale = 1.0
        if max(width, height) > MAX_DIM:
            scale = MAX_DIM / max(width, height)
            out_shape = (int(height * scale), int(width * scale))
            logger.info(
                f"[BUILDINGS] Downsampling ortho {width}x{height} → "
                f"{out_shape[1]}x{out_shape[0]} (factor={scale:.3f})"
            )
            from rasterio.enums import Resampling
            bands = [
                src.read(i, out_shape=out_shape, resampling=Resampling.bilinear)
                .astype(np.float32)
                for i in range(1, band_count + 1)
            ]
            # Adjust transform to match downsampled pixel grid
            transform = rasterio.transform.from_bounds(
                src.bounds.left, src.bounds.bottom,
                src.bounds.right, src.bounds.top,
                out_shape[1], out_shape[0],
            )
        else:
            bands = [src.read(i).astype(np.float32) for i in range(1, band_count + 1)]
            transform = orig_transform

        while len(bands) < 3:
            bands.append(bands[0].copy())

    red, green, blue = bands[0], bands[1], bands[2]
    del bands  # Free list reference
    max_val = max(red.max(), green.max(), blue.max(), 1.0)
    red_n, green_n, blue_n = red / max_val, green / max_val, blue / max_val
    del red, green, blue  # Free full-precision arrays

    # Load DSM for height data — apply same downsampling
    with rasterio.open(dsm_path) as dsm_src:
        if scale < 1.0:
            dsm_out_shape = (int(dsm_src.height * scale), int(dsm_src.width * scale))
            # Match the ortho dimensions exactly
            dsm_out_shape = (red_n.shape[0], red_n.shape[1])
            from rasterio.enums import Resampling
            dsm_data = dsm_src.read(
                1, out_shape=dsm_out_shape, resampling=Resampling.bilinear
            ).astype(np.float32)
        else:
            dsm_data = dsm_src.read(1).astype(np.float32)
        dsm_nodata = dsm_src.nodata

    # Ensure DSM matches ortho dimensions (edge case from rounding)
    if dsm_data.shape != red_n.shape:
        target_h, target_w = red_n.shape
        from scipy.ndimage import zoom as scipy_zoom
        zoom_y = target_h / dsm_data.shape[0]
        zoom_x = target_w / dsm_data.shape[1]
        dsm_data = scipy_zoom(dsm_data, (zoom_y, zoom_x), order=1)
        dsm_data = dsm_data[:target_h, :target_w]

    # Calculate height above ground
    if dsm_nodata is not None:
        valid_mask = dsm_data != dsm_nodata
        dsm_data[~valid_mask] = base_elevation
    height_above_ground = dsm_data - base_elevation
    del dsm_data  # Free DSM array

    # Create building mask from height + color analysis
    building_mask = _create_building_mask(
        red_n, green_n, blue_n, height_above_ground, min_height_m
    )

    logger.info(
        f"[BUILDINGS] Building pixels: {building_mask.sum()} / "
        f"{building_mask.size} ({building_mask.sum() / building_mask.size * 100:.1f}%)"
    )

    # Extract contours and convert to polygons
    features = _extract_polygon_features(
        building_mask, height_above_ground, transform, crs, red_n, green_n, blue_n
    )
    del red_n, green_n, blue_n, height_above_ground, building_mask  # Free all arrays

    logger.info(f"[BUILDINGS] Extracted {len(features)} building footprints")

    # Build GeoJSON FeatureCollection
    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "source": "CM-TECHMAP synthetic extraction",
            "orthophoto": orthophoto_path.name,
            "total_buildings": len(features),
        },
    }

    with open(output_path, "w") as f:
        json.dump(geojson, f)

    logger.info(f"[BUILDINGS] Written: {output_path.name}")
    return output_path


def _create_building_mask(red, green, blue, height_ag, min_height):
    """Create a binary mask of building pixels using height + color."""
    from scipy.ndimage import binary_fill_holes, binary_opening, binary_closing

    # Height-based mask
    height_mask = height_ag > min_height

    # Color-based mask (non-vegetation, non-water)
    value = np.maximum(np.maximum(red, green), blue)
    greenness = (green - red) / (green + red + 1e-6)
    saturation_approx = (value - np.minimum(np.minimum(red, green), blue)) / (value + 1e-6)

    # Exclude vegetation
    not_vegetation = greenness < 0.12

    # Exclude very dark (shadows) and very saturated (water)
    not_shadow = value > 0.15
    not_water = ~((value < 0.35) & (saturation_approx > 0.15))

    # Combine
    color_mask = not_vegetation & not_shadow & not_water

    # Final building mask
    building_mask = height_mask & color_mask

    # Morphological cleanup
    struct = np.ones((5, 5))
    building_mask = binary_closing(building_mask, structure=struct, iterations=2)
    building_mask = binary_opening(building_mask, structure=struct, iterations=1)
    building_mask = binary_fill_holes(building_mask)

    return building_mask.astype(np.uint8)


def _extract_polygon_features(mask, height_ag, transform, crs, red, green, blue):
    """Extract polygon features from binary mask."""
    from scipy.ndimage import label

    # Label connected components
    labeled, num_features = label(mask)

    features = []
    from pyproj import Transformer

    # Prepare CRS transformer if needed
    need_transform = crs and str(crs) != "EPSG:4326"
    transformer = None
    if need_transform:
        try:
            transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        except Exception:
            need_transform = False

    for region_id in range(1, min(num_features + 1, 500)):  # Cap at 500 buildings
        region_mask = labeled == region_id
        area_px = region_mask.sum()

        if area_px < MIN_BUILDING_AREA_PX:
            continue

        # Get bounding box for this region
        rows, cols = np.where(region_mask)
        if len(rows) == 0:
            continue

        r_min, r_max = rows.min(), rows.max()
        c_min, c_max = cols.min(), cols.max()

        # Calculate mean height for this building
        building_heights = height_ag[region_mask]
        mean_height = float(np.mean(building_heights))
        max_height = float(np.max(building_heights))

        if mean_height < 1.0:
            continue

        # Create simplified polygon from bounding region
        # Use convex hull of the region's pixel coordinates
        polygon_pixels = _region_to_polygon(region_mask, r_min, r_max, c_min, c_max)
        if polygon_pixels is None:
            continue

        # Transform pixel coordinates to geographic coordinates
        geo_coords = []
        for px, py in polygon_pixels:
            gx, gy = rasterio.transform.xy(transform, py, px)
            if need_transform and transformer:
                gx, gy = transformer.transform(gx, gy)
            geo_coords.append((gx, gy))

        if len(geo_coords) < 4:
            continue

        # Close the polygon
        if geo_coords[0] != geo_coords[-1]:
            geo_coords.append(geo_coords[0])

        try:
            poly = Polygon(geo_coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area == 0:
                continue

            # Simplify for performance
            simplified = poly.simplify(transform.a * SIMPLIFY_TOLERANCE, preserve_topology=True)
            if simplified.is_empty:
                simplified = poly

            # Classify building type by height
            if mean_height > 15:
                btype = "commercial"
            elif mean_height > 8:
                btype = "residential_multi"
            elif mean_height > 4:
                btype = "residential"
            else:
                btype = "garage"

            # Determine dominant color
            bld_red = float(np.mean(red[region_mask]))
            bld_green = float(np.mean(green[region_mask]))
            bld_blue = float(np.mean(blue[region_mask]))

            area_m2 = float(area_px * abs(transform.a * transform.e))

            # Skip features that are too large — these are terrain hulls,
            # not individual buildings
            if area_m2 > MAX_BUILDING_AREA_M2:
                logger.debug(f"[BUILDINGS] Skipping oversized polygon: {area_m2:.0f}m²")
                continue

            feature = {
                "type": "Feature",
                "geometry": mapping(simplified),
                "properties": {
                    "height": round(mean_height, 1),
                    "max_height": round(max_height, 1),
                    "area_m2": round(area_m2, 1),
                    "building_type": btype,
                    "color_r": round(bld_red, 3),
                    "color_g": round(bld_green, 3),
                    "color_b": round(bld_blue, 3),
                },
            }
            features.append(feature)

        except Exception as e:
            logger.debug(f"[BUILDINGS] Skipping invalid polygon: {e}")
            continue

    return features


def _region_to_polygon(mask, r_min, r_max, c_min, c_max):
    """Convert a labeled region to a simplified polygon outline."""
    from scipy.ndimage import binary_erosion

    # Extract sub-region
    sub = mask[r_min:r_max + 1, c_min:c_max + 1].copy()

    if sub.sum() < MIN_BUILDING_AREA_PX:
        return None

    # Get boundary pixels
    eroded = binary_erosion(sub)
    boundary = sub & ~eroded

    rows, cols = np.where(boundary)
    if len(rows) < 4:
        # Fall back to bounding box
        return [
            (c_min, r_min),
            (c_max, r_min),
            (c_max, r_max),
            (c_min, r_max),
            (c_min, r_min),
        ]

    # Offset back to full image coordinates
    rows = rows + r_min
    cols = cols + c_min

    # Create convex hull from boundary points
    try:
        from scipy.spatial import ConvexHull
        points = np.column_stack((cols, rows))
        if len(points) < 3:
            return None
        hull = ConvexHull(points)
        hull_pts = [(int(points[v, 0]), int(points[v, 1])) for v in hull.vertices]
        hull_pts.append(hull_pts[0])
        return hull_pts
    except Exception:
        # Fallback to bounding box
        return [
            (c_min, r_min),
            (c_max, r_min),
            (c_max, r_max),
            (c_min, r_max),
            (c_min, r_min),
        ]


def extract_buildings_from_elevation(
    dsm_path: str | Path,
    dtm_path: str | Path | None = None,
    output_path: str | Path | None = None,
    min_height_m: float = 2.0,
) -> Path:
    """
    Extract building footprints from REAL DSM/DTM elevation data.

    This function is designed for photogrammetric DSM output (e.g., from NodeODM)
    where elevation values are actual heights in meters, not synthetic estimates.

    When DTM is available, computes nDSM = DSM - DTM for normalized above-ground
    heights. When only DSM is available, estimates ground level using morphological
    opening (large structuring element) to approximate bare earth.

    Args:
        dsm_path: Path to the DSM GeoTIFF (real elevation from photogrammetry)
        dtm_path: Optional path to DTM GeoTIFF (bare earth model)
        output_path: Path for output GeoJSON
        min_height_m: Minimum above-ground height to classify as building

    Returns:
        Path to the generated GeoJSON file
    """
    from scipy.ndimage import (
        binary_fill_holes, binary_opening, binary_closing,
        label, uniform_filter, median_filter,
    )
    from pyproj import Transformer

    dsm_path = Path(dsm_path)
    if output_path is None:
        output_path = dsm_path.parent / "footprints.geojson"
    output_path = Path(output_path)

    logger.info(f"[REAL-BUILDINGS] Extracting from real DSM: {dsm_path.name}")

    # Load DSM
    with rasterio.open(dsm_path) as dsm_src:
        dsm_data = dsm_src.read(1).astype(np.float32)
        dsm_nodata = dsm_src.nodata
        dsm_transform = dsm_src.transform
        dsm_crs = dsm_src.crs

    # Handle nodata
    if dsm_nodata is not None:
        valid_mask = dsm_data != dsm_nodata
        dsm_data[~valid_mask] = np.nanmedian(dsm_data[valid_mask]) if valid_mask.any() else 0
    else:
        valid_mask = np.ones_like(dsm_data, dtype=bool)

    # Compute above-ground heights (nDSM)
    if dtm_path is not None:
        dtm_path = Path(dtm_path)
        logger.info(f"[REAL-BUILDINGS] Computing nDSM using DTM: {dtm_path.name}")
        with rasterio.open(dtm_path) as dtm_src:
            dtm_data = dtm_src.read(1).astype(np.float32)
            dtm_nodata = dtm_src.nodata
            if dtm_nodata is not None:
                dtm_valid = dtm_data != dtm_nodata
                dtm_data[~dtm_valid] = np.nanmedian(dtm_data[dtm_valid]) if dtm_valid.any() else 0

            # Resample DTM to match DSM dimensions if needed
            if dtm_data.shape != dsm_data.shape:
                from scipy.ndimage import zoom
                zoom_y = dsm_data.shape[0] / dtm_data.shape[0]
                zoom_x = dsm_data.shape[1] / dtm_data.shape[1]
                dtm_data = zoom(dtm_data, (zoom_y, zoom_x), order=1)
                # Trim/pad if needed
                dtm_data = dtm_data[:dsm_data.shape[0], :dsm_data.shape[1]]

        ndsm = dsm_data - dtm_data
    else:
        # No DTM — estimate ground using morphological opening
        logger.info("[REAL-BUILDINGS] No DTM — estimating ground from DSM")
        # Large kernel median filter approximates bare earth
        kernel_size = max(51, min(dsm_data.shape) // 20)
        if kernel_size % 2 == 0:
            kernel_size += 1
        ground_estimate = median_filter(dsm_data, size=kernel_size)
        ndsm = dsm_data - ground_estimate

    # Clip negative values (artifacts)
    ndsm = np.clip(ndsm, 0, None)

    logger.info(f"[REAL-BUILDINGS] nDSM range: {ndsm.min():.1f}m - {ndsm.max():.1f}m")

    # Create building mask from height threshold
    building_mask = ndsm > min_height_m
    building_mask = building_mask & valid_mask

    # Morphological cleanup
    struct = np.ones((5, 5))
    building_mask = binary_closing(building_mask, structure=struct, iterations=2)
    building_mask = binary_opening(building_mask, structure=struct, iterations=1)
    building_mask = binary_fill_holes(building_mask)
    building_mask = building_mask.astype(np.uint8)

    logger.info(
        f"[REAL-BUILDINGS] Building pixels: {building_mask.sum()} / "
        f"{building_mask.size} ({building_mask.sum() / building_mask.size * 100:.1f}%)"
    )

    # Label connected components
    labeled, num_features = label(building_mask)

    # Prepare CRS transformer
    need_transform = dsm_crs and str(dsm_crs) != "EPSG:4326"
    transformer = None
    if need_transform:
        try:
            transformer = Transformer.from_crs(dsm_crs, "EPSG:4326", always_xy=True)
        except Exception:
            need_transform = False

    features = []
    for region_id in range(1, min(num_features + 1, 800)):
        region_mask = labeled == region_id
        area_px = region_mask.sum()

        if area_px < MIN_BUILDING_AREA_PX:
            continue

        rows, cols = np.where(region_mask)
        if len(rows) == 0:
            continue

        # Calculate real height statistics from nDSM
        building_heights = ndsm[region_mask]
        mean_height = float(np.mean(building_heights))
        max_height = float(np.max(building_heights))
        median_height = float(np.median(building_heights))

        if mean_height < 1.0:
            continue

        r_min, r_max = rows.min(), rows.max()
        c_min, c_max = cols.min(), cols.max()

        # Create polygon from region boundary
        polygon_pixels = _region_to_polygon(region_mask, r_min, r_max, c_min, c_max)
        if polygon_pixels is None:
            continue

        # Transform pixel coordinates to geographic coordinates
        geo_coords = []
        for px, py in polygon_pixels:
            gx, gy = rasterio.transform.xy(dsm_transform, py, px)
            if need_transform and transformer:
                gx, gy = transformer.transform(gx, gy)
            geo_coords.append((gx, gy))

        if len(geo_coords) < 4:
            continue

        if geo_coords[0] != geo_coords[-1]:
            geo_coords.append(geo_coords[0])

        try:
            poly = Polygon(geo_coords)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area == 0:
                continue

            simplified = poly.simplify(
                dsm_transform.a * SIMPLIFY_TOLERANCE, preserve_topology=True
            )
            if simplified.is_empty:
                simplified = poly

            # Classify building type by real height
            if median_height > 20:
                btype = "highrise"
            elif median_height > 12:
                btype = "commercial"
            elif median_height > 6:
                btype = "residential_multi"
            elif median_height > 3:
                btype = "residential"
            else:
                btype = "garage"

            area_m2 = float(area_px * abs(dsm_transform.a * dsm_transform.e))

            # Skip features that are too large — terrain hulls, not buildings
            if area_m2 > MAX_BUILDING_AREA_M2:
                logger.debug(f"[REAL-BUILDINGS] Skipping oversized polygon: {area_m2:.0f}m²")
                continue

            feature = {
                "type": "Feature",
                "geometry": mapping(simplified),
                "properties": {
                    "height": round(median_height, 1),
                    "max_height": round(max_height, 1),
                    "mean_height": round(mean_height, 1),
                    "area_m2": round(area_m2, 1),
                    "building_type": btype,
                    "dsm_source": "real",
                },
            }
            features.append(feature)

        except Exception as e:
            logger.debug(f"[REAL-BUILDINGS] Skipping invalid polygon: {e}")
            continue

    logger.info(f"[REAL-BUILDINGS] Extracted {len(features)} building footprints from real DSM")

    # Build GeoJSON FeatureCollection
    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "source": "CM-TECHMAP real DSM extraction",
            "dsm_file": dsm_path.name,
            "dtm_used": dtm_path is not None,
            "total_buildings": len(features),
            "dsm_source": "real",
        },
    }

    import json
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    logger.info(f"[REAL-BUILDINGS] Written: {output_path.name}")
    return output_path
