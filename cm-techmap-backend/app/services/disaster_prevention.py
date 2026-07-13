"""
CM TECHMAP — Disaster Prevention Service
Analyzes DSM (Digital Surface Model) data to identify flood and landslide risk zones.

Produces risk maps based on:
- Slope analysis (landslide risk)
- Low-elevation zones relative to water bodies (flood risk)
- Terrain drainage analysis
"""

import json
import logging
from typing import Any

import numpy as np

logger = logging.getLogger("cm_techmap.disaster_prevention")


class RiskLevel:
    """Risk classification levels."""
    VERY_HIGH = "very_high"
    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
    MINIMAL = "minimal"


class DisasterPreventionService:
    """
    Analyzes terrain data to produce risk assessments for natural disasters.
    Works with DSM/DTM rasters to identify vulnerable areas.
    """

    @staticmethod
    def analyze_slope_risk(
        dsm_path: str,
        *,
        cell_size_m: float = 1.0,
        landslide_threshold_deg: float = 30.0,
        high_risk_threshold_deg: float = 45.0,
    ) -> dict[str, Any]:
        """
        Compute slope-based landslide risk from a DSM raster.
        
        Args:
            dsm_path: Path to DSM GeoTIFF
            cell_size_m: Resolution of the raster in meters
            landslide_threshold_deg: Slope angle above which landslide risk begins
            high_risk_threshold_deg: Slope angle for very high risk
            
        Returns:
            Risk analysis results with zone counts and GeoJSON features
        """
        import rasterio
        from rasterio.transform import rowcol

        logger.info(f"[DISASTER] Analyzing slope risk from {dsm_path}")

        with rasterio.open(dsm_path) as src:
            elevation = src.read(1).astype(np.float64)
            transform = src.transform
            crs = src.crs
            nodata = src.nodata

        # Mask nodata
        if nodata is not None:
            elevation = np.where(elevation == nodata, np.nan, elevation)

        # Compute slope (degrees) using gradient
        dy, dx = np.gradient(elevation, cell_size_m)
        slope_rad = np.arctan(np.sqrt(dx**2 + dy**2))
        slope_deg = np.degrees(slope_rad)

        # Classify risk zones
        risk_map = np.full_like(slope_deg, 0, dtype=np.int8)
        risk_map[slope_deg >= landslide_threshold_deg] = 2  # moderate
        risk_map[slope_deg >= high_risk_threshold_deg] = 3   # high
        risk_map[slope_deg >= 60] = 4                        # very high

        # Statistics
        total_cells = np.count_nonzero(~np.isnan(slope_deg))
        stats = {
            "total_area_cells": int(total_cells),
            "total_area_sqm": int(total_cells * cell_size_m**2),
            "slope_stats": {
                "mean_deg": round(float(np.nanmean(slope_deg)), 2),
                "max_deg": round(float(np.nanmax(slope_deg)), 2),
                "std_deg": round(float(np.nanstd(slope_deg)), 2),
            },
            "risk_zones": {
                RiskLevel.MINIMAL: int(np.sum(risk_map == 0)),
                RiskLevel.LOW: int(np.sum(risk_map == 1)),
                RiskLevel.MODERATE: int(np.sum(risk_map == 2)),
                RiskLevel.HIGH: int(np.sum(risk_map == 3)),
                RiskLevel.VERY_HIGH: int(np.sum(risk_map == 4)),
            },
            "high_risk_area_sqm": int(np.sum(risk_map >= 3) * cell_size_m**2),
            "high_risk_pct": round(float(np.sum(risk_map >= 3) / max(total_cells, 1) * 100), 2),
        }

        logger.info(
            f"[DISASTER] Slope analysis: {stats['high_risk_pct']}% high risk, "
            f"max slope: {stats['slope_stats']['max_deg']}°"
        )

        return {
            "analysis_type": "landslide_slope",
            "stats": stats,
        }

    @staticmethod
    def analyze_flood_risk(
        dsm_path: str,
        *,
        base_flood_elevation: float | None = None,
        flood_depth_thresholds: list[float] | None = None,
        cell_size_m: float = 1.0,
    ) -> dict[str, Any]:
        """
        Compute flood risk zones from DSM elevation data.
        
        Identifies areas below specified flood elevation thresholds.
        If no base flood elevation is provided, uses the lowest terrain
        + standard flood stage increments.
        """
        import rasterio

        logger.info(f"[DISASTER] Analyzing flood risk from {dsm_path}")

        with rasterio.open(dsm_path) as src:
            elevation = src.read(1).astype(np.float64)
            nodata = src.nodata

        if nodata is not None:
            elevation = np.where(elevation == nodata, np.nan, elevation)

        min_elev = float(np.nanmin(elevation))
        max_elev = float(np.nanmax(elevation))
        mean_elev = float(np.nanmean(elevation))

        # Define flood elevation if not provided
        if base_flood_elevation is None:
            base_flood_elevation = min_elev + 2.0  # 2m above lowest point

        if flood_depth_thresholds is None:
            flood_depth_thresholds = [0.5, 1.0, 2.0, 5.0]  # meters above base

        # Compute flood zones
        flood_zones = {}
        total_cells = np.count_nonzero(~np.isnan(elevation))

        for depth in flood_depth_thresholds:
            threshold = base_flood_elevation + depth
            flooded = np.sum((elevation <= threshold) & ~np.isnan(elevation))
            flood_zones[f"{depth}m"] = {
                "cells": int(flooded),
                "area_sqm": int(flooded * cell_size_m**2),
                "pct": round(float(flooded / max(total_cells, 1) * 100), 2),
                "flood_level_m": round(threshold, 2),
            }

        # Risk classification
        shallow_flood_pct = flood_zones.get("1.0m", {}).get("pct", 0)
        if shallow_flood_pct > 30:
            overall_risk = RiskLevel.VERY_HIGH
        elif shallow_flood_pct > 15:
            overall_risk = RiskLevel.HIGH
        elif shallow_flood_pct > 5:
            overall_risk = RiskLevel.MODERATE
        else:
            overall_risk = RiskLevel.LOW

        stats = {
            "elevation_stats": {
                "min_m": round(min_elev, 2),
                "max_m": round(max_elev, 2),
                "mean_m": round(mean_elev, 2),
                "range_m": round(max_elev - min_elev, 2),
            },
            "base_flood_elevation_m": round(base_flood_elevation, 2),
            "flood_zones": flood_zones,
            "overall_risk": overall_risk,
            "total_area_sqm": int(total_cells * cell_size_m**2),
        }

        logger.info(f"[DISASTER] Flood analysis: overall risk = {overall_risk}")

        return {
            "analysis_type": "flood_risk",
            "stats": stats,
        }

    @staticmethod
    def generate_combined_report(
        slope_analysis: dict | None = None,
        flood_analysis: dict | None = None,
        project_name: str = "",
        city: str = "",
    ) -> dict[str, Any]:
        """Combine slope and flood analyses into a unified risk report."""
        report = {
            "project": project_name,
            "city": city,
            "analyses": [],
            "recommendations": [],
        }

        if slope_analysis:
            report["analyses"].append(slope_analysis)
            slope_stats = slope_analysis.get("stats", {})
            high_risk_pct = slope_stats.get("high_risk_pct", 0)

            if high_risk_pct > 10:
                report["recommendations"].append({
                    "priority": "alta",
                    "category": "deslizamento",
                    "action": f"Área com {high_risk_pct}% de risco elevado de deslizamento. "
                              "Recomenda-se mapeamento geotécnico detalhado e plano de contenção.",
                })
            if high_risk_pct > 5:
                report["recommendations"].append({
                    "priority": "média",
                    "category": "deslizamento",
                    "action": "Instalação de sensores de monitoramento de solo nas encostas identificadas.",
                })

        if flood_analysis:
            report["analyses"].append(flood_analysis)
            flood_stats = flood_analysis.get("stats", {})
            overall_risk = flood_stats.get("overall_risk", "low")

            if overall_risk in (RiskLevel.HIGH, RiskLevel.VERY_HIGH):
                report["recommendations"].append({
                    "priority": "alta",
                    "category": "enchente",
                    "action": "Região com alto risco de inundação. "
                              "Recomenda-se revisão da drenagem urbana e plano de contingência.",
                })
            report["recommendations"].append({
                "priority": "média",
                "category": "enchente",
                "action": "Verificar obstruções nos canais de drenagem e bueiros na região mapeada.",
            })

        return report
