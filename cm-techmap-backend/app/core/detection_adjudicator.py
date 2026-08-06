"""
CM TECHMAP — Adjudicação de detecções "weak" em resolução NATIVA

A primeira passada da rede roda na ortofoto reamostrada (≤8192 px) por
memória. Detecções que saíram "weak" na validação 3D merecem o caminho
caro: recortar o bbox do polígono na resolução ORIGINAL (ex.: 2,31 cm/px),
reinferir com ENSEMBLE de thresholds e reavaliar a evidência 3D no recorte.

Juízes independentes por detecção (unanimidade é MEDIDA, não retórica):
  J1..J3  rede neural em resolução nativa @ thresholds 0.35 / 0.50 / 0.65
          (voto = o polígono re-detectado cobre ≥50% do original)
  J4      altura 3D (nDSM médio ≥ limiar forte)
  J5      planaridade de telhado
Veredito: unânime (5/5) > confirmada (≥4) > rejeitada (≤1) > mantém weak.
"""

import json
import logging

import numpy as np

logger = logging.getLogger(__name__)

THRESHOLDS = (0.35, 0.50, 0.65)
COVER_MIN = 0.5
PAD_M = 4.0


def adjudicate_detection(sess, inp_name, ortho_ds, ndsm_win_fn, det: dict) -> dict:
    """
    Reinfere UMA detecção no recorte nativo da ortofoto e conta os votos.
    `det`: {id, geometry(4326)}; `ndsm_win_fn(geom_native)` → array nDSM.
    Retorna {id, votes:{...}, unanimous, promoted_status}.
    """
    import cv2
    from rasterio.mask import mask as rio_mask
    from rasterio.warp import transform_geom
    from shapely.geometry import shape

    geom_native = transform_geom("EPSG:4326", ortho_ds.crs.to_string(), det["geometry"])
    poly = shape(geom_native).buffer(PAD_M)

    rgb, tr = rio_mask(ortho_ds, [poly.envelope], crop=True, filled=True, nodata=0)
    img = np.transpose(rgb[:3].astype(np.float32) / 255.0, (1, 2, 0))
    h, w = img.shape[:2]
    if h < 8 or w < 8:
        return {"id": det["id"], "votes": {}, "unanimous": False, "promoted_status": None}

    # Inferência em janelas 256 cobrindo o recorte nativo
    prob = np.zeros((h, w), dtype=np.float32)
    for y0 in range(0, h, 256):
        for x0 in range(0, w, 256):
            th, tw = min(256, h - y0), min(256, w - x0)
            tile = np.zeros((256, 256, 3), dtype=np.float32)
            tile[:th, :tw] = img[y0:y0 + th, x0:x0 + tw]
            out = sess.run(None, {inp_name: tile[None]})[0][0, :, :, 0]
            prob[y0:y0 + th, x0:x0 + tw] = out[:th, :tw]

    # Máscara do polígono ORIGINAL no grid do recorte (para medir cobertura)
    from rasterio.features import geometry_mask
    orig_mask = ~geometry_mask([geom_native], out_shape=(h, w), transform=tr, invert=False)
    orig_px = max(1, int(orig_mask.sum()))

    votes: dict[str, bool] = {}
    for t in THRESHOLDS:
        m = prob > t
        cover = float((m & orig_mask).sum()) / orig_px
        votes[f"net@{t}"] = cover >= COVER_MIN

    nd = ndsm_win_fn(geom_native)
    if nd is not None and nd.size >= 4:
        hmean = float(np.mean(nd))
        votes["height3d"] = hmean >= 2.0
        lap = np.abs(np.gradient(np.gradient(nd.astype("float64"))))
        votes["planarity"] = float(1.0 / (1.0 + np.var(lap))) >= 0.55
    else:
        votes["height3d"] = False
        votes["planarity"] = False

    yes = sum(votes.values())
    unanimous = yes == len(votes)
    promoted = (
        "confirmed_unanimous" if unanimous
        else "confirmed" if yes >= 4
        else "rejected" if yes <= 1
        else None  # permanece weak — honestidade sobre a dúvida
    )
    return {"id": det["id"], "votes": votes, "unanimous": unanimous,
            "promoted_status": promoted, "votes_json": json.dumps(votes)}
