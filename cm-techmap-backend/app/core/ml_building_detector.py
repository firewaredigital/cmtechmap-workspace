"""
CM TECHMAP — Detector de edificações por rede neural (ONNX, CPU)

Substitui a heurística de cor+altura por um modelo de segmentação treinado
(geobase/building-footprint-segmentation — U-Net exportada em ONNX
quantizado, 16 MB). Contrato do modelo, descoberto empiricamente:
entrada (N, 256, 256, 3) canais-por-último em [0,1]; saída (N, 256, 256, 1)
probabilidade sigmoide.

Validação empírica na ortofoto real de teste (27/07/2026): 1,4% dos pixels
marcados como edificação, concentrados nos tiles com estruturas — contra
55,4% da heurística antiga, que inviabilizava a malha fina por excesso de
falsos positivos.

Os pesos NÃO são redistribuídos no instalador (licença do fine-tune não
declarada): são baixados do Hugging Face na primeira execução, uma vez,
para um volume persistente.
"""

import json
import logging
import os
import urllib.request
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

TILE = 256
DEFAULT_MODEL_URL = (
    "https://huggingface.co/geobase/building-footprint-segmentation"
    "/resolve/main/onnx/model_quantized.onnx"
)


def ensure_model(model_path: str, model_url: str | None = None) -> str:
    """Baixa o modelo uma única vez para o caminho persistente."""
    path = Path(model_path)
    if path.exists() and path.stat().st_size > 1_000_000:
        return str(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".w"
        probe.touch()
        probe.unlink()
    except (PermissionError, OSError):
        # Volume sem escrita para o usuário do container: usar cache efêmero
        # (baixa de novo a cada restart — 16 MB, aceitável e logado).
        fallback = Path("/tmp/cm-ai-models") / path.name
        logger.warning(f"[ML] {path.parent} sem escrita — usando {fallback}")
        path = fallback
        if path.exists() and path.stat().st_size > 1_000_000:
            return str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
    url = model_url or DEFAULT_MODEL_URL
    logger.info(f"[ML] Baixando modelo de segmentação (~16 MB): {url}")
    tmp = path.with_suffix(".part")
    urllib.request.urlretrieve(url, tmp)  # noqa: S310
    tmp.rename(path)
    logger.info(f"[ML] Modelo salvo em {path} ({path.stat().st_size/1e6:.1f} MB)")
    return str(path)


def extract_buildings_ml(
    orthophoto_path,
    output_path,
    dsm_path=None,
    base_elevation: float = 0.0,
    threshold: float = 0.5,
    min_area_m2: float = 20.0,
    model_path: str = "/models/building_segmentation.onnx",
    model_url: str | None = None,
    max_dim: int = 8192,
):
    """
    Segmenta edificações da ortofoto com a rede neural e escreve um GeoJSON
    no MESMO contrato do extrator heurístico (properties: height, area_m2,
    confidence, building_type) — o restante do pipeline não muda.
    """
    import cv2
    import onnxruntime as ort
    import rasterio
    from rasterio import features as rio_features
    from shapely.geometry import mapping, shape

    sess = ort.InferenceSession(
        ensure_model(model_path, model_url), providers=["CPUExecutionProvider"]
    )
    inp = sess.get_inputs()[0].name

    with rasterio.open(str(orthophoto_path)) as src:
        src_h, src_w = src.height, src.width
        scale = 1.0
        if max(src_h, src_w) > max_dim:
            scale = max_dim / max(src_h, src_w)
            out_h, out_w = int(src_h * scale), int(src_w * scale)
            logger.info(f"[ML] Reamostrando {src_w}x{src_h} → {out_w}x{out_h} para caber na memória")
            img_full = src.read([1, 2, 3], out_shape=(3, out_h, out_w)).astype(np.float32)
            transform = src.transform * src.transform.scale(src_w / out_w, src_h / out_h)
        else:
            out_h, out_w = src_h, src_w
            img_full = src.read([1, 2, 3]).astype(np.float32)
            transform = src.transform
        crs = src.crs
        res_x = abs(transform.a)
        res_y = abs(transform.e)

    # metros por pixel: CRS projetado usa a resolução direto; geográfico converte
    if crs and crs.is_projected:
        px_area_m2 = res_x * res_y
    else:
        lat = abs(transform.f)
        px_area_m2 = (res_x * 111_320 * np.cos(np.radians(lat))) * (res_y * 110_540)

    img_full /= 255.0
    prob = np.zeros((out_h, out_w), dtype=np.float32)

    tiles = 0
    for y0 in range(0, out_h, TILE):
        for x0 in range(0, out_w, TILE):
            h = min(TILE, out_h - y0)
            w = min(TILE, out_w - x0)
            tile = np.zeros((TILE, TILE, 3), dtype=np.float32)
            tile[:h, :w] = np.transpose(img_full[:, y0:y0 + h, x0:x0 + w], (1, 2, 0))
            out = sess.run(None, {inp: tile[None]})[0][0, :, :, 0]
            prob[y0:y0 + h, x0:x0 + w] = out[:h, :w]
            tiles += 1
    logger.info(f"[ML] Inferência concluída: {tiles} tiles de {TILE}px")

    mask = (prob > threshold).astype(np.uint8)
    # limpeza morfológica: remove ruído de 1-2 px e fecha frestas de telhado
    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
    frac = float(mask.mean())
    logger.info(f"[ML] {frac*100:.2f}% dos pixels classificados como edificação")

    # altura via DSM (opcional): média por polígono, amostrada no grid do mask
    dsm_arr = None
    if dsm_path and os.path.exists(str(dsm_path)):
        with rasterio.open(str(dsm_path)) as d:
            dsm_arr = d.read(1, out_shape=(out_h, out_w)).astype(np.float32)

    min_px = max(3, int(min_area_m2 / max(px_area_m2, 1e-9)))
    features = []
    for geom, val in rio_features.shapes(mask, mask=mask.astype(bool), transform=transform):
        if val != 1:
            continue
        poly = shape(geom)
        if not poly.is_valid:
            poly = poly.buffer(0)
        # área em px para o corte mínimo (rápido) e em m² para o atributo
        px_count = poly.area / (res_x * res_y)
        if px_count < min_px:
            continue
        area_m2 = px_count * px_area_m2
        simplified = poly.simplify(res_x * 1.5, preserve_topology=True)

        # confiança = probabilidade média dentro do bounding box do polígono
        minx, miny, maxx, maxy = poly.bounds
        c0 = max(0, int((minx - transform.c) / transform.a))
        r0 = max(0, int((maxy - transform.f) / transform.e))
        c1 = min(out_w, max(c0 + 1, int((maxx - transform.c) / transform.a)))
        r1 = min(out_h, max(r0 + 1, int((miny - transform.f) / transform.e)))
        window_prob = prob[r0:r1, c0:c1]
        window_mask = mask[r0:r1, c0:c1].astype(bool)
        conf = float(window_prob[window_mask].mean()) if window_mask.any() else float(prob[r0:r1, c0:c1].mean())

        height = None
        if dsm_arr is not None:
            hwin = dsm_arr[r0:r1, c0:c1]
            if window_mask.any():
                height = float(np.nanmean(hwin[window_mask]) - base_elevation)
                height = max(0.0, round(height, 1))

        features.append({
            "type": "Feature",
            "geometry": mapping(simplified),
            "properties": {
                "area_m2": round(area_m2, 1),
                "confidence": round(conf, 3),
                "building_type": "ml",
                **({"height": height} if height is not None else {}),
            },
        })

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "source": "CM-TECHMAP ML segmentation (ONNX)",
            "model": os.path.basename(model_path),
            "threshold": threshold,
            "building_pixel_fraction": round(frac, 4),
            "total_buildings": len(features),
        },
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    logger.info(f"[ML] {len(features)} edificações extraídas → {output_path}")
    return output_path
