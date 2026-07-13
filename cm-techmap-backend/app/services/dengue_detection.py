"""
CM TECHMAP — Dengue Hotspot Detection Service
Identifies potential mosquito breeding sites by detecting standing water
and vegetation patterns from orthomosaics and DSM data.

Uses spectral analysis (NDVI, NDWI) combined with terrain depression detection
to locate stagnant water pools, open containers, and unmaintained lots.
"""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger("cm_techmap.dengue")


class DengueRiskCategory:
    """Risk categories for dengue hotspots."""
    STANDING_WATER = "standing_water"        # Pools, puddles, containers
    TERRAIN_DEPRESSION = "terrain_depression"  # Low spots that accumulate water
    ABANDONED_LOT = "abandoned_lot"           # Unmaintained vegetation
    OPEN_CONTAINER = "open_container"         # Tires, buckets, etc. (from AI)
    CONSTRUCTION_SITE = "construction_site"   # Active construction with exposed water


class DengueHotspotService:
    """
    Detects potential Aedes aegypti breeding sites from aerial imagery.
    
    Methodology:
    1. NDWI (Normalized Difference Water Index) → detect surface water
    2. Terrain depression analysis → find water accumulation zones
    3. NDVI anomaly detection → identify abandoned/unmaintained lots
    4. Cross-reference with parcel data → identify responsible parties
    """

    @staticmethod
    def detect_water_bodies(
        orthophoto_path: str,
        *,
        ndwi_threshold: float = 0.1,
        min_area_sqm: float = 0.5,
        cell_size_m: float = 0.1,
    ) -> dict[str, Any]:
        """
        Detect standing water using NDWI from multispectral imagery.
        
        NDWI = (Green - NIR) / (Green + NIR)
        For RGB-only imagery, uses a simplified water detection:
        NDWI_approx = (Green - Red) / (Green + Red)
        """
        import rasterio

        logger.info(f"[DENGUE] Detecting water bodies from {orthophoto_path}")

        with rasterio.open(orthophoto_path) as src:
            band_count = src.count
            transform = src.transform

            if band_count >= 4:
                # Multispectral: use proper NDWI
                green = src.read(2).astype(np.float64)
                nir = src.read(4).astype(np.float64)
                ndwi = np.where((green + nir) > 0, (green - nir) / (green + nir), 0)
            elif band_count >= 3:
                # RGB: simplified water index
                red = src.read(1).astype(np.float64)
                green = src.read(2).astype(np.float64)
                blue = src.read(3).astype(np.float64)
                # Water tends to be blue-ish with low red
                ndwi = np.where((green + red) > 0, (blue - red) / (blue + red + 1), 0)
            else:
                return {"error": "Insufficient bands for water detection"}

        # Threshold water pixels
        water_mask = ndwi > ndwi_threshold
        water_pixels = int(np.sum(water_mask))
        total_pixels = water_mask.size
        water_area_sqm = water_pixels * cell_size_m**2

        # Connected component analysis (simplified)
        from scipy import ndimage
        labeled, num_features = ndimage.label(water_mask)

        # Filter by minimum area
        hotspots = []
        min_area_px = min_area_sqm / (cell_size_m**2)

        for label_id in range(1, num_features + 1):
            component = labeled == label_id
            area_px = int(np.sum(component))
            if area_px < min_area_px:
                continue

            # Get centroid
            rows, cols = np.where(component)
            center_row = int(np.mean(rows))
            center_col = int(np.mean(cols))

            # Convert to geographic coordinates
            lon = transform.c + center_col * transform.a + center_row * transform.b
            lat = transform.f + center_col * transform.d + center_row * transform.e

            area_sqm = area_px * cell_size_m**2

            # Classify by size
            if area_sqm < 1:
                risk_level = "medium"
                category = DengueRiskCategory.OPEN_CONTAINER
            elif area_sqm < 10:
                risk_level = "high"
                category = DengueRiskCategory.STANDING_WATER
            else:
                risk_level = "critical"
                category = DengueRiskCategory.STANDING_WATER

            hotspots.append({
                "id": label_id,
                "category": category,
                "risk_level": risk_level,
                "area_sqm": round(area_sqm, 3),
                "center_lon": round(lon, 8),
                "center_lat": round(lat, 8),
                "ndwi_mean": round(float(np.mean(ndwi[component])), 4),
            })

        stats = {
            "total_water_pixels": water_pixels,
            "total_water_area_sqm": round(water_area_sqm, 2),
            "water_coverage_pct": round(water_pixels / max(total_pixels, 1) * 100, 4),
            "total_hotspots": len(hotspots),
            "hotspots_by_risk": {
                "critical": len([h for h in hotspots if h["risk_level"] == "critical"]),
                "high": len([h for h in hotspots if h["risk_level"] == "high"]),
                "medium": len([h for h in hotspots if h["risk_level"] == "medium"]),
            },
        }

        logger.info(f"[DENGUE] Detected {len(hotspots)} hotspots ({stats['total_water_area_sqm']} m² water)")

        return {
            "analysis_type": "dengue_water_detection",
            "stats": stats,
            "hotspots": hotspots,
        }

    @staticmethod
    def detect_terrain_depressions(
        dsm_path: str,
        *,
        depression_threshold_m: float = 0.3,
        min_area_sqm: float = 1.0,
        cell_size_m: float = 1.0,
    ) -> dict[str, Any]:
        """
        Detect terrain depressions that may accumulate rainwater.
        Uses fill-based depression analysis on DSM.
        """
        import rasterio
        from scipy import ndimage

        logger.info(f"[DENGUE] Detecting terrain depressions from {dsm_path}")

        with rasterio.open(dsm_path) as src:
            elevation = src.read(1).astype(np.float64)
            transform = src.transform
            nodata = src.nodata

        if nodata is not None:
            elevation = np.where(elevation == nodata, np.nan, elevation)

        # Simple fill: smooth and compare
        # Areas where smoothed elevation > original = depressions
        smoothed = ndimage.uniform_filter(elevation, size=5)
        depression_depth = smoothed - elevation
        depression_mask = depression_depth > depression_threshold_m

        # Label connected depressions
        labeled, num_features = ndimage.label(depression_mask & ~np.isnan(elevation))

        min_area_px = min_area_sqm / (cell_size_m**2)
        depressions = []

        for label_id in range(1, min(num_features + 1, 500)):  # Cap at 500
            component = labeled == label_id
            area_px = int(np.sum(component))
            if area_px < min_area_px:
                continue

            depth = float(np.max(depression_depth[component]))
            rows, cols = np.where(component)
            center_row = int(np.mean(rows))
            center_col = int(np.mean(cols))
            lon = transform.c + center_col * transform.a
            lat = transform.f + center_row * transform.e

            volume_m3 = float(np.sum(depression_depth[component])) * cell_size_m**2

            depressions.append({
                "id": label_id,
                "category": DengueRiskCategory.TERRAIN_DEPRESSION,
                "risk_level": "high" if volume_m3 > 5 else "medium",
                "area_sqm": round(area_px * cell_size_m**2, 2),
                "max_depth_m": round(depth, 3),
                "estimated_volume_m3": round(volume_m3, 3),
                "center_lon": round(lon, 8),
                "center_lat": round(lat, 8),
            })

        stats = {
            "total_depressions": len(depressions),
            "total_depression_volume_m3": round(sum(d["estimated_volume_m3"] for d in depressions), 2),
            "critical_depressions": len([d for d in depressions if d["risk_level"] == "high"]),
        }

        logger.info(f"[DENGUE] Detected {len(depressions)} terrain depressions")

        return {
            "analysis_type": "dengue_terrain_depressions",
            "stats": stats,
            "depressions": depressions,
        }

    @staticmethod
    def generate_dengue_report(
        water_analysis: dict | None = None,
        depression_analysis: dict | None = None,
        project_name: str = "",
        city: str = "",
    ) -> dict[str, Any]:
        """Generate combined dengue risk report with municipal action items."""
        all_hotspots = []
        if water_analysis:
            all_hotspots.extend(water_analysis.get("hotspots", []))
        if depression_analysis:
            all_hotspots.extend(depression_analysis.get("depressions", []))

        # Sort by risk level
        risk_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        all_hotspots.sort(key=lambda h: risk_order.get(h.get("risk_level", "low"), 3))

        report = {
            "project": project_name,
            "city": city,
            "total_hotspots": len(all_hotspots),
            "critical_count": len([h for h in all_hotspots if h["risk_level"] == "critical"]),
            "high_count": len([h for h in all_hotspots if h["risk_level"] == "high"]),
            "hotspots": all_hotspots[:100],  # Top 100 for report
            "recommendations": [
                {
                    "priority": "urgente",
                    "action": f"Vistoria imediata nos {len([h for h in all_hotspots if h['risk_level'] == 'critical'])} "
                              "pontos críticos de acúmulo de água detectados.",
                },
                {
                    "priority": "alta",
                    "action": "Notificar proprietários de lotes com depressões de terreno "
                              "que acumulam água parada.",
                },
                {
                    "priority": "média",
                    "action": "Incluir os pontos detectados no roteiro de nebulização "
                              "e controle vetorial do município.",
                },
                {
                    "priority": "preventiva",
                    "action": "Programar nova aquisição de imagens aéreas em 30 dias "
                              "para monitorar evolução dos pontos identificados.",
                },
            ],
        }

        return report
