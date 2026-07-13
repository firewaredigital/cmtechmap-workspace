"""
CM TECHMAP — COG Converter & Geospatial Metadata Extractor
Converts GeoTIFFs to Cloud Optimized GeoTIFF and extracts spatial metadata.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def convert_to_cog(
    input_path: str | Path,
    output_path: str | Path | None = None,
    blocksize: int = 512,
    overview_resampling: str = "nearest",
) -> Path:
    """
    Convert a GeoTIFF to Cloud Optimized GeoTIFF using rio-cogeo.

    Args:
        input_path: Path to the input GeoTIFF
        output_path: Path for the output COG (defaults to input_path with _cog suffix)
        blocksize: COG tile size (256 or 512)
        overview_resampling: Resampling method for overviews

    Returns:
        Path to the generated COG file
    """
    input_path = Path(input_path)
    if output_path is None:
        output_path = input_path.parent / f"{input_path.stem}_cog{input_path.suffix}"
    output_path = Path(output_path)

    logger.info(f"[COG] Converting {input_path.name} → {output_path.name}")

    cmd = [
        "rio", "cogeo", "create",
        str(input_path),
        str(output_path),
        "--blocksize", str(blocksize),
        "--overview-resampling", overview_resampling,
        "--overview-level", "6",
        "--co", "COMPRESS=DEFLATE",
        "--co", "PREDICTOR=2",
        "--co", f"BLOCKXSIZE={blocksize}",
        "--co", f"BLOCKYSIZE={blocksize}",
        "--quiet",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(f"[COG] Conversion failed: {result.stderr}")
            raise RuntimeError(f"COG conversion failed: {result.stderr}")

        file_size = output_path.stat().st_size
        logger.info(
            f"[COG] Created {output_path.name} "
            f"({file_size / 1024 / 1024:.1f} MB)"
        )
        return output_path
    except subprocess.TimeoutExpired:
        raise RuntimeError("COG conversion timed out after 600s")


def validate_cog(file_path: str | Path) -> bool:
    """Validate that a file is a valid Cloud Optimized GeoTIFF."""
    cmd = ["rio", "cogeo", "validate", str(file_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        is_valid = "is a valid cloud optimized" in result.stdout.lower()
        if not is_valid:
            logger.warning(f"[COG] {file_path} is NOT a valid COG: {result.stdout}")
        return is_valid
    except Exception as e:
        logger.error(f"[COG] Validation failed: {e}")
        return False


def extract_geospatial_metadata(file_path: str | Path) -> dict[str, Any]:
    """
    Extract geospatial metadata from a GeoTIFF using rasterio.

    Returns:
        dict with keys: bounds, center, crs, resolution_cm, width, height,
                        file_size_bytes, band_count, dtype
    """
    import rasterio
    from pyproj import Transformer

    file_path = Path(file_path)

    with rasterio.open(file_path) as ds:
        bounds = ds.bounds
        crs = ds.crs
        transform = ds.transform
        width = ds.width
        height = ds.height
        band_count = ds.count
        dtype = str(ds.dtypes[0])

        # Calculate resolution in cm
        res_x = abs(transform.a)
        res_y = abs(transform.e)

        # Convert bounds to WGS84 (EPSG:4326) if needed
        if crs and str(crs) != "EPSG:4326":
            try:
                transformer = Transformer.from_crs(
                    crs, "EPSG:4326", always_xy=True
                )
                min_lon, min_lat = transformer.transform(bounds.left, bounds.bottom)
                max_lon, max_lat = transformer.transform(bounds.right, bounds.top)
                bounds_wgs84 = {
                    "west": min_lon, "south": min_lat,
                    "east": max_lon, "north": max_lat,
                }
            except Exception:
                bounds_wgs84 = {
                    "west": bounds.left, "south": bounds.bottom,
                    "east": bounds.right, "north": bounds.top,
                }
        else:
            bounds_wgs84 = {
                "west": bounds.left, "south": bounds.bottom,
                "east": bounds.right, "north": bounds.top,
            }

        center_lon = (bounds_wgs84["west"] + bounds_wgs84["east"]) / 2
        center_lat = (bounds_wgs84["south"] + bounds_wgs84["north"]) / 2

        # Estimate resolution in cm (approximate for geographic CRS)
        if crs and crs.is_geographic:
            # ~111,320 m per degree at equator
            resolution_cm = res_x * 111320 * 100
        else:
            resolution_cm = res_x * 100

    metadata = {
        "bounds": bounds_wgs84,
        "center": {"lon": center_lon, "lat": center_lat},
        "crs": str(crs) if crs else "EPSG:4326",
        "srid": crs.to_epsg() if crs else 4326,
        "resolution_cm": round(resolution_cm, 2),
        "width_px": width,
        "height_px": height,
        "band_count": band_count,
        "dtype": dtype,
        "file_size_bytes": file_path.stat().st_size,
    }

    logger.info(
        f"[META] {file_path.name}: "
        f"{width}x{height}px, {resolution_cm:.1f}cm/px, "
        f"CRS={crs}, "
        f"bounds=[{bounds_wgs84['west']:.6f}, {bounds_wgs84['south']:.6f}, "
        f"{bounds_wgs84['east']:.6f}, {bounds_wgs84['north']:.6f}]"
    )
    return metadata


def extract_elevation_stats(file_path: str | Path) -> dict[str, float]:
    """Extract min/max elevation from a DSM/DTM GeoTIFF."""
    import rasterio
    import numpy as np

    with rasterio.open(file_path) as ds:
        data = ds.read(1)
        # Filter out nodata
        nodata = ds.nodata
        if nodata is not None:
            data = data[data != nodata]

        return {
            "min_elevation_m": float(np.nanmin(data)),
            "max_elevation_m": float(np.nanmax(data)),
            "mean_elevation_m": float(np.nanmean(data)),
        }
