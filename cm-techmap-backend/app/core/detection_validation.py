"""
CM TECHMAP — Validação geométrica de detecções por evidência de elevação

A rede neural olha COR (RGB); a fotogrametria mede FORMA (DSM/DTM). Este
módulo cruza os dois sensores independentes para cada polígono detectado e
produz medições com incerteza declarada — a "verdade dita pela IA" que dá
lastro à análise fiscal sem depender de anotação humana:

  height_measured_m   média de nDSM = DSM − DTM dentro do polígono
                      (altura fotogramétrica acima do solo, não estimativa)
  height_std_m        desvio-padrão do nDSM no polígono (incerteza vertical)
  volume_m3           Σ (nDSM × área do pixel) — volume construído medido
  area_uncertainty_m2 perímetro × GSD: faixa de 1 pixel na borda da máscara
                      (o erro de contorno honesto da segmentação)
  planarity           1/(1+var(∇²nDSM)): telhado é plano por partes (→1);
                      copa de árvore é rugosa (→0). Separa vegetação de laje.
  evidence_score      combinação [0..1] das evidências independentes
  validation_status   confirmed  — elevação E planaridade sustentam a rede
                      weak       — evidência parcial (revisão recomendada)
                      contradicted — o 3D nega a detecção (ex.: pintura no
                                     chão que a rede achou telhado)

Todos os cálculos são determinísticos e reproduzíveis a partir dos COGs
publicados — qualquer auditor com QGIS chega aos MESMOS números.
"""

import logging
import math

import numpy as np

logger = logging.getLogger(__name__)

# Limiares de evidência (metros) — documentados, não mágicos:
# edificação real eleva-se do solo; abaixo de MIN_ é indistinguível de piso.
HEIGHT_STRONG_M = 2.0
HEIGHT_WEAK_M = 0.8
PLANARITY_STRONG = 0.55


def validate_detections_against_elevation(
    dsm_path: str,
    dtm_path: str | None,
    detections: list[dict],
) -> list[dict]:
    """
    Para cada detecção {id, geometry(GeoJSON, EPSG:4326)}, mede as evidências
    de elevação no recorte do polígono e devolve as métricas + veredito.

    Sem DTM, usa o DSM já normalizado (pipeline publica dsm_normalized: nDSM
    direto) — detectado automaticamente pela faixa de valores.
    """
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    results: list[dict] = []

    with rasterio.open(dsm_path) as dsm:
        dsm_crs = dsm.crs
        px_area = abs(dsm.transform.a * dsm.transform.e)
        gsd_m = math.sqrt(px_area) if dsm_crs and dsm_crs.is_projected else None

        dtm_ds = None
        if dtm_path:
            dtm_ds = rasterio.open(dtm_path)

        # DSM "normalizado" (já é altura sobre o solo)? Heurística explícita:
        # cota mínima ~0 e máxima baixa ⇒ nDSM; cotas absolutas passam de 100 m.
        sample = dsm.read(1, out_shape=(min(dsm.height, 512), min(dsm.width, 512)), masked=True)
        is_normalized = float(np.nanmax(sample.filled(np.nan))) < 100.0 and \
            abs(float(np.nanmin(sample.filled(np.nan)))) < 5.0

        for det in detections:
            det_id = det["id"]
            try:
                geom4326 = det["geometry"]
                geom_native = transform_geom("EPSG:4326", dsm_crs.to_string(), geom4326)

                dsm_win, dsm_tr = rio_mask(dsm, [geom_native], crop=True, filled=False)
                nd = dsm_win[0].astype("float64")

                if dtm_ds is not None and not is_normalized:
                    dtm_win, _ = rio_mask(dtm_ds, [geom_native], crop=True, filled=False)
                    nd = nd - dtm_win[0].astype("float64")

                valid = nd.compressed() if np.ma.isMaskedArray(nd) else nd[np.isfinite(nd)]
                valid = valid[np.isfinite(valid)]
                if valid.size < 4:
                    results.append({"id": det_id, "validation_status": "no_data"})
                    continue

                height_measured = float(np.mean(valid))
                height_std = float(np.std(valid))
                volume = float(np.sum(np.clip(valid, 0, None)) * px_area)

                # Planaridade: laplaciano do recorte (bordas do prédio geram
                # gradiente — por isso o corte usa só o interior válido)
                filled = np.ma.filled(nd, np.nan) if np.ma.isMaskedArray(nd) else nd
                lap = np.abs(np.gradient(np.gradient(np.nan_to_num(filled, nan=height_measured), axis=0), axis=0)) + \
                    np.abs(np.gradient(np.gradient(np.nan_to_num(filled, nan=height_measured), axis=1), axis=1))
                lap_vals = lap[np.isfinite(filled)]
                planarity = float(1.0 / (1.0 + np.var(lap_vals))) if lap_vals.size else 0.0

                # Incerteza de área: 1 pixel de banda ao longo do perímetro
                poly = shape(geom_native) if dsm_crs.is_projected else None
                if poly is not None and gsd_m:
                    perimeter_m = float(poly.length)
                    area_uncertainty = round(perimeter_m * gsd_m, 2)
                else:
                    # CRS geográfico: aproximação metrópica do perímetro
                    poly4326 = shape(geom4326)
                    lat = poly4326.centroid.y
                    perimeter_m = float(poly4326.length) * 111_320 * math.cos(math.radians(lat))
                    area_uncertainty = round(perimeter_m * (gsd_m or 0.05), 2)

                # Evidências independentes → veredito
                h_evid = 1.0 if height_measured >= HEIGHT_STRONG_M else (
                    (height_measured - HEIGHT_WEAK_M) / (HEIGHT_STRONG_M - HEIGHT_WEAK_M)
                    if height_measured > HEIGHT_WEAK_M else 0.0
                )
                p_evid = min(1.0, planarity / PLANARITY_STRONG)
                evidence = round(0.6 * h_evid + 0.4 * p_evid, 3)

                if h_evid >= 1.0 and p_evid >= 0.6:
                    status = "confirmed"
                elif evidence >= 0.35:
                    status = "weak"
                else:
                    status = "contradicted"

                results.append({
                    "id": det_id,
                    "height_measured_m": round(height_measured, 3),
                    "height_std_m": round(height_std, 3),
                    "volume_m3": round(volume, 2),
                    "area_uncertainty_m2": area_uncertainty,
                    "planarity": round(planarity, 3),
                    "evidence_score": evidence,
                    "validation_status": status,
                })
            except Exception as e:
                logger.warning(f"[VALIDATE] detecção {det_id}: {e}")
                results.append({"id": det_id, "validation_status": "error"})

        if dtm_ds is not None:
            dtm_ds.close()

    confirmed = sum(1 for r in results if r.get("validation_status") == "confirmed")
    contradicted = sum(1 for r in results if r.get("validation_status") == "contradicted")
    logger.info(
        f"[VALIDATE] {len(results)} detecções: {confirmed} confirmadas pela "
        f"elevação, {contradicted} contraditadas"
    )
    return results
