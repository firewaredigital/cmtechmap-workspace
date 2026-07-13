"""
CM TECHMAP — Groq Inference Service
Unified wrappers for vision extraction and report narrative generation.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.services.groq_rate_control import groq_rate_controller

logger = logging.getLogger("cm_techmap.groq")
settings = get_settings()


@dataclass
class GroqCallResult:
    """Standardized result envelope for Groq requests."""

    ok: bool
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any] | None = None
    error: str | None = None


class GroqInferenceService:
    """Service layer around Groq with strict JSON-first prompts and retries."""

    def __init__(self) -> None:
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client

        api_key = settings.groq_api_key or os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")

        try:
            from groq import Groq
        except ImportError as exc:
            raise RuntimeError("groq package not installed") from exc

        self._client = Groq(api_key=api_key)
        return self._client

    def _estimate_tokens(self, text: str, image_count: int = 0) -> int:
        # Conservative rough estimate: 1 token ~= 4 chars plus image overhead.
        return max(1, math.ceil(len(text) / 4) + (image_count * 600))

    def _chat_with_retries(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        max_completion_tokens: int,
        fairness_entity: str | None = None,
    ) -> GroqCallResult:
        client = self._get_client()

        serialized = json.dumps(messages, ensure_ascii=False)
        input_est = self._estimate_tokens(serialized)
        if fairness_entity:
            reserve = groq_rate_controller.reserve_for_entity(
                entity_key=fairness_entity,
                input_tokens=input_est,
                output_tokens=max_completion_tokens,
            )
        else:
            reserve = groq_rate_controller.reserve(input_est, max_completion_tokens)
        if not reserve.granted:
            return GroqCallResult(
                ok=False,
                content="",
                model=model,
                input_tokens=0,
                output_tokens=0,
                error=f"rate_guard:{reserve.reason}",
            )

        last_error = "unknown"
        for attempt in range(max(1, settings.groq_max_retries)):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_completion_tokens=max_completion_tokens,
                    response_format={"type": "json_object"},
                )

                # The Groq SDK exposes _response headers in some versions.
                raw_headers = {}
                try:
                    if hasattr(response, "_response") and hasattr(response._response, "headers"):
                        raw_headers = dict(response._response.headers)
                except Exception:
                    raw_headers = {}
                if raw_headers:
                    groq_rate_controller.remember_headers(raw_headers)

                usage = getattr(response, "usage", None)
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                content = response.choices[0].message.content or "{}"
                groq_rate_controller.record_success()

                return GroqCallResult(
                    ok=True,
                    content=content,
                    model=model,
                    input_tokens=prompt_tokens,
                    output_tokens=completion_tokens,
                    raw={"id": getattr(response, "id", None)},
                )
            except Exception as exc:
                last_error = str(exc)
                groq_rate_controller.record_failure(last_error)

                if groq_rate_controller.is_circuit_open():
                    break

                sleep_s = groq_rate_controller.compute_retry_delay(last_error, attempt)
                time.sleep(max(0.05, sleep_s))

        return GroqCallResult(
            ok=False,
            content="",
            model=model,
            input_tokens=0,
            output_tokens=0,
            error=last_error,
        )

    def analyze_orthomosaic(
        self,
        *,
        image_path: str,
        dsm_path: str | None,
        project_context: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Run vision inference returning a strict JSON payload for detections.

        Output contract:
        {
          "detections": [{"polygon": [[lon,lat],...], "area_sqm": ..., "perimeter_m": ..., "height_m": ..., "confidence": ..., "building_type": ...}],
          "terrain": [{"polygon": [[lon,lat],...], "area_sqm": ..., "perimeter_m": ..., "compactness": ..., "confidence": ...}],
          "notes": []
        }
        """
        with open(image_path, "rb") as fh:
            image_b64 = base64.b64encode(fh.read()).decode("ascii")

        fairness_entity = self._build_fairness_entity(project_context)

        dsm_hint = "DSM disponível" if dsm_path else "DSM indisponível"

        system_prompt = (
            "Você é um especialista geoespacial. Responda APENAS JSON válido. "
            "Não use markdown. Não invente campos. "
            "Use coordenadas WGS84 quando possível e inclua confiança por item."
        )

        user_payload = {
            "task": "extract_buildings_and_terrain",
            "constraints": {
                "min_confidence": 0.5,
                "max_buildings": 400,
                "max_terrain_patches": 250,
            },
            "project_context": project_context,
            "dsm_hint": dsm_hint,
            "output_schema": {
                "detections": [
                    {
                        "polygon": [["lon", "lat"], ["lon", "lat"], ["lon", "lat"], ["lon", "lat"]],
                        "area_sqm": "float",
                        "perimeter_m": "float",
                        "height_m": "float",
                        "confidence": "float_0_1",
                        "building_type": "residential|commercial|industrial|unknown",
                    }
                ],
                "terrain": [
                    {
                        "polygon": [["lon", "lat"], ["lon", "lat"], ["lon", "lat"], ["lon", "lat"]],
                        "area_sqm": "float",
                        "perimeter_m": "float",
                        "compactness": "float_0_1",
                        "confidence": "float_0_1",
                    }
                ],
                "notes": ["string"],
            },
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/tiff;base64,{image_b64}",
                        },
                    },
                ],
            },
        ]

        call = self._chat_with_retries(
            model=settings.groq_vision_model,
            messages=messages,
            max_completion_tokens=settings.groq_vision_max_completion_tokens,
            fairness_entity=fairness_entity,
        )
        if not call.ok:
            raise RuntimeError(f"groq_vision_failed:{call.error}")

        try:
            data = json.loads(call.content)
        except Exception as exc:
            raise RuntimeError("groq_vision_invalid_json") from exc

        if settings.groq_vision_enable_tiling:
            try:
                tile_contexts = self._build_tile_contexts(image_path)
                if tile_contexts:
                    tile_result = self._run_tiled_refinement(
                        image_b64=image_b64,
                        dsm_hint=dsm_hint,
                        project_context=project_context,
                        base_result=data,
                        tile_contexts=tile_contexts,
                        fairness_entity=fairness_entity,
                    )
                    data = self._merge_vision_payloads(data, tile_result)
            except Exception as exc:
                logger.warning("[GROQ] Tiled refinement skipped due error: %s", exc)

        data["_meta"] = {
            "model": call.model,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
        }
        return data

    def generate_report_narrative(
        self,
        *,
        report_profile: str,
        project_data: dict[str, Any],
        analytics_data: dict[str, Any],
        config: dict[str, Any],
        model_override: str | None = None,
        max_completion_tokens_override: int | None = None,
    ) -> dict[str, str]:
        """Generate structured narrative blocks used by report templates."""
        system_prompt = (
            "Você escreve relatórios técnicos municipais. Responda APENAS JSON válido. "
            "Sem markdown. Sem inventar números. Use somente os dados recebidos."
        )

        payload = {
            "task": "generate_report_narrative",
            "report_profile": report_profile,
            "style": "formal_technical_brazil_public_sector",
            "output_schema": {
                "executive_summary": "string_max_900",
                "fiscal_analysis": "string_max_900",
                "qa_analysis": "string_max_900",
                "recommendations": "string_max_900",
            },
            "project": {
                "code": project_data.get("code"),
                "name": project_data.get("name"),
                "city": project_data.get("city"),
                "state": project_data.get("state"),
            },
            "analytics": analytics_data,
            "config": {
                "iptu_rate_per_sqm": config.get("iptu_rate_per_sqm"),
                "assumed_irregular_share": config.get("assumed_irregular_share"),
                "qa_threshold": config.get("qa_threshold"),
            },
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]

        call = self._chat_with_retries(
            model=(model_override or settings.groq_report_model),
            messages=messages,
            max_completion_tokens=(
                int(max_completion_tokens_override)
                if max_completion_tokens_override
                else settings.groq_report_max_completion_tokens
            ),
            fairness_entity=self._build_fairness_entity(config),
        )
        if not call.ok:
            raise RuntimeError(f"groq_report_failed:{call.error}")

        try:
            data = json.loads(call.content)
        except Exception as exc:
            raise RuntimeError("groq_report_invalid_json") from exc

        return {
            "executive_summary": str(data.get("executive_summary") or ""),
            "fiscal_analysis": str(data.get("fiscal_analysis") or ""),
            "qa_analysis": str(data.get("qa_analysis") or ""),
            "recommendations": str(data.get("recommendations") or ""),
        }

    def _build_fairness_entity(self, payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        tenant = str(payload.get("tenant_id") or payload.get("tenant") or "").strip()
        project = str(payload.get("project_id") or payload.get("project") or "").strip()
        if tenant and project:
            return f"tenant:{tenant}:project:{project}"
        if project:
            return f"project:{project}"
        if tenant:
            return f"tenant:{tenant}"
        return None

    def _build_tile_contexts(self, image_path: str) -> list[dict[str, Any]]:
        try:
            from PIL import Image
        except Exception:
            return []

        with Image.open(image_path) as img:
            width, height = img.size

        tile_size = max(256, settings.groq_vision_tile_size_px)
        overlap = max(0, min(tile_size // 2, settings.groq_vision_tile_overlap_px))
        max_tiles = max(1, settings.groq_vision_max_tiles)

        if width <= tile_size and height <= tile_size:
            return []

        step = max(1, tile_size - overlap)
        tiles: list[dict[str, Any]] = []
        y = 0
        while y < height and len(tiles) < max_tiles:
            x = 0
            while x < width and len(tiles) < max_tiles:
                x2 = min(width, x + tile_size)
                y2 = min(height, y + tile_size)
                tiles.append({
                    "x": x,
                    "y": y,
                    "x2": x2,
                    "y2": y2,
                    "width": width,
                    "height": height,
                })
                if x2 >= width:
                    break
                x += step
            if y + tile_size >= height:
                break
            y += step

        return tiles

    def _run_tiled_refinement(
        self,
        *,
        image_b64: str,
        dsm_hint: str,
        project_context: dict[str, Any],
        base_result: dict[str, Any],
        tile_contexts: list[dict[str, Any]],
        fairness_entity: str | None,
    ) -> dict[str, Any]:
        system_prompt = (
            "Você está refinando detecções geoespaciais por janelas (tiles). "
            "Responda apenas JSON válido no schema solicitado. "
            "Não repita itens idênticos; use confiança quando houver incerteza."
        )

        payload = {
            "task": "refine_buildings_and_terrain_by_tiles",
            "project_context": project_context,
            "dsm_hint": dsm_hint,
            "base_detection_counts": {
                "buildings": len(base_result.get("detections", []) or []),
                "terrain": len(base_result.get("terrain", []) or []),
            },
            "tiles": tile_contexts,
            "output_schema": {
                "detections": [
                    {
                        "polygon": [["lon", "lat"], ["lon", "lat"], ["lon", "lat"], ["lon", "lat"]],
                        "area_sqm": "float",
                        "perimeter_m": "float",
                        "height_m": "float",
                        "confidence": "float_0_1",
                        "building_type": "residential|commercial|industrial|unknown",
                    }
                ],
                "terrain": [
                    {
                        "polygon": [["lon", "lat"], ["lon", "lat"], ["lon", "lat"], ["lon", "lat"]],
                        "area_sqm": "float",
                        "perimeter_m": "float",
                        "compactness": "float_0_1",
                        "confidence": "float_0_1",
                    }
                ],
                "notes": ["string"],
            },
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(payload, ensure_ascii=False)},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/tiff;base64,{image_b64}",
                        },
                    },
                ],
            },
        ]

        call = self._chat_with_retries(
            model=settings.groq_vision_model,
            messages=messages,
            max_completion_tokens=settings.groq_vision_tile_max_completion_tokens,
            fairness_entity=fairness_entity,
        )
        if not call.ok:
            raise RuntimeError(f"groq_vision_tile_failed:{call.error}")

        try:
            return json.loads(call.content)
        except Exception as exc:
            raise RuntimeError("groq_vision_tile_invalid_json") from exc

    def _merge_vision_payloads(
        self,
        base_result: dict[str, Any],
        tile_result: dict[str, Any],
    ) -> dict[str, Any]:
        merged = {
            "detections": [],
            "terrain": [],
            "notes": [],
        }

        base_d = list(base_result.get("detections", []) or [])
        tile_d = list(tile_result.get("detections", []) or [])
        base_t = list(base_result.get("terrain", []) or [])
        tile_t = list(tile_result.get("terrain", []) or [])

        merged["detections"] = self._dedupe_polygons(base_d + tile_d)
        merged["terrain"] = self._dedupe_polygons(base_t + tile_t)
        merged["notes"] = list(base_result.get("notes", []) or []) + list(tile_result.get("notes", []) or [])

        return merged

    def _dedupe_polygons(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            poly = item.get("polygon") or []
            try:
                key = json.dumps(poly, sort_keys=True)
            except Exception:
                key = str(poly)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out


def normalize_vision_output_to_features(vision_result: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert Groq output contract into existing internal feature dictionaries."""
    buildings: list[dict[str, Any]] = []
    terrain: list[dict[str, Any]] = []

    for idx, item in enumerate(vision_result.get("detections", []) or [], 1):
        poly = item.get("polygon") or []
        if len(poly) < 3:
            continue
        # Ensure polygon closure
        if poly[0] != poly[-1]:
            poly = poly + [poly[0]]
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [poly],
            },
            "properties": {
                "area_sqm": float(item.get("area_sqm", 0) or 0),
                "perimeter_m": float(item.get("perimeter_m", 0) or 0),
                "height_m": float(item.get("height_m", 0) or 0),
                "confidence": float(item.get("confidence", 0) or 0),
                "building_type": str(item.get("building_type", "unknown") or "unknown"),
                "source_rank": idx,
            },
        }
        buildings.append(feature)

    for idx, item in enumerate(vision_result.get("terrain", []) or [], 1):
        poly = item.get("polygon") or []
        if len(poly) < 3:
            continue
        if poly[0] != poly[-1]:
            poly = poly + [poly[0]]
        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [poly],
            },
            "properties": {
                "area_sqm": float(item.get("area_sqm", 0) or 0),
                "perimeter_m": float(item.get("perimeter_m", 0) or 0),
                "compactness": float(item.get("compactness", 0) or 0),
                "confidence": float(item.get("confidence", 0.75) or 0.75),
                "surface_type": "terrain",
                "source_rank": idx,
            },
        }
        terrain.append(feature)

    return buildings, terrain
