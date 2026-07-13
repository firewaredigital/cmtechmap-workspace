"""
CM TECHMAP — Synthetic DSM Generator (Smoothed)
Generates a Digital Surface Model from an orthophoto using multi-signal
image analysis: texture energy, edge detection, and color classification.

CRITICAL: This module generates ESTIMATED elevation — it is NOT photogrammetric.
For real 3D, drone photos must be processed through NodeODM (--dsm flag).
The synthetic DSM is designed for subtle, smooth terrain visualization
that gives a gentle "drape" effect over the orthomosaic, not dramatic 3D.

Output: georeferenced single-band float32 GeoTIFF with elevation in meters.
"""

import logging
from pathlib import Path
from typing import Any

import numpy as np
import rasterio

logger = logging.getLogger(__name__)

# Near-flat elevation assignments — synthetic DSM should produce
# almost imperceptible elevation differences. The frontend disables
# terrain for synthetic sources (orthoFlatMode), so these values are
# only used for building extraction heuristics.
ELEVATION_MAP = {
    "building_high": 3.0,     # Reduced from 6.0
    "building_medium": 2.0,   # Reduced from 4.0
    "building_low": 1.0,      # Reduced from 2.0
    "vegetation_tall": 1.5,   # Reduced from 3.0
    "vegetation_medium": 0.8, # Reduced from 1.5
    "vegetation_low": 0.3,    # Reduced from 0.5
    "road": 0.0,
    "bare_soil": 0.05,        # Reduced from 0.1
    "water": -0.05,           # Reduced from -0.1
    "shadow": 0.0,
}


def _normalize(arr: np.ndarray) -> np.ndarray:
    mn, mx = arr.min(), arr.max()
    if mx - mn < 1e-8:
        return np.zeros_like(arr)
    return (arr - mn) / (mx - mn)


def _gradient_magnitude(gray: np.ndarray) -> np.ndarray:
    gy, gx = np.gradient(gray)
    return np.sqrt(gx**2 + gy**2)


def _compute_texture_energy(red, green, blue):
    from scipy.ndimage import gaussian_filter, uniform_filter, laplace

    gray = 0.299 * red + 0.587 * green + 0.114 * blue
    gy, gx = np.gradient(gray)
    gradient_mag = np.sqrt(gx**2 + gy**2)

    mean_local = uniform_filter(gray, size=7)
    mean_sq = uniform_filter(gray**2, size=7)
    local_var = np.clip(mean_sq - mean_local**2, 0, None)

    log_response = np.abs(laplace(gaussian_filter(gray, sigma=1.5)))

    texture = (
        _normalize(gradient_mag) * 0.4
        + _normalize(np.sqrt(local_var)) * 0.35
        + _normalize(log_response) * 0.25
    )
    return texture


def _compute_edge_structure(red, green, blue):
    from scipy.ndimage import distance_transform_edt, gaussian_filter

    gray = 0.299 * red + 0.587 * green + 0.114 * blue

    edges_fine = _gradient_magnitude(gaussian_filter(gray, sigma=1.0))
    edges_coarse = _gradient_magnitude(gaussian_filter(gray, sigma=3.0))
    edges = edges_fine * 0.6 + edges_coarse * 0.4

    threshold = np.percentile(edges, 85)
    binary_edges = (edges > threshold).astype(np.float32)

    interior_dist = distance_transform_edt(1 - binary_edges)
    max_dist = max(np.percentile(interior_dist, 99), 1.0)
    interior_dist = np.clip(interior_dist / max_dist, 0, 1)
    interior_dist = gaussian_filter(interior_dist, sigma=2.0)
    return interior_dist


def _classify_surfaces(red, green, blue):
    h, w = red.shape
    class_labels = np.full((h, w), "bare_soil", dtype="U20")

    value = np.maximum(np.maximum(red, green), blue)
    min_rgb = np.minimum(np.minimum(red, green), blue)
    range_rgb = value - min_rgb
    saturation = np.where(value > 0.01, range_rgb / (value + 1e-6), 0)
    greenness = (green - red) / (green + red + 1e-6)
    redness = (red - green) / (red + green + 1e-6)
    blueness = (blue - np.maximum(red, green)) / (blue + np.maximum(red, green) + 1e-6)

    water_mask = (value < 0.35) & (blueness > 0.05) & (saturation > 0.1)
    class_labels[water_mask] = "water"

    road_mask = (value < 0.55) & (value > 0.15) & (saturation < 0.15) & ~water_mask
    class_labels[road_mask] = "road"

    shadow_mask = value < 0.12
    class_labels[shadow_mask] = "shadow"

    veg_tall = (greenness > 0.15) & (saturation > 0.15) & (value > 0.2)
    veg_med = (greenness > 0.08) & (saturation > 0.1) & ~veg_tall
    veg_low = (greenness > 0.03) & (saturation > 0.05) & ~veg_tall & ~veg_med

    class_labels[veg_low] = "vegetation_low"
    class_labels[veg_med] = "vegetation_medium"
    class_labels[veg_tall] = "vegetation_tall"

    bld_high = (value > 0.6) & (saturation < 0.25) & (greenness < 0.05) & ~veg_tall & ~veg_med
    bld_med = (value > 0.4) & (value <= 0.6) & (saturation < 0.3) & (greenness < 0.08) & ~road_mask & ~veg_tall & ~veg_med
    bld_low = (value > 0.3) & (value <= 0.4) & (saturation < 0.2) & (greenness < 0.05) & ~road_mask & ~veg_tall & ~veg_med
    red_roof = (redness > 0.1) & (saturation > 0.15) & (value > 0.3)

    class_labels[bld_low] = "building_low"
    class_labels[bld_med] = "building_medium"
    class_labels[bld_high] = "building_high"
    class_labels[red_roof] = "building_medium"

    class_map = np.zeros_like(red, dtype=np.int32)
    for i, name in enumerate(ELEVATION_MAP.keys()):
        class_map[class_labels == name] = i

    return class_map, class_labels


def _map_classes_to_elevation(class_labels, max_bld, max_tree):
    elevation = np.zeros(class_labels.shape, dtype=np.float32)
    for name, base in ELEVATION_MAP.items():
        mask = class_labels == name
        if "building" in name:
            scaled = base * (max_bld / 6.0)
        elif "vegetation" in name:
            scaled = base * (max_tree / 3.0)
        else:
            scaled = base
        count = mask.sum()
        if count > 0:
            # Very low noise — 3% instead of 15% to prevent spikes
            noise = np.random.default_rng(42).normal(0, max(scaled * 0.03, 0.005), count)
            elevation[mask] = scaled + noise
    return np.clip(elevation, -0.5, max_bld * 1.2)


def generate_synthetic_dsm(
    orthophoto_path: str | Path,
    output_path: str | Path | None = None,
    base_elevation: float = 800.0,
    max_building_height: float = 3.0,    # Reduced: near-flat synthetic terrain
    max_tree_height: float = 2.0,        # Reduced: minimal vegetation bumps
    smoothing_sigma: float = 15.0,       # Heavy: produces very smooth terrain
) -> Path:
    """Generate a synthetic DSM from an orthophoto.

    IMPORTANT: This produces a heavily smoothed, near-flat elevation model.
    The frontend DISABLES terrain rendering for synthetic DSMs (orthoFlatMode),
    so distortion is prevented at the rendering level. This DSM is used
    primarily for building footprint extraction heuristics.

    For real 3D terrain, drone photos must be processed through NodeODM.
    """
    from scipy.ndimage import gaussian_filter, median_filter, zoom

    orthophoto_path = Path(orthophoto_path)
    if output_path is None:
        output_path = orthophoto_path.parent / f"{orthophoto_path.stem}_dsm.tif"
    output_path = Path(output_path)

    logger.info(f"[DSM] Generating synthetic DSM from {orthophoto_path.name}")

    with rasterio.open(orthophoto_path) as src:
        transform = src.transform
        crs = src.crs
        width = src.width
        height = src.height
        band_count = min(src.count, 3)

        # For large images, downsample to max 4096px to avoid OOM
        MAX_DIM = 4096
        scale_factor = 1.0
        if max(width, height) > MAX_DIM:
            scale_factor = MAX_DIM / max(width, height)
            out_shape = (int(height * scale_factor), int(width * scale_factor))
            logger.info(f"[DSM] Downsampling {width}x{height} → {out_shape[1]}x{out_shape[0]} (factor={scale_factor:.3f})")
            from rasterio.enums import Resampling
            bands = []
            for i in range(1, band_count + 1):
                band = src.read(i, out_shape=out_shape, resampling=Resampling.bilinear).astype(np.float32)
                bands.append(band)
        else:
            bands = [src.read(i).astype(np.float32) for i in range(1, band_count + 1)]

        while len(bands) < 3:
            bands.append(bands[0].copy())

    red, green, blue = bands[0], bands[1], bands[2]
    max_val = max(red.max(), green.max(), blue.max(), 1.0)
    red_n, green_n, blue_n = red / max_val, green / max_val, blue / max_val

    proc_h, proc_w = red_n.shape
    logger.info(f"[DSM] Processing: {proc_w}x{proc_h}px, CRS={crs}")

    # Free original band arrays
    del red, green, blue, bands

    texture_map = _compute_texture_energy(red_n, green_n, blue_n)
    edge_map = _compute_edge_structure(red_n, green_n, blue_n)
    _, class_labels = _classify_surfaces(red_n, green_n, blue_n)

    # Free normalized arrays
    del red_n, green_n, blue_n

    color_elevation = _map_classes_to_elevation(class_labels, max_building_height, max_tree_height)
    del class_labels

    texture_norm = _normalize(texture_map)
    del texture_map
    edge_norm = _normalize(edge_map)
    del edge_map

    # Blend signals with more weight on color classification (most stable)
    raw = (texture_norm * 0.25 + edge_norm * 0.15 + _normalize(color_elevation) * 0.60)
    raw = raw * max_building_height
    blended = raw * 0.3 + color_elevation * 0.7
    del raw, color_elevation

    # ── Heavy smoothing pipeline ─────────────────────────────────────────
    # Step 1: Large median filter to remove salt-and-pepper noise
    smoothed = median_filter(blended, size=7)

    # Step 2: Strong Gaussian blur for overall smoothness
    smoothed = gaussian_filter(smoothed, sigma=smoothing_sigma)

    # Step 3: Second Gaussian pass for extra smooth terrain
    smoothed = gaussian_filter(smoothed, sigma=smoothing_sigma * 0.5)

    # Step 4: Clamp extreme outliers (> 2σ from mean)
    mean_elev = smoothed.mean()
    std_elev = smoothed.std()
    if std_elev > 0.01:
        upper_clamp = mean_elev + 2.0 * std_elev
        lower_clamp = mean_elev - 2.0 * std_elev
        smoothed = np.clip(smoothed, lower_clamp, upper_clamp)

    # Step 5: Final gentle smooth after clamping
    smoothed = gaussian_filter(smoothed, sigma=3.0)

    del edge_norm, texture_norm, blended

    # If we downsampled, upsample the DSM back to original resolution
    if scale_factor < 1.0:
        logger.info(f"[DSM] Upsampling DSM to original resolution: {width}x{height}")
        zoom_y = height / smoothed.shape[0]
        zoom_x = width / smoothed.shape[1]
        smoothed = zoom(smoothed, (zoom_y, zoom_x), order=1)
        # Ensure exact dimensions match
        if smoothed.shape != (height, width):
            result = np.full((height, width), base_elevation, dtype=np.float32)
            h_crop = min(smoothed.shape[0], height)
            w_crop = min(smoothed.shape[1], width)
            result[:h_crop, :w_crop] = smoothed[:h_crop, :w_crop]
            smoothed = result

    dsm = base_elevation + smoothed
    del smoothed

    logger.info(f"[DSM] Final: min={dsm.min():.1f}m, max={dsm.max():.1f}m, range={dsm.max()-dsm.min():.1f}m")

    dsm_profile = {
        "driver": "GTiff", "dtype": "float32",
        "width": width, "height": height, "count": 1,
        "crs": crs, "transform": transform,
        "nodata": -9999.0, "compress": "deflate",
        "predictor": 2, "tiled": True,
        "blockxsize": 256, "blockysize": 256,
    }

    with rasterio.open(output_path, "w", **dsm_profile) as dst:
        dst.write(dsm.astype(np.float32), 1)
        dst.update_tags(
            PROCESSING="CM-TECHMAP synthetic DSM v3 (ultra-smooth)",
            ELEVATION_UNIT="meters",
            DSM_SOURCE="synthetic",
            DSM_NOTE="Near-flat synthetic terrain. Frontend uses orthoFlatMode to disable terrain rendering.",
        )

    logger.info(f"[DSM] Written: {output_path.name} ({output_path.stat().st_size / 1048576:.1f} MB)")
    return output_path


def extract_dsm_metadata(dsm_path: str | Path) -> dict[str, Any]:
    """Extract metadata from a generated DSM file."""
    from pyproj import Transformer

    dsm_path = Path(dsm_path)
    with rasterio.open(dsm_path) as ds:
        bounds = ds.bounds
        crs = ds.crs
        data = ds.read(1)
        nodata = ds.nodata
        valid = data[data != nodata] if nodata is not None else data

        if crs and str(crs) != "EPSG:4326":
            try:
                t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
                min_lon, min_lat = t.transform(bounds.left, bounds.bottom)
                max_lon, max_lat = t.transform(bounds.right, bounds.top)
                bw = {"west": min_lon, "south": min_lat, "east": max_lon, "north": max_lat}
            except Exception:
                bw = {"west": bounds.left, "south": bounds.bottom, "east": bounds.right, "north": bounds.top}
        else:
            bw = {"west": bounds.left, "south": bounds.bottom, "east": bounds.right, "north": bounds.top}

        res = abs(ds.transform.a)
        if crs and not crs.is_geographic:
            res_cm = res * 100
        elif crs and crs.is_geographic:
            res_cm = res * 111320 * 100
        else:
            res_cm = 0

    return {
        "bounds": bw, "crs": str(crs) if crs else "EPSG:4326",
        "srid": crs.to_epsg() if crs else 4326,
        "width_px": ds.width, "height_px": ds.height,
        "min_elevation_m": float(np.nanmin(valid)),
        "max_elevation_m": float(np.nanmax(valid)),
        "mean_elevation_m": float(np.nanmean(valid)),
        "file_size_bytes": dsm_path.stat().st_size,
        "resolution_cm": round(res_cm, 2),
    }
