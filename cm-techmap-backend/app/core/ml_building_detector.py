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


def estimate_ground_elevation(dsm_path, percentile: float = 5.0) -> float:
    """
    Estima a cota do solo de um DSM ABSOLUTO (elevações reais, ex.: ~1100 m
    em Brasília) pelo percentil baixo — alturas de edificação passam a ser
    (média do DSM no polígono − solo). Lê o raster decimado (1024²) porque
    um percentil não precisa da resolução completa.
    """
    import rasterio

    with rasterio.open(str(dsm_path)) as d:
        h = min(d.height, 1024)
        w = min(d.width, 1024)
        arr = d.read(1, out_shape=(h, w), masked=True).astype("float32")
    vals = arr.compressed() if hasattr(arr, "compressed") else arr[~np.isnan(arr)]
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 0.0
    return float(np.nanpercentile(vals, percentile))


def extract_buildings_exhaustive(
    orthophoto_path,
    output_path,
    dsm_path=None,
    base_elevation: float = 0.0,
    threshold: float = 0.35,
    min_area_m2: float = 3.0,
    model_path: str = "/models/building_segmentation.onnx",
    model_url: str | None = None,
    overlap: float = 0.5,
    tta: bool = True,
    split_merged: bool = True,
):
    """
    Varredura EXAUSTIVA da ortofoto — nenhum lote pulado por atalho técnico.

    A versão anterior perdia mais da metade dos lotes por quatro atalhos que
    pareciam inofensivos e não eram (medido no voo real: 35 de 65 lotes sem
    nenhuma detecção):

    1. REAMOSTRAGEM: a imagem de 13229×11944 era reduzida a 8192 px para
       caber na memória — 2,31 cm/px viravam 3,7 cm/px e todo telhado pequeno
       virava borrão. Agora a inferência roda na resolução NATIVA, lendo
       janela por janela do COG; a memória fica limitada por memmap em disco,
       não pela dimensão da imagem.
    2. JANELAS SEM SOBREPOSIÇÃO: um telhado na costura de dois tiles era
       visto pela metade em cada um e sumia. Agora as janelas se sobrepõem
       (50%) e as probabilidades são somadas com peso — a costura deixa de
       existir.
    3. CORTE DE ÁREA em 20 m²: garagem, edícula, puxadinho e qualquer
       construção pequena eram DESCARTADOS em silêncio. O piso cai para
       3 m² (configurável) e o que for cortado é CONTADO e reportado.
    4. LIMIAR ÚNICO em 0,5 + abertura morfológica agressiva: estruturas de
       resposta fraca desapareciam. O limiar cai para 0,35 e a limpeza
       preserva objetos pequenos — podemos ser agressivos na captura porque
       o VETO DO 3D a jusante derruba o que não se eleva do solo. Recall
       alto sem lixo na malha fina.

    Extras desta versão:
    - TTA (4 transformações espelhadas) média as predições: telhados em
      orientações incomuns deixam de escapar;
    - separação por watershed de blocos colados: duas casas geminadas viram
      dois polígonos, não um.
    """
    import cv2
    import numpy as np
    import onnxruntime as ort
    import rasterio
    from rasterio import features as rio_features
    from rasterio.warp import transform_geom
    from rasterio.windows import Window
    from shapely.geometry import mapping, shape

    sess = ort.InferenceSession(
        ensure_model(model_path, model_url), providers=["CPUExecutionProvider"]
    )
    inp = sess.get_inputs()[0].name
    stride = max(32, int(TILE * (1.0 - overlap)))

    import tempfile

    workdir = tempfile.mkdtemp(prefix="cm_exh_")
    try:
        with rasterio.open(str(orthophoto_path)) as src:
            H, W = src.height, src.width
            transform = src.transform
            crs = src.crs
            res_x, res_y = abs(transform.a), abs(transform.e)

            if crs and crs.is_projected:
                px_area_m2 = res_x * res_y
            else:
                lat = abs(transform.f)
                px_area_m2 = (res_x * 111_320 * np.cos(np.radians(lat))) * (res_y * 110_540)

            # Acumuladores em DISCO: a resolução nativa não cabe na RAM de uma
            # máquina de 8 GB, mas cabe folgada em memmap.
            acc = np.memmap(f"{workdir}/acc.f32", dtype=np.float32, mode="w+", shape=(H, W))
            wgt = np.memmap(f"{workdir}/wgt.f32", dtype=np.float32, mode="w+", shape=(H, W))

            # Peso cosseno: o centro da janela vale mais que a borda, então a
            # emenda entre janelas some sem costura visível.
            ramp = np.hanning(TILE).astype(np.float32)
            wtile = np.outer(ramp, ramp) + 1e-3

            windows = 0
            for y0 in range(0, H, stride):
                for x0 in range(0, W, stride):
                    h = min(TILE, H - y0)
                    w = min(TILE, W - x0)
                    if h < 16 or w < 16:
                        continue
                    block = src.read(
                        [1, 2, 3], window=Window(x0, y0, w, h), boundless=True, fill_value=0
                    ).astype(np.float32) / 255.0
                    tile = np.zeros((TILE, TILE, 3), dtype=np.float32)
                    tile[:h, :w] = np.transpose(block, (1, 2, 0))

                    if tta:
                        variants = [
                            (tile, lambda a: a),
                            (tile[:, ::-1], lambda a: a[:, ::-1]),
                            (tile[::-1, :], lambda a: a[::-1, :]),
                            (tile[::-1, ::-1], lambda a: a[::-1, ::-1]),
                        ]
                    else:
                        variants = [(tile, lambda a: a)]

                    probs = []
                    for variant, undo in variants:
                        out = sess.run(None, {inp: np.ascontiguousarray(variant)[None]})[0][0, :, :, 0]
                        probs.append(undo(out))
                    prob_tile = np.mean(probs, axis=0)

                    acc[y0:y0 + h, x0:x0 + w] += prob_tile[:h, :w] * wtile[:h, :w]
                    wgt[y0:y0 + h, x0:x0 + w] += wtile[:h, :w]
                    windows += 1

            logger.info(
                f"[EXH] {windows} janelas de {TILE}px (passo {stride}, "
                f"TTA={'4x' if tta else 'off'}) em resolução NATIVA {W}x{H}"
            )

            np.divide(acc, np.maximum(wgt, 1e-6), out=acc)
            mask = (acc > threshold).astype(np.uint8)
            del wgt

        # Limpeza que PRESERVA o pequeno: fecha frestas de telhado, remove só
        # ruído de 1 px (kernel 2×2 em vez de 3×3 da versão anterior).
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

        if split_merged:
            # Casas geminadas viram um blob só. A transformada de distância
            # acha os núcleos e o watershed corta na cintura entre eles.
            dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
            peak_thr = 0.45 * dist.max() if dist.max() > 0 else 0
            _, cores = cv2.threshold(dist, peak_thr, 255, cv2.THRESH_BINARY)
            cores = cores.astype(np.uint8)
            n_cores, markers = cv2.connectedComponents(cores)
            if n_cores > 2:
                unknown = cv2.subtract(mask, cores)
                markers = markers + 1
                markers[unknown == 1] = 0
                rgb = cv2.cvtColor((mask * 255), cv2.COLOR_GRAY2BGR)
                markers = cv2.watershed(rgb, markers.astype(np.int32))
                mask[markers == -1] = 0  # linha de corte entre vizinhos
                logger.info(f"[EXH] watershed separou até {n_cores - 1} núcleos colados")

        frac = float(mask.mean())
        logger.info(f"[EXH] {frac*100:.2f}% dos pixels acima do limiar {threshold}")

        dsm_arr = None
        if dsm_path and os.path.exists(str(dsm_path)):
            with rasterio.open(str(dsm_path)) as d:
                dsm_arr = d.read(1, out_shape=(mask.shape[0], mask.shape[1])).astype(np.float32)

        min_px = max(2, int(min_area_m2 / max(px_area_m2, 1e-9)))
        features = []
        discarded_small = 0
        smallest_kept = None

        for geom, val in rio_features.shapes(mask, mask=mask.astype(bool), transform=transform):
            if val != 1:
                continue
            poly = shape(geom)
            if not poly.is_valid:
                poly = poly.buffer(0)
            px_count = poly.area / (res_x * res_y)
            if px_count < min_px:
                discarded_small += 1
                continue
            area_m2 = px_count * px_area_m2
            smallest_kept = area_m2 if smallest_kept is None else min(smallest_kept, area_m2)
            simplified = poly.simplify(res_x * 1.2, preserve_topology=True)

            minx, miny, maxx, maxy = poly.bounds
            c0 = max(0, int((minx - transform.c) / transform.a))
            r0 = max(0, int((maxy - transform.f) / transform.e))
            c1 = min(mask.shape[1], max(c0 + 1, int((maxx - transform.c) / transform.a)))
            r1 = min(mask.shape[0], max(r0 + 1, int((miny - transform.f) / transform.e)))
            wmask = mask[r0:r1, c0:c1].astype(bool)
            conf = float(acc[r0:r1, c0:c1][wmask].mean()) if wmask.any() else float(threshold)

            height = None
            if dsm_arr is not None and wmask.any():
                hwin = dsm_arr[r0:r1, c0:c1]
                height = max(0.0, round(float(np.nanmean(hwin[wmask])) - base_elevation, 2))

            geom_out = mapping(simplified)
            if crs and crs.is_projected:
                geom_out = transform_geom(crs.to_string(), "EPSG:4326", geom_out)

            features.append({
                "type": "Feature",
                "geometry": geom_out,
                "properties": {
                    "area_m2": round(area_m2, 2),
                    "confidence": round(conf, 3),
                    "building_type": "ml_exhaustive",
                    **({"height": height} if height is not None else {}),
                },
            })

        geojson = {
            "type": "FeatureCollection",
            "features": features,
            "properties": {
                "source": "CM-TECHMAP exhaustive segmentation (native res, overlap, TTA)",
                "model": os.path.basename(model_path),
                "threshold": threshold,
                "min_area_m2": min_area_m2,
                "native_resolution": True,
                "windows": windows,
                "tta": tta,
                "building_pixel_fraction": round(frac, 5),
                "total_buildings": len(features),
                # Transparência: o que foi descartado é DECLARADO, nunca some
                # em silêncio como acontecia com o corte de 20 m².
                "discarded_below_min_area": discarded_small,
                "smallest_kept_m2": round(smallest_kept, 2) if smallest_kept else None,
            },
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(geojson, f)
        logger.info(
            f"[EXH] {len(features)} objetos extraídos (menor: {smallest_kept:.1f} m², "
            f"{discarded_small} abaixo de {min_area_m2} m² descartados) → {output_path}"
        )
        return output_path
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


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
    from rasterio.warp import transform_geom
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

        # GeoJSON é POR DEFINIÇÃO WGS84: raster projetado (UTM) precisa de
        # reprojeção explícita — sem ela, metros entravam na coluna 4326 e
        # as marcações caíam fora do mundo (nunca apareciam no mapa).
        geom_out = mapping(simplified)
        if crs and crs.is_projected:
            geom_out = transform_geom(crs.to_string(), "EPSG:4326", geom_out)

        features.append({
            "type": "Feature",
            "geometry": geom_out,
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
