"""
CM TECHMAP — Analisador de TELHADOS (rejeição de sombra + tipologia + área real)

Três problemas resolvidos aqui, nesta ordem:

1. SOMBRA NÃO É CONSTRUÇÃO. A rede neural dispara em manchas escuras
   adjacentes a prédios altos — a sombra tem a FORMA de um telhado visto de
   cima. O que a desmascara é a física: sombra é escura E está no nível do
   solo. Um telhado escuro tem altura; uma sombra clara não existe. O teste
   cruza luminância com nDSM e rejeita o que for escuro-e-rasteiro.

2. TIPOLOGIA POR ÁGUAS. Cada face de telhado é um plano com uma orientação
   (azimute). Contando os modos significativos do histograma de azimute —
   ponderado pela declividade — sai o número de águas: 1 plano inclinado
   (uma água), 2 opostos (duas águas), 4 convergentes (quatro águas), quase
   plano (embutido com platibanda). A inclinação média confirma a classe.

3. ÁREA REAL vs PROJETADA. O ortomosaico é uma projeção vertical: um
   telhado inclinado ocupa MENOS pixels do que sua superfície real. A área
   verdadeira integra sqrt(1 + (dz/dx)² + (dz/dy)²) pixel a pixel — é a
   metragem que interessa para orçamento de telha e para IPTU justo.

Material da telha (cerâmica, fibrocimento, concreto, termoacústica) sai de
cor (HSV), saturação, brilho e textura — com confiança declarada, porque
sem calibração de campo isso é indício forte, não certeza absoluta.
"""

import logging
import math

import numpy as np

logger = logging.getLogger(__name__)

# ── Limiares documentados (não são mágicos: cada um tem razão física) ────────
SHADOW_LUMINANCE_MAX = 0.34   # abaixo disso a superfície é escura de fato
SHADOW_HEIGHT_MAX = 0.80      # sombra vive no chão; telhado se eleva
ROOF_MIN_HEIGHT_M = 1.20      # menor edícula habitável

# Faixas de inclinação da especificação (percentual)
SLOPE_FLAT_MAX = 10.0         # embutido/platibanda: 2% a 10%
SLOPE_ONE_WATER_MAX = 25.0    # uma água: 10% a 25%
SLOPE_TWO_WATER_MAX = 35.0    # duas águas: 25% a 35%


def _luminance(rgb: np.ndarray) -> np.ndarray:
    """Luminância perceptual (Rec. 709) em [0,1]."""
    return (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255.0


def shadow_pixel_mask(rgb_win: np.ndarray, height_win: np.ndarray, gsd_m: float) -> np.ndarray:
    """
    Máscara PIXEL A PIXEL do que é sombra ou vegetação sombreada.

    Três assinaturas físicas, cada uma insuficiente sozinha:
    - CROMÁTICA: sombra é iluminada pelo céu, não pelo sol — escura E
      azulada (B domina). Telha escura reflete o sol e não puxa pro azul.
    - VEGETAÇÃO SOMBREADA: escura E rugosa no nDSM. Copa na sombra herda a
      ALTURA da árvore e furava o teste antigo (medido no relato do campo:
      'sombra' com h=12,7 m — era copa). Telhado é liso; copa nunca é.
    - RASTEIRA: escura e sem elevação (a sombra clássica no chão).
    """
    import cv2

    lum = _luminance(rgb_win)
    total = np.maximum(rgb_win.sum(axis=0), 1e-6)
    blue_ratio = rgb_win[2] / total
    dark = lum < SHADOW_LUMINANCE_MAX
    bluish = blue_ratio > 0.345

    k = max(3, int(round(0.5 / max(gsd_m, 1e-6))) | 1)
    mu = cv2.blur(height_win.astype(np.float32), (k, k))
    sq = cv2.blur((height_win * height_win).astype(np.float32), (k, k))
    rough = np.sqrt(np.maximum(sq - mu * mu, 0.0))

    low = height_win < SHADOW_HEIGHT_MAX
    shadowed_canopy = dark & (rough > 1.0)
    chromatic = dark & bluish
    ground_shadow = dark & low
    return ground_shadow | chromatic | shadowed_canopy


def detect_shadow(
    rgb_win: np.ndarray,
    height_win: np.ndarray,
    mask: np.ndarray,
    gsd_m: float = 0.05,
) -> dict:
    """
    Julga a detecção inteira E devolve a máscara de telhado APARADA.

    O caso que o teste antigo perdia: polígono que mistura telhado com a
    saia de sombra ao lado (ou copa sombreada). A decisão vira fração:
    - maioria sombra → rejeita a detecção inteira;
    - minoria sombra → APARA: a medição passa a valer só nos pixels de
      telhado, como uma pessoa contornando a beirada com o dedo.
    """
    if not mask.any():
        return {"is_shadow": False, "reason": "sem pixels"}
    lum = float(_luminance(rgb_win)[mask].mean())
    h_p90 = float(np.percentile(height_win[mask], 90))

    shadow_px = shadow_pixel_mask(rgb_win, height_win, gsd_m)
    shadow_frac = float((shadow_px & mask).sum()) / float(mask.sum())
    valid = mask & ~shadow_px

    is_shadow = shadow_frac >= 0.60 or (lum < SHADOW_LUMINANCE_MAX and h_p90 < SHADOW_HEIGHT_MAX)
    return {
        "is_shadow": is_shadow,
        "luminance": round(lum, 4),
        "height_p90_m": round(h_p90, 3),
        "shadow_fraction": round(shadow_frac, 3),
        "valid_mask": valid,
        "trimmed": bool(0.05 < shadow_frac < 0.60),
        "reason": (
            f"{shadow_frac*100:.0f}% dos pixels são sombra/vegetação sombreada"
            if is_shadow else
            f"telhado com {shadow_frac*100:.0f}% de saia de sombra aparada"
            if shadow_frac > 0.05 else
            f"luminância {lum:.2f}, altura p90 {h_p90:.2f} m — superfície real"
        ),
    }


def _slope_and_aspect(height: np.ndarray, gsd_m: float, smooth_m: float = 0.30):
    """
    Declividade (%) e azimute (graus) por pixel.

    O nDSM fotogramétrico traz ruído de reconstrução: a 2,31 cm/px, uma
    variação de 5 cm entre pixels vizinhos vira 216% de caimento. Suavizar
    numa janela de ~30 cm (a escala de uma telha, não a de um telhado)
    remove o ruído sem apagar a geometria do caimento.
    """
    import cv2

    k = max(3, int(round(smooth_m / max(gsd_m, 1e-6))) | 1)  # ímpar
    hs = cv2.GaussianBlur(height.astype(np.float64), (k, k), 0)
    dzdy, dzdx = np.gradient(hs, gsd_m, gsd_m)
    slope_pct = np.sqrt(dzdx ** 2 + dzdy ** 2) * 100.0
    aspect = (np.degrees(np.arctan2(-dzdx, dzdy)) + 360.0) % 360.0
    return slope_pct, aspect, dzdx, dzdy


def fit_dominant_plane(height: np.ndarray, mask: np.ndarray, gsd_m: float) -> dict:
    """
    Ajusta o PLANO DOMINANTE do telhado por mínimos quadrados com rejeição
    de outliers — a forma como a fotogrametria profissional mede caimento.

    Gradiente pixel a pixel responde ao ruído e ao degrau da parede; um
    plano ajustado responde à SUPERFÍCIE. Duas rodadas de rejeição (2σ)
    descartam calhas, caixas d'água, antenas e restos de borda. O resíduo
    que sobra vira a medida de planaridade: telhado é plano por partes,
    copa de árvore não é.
    """
    ys, xs = np.nonzero(mask)
    if ys.size < 12:
        return {"valid": False}
    z = height[ys, xs].astype(np.float64)
    X = np.column_stack([xs * gsd_m, ys * gsd_m, np.ones(xs.size)])
    keep = np.ones(z.size, dtype=bool)
    coef = None
    for _ in range(3):
        if keep.sum() < 8:
            break
        coef, *_ = np.linalg.lstsq(X[keep], z[keep], rcond=None)
        resid = z - X @ coef
        sigma = float(np.std(resid[keep])) or 1e-6
        keep = np.abs(resid) <= 2.0 * sigma
    if coef is None:
        return {"valid": False}
    a, b, _c = coef
    slope_pct = float(np.sqrt(a * a + b * b) * 100.0)
    aspect = float((np.degrees(np.arctan2(-a, b)) + 360.0) % 360.0)
    resid = z - X @ coef
    rmse = float(np.sqrt(np.mean(resid[keep] ** 2))) if keep.any() else float("nan")
    return {
        "valid": True,
        "slope_pct": round(slope_pct, 2),
        "aspect_deg": round(aspect, 1),
        "rmse_m": round(rmse, 4),
        "inliers_pct": round(100.0 * keep.sum() / z.size, 1),
        # Fator de esticamento do PLANO: geometria pura, sem ruído.
        "stretch": round(float(np.sqrt(1.0 + a * a + b * b)), 5),
    }


def _count_waters(slope_pct: np.ndarray, aspect: np.ndarray, mask: np.ndarray) -> tuple[int, list]:
    """
    Conta as ÁGUAS pelos modos do histograma de azimute.

    Só pixels com declividade real votam (um plano horizontal não tem
    direção de caimento). Os modos são achados em 24 setores de 15° com
    supressão de vizinhança, e cada um precisa de 12% da massa para contar
    como água — assim ruído de borda não vira uma face inexistente.
    """
    valid = mask & (slope_pct > 6.0) & np.isfinite(aspect)
    if valid.sum() < 25:
        return 0, []
    weights = slope_pct[valid]
    hist, edges = np.histogram(aspect[valid], bins=24, range=(0, 360), weights=weights)
    total = hist.sum()
    if total <= 0:
        return 0, []
    hist = hist / total

    peaks = []
    order = np.argsort(hist)[::-1]
    for idx in order:
        if hist[idx] < 0.12:
            break
        # supressão: um pico já aceito absorve os setores vizinhos (±30°)
        if any(min(abs(idx - p), 24 - abs(idx - p)) <= 2 for p in peaks):
            continue
        peaks.append(int(idx))
        if len(peaks) >= 6:
            break
    directions = [round(float(edges[p] + 7.5), 1) for p in peaks]
    return len(peaks), directions


def _classify_structure(waters: int, slope_mean: float, directions: list) -> tuple[str, str, float]:
    """Nome, design e confiança da tipologia estrutural."""
    if slope_mean <= SLOPE_FLAT_MAX or waters == 0:
        return ("Telhado Embutido com Platibanda", "Oculto / Plano Moderno",
                0.85 if slope_mean <= SLOPE_FLAT_MAX else 0.6)
    if waters == 1:
        conf = 0.85 if slope_mean <= SLOPE_ONE_WATER_MAX else 0.7
        return "Telhado de Uma Água", "Plano Inclinado Único", conf
    if waters == 2:
        # Duas águas verdadeiras têm caimentos aproximadamente opostos
        opposed = False
        if len(directions) >= 2:
            d = abs(directions[0] - directions[1]) % 360
            opposed = 140 <= min(d, 360 - d) <= 220
        conf = 0.9 if opposed and slope_mean <= SLOPE_TWO_WATER_MAX else 0.72
        return "Telhado de Duas Águas", "Gable / Triangular", conf
    if waters in (3, 4):
        return "Telhado de Quatro Águas", "Hip / Piramidal", 0.85 if waters == 4 else 0.7
    return "Telhado Múltiplas Águas (composto)", "Composto", 0.65


def _classify_material(rgb_win: np.ndarray, mask: np.ndarray) -> tuple[str, str, float, dict]:
    """
    Material provável da telha por cor, saturação, brilho e textura.

    Cerâmica: matiz laranja-avermelhado e saturação alta.
    Fibrocimento: cinza dessaturado, textura ondulada (variância média).
    Concreto: cinza escuro dessaturado, textura baixa.
    Termoacústica/metálica: brilho alto e textura quase nula (chapa lisa).
    """
    import cv2

    px = rgb_win[:, mask].T.astype(np.uint8)  # (N,3) RGB
    if px.size == 0:
        return "Indeterminado", "—", 0.0, {}
    hsv = cv2.cvtColor(px.reshape(-1, 1, 3), cv2.COLOR_RGB2HSV).reshape(-1, 3).astype(np.float32)
    hue = hsv[:, 0] * 2.0        # OpenCV usa 0..179
    sat = hsv[:, 1] / 255.0
    val = hsv[:, 2] / 255.0

    lum = _luminance(rgb_win)
    texture = float(np.std(lum[mask]))
    hue_med = float(np.median(hue))
    sat_med = float(np.median(sat))
    val_med = float(np.median(val))
    # Matiz de telha cerâmica cobre laranja/vermelho (0–35° e 340–360°)
    ceramic_hue = (hue_med <= 35.0) or (hue_med >= 340.0)

    metrics = {
        "hue_deg": round(hue_med, 1),
        "saturation": round(sat_med, 3),
        "brightness": round(val_med, 3),
        "texture_std": round(texture, 4),
    }

    if ceramic_hue and sat_med >= 0.28:
        return "Telha Cerâmica / Colonial", "Tradicional / Colonial", round(min(0.95, 0.55 + sat_med), 2), metrics
    if sat_med < 0.18 and val_med >= 0.62 and texture < 0.055:
        return "Telha Termoacústica (Sanduíche)", "Sanduíche Metálico", 0.78, metrics
    if sat_med < 0.22 and 0.30 <= val_med < 0.62 and texture >= 0.045:
        return "Telha de Fibrocimento", "Placa Ondulada Rígida", 0.75, metrics
    if sat_med < 0.22 and val_med < 0.42:
        return "Telha de Concreto", "Cimento Prensado", 0.70, metrics
    if ceramic_hue:
        return "Telha Cerâmica / Colonial", "Tradicional / Colonial", 0.6, metrics
    return "Material Indeterminado", "—", 0.4, metrics


# ── Assinaturas de referência de material (matiz°, saturação, brilho, textura)
# Derivadas da especificação técnica: cerâmica é argila cozida (laranja
# saturado, ondulada), fibrocimento é placa cinza ondulada, concreto é
# cimento prensado (cinza escuro, liso), termoacústica é chapa metálica
# (clara, lisíssima). Servem de ÂNCORA para a calibração por voo — não de
# limiar rígido, porque cada município tem luz, câmera e horário próprios.
MATERIAL_SIGNATURES = {
    "Telha Cerâmica / Colonial": {
        "design": "Tradicional / Colonial",
        "hue": 18.0, "sat": 0.52, "val": 0.50, "tex": 0.075,
        "peso": "Alto",
    },
    "Telha de Fibrocimento": {
        "design": "Placa Ondulada Rígida",
        "hue": 40.0, "sat": 0.10, "val": 0.52, "tex": 0.065,
        "peso": "Leve",
    },
    "Telha de Concreto": {
        "design": "Cimento Prensado",
        "hue": 30.0, "sat": 0.12, "val": 0.33, "tex": 0.040,
        "peso": "Muito Alto",
    },
    "Telha Termoacústica (Sanduíche)": {
        "design": "Sanduíche Metálico",
        "hue": 200.0, "sat": 0.08, "val": 0.74, "tex": 0.028,
        "peso": "Leve",
    },
}

# Peso de cada dimensão na distância ao material de referência. O matiz
# separa cerâmica do resto; saturação e brilho separam os cinzas entre si;
# textura separa chapa lisa de placa ondulada.
_FEATURE_WEIGHTS = {"hue": 1.0, "sat": 2.2, "val": 1.6, "tex": 1.4}


def _hue_distance(a: float, b: float) -> float:
    """Distância angular normalizada entre matizes (0..1)."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d) / 180.0


def classify_material_calibrated(metrics: dict, calibration: dict | None = None) -> tuple[str, str, float]:
    """
    Material por PROXIMIDADE às assinaturas de referência, com a escala do
    voo corrigida pela calibração.

    Por que não limiares fixos: a mesma telha cerâmica fotografada às 9h em
    Goiás e às 15h no Paraná tem brilho e saturação diferentes. Limiar
    rígido gera "Indeterminado" — medido: 23% dos telhados. A calibração
    por voo normaliza brilho e saturação pela MEDIANA do próprio voo antes
    de comparar, então a decisão passa a ser relativa ao conjunto real.

    Retorna (material, design, confiança). A confiança cai quando o segundo
    colocado está perto — ambiguidade declarada, nunca escondida.
    """
    cal = calibration or {}
    val_shift = cal.get("val_shift", 0.0)
    sat_scale = cal.get("sat_scale", 1.0)

    m = {
        "hue": float(metrics.get("hue_deg", 0.0)),
        "sat": float(metrics.get("saturation", 0.0)) * sat_scale,
        "val": float(metrics.get("brightness", 0.0)) - val_shift,
        "tex": float(metrics.get("texture_std", 0.0)),
    }

    scored = []
    for name, ref in MATERIAL_SIGNATURES.items():
        d = (
            _FEATURE_WEIGHTS["hue"] * _hue_distance(m["hue"], ref["hue"])
            + _FEATURE_WEIGHTS["sat"] * abs(m["sat"] - ref["sat"]) / 0.6
            + _FEATURE_WEIGHTS["val"] * abs(m["val"] - ref["val"]) / 0.6
            + _FEATURE_WEIGHTS["tex"] * abs(m["tex"] - ref["tex"]) / 0.12
        )
        scored.append((d, name, ref["design"]))
    scored.sort()
    best_d, best_name, best_design = scored[0]
    second_d = scored[1][0] if len(scored) > 1 else best_d + 1.0

    # Confiança: quão melhor o primeiro é que o segundo, temperado pela
    # distância absoluta (um telhado longe de TODAS as referências não
    # merece confiança alta, mesmo sendo o "menos pior").
    margin = (second_d - best_d) / max(second_d, 1e-6)
    absolute = max(0.0, 1.0 - best_d / 4.0)
    conf = round(min(0.97, 0.35 + 0.4 * margin + 0.35 * absolute), 2)
    return best_name, best_design, conf


def build_flight_calibration(samples: list[dict]) -> dict:
    """
    Calibração por VOO: normaliza brilho e saturação pelo próprio conjunto.

    Recebe as métricas de cor de uma amostra de telhados do voo e devolve os
    deslocamentos que alinham este voo com a escala das assinaturas de
    referência. É o que faz o mesmo classificador funcionar em 7 mil
    municípios com câmeras, horários e latitudes diferentes.
    """
    if not samples:
        return {"val_shift": 0.0, "sat_scale": 1.0, "samples": 0}
    vals = np.array([s.get("brightness", 0.0) for s in samples], dtype=float)
    sats = np.array([s.get("saturation", 0.0) for s in samples], dtype=float)
    ref_val = float(np.mean([r["val"] for r in MATERIAL_SIGNATURES.values()]))
    ref_sat = float(np.mean([r["sat"] for r in MATERIAL_SIGNATURES.values()]))
    val_med = float(np.median(vals)) if vals.size else ref_val
    sat_med = float(np.median(sats)) if sats.size else ref_sat
    return {
        # Deslocamento de brilho: a mediana do voo passa a coincidir com a
        # mediana das referências (corrige hora do dia e exposição).
        "val_shift": round(val_med - ref_val, 4),
        # Escala de saturação: corrige câmera "lavada" ou saturada demais.
        "sat_scale": round(ref_sat / sat_med, 4) if sat_med > 0.02 else 1.0,
        "samples": int(len(samples)),
        "flight_val_median": round(val_med, 4),
        "flight_sat_median": round(sat_med, 4),
    }


def detect_ridge(height_win: np.ndarray, mask: np.ndarray, gsd_m: float) -> dict:
    """
    Procura a CUMEEIRA — a linha alta onde duas águas se encontram.

    Um telhado de duas águas de verdade tem uma crista contínua; sem ela, a
    classificação por azimute pode estar vendo duas encostas separadas. A
    crista é achada nos máximos locais de altura ao longo do eixo
    perpendicular ao caimento, e sua CONTINUIDADE vira evidência.
    """
    if mask.sum() < 30:
        return {"has_ridge": False, "ridge_score": 0.0}
    h = np.where(mask, height_win, np.nan)
    # A cumeeira é aproximadamente uma linha reta: nos dois eixos, o pico de
    # altura por faixa deve ficar quase sempre na mesma posição relativa.
    scores = []
    for axis in (0, 1):
        with np.errstate(invalid="ignore"):
            profile = np.nanmax(h, axis=axis)
            argmax = np.nanargmax(np.nan_to_num(h, nan=-1e9), axis=axis).astype(float)
        valid = np.isfinite(profile) & (profile > 0)
        if valid.sum() < 8:
            continue
        pos = argmax[valid]
        # Desvio pequeno da posição do pico ⇒ crista alinhada
        spread = float(np.std(pos)) / max(1.0, h.shape[axis])
        scores.append(max(0.0, 1.0 - spread * 4.0))
    ridge_score = float(max(scores)) if scores else 0.0
    return {
        "has_ridge": ridge_score >= 0.55,
        "ridge_score": round(ridge_score, 3),
    }


def slope_matches_type(roof_type: str, slope_pct: float) -> tuple[bool, str]:
    """
    Confere a inclinação medida contra a faixa da ESPECIFICAÇÃO.

    É o contraditório interno da classificação: se o tipo diz "duas águas"
    (25%–35%) e a inclinação medida é 3%, alguma das duas leituras está
    errada — e o sistema declara isso em vez de fingir coerência.
    """
    faixas = {
        "Telhado Embutido com Platibanda": (0.0, 10.0),
        "Telhado de Uma Água": (10.0, 25.0),
        "Telhado de Duas Águas": (25.0, 35.0),
        "Telhado de Quatro Águas": (25.0, 40.0),
    }
    faixa = faixas.get(roof_type)
    if not faixa:
        return True, "tipo composto — sem faixa normativa"
    lo, hi = faixa
    # Tolerância de 40% da largura da faixa: telhado real não obedece tabela
    # com régua, mas fora disso é incoerência de verdade.
    tol = (hi - lo) * 0.4
    ok = (lo - tol) <= slope_pct <= (hi + tol)
    return ok, (
        f"inclinação {slope_pct:.1f}% dentro de {lo:.0f}–{hi:.0f}% (±{tol:.0f})"
        if ok else
        f"inclinação {slope_pct:.1f}% FORA de {lo:.0f}–{hi:.0f}% — leitura incoerente"
    )


def segment_roof_levels(height: np.ndarray, mask: np.ndarray, max_levels: int = 4) -> tuple[np.ndarray, int]:
    """
    Separa NÍVEIS de telhado dentro de um mesmo polígono.

    A detecção às vezes funde prédios vizinhos num blob só — medido no voo
    real: um polígono de 845 m² indo de 1,12 m a 17,86 m de altura. Isso não
    é um telhado inclinado a 42%, são construções de alturas diferentes
    coladas. Ajustar um plano nesse conjunto produz um caimento fictício.

    A separação usa agrupamento 1-D nas alturas (k-means simples com
    inicialização por quantis). O número de níveis é escolhido pela queda de
    inércia: só divide quando a divisão REALMENTE explica a variação.
    Retorna a máscara do nível DOMINANTE (maior área) e quantos níveis há.
    """
    vals = height[mask]
    if vals.size < 40:
        return mask, 1
    amplitude = float(np.percentile(vals, 97) - np.percentile(vals, 3))
    # Menos de 2,5 m de amplitude é telhado inclinado normal, não prédios
    # de andares diferentes — não há o que separar.
    if amplitude < 2.5:
        return mask, 1

    best_labels, best_k, best_score = None, 1, None
    for k in range(2, max_levels + 1):
        centers = np.percentile(vals, np.linspace(10, 90, k))
        for _ in range(12):
            d = np.abs(vals[:, None] - centers[None, :])
            lab = np.argmin(d, axis=1)
            new_centers = np.array([
                vals[lab == i].mean() if np.any(lab == i) else centers[i]
                for i in range(k)
            ])
            if np.allclose(new_centers, centers, atol=1e-3):
                break
            centers = new_centers
        inertia = float(np.mean((vals - centers[lab]) ** 2))
        # Penaliza divisões que criam níveis quase iguais (separação < 1,2 m
        # é degrau de platibanda, não outro prédio).
        sep = np.min(np.diff(np.sort(centers))) if k > 1 else np.inf
        if sep < 1.2:
            continue
        score = inertia
        if best_score is None or score < best_score * 0.6:
            best_score, best_k, best_labels = score, k, lab

    if best_labels is None:
        return mask, 1

    ys, xs = np.nonzero(mask)
    counts = np.bincount(best_labels, minlength=best_k)
    dominant = int(np.argmax(counts))
    out = np.zeros_like(mask)
    sel = best_labels == dominant
    out[ys[sel], xs[sel]] = True
    return out, best_k


def fit_per_water_planes(
    height: np.ndarray,
    mask: np.ndarray,
    aspect: np.ndarray,
    slope_pct: np.ndarray,
    directions: list,
    gsd_m: float,
) -> dict:
    """
    Ajusta UM PLANO POR ÁGUA — a única forma correta de medir caimento em
    telhado de duas ou quatro águas.

    Um plano único atravessando a cumeeira mede a média de duas superfícies
    OPOSTAS: o resultado não é o caimento de nenhuma das duas. Medido no voo
    real: "Duas Águas" saía com 54% quando a especificação diz 25%–35%.
    Cada água é isolada pelo seu azimute, ajustada em separado, e o caimento
    do telhado passa a ser a mediana das águas — que é o número que o
    telhadista e o fiscal usam.
    """
    if not directions:
        return {"valid": False}
    planes = []
    for d in directions:
        # Pixels cujo caimento aponta para esta água (±45°)
        delta = np.abs(((aspect - d + 180.0) % 360.0) - 180.0)
        sel = mask & (delta <= 45.0) & (slope_pct > 3.0)
        if sel.sum() < 30:
            continue
        pl = fit_dominant_plane(height, sel, gsd_m)
        if pl.get("valid"):
            pl["pixels"] = int(sel.sum())
            pl["assigned_aspect"] = d
            planes.append(pl)
    if not planes:
        return {"valid": False}
    slopes = [p["slope_pct"] for p in planes]
    stretches = [p["stretch"] for p in planes]
    weights = np.array([p["pixels"] for p in planes], dtype=float)
    weights /= weights.sum()
    return {
        "valid": True,
        "planes": planes,
        "n_planes": len(planes),
        # Mediana entre as águas: robusta a uma face mal segmentada
        "slope_pct": round(float(np.median(slopes)), 2),
        # Esticamento ponderado pela área de cada água
        "stretch": round(float(np.sum(weights * np.array(stretches))), 5),
        "rmse_m": round(float(np.median([p["rmse_m"] for p in planes])), 4),
    }


def analyze_roof(
    rgb_win: np.ndarray,
    height_win: np.ndarray,
    mask: np.ndarray,
    gsd_m: float,
    calibration: dict | None = None,
) -> dict:
    """
    Análise completa de UM telhado: sombra?, tipologia, material, inclinação
    e AS DUAS ÁREAS — projetada (o que se vê de cima) e real (a superfície
    que precisa ser cobertura de telha).
    """
    out: dict = {}
    if not mask.any():
        return {"roof_valid": False, "reason": "máscara vazia"}

    shadow = detect_shadow(rgb_win, height_win, mask, gsd_m)
    valid_mask = shadow.pop("valid_mask", mask)
    out.update({
        "shadow_check": {k: v for k, v in shadow.items() if k != "valid_mask"},
        "luminance": shadow.get("luminance"),
        "shadow_fraction": shadow.get("shadow_fraction"),
        "boundary_trimmed": shadow.get("trimmed", False),
    })
    if shadow["is_shadow"]:
        return {**out, "roof_valid": False, "rejected_as": "shadow", "reason": shadow["reason"]}
    # Daqui em diante, TODA medição vale só nos pixels de telhado — a saia
    # de sombra ao lado não entra na área, na altura nem no material.
    if valid_mask.sum() >= 12:
        mask = valid_mask

    h = height_win[mask]
    if float(np.percentile(h, 90)) < ROOF_MIN_HEIGHT_M:
        return {
            **out, "roof_valid": False, "rejected_as": "ground_level",
            "reason": f"altura p90 {float(np.percentile(h, 90)):.2f} m — abaixo de telhado habitável",
        }

    # Antes de medir qualquer coisa: este polígono é UM telhado ou vários
    # prédios colados? Medir um plano sobre níveis diferentes inventa
    # caimento que não existe.
    level_mask, n_levels = segment_roof_levels(height_win, mask)
    if n_levels > 1 and level_mask.sum() >= 40:
        mask = level_mask
        h = height_win[mask]

    slope_pct, aspect, dzdx, dzdy = _slope_and_aspect(height_win, gsd_m)

    # INTERIOR do telhado: na BORDA o nDSM salta do chão (0 m) para a laje
    # (3 m) em um pixel — gradiente de ~130 e fator de esticamento de 130.
    # Medido antes desta correção: ganho médio de área de 700%, fisicamente
    # impossível (um telhado de 35% de caimento ganha ~6%). A parede não é
    # telhado: erodir a máscara tira o precipício da conta e também conserta
    # a classificação, que era jogada para "inclinado" pelas bordas.
    import cv2

    # Erosão PROPORCIONAL: a transição chão→laje no nDSM fotogramétrico se
    # espalha por vários pixels (a reconstrução borra a parede). Um telhado
    # grande suporta uma faixa maior de recuo; um pequeno, menos.
    radius_px = max(2, int(0.06 * math.sqrt(max(mask.sum(), 1))))
    ksz = min(31, radius_px * 2 + 1)
    eroded = cv2.erode(mask.astype(np.uint8), np.ones((ksz, ksz), np.uint8), iterations=1).astype(bool)
    interior = eroded if eroded.sum() >= max(12, int(0.10 * mask.sum())) else mask

    # A INCLINAÇÃO vem do plano ajustado (robusto), não do gradiente por
    # pixel: medido antes desta correção, "Embutido com Platibanda" saía com
    # 77,9% de caimento — a especificação diz 2% a 10%. O ruído do nDSM e o
    # degrau da parede sequestravam a leitura.
    waters, directions = _count_waters(slope_pct, aspect, interior)

    # Um plano por ÁGUA quando há mais de uma: atravessar a cumeeira com um
    # plano só mede a média de duas superfícies opostas e não o caimento de
    # nenhuma delas.
    plane = fit_dominant_plane(height_win, interior, gsd_m)
    per_water = (
        fit_per_water_planes(height_win, interior, aspect, slope_pct, directions, gsd_m)
        if waters >= 2 else {"valid": False}
    )
    if per_water.get("valid"):
        slope_mean = per_water["slope_pct"]
        plane = {**plane, "per_water": per_water, "source": "per_water_planes"}
    elif plane.get("valid"):
        slope_mean = plane["slope_pct"]
        plane = {**plane, "source": "dominant_plane"}
    else:
        slope_mean = float(np.median(np.clip(slope_pct[interior], 0.0, 300.0)))
        plane = {**plane, "source": "pixel_gradient"}
    slope_p90 = float(np.percentile(np.clip(slope_pct[interior], 0, 300), 90))
    roof_name, design, struct_conf = _classify_structure(waters, slope_mean, directions)
    # Material medido SÓ no telhado ensolarado: pixels de sombra/penumbra
    # mudam de amostragem a cada aparo e faziam a classificação oscilar
    # (Cerâmica 65→15 entre duas execuções — inaceitável para laudo). A luz
    # do sol é a única iluminação estável entre execuções e entre voos.
    sunlit = mask & (_luminance(rgb_win) >= 0.40)
    material_basis = sunlit if sunlit.sum() >= max(12, int(0.10 * mask.sum())) else mask
    _m_legacy, _d_legacy, _c_legacy, mat_metrics = _classify_material(rgb_win, material_basis)
    mat_metrics["sunlit_fraction"] = round(float(sunlit.sum()) / float(mask.sum()), 3)
    # Classificação por PROXIMIDADE às assinaturas, com a escala do voo
    # corrigida — é o que faz o mesmo classificador servir a municípios com
    # câmeras, horários e latitudes diferentes.
    material, mat_design, mat_conf = classify_material_calibrated(mat_metrics, calibration)

    # ÁREA REAL: a superfície inclinada é maior que sua sombra vertical.
    # O fator de esticamento é medido no INTERIOR (sem os precipícios da
    # borda) e aplicado à área projetada inteira — assim a metragem reflete
    # o caimento do telhado, não o degrau da parede.
    px_area = gsd_m * gsd_m
    # Esticamento pelo PLANO ajustado — a superfície de telha que o caimento
    # exige. Sem isso o número saía em 700% (o degrau da parede virava
    # "telhado") e depois 55% (ruído do nDSM); um telhado de 35% de caimento
    # ganha ~6%, e é isso que o plano entrega.
    if per_water.get("valid"):
        stretch_mean = min(float(per_water["stretch"]), 1.75)
    elif plane.get("valid"):
        stretch_mean = min(float(plane["stretch"]), 1.75)  # teto: 143% de caimento
    else:
        stretch = np.sqrt(1.0 + np.clip(dzdx, -1.5, 1.5) ** 2 + np.clip(dzdy, -1.5, 1.5) ** 2)
        stretch_mean = min(float(np.median(stretch[interior])) if interior.any() else 1.0, 1.75)
    area_projected = float(mask.sum() * px_area)
    area_real = area_projected * stretch_mean

    out.update({
        "roof_valid": True,
        "roof_type": roof_name,
        "roof_design": design,
        "roof_waters": waters,
        "roof_aspects_deg": directions,
        "roof_type_confidence": struct_conf,
        "roof_material": material,
        "roof_material_design": mat_design,
        "roof_material_confidence": mat_conf,
        "material_metrics": mat_metrics,
        "slope_pct": round(slope_mean, 2),
        "slope_p90_pct": round(slope_p90, 2),
        "slope_deg": round(math.degrees(math.atan(slope_mean / 100.0)), 2),
        "area_projected_m2": round(area_projected, 2),
        "area_real_m2": round(area_real, 2),
        # Quanto a projeção subestima a telha necessária — o número que
        # orçamento de obra e IPTU justo precisam.
        "area_gain_pct": round((area_real / area_projected - 1.0) * 100.0, 2) if area_projected > 0 else 0.0,
        "height_mean_m": round(float(h.mean()), 2),
        "height_max_m": round(float(h.max()), 2),
        "stretch_factor": round(stretch_mean, 4),
        "interior_pixels": int(interior.sum()),
        "plane_fit": plane,
        "roof_levels": n_levels,
        "multi_level": n_levels > 1,
    })

    # ÁREA DE PISO — a grandeza que a MEDIÇÃO MANUAL entrega. O telhado
    # ultrapassa a parede pelo beiral (NBR: 0,5–0,8 m típicos no Brasil).
    # piso ≈ projetada − perímetro×beiral + 4×beiral² (cantos descontados
    # duas vezes). A incerteza declarada cobre a faixa real de beirais:
    # o valor verdadeiro fica DENTRO do intervalo publicado.
    px_lin = math.sqrt(px_area)
    ys_m, xs_m = np.nonzero(mask)
    if ys_m.size:
        import cv2 as _cv2f
        # Perímetro da SILHUETA EXTERNA — como um medidor contorna a casa.
        # O gradiente morfológico contava também as bordas INTERNAS dos
        # recortes do aparo de sombra e dobrava o perímetro (medido: piso de
        # 2,6 m² para telhado de 34 m², absurdo). Fechar os buracos e seguir
        # só o contorno de fora devolve o perímetro que existe no mundo.
        closed = _cv2f.morphologyEx(
            mask.astype(np.uint8), _cv2f.MORPH_CLOSE, np.ones((7, 7), np.uint8)
        )
        contours, _h = _cv2f.findContours(closed, _cv2f.RETR_EXTERNAL, _cv2f.CHAIN_APPROX_SIMPLE)
        if contours:
            biggest = max(contours, key=_cv2f.contourArea)
            # 0.92: o contorno em escada de pixels supera a reta em ~8%
            perimeter_m = float(_cv2f.arcLength(biggest, True)) * px_lin * 0.92
        else:
            perimeter_m = 4.0 * math.sqrt(max(area_projected, 1e-6))
        eave, eave_lo, eave_hi = 0.60, 0.40, 0.80
        def _floor(e):
            return max(0.0, area_projected - perimeter_m * e + 4 * e * e)
        floor_est = _floor(eave)
        # Sanidade física: beiral não engole a casa. Se o desconto passar de
        # 70% da projeção, o polígono é estreito/fragmentado demais para a
        # fórmula — publica-se o piso mínimo plausível com a ressalva.
        floor_min_plausible = 0.30 * area_projected
        floor_clamped = floor_est < floor_min_plausible
        if floor_clamped:
            floor_est = floor_min_plausible
        out.update({
            "floor_area_est_m2": round(floor_est, 2),
            "floor_area_range_m2": [round(_floor(eave_hi), 2), round(_floor(eave_lo), 2)],
            "eave_assumed_m": eave,
            "perimeter_m": round(perimeter_m, 2),
            "floor_area_clamped": floor_clamped,
            "floor_area_note": (
                "Área de piso estimada = projeção do telhado − beiral de "
                f"{eave} m no perímetro; faixa cobre beirais de {eave_lo} a {eave_hi} m. "
                "É a grandeza comparável à medição manual de paredes."
            ),
        })

    # ── PORTÃO DE QUALIDADE ─────────────────────────────────────────────
    # Só recebe TIPO de telhado o que se comporta como telhado. Afirmar
    # "Duas Águas" sobre um amontoado de construções coladas, uma caixa
    # d'água ou uma superfície irregular é inventar informação — e numa
    # análise fiscal isso vira erro de laudo. O que não passa é declarado
    # como estrutura não classificável, COM O MOTIVO, e continua medido
    # (área, altura, volume) porque a medição não depende da tipologia.
    rmse_gate = (plane or {}).get("rmse_m")
    gate_reasons = []
    if rmse_gate is not None and rmse_gate > 0.80:
        gate_reasons.append(f"superfície irregular (resíduo {rmse_gate:.2f} m ao plano)")
    if slope_mean > 100.0:
        gate_reasons.append(f"caimento de {slope_mean:.0f}% — acima de qualquer telhado real")
    if n_levels > 1 and (plane or {}).get("valid") and rmse_gate and rmse_gate > 0.5:
        gate_reasons.append(f"{n_levels} níveis de altura no mesmo polígono")

    if gate_reasons:
        out.update({
            "roof_type": "Estrutura Não Classificável",
            "roof_design": "—",
            "roof_type_confidence": 0.0,
            "classifiable": False,
            "unclassifiable_reasons": gate_reasons,
            "spec_coherent": None,
            "coherence_note": "; ".join(gate_reasons),
            "ridge": detect_ridge(height_win, interior, gsd_m),
        })
        return out

    out["classifiable"] = True

    # Contraditório interno: a inclinação medida bate com a faixa que a
    # especificação define para o tipo classificado?
    coherent, coherence_note = slope_matches_type(roof_name, slope_mean)
    ridge = detect_ridge(height_win, interior, gsd_m)

    # ── DUAS TESTEMUNHAS INDEPENDENTES ──────────────────────────────────
    # A contagem de águas (geometria dos azimutes) e a inclinação (planos
    # ajustados) são medições independentes. Quando CONCORDAM, o tipo é
    # afirmado. Quando se contradizem — "duas águas" com 62% de caimento,
    # medido no voo real — nenhuma das duas merece fé cega: o telhado sai
    # com TIPOLOGIA INDETERMINADA e todas as medições preservadas.
    #
    # É o que separa um sistema que classifica tudo (e erra em silêncio) de
    # um que afirma só o que duas evidências sustentam. Numa análise fiscal
    # que vira lançamento tributário, essa distinção é a diferença entre um
    # laudo defensável e um indefensável.
    if not coherent:
        out.update({
            "roof_type": "Tipologia Indeterminada",
            "roof_design": "—",
            "roof_type_confidence": 0.0,
            "type_indeterminate": True,
            "measured_waters": waters,
            "measured_slope_pct": round(slope_mean, 2),
            "candidate_type": roof_name,
            "spec_coherent": False,
            "coherence_note": (
                f"{waters} água(s) sugerem '{roof_name}', mas a inclinação medida "
                f"({slope_mean:.1f}%) fica fora da faixa normativa — evidências "
                f"discordantes, tipo não afirmado"
            ),
            "ridge": ridge,
        })
        return out
    out.update({
        "spec_coherent": coherent,
        "coherence_note": coherence_note,
        "ridge": ridge,
    })
    # Duas águas SEM cumeeira contínua é leitura frágil — a confiança cai e
    # o fato fica declarado, em vez de o sistema afirmar o que não sustenta.
    if roof_name == "Telhado de Duas Águas" and not ridge["has_ridge"]:
        out["roof_type_confidence"] = round(out["roof_type_confidence"] * 0.7, 2)
        out["coherence_note"] += " · sem cumeeira contínua detectada"
    # RESÍDUO DO PLANO como termômetro: um telhado real fica a poucos
    # centímetros do plano ajustado. Acima de 80 cm a superfície não é
    # plana — ou o polígono ainda mistura construções, ou é vegetação. A
    # confiança cai e o motivo fica escrito.
    rmse = (plane or {}).get("rmse_m")
    if rmse is not None and rmse > 0.80:
        out["roof_type_confidence"] = round(out["roof_type_confidence"] * 0.5, 2)
        out["coherence_note"] += f" · superfície irregular (resíduo {rmse:.2f} m)"
        out["surface_irregular"] = True
    return out
