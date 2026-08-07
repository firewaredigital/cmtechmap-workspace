"""
CM TECHMAP — Detecção de estruturas pela GEOMETRIA (nDSM + espectro)

Por que existir: a rede neural olha cor e textura e, treinada em outra
geografia, simplesmente NÃO DISPARA em certos telhados. A auditoria de
cobertura provou o custo disso no voo real — 18 lotes com estrutura de até
18,7 m de altura e 400 m² sem nenhuma detecção. Varrer a imagem com mais
zelo não conserta um modelo que não reconhece o objeto.

A elevação, porém, não depende de reconhecimento: uma construção EXISTE
como volume acima do solo. Este módulo detecta estruturas a partir do nDSM
e usa o espectro apenas para separar o que é telhado do que é vegetação:

  altura      nDSM ≥ min_height (padrão 1,5 m — acima de muro e carro)
  rugosidade  desvio local da altura: telhado é liso, copa é rugosa
  verde       ExG = 2G − R − B: vegetação é verde, telhado normalmente não

O resultado entra em FUSÃO com as detecções da rede: onde as duas
concordam, a confiança é máxima; onde só uma vê, a origem fica registrada
em `detection_source` para o operador saber de onde veio cada polígono.
"""

import json
import logging
import os

import numpy as np

logger = logging.getLogger(__name__)


def detect_structures_from_elevation(
    dsm_path,
    output_path,
    orthophoto_path=None,
    min_height_m: float = 1.5,
    min_area_m2: float = 3.0,
    roughness_max: float = 1.2,
    green_excess_max: float = 0.16,
    split_merged: bool = True,
):
    """
    Extrai polígonos de TODA estrutura elevada e não-vegetada do nDSM.

    Escreve GeoJSON no mesmo contrato do extrator neural (area_m2, height,
    confidence, building_type) somando as evidências geométricas medidas:
    altura média/máxima, volume, rugosidade e índice de verde.
    """
    import cv2
    import rasterio
    from rasterio import features as rio_features
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping, shape

    with rasterio.open(str(dsm_path)) as d:
        nd = d.read(1, masked=True).astype(np.float32)
        transform = d.transform
        crs = d.crs
        res_x, res_y = abs(transform.a), abs(transform.e)
        H, W = nd.shape

    px_area_m2 = (
        res_x * res_y
        if crs and crs.is_projected
        else (res_x * 111_320 * float(np.cos(np.radians(abs(transform.f))))) * (res_y * 110_540)
    )
    height = np.ma.filled(nd, 0.0)
    height[~np.isfinite(height)] = 0.0

    # ── 1. Massa elevada ────────────────────────────────────────────────
    mask = (height >= min_height_m).astype(np.uint8)
    logger.info(f"[3D] {mask.mean()*100:.2f}% dos pixels acima de {min_height_m} m")

    # ── 2. Rugosidade local: telhado é liso, copa de árvore é rugosa ────
    k = 5
    mean_local = cv2.blur(height, (k, k))
    sq_local = cv2.blur(height * height, (k, k))
    roughness = np.sqrt(np.maximum(sq_local - mean_local * mean_local, 0.0))
    smooth = (roughness <= roughness_max).astype(np.uint8)

    # ── 3. Verde excessivo: separa vegetação por espectro, quando há RGB ─
    green = np.zeros_like(mask)
    if orthophoto_path and os.path.exists(str(orthophoto_path)):
        with rasterio.open(str(orthophoto_path)) as o:
            rgb = o.read([1, 2, 3], out_shape=(3, H, W)).astype(np.float32)
        total = np.maximum(rgb.sum(axis=0), 1e-6)
        r, g, b = rgb[0] / total, rgb[1] / total, rgb[2] / total
        exg = 2 * g - r - b
        green = (exg > green_excess_max).astype(np.uint8)
        logger.info(f"[3D] {green.mean()*100:.1f}% dos pixels classificados como vegetação (ExG)")

    structure = ((mask == 1) & (smooth == 1) & (green == 0)).astype(np.uint8)

    # ── 4. Limpeza que preserva o pequeno ───────────────────────────────
    structure = cv2.morphologyEx(structure, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8), iterations=2)
    structure = cv2.morphologyEx(structure, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    # ── 5. Separar construções coladas ──────────────────────────────────
    if split_merged and structure.any():
        dist = cv2.distanceTransform(structure, cv2.DIST_L2, 3)
        if dist.max() > 0:
            _, cores = cv2.threshold(dist, 0.5 * dist.max(), 255, cv2.THRESH_BINARY)
            cores = cores.astype(np.uint8)
            n, markers = cv2.connectedComponents(cores)
            if n > 2:
                unknown = cv2.subtract(structure, cores)
                markers = markers + 1
                markers[unknown == 1] = 0
                markers = cv2.watershed(
                    cv2.cvtColor(structure * 255, cv2.COLOR_GRAY2BGR), markers.astype(np.int32)
                )
                structure[markers == -1] = 0
                logger.info(f"[3D] watershed separou até {n - 1} estruturas coladas")

    # ── 6. Polígonos com as medições que a geometria fornece ────────────
    min_px = max(2, int(min_area_m2 / max(px_area_m2, 1e-9)))
    features, discarded = [], 0
    for geom, val in rio_features.shapes(structure, mask=structure.astype(bool), transform=transform):
        if val != 1:
            continue
        poly = shape(geom)
        if not poly.is_valid:
            poly = poly.buffer(0)
        px_count = poly.area / (res_x * res_y)
        if px_count < min_px:
            discarded += 1
            continue

        minx, miny, maxx, maxy = poly.bounds
        c0 = max(0, int((minx - transform.c) / transform.a))
        r0 = max(0, int((maxy - transform.f) / transform.e))
        c1 = min(W, max(c0 + 1, int((maxx - transform.c) / transform.a)))
        r1 = min(H, max(r0 + 1, int((miny - transform.f) / transform.e)))
        wmask = structure[r0:r1, c0:c1].astype(bool)
        hwin = height[r0:r1, c0:c1]
        hvals = hwin[wmask] if wmask.any() else np.array([0.0])

        area_m2 = px_count * px_area_m2
        features.append({
            "type": "Feature",
            "geometry": (
                transform_geom(crs.to_string(), "EPSG:4326", mapping(poly.simplify(res_x * 1.2, preserve_topology=True)))
                if crs and crs.is_projected
                else mapping(poly.simplify(res_x * 1.2, preserve_topology=True))
            ),
            "properties": {
                "area_m2": round(area_m2, 2),
                "height": round(float(np.mean(hvals)), 2),
                "height_max_m": round(float(np.max(hvals)), 2),
                "volume_m3": round(float(np.sum(np.clip(hvals, 0, None)) * px_area_m2), 2),
                "roughness_m": round(float(np.mean(roughness[r0:r1, c0:c1][wmask])) if wmask.any() else 0.0, 3),
                # Confiança geométrica: altura acima do mínimo satura em 6 m.
                # Não é probabilidade de rede — é o quanto o volume se impõe.
                "confidence": round(min(0.99, 0.55 + 0.45 * min(1.0, float(np.mean(hvals)) / 6.0)), 3),
                "building_type": "elevation_3d",
                "detection_source": "elevation",
            },
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "source": "CM-TECHMAP 3D structure detection (nDSM + ExG)",
            "min_height_m": min_height_m,
            "min_area_m2": min_area_m2,
            "total_structures": len(features),
            "discarded_below_min_area": discarded,
        },
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    logger.info(f"[3D] {len(features)} estruturas detectadas pela geometria → {output_path}")
    return output_path


def fuse_detections(neural_features: list, elevation_features: list, iou_threshold: float = 0.30) -> list:
    """
    FUSÃO das duas evidências independentes.

    O que as duas veem vira `both` (evidência máxima); o que só a rede vê
    mantém `neural`; o que só a geometria vê entra como `elevation` — e é
    justamente esse conjunto que antes SUMIA do mapa. A origem fica gravada
    em cada polígono: o operador nunca precisa adivinhar de onde veio.
    """
    from shapely.geometry import shape

    fused = []
    el_shapes = [(i, shape(f["geometry"])) for i, f in enumerate(elevation_features)]
    matched_el: set[int] = set()

    for nf in neural_features:
        ns = shape(nf["geometry"])
        best_i, best_iou = None, 0.0
        for i, es in el_shapes:
            if i in matched_el or not ns.intersects(es):
                continue
            inter = ns.intersection(es).area
            union = ns.union(es).area
            iou = inter / union if union > 0 else 0.0
            if iou > best_iou:
                best_i, best_iou = i, iou
        props = dict(nf.get("properties") or {})
        if best_i is not None and best_iou >= iou_threshold:
            matched_el.add(best_i)
            ep = elevation_features[best_i]["properties"]
            # Geometria manda na altura/volume (é medição); a rede contribui
            # com a confiança semântica. Concordância eleva a confiança.
            props.update({
                "height": ep.get("height", props.get("height")),
                "height_max_m": ep.get("height_max_m"),
                "volume_m3": ep.get("volume_m3"),
                "roughness_m": ep.get("roughness_m"),
                "detection_source": "both",
                "iou_agreement": round(best_iou, 3),
                "confidence": round(min(0.99, (props.get("confidence", 0.6) + ep.get("confidence", 0.8)) / 2 + 0.15), 3),
            })
        else:
            props["detection_source"] = "neural"
        fused.append({"type": "Feature", "geometry": nf["geometry"], "properties": props})

    for i, ef in enumerate(elevation_features):
        if i not in matched_el:
            fused.append(ef)

    both = sum(1 for f in fused if f["properties"].get("detection_source") == "both")
    only_n = sum(1 for f in fused if f["properties"].get("detection_source") == "neural")
    only_e = sum(1 for f in fused if f["properties"].get("detection_source") == "elevation")
    logger.info(
        f"[FUSÃO] {len(fused)} estruturas: {both} confirmadas pelos DOIS sensores, "
        f"{only_n} só pela rede, {only_e} só pela geometria (as que sumiam do mapa)"
    )
    return fused
