"""
CM TECHMAP — Temporal Comparison Service
Multi-epoch change detection: compares two sets of aerial data (orthomosaics/DSM)
to identify urban changes between time periods.

Use cases:
- Detecting new constructions
- Identifying demolished buildings
- Tracking urban expansion
- Monitoring vegetation loss
"""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("cm_techmap.temporal")


class ChangeType:
    """Types of urban changes detected."""
    NEW_CONSTRUCTION = "new_construction"
    DEMOLISHED = "demolished"
    HEIGHT_CHANGE = "height_change"
    VEGETATION_LOSS = "vegetation_loss"
    VEGETATION_GAIN = "vegetation_gain"
    TERRAIN_CHANGE = "terrain_change"


class TemporalComparisonService:
    """
    Compares two epochs of geospatial data to detect and classify urban changes.
    """

    @staticmethod
    def compare_dsm_epochs(
        dsm_epoch1_path: str,
        dsm_epoch2_path: str,
        *,
        change_threshold_m: float = 2.0,
        cell_size_m: float = 1.0,
    ) -> dict[str, Any]:
        """
        Compare two DSM rasters to detect elevation changes.
        
        Args:
            dsm_epoch1_path: Earlier DSM raster
            dsm_epoch2_path: Later DSM raster
            change_threshold_m: Minimum height change to register
            cell_size_m: Raster resolution
            
        Returns:
            Change analysis with classified zones
        """
        import rasterio

        logger.info("[TEMPORAL] Comparing DSM epochs")

        with rasterio.open(dsm_epoch1_path) as src1:
            dsm1 = src1.read(1).astype(np.float64)
            transform = src1.transform
            nodata1 = src1.nodata

        with rasterio.open(dsm_epoch2_path) as src2:
            dsm2 = src2.read(1).astype(np.float64)
            nodata2 = src2.nodata

        # Ensure same dimensions
        min_h = min(dsm1.shape[0], dsm2.shape[0])
        min_w = min(dsm1.shape[1], dsm2.shape[1])
        dsm1 = dsm1[:min_h, :min_w]
        dsm2 = dsm2[:min_h, :min_w]

        # Mask nodata
        valid_mask = np.ones_like(dsm1, dtype=bool)
        if nodata1 is not None:
            valid_mask &= dsm1 != nodata1
        if nodata2 is not None:
            valid_mask &= dsm2 != nodata2

        # Compute difference
        diff = np.where(valid_mask, dsm2 - dsm1, np.nan)

        # Classify changes
        new_construction_mask = diff > change_threshold_m  # Building went up
        demolished_mask = diff < -change_threshold_m        # Building removed
        no_change_mask = np.abs(diff) <= change_threshold_m

        from scipy import ndimage

        # Label connected regions of change
        changes = []

        # New constructions
        labeled_new, n_new = ndimage.label(new_construction_mask & valid_mask)
        for label_id in range(1, min(n_new + 1, 200)):
            component = labeled_new == label_id
            area_px = int(np.sum(component))
            if area_px < 4:  # Minimum 4 pixels
                continue

            height_gain = float(np.mean(diff[component]))
            rows, cols = np.where(component)
            lon = transform.c + int(np.mean(cols)) * transform.a
            lat = transform.f + int(np.mean(rows)) * transform.e

            changes.append({
                "change_type": ChangeType.NEW_CONSTRUCTION,
                "area_sqm": round(area_px * cell_size_m**2, 2),
                "height_change_m": round(height_gain, 2),
                "center_lon": round(lon, 8),
                "center_lat": round(lat, 8),
                "confidence": min(0.95, round(0.5 + height_gain / 20, 2)),
            })

        # Demolitions
        labeled_demo, n_demo = ndimage.label(demolished_mask & valid_mask)
        for label_id in range(1, min(n_demo + 1, 200)):
            component = labeled_demo == label_id
            area_px = int(np.sum(component))
            if area_px < 4:
                continue

            height_loss = float(np.mean(diff[component]))
            rows, cols = np.where(component)
            lon = transform.c + int(np.mean(cols)) * transform.a
            lat = transform.f + int(np.mean(rows)) * transform.e

            changes.append({
                "change_type": ChangeType.DEMOLISHED,
                "area_sqm": round(area_px * cell_size_m**2, 2),
                "height_change_m": round(height_loss, 2),
                "center_lon": round(lon, 8),
                "center_lat": round(lat, 8),
                "confidence": min(0.95, round(0.5 + abs(height_loss) / 20, 2)),
            })

        total_valid = int(np.sum(valid_mask))
        stats = {
            "total_area_sqm": int(total_valid * cell_size_m**2),
            "new_construction_count": len([c for c in changes if c["change_type"] == ChangeType.NEW_CONSTRUCTION]),
            "demolished_count": len([c for c in changes if c["change_type"] == ChangeType.DEMOLISHED]),
            "new_construction_area_sqm": round(float(np.sum(new_construction_mask & valid_mask) * cell_size_m**2), 2),
            "demolished_area_sqm": round(float(np.sum(demolished_mask & valid_mask) * cell_size_m**2), 2),
            "unchanged_pct": round(float(np.sum(no_change_mask & valid_mask) / max(total_valid, 1) * 100), 2),
            "elevation_diff_stats": {
                "mean_m": round(float(np.nanmean(diff)), 3),
                "std_m": round(float(np.nanstd(diff)), 3),
                "max_gain_m": round(float(np.nanmax(diff)), 2),
                "max_loss_m": round(float(np.nanmin(diff)), 2),
            },
        }

        logger.info(
            f"[TEMPORAL] Found {stats['new_construction_count']} new constructions, "
            f"{stats['demolished_count']} demolitions"
        )

        return {
            "analysis_type": "temporal_dsm_comparison",
            "stats": stats,
            "changes": sorted(changes, key=lambda c: c["area_sqm"], reverse=True),
        }

    @staticmethod
    def compare_vegetation(
        ortho_epoch1_path: str,
        ortho_epoch2_path: str,
        *,
        ndvi_threshold: float = 0.2,
        change_threshold: float = 0.15,
        cell_size_m: float = 0.1,
    ) -> dict[str, Any]:
        """
        Compare vegetation (NDVI) between two epochs.
        Detects deforestation, new vegetation, and vegetation health changes.
        """
        import rasterio

        logger.info("[TEMPORAL] Comparing vegetation between epochs")

        def compute_ndvi(path):
            with rasterio.open(path) as src:
                if src.count >= 4:
                    red = src.read(1).astype(np.float64)
                    nir = src.read(4).astype(np.float64)
                else:
                    red = src.read(1).astype(np.float64)
                    green = src.read(2).astype(np.float64)
                    # Approximate NDVI from RGB
                    nir = green  # Proxy
                return np.where((nir + red) > 0, (nir - red) / (nir + red), 0)

        ndvi1 = compute_ndvi(ortho_epoch1_path)
        ndvi2 = compute_ndvi(ortho_epoch2_path)

        # Ensure same dims
        min_h = min(ndvi1.shape[0], ndvi2.shape[0])
        min_w = min(ndvi1.shape[1], ndvi2.shape[1])
        ndvi1 = ndvi1[:min_h, :min_w]
        ndvi2 = ndvi2[:min_h, :min_w]

        ndvi_diff = ndvi2 - ndvi1
        total_px = ndvi_diff.size

        veg_loss = ndvi_diff < -change_threshold
        veg_gain = ndvi_diff > change_threshold

        stats = {
            "vegetation_loss_pct": round(float(np.sum(veg_loss) / total_px * 100), 3),
            "vegetation_gain_pct": round(float(np.sum(veg_gain) / total_px * 100), 3),
            "vegetation_loss_area_sqm": round(float(np.sum(veg_loss) * cell_size_m**2), 2),
            "vegetation_gain_area_sqm": round(float(np.sum(veg_gain) * cell_size_m**2), 2),
            "mean_ndvi_epoch1": round(float(np.mean(ndvi1)), 4),
            "mean_ndvi_epoch2": round(float(np.mean(ndvi2)), 4),
            "ndvi_trend": "improving" if np.mean(ndvi2) > np.mean(ndvi1) else "declining",
        }

        return {
            "analysis_type": "temporal_vegetation_comparison",
            "stats": stats,
        }
