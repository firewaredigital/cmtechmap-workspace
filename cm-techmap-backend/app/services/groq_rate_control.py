"""
CM TECHMAP — Groq Rate Controller
Redis-backed admission guard with conservative free-tier defaults.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import redis

from app.config import get_settings

logger = logging.getLogger("cm_techmap.groq.rate")
settings = get_settings()


@dataclass
class RateReservation:
    """Token reservation result used by Groq request wrappers."""

    granted: bool
    reason: str
    input_tokens: int
    output_tokens: int


class GroqRateController:
    """
    Very lightweight rate guard.

    It is intentionally conservative and simple for predictable free-tier behavior.
    We keep a rolling one-minute bucket in Redis and reject early when expected
    token/request budget is exceeded.
    """

    _REQ_KEY = "cm_techmap:groq:minute:req"
    _TOK_KEY = "cm_techmap:groq:minute:tok"
    _HDR_KEY = "cm_techmap:groq:last_headers"
    _STATE_KEY = "cm_techmap:groq:adaptive_state"
    _CB_KEY = "cm_techmap:groq:circuit_breaker"
    _TELEMETRY_EVENTS_KEY = "cm_techmap:groq:telemetry:events"

    def __init__(self) -> None:
        self._redis = redis.from_url(settings.redis_url, decode_responses=True)

    def reserve(self, input_tokens: int, output_tokens: int) -> RateReservation:
        """Reserve request + tokens for next minute window."""
        if self.is_circuit_open():
            self._emit_counter("denied", 1)
            self._emit_event("reserve_denied", {"reason": "circuit_open"})
            return RateReservation(False, "circuit_open", input_tokens, output_tokens)

        total_tokens = max(1, input_tokens + output_tokens)
        now_minute = int(time.time() // 60)

        req_key = f"{self._REQ_KEY}:{now_minute}"
        tok_key = f"{self._TOK_KEY}:{now_minute}"

        pipe = self._redis.pipeline()
        pipe.incr(req_key, 1)
        pipe.expire(req_key, 130)
        pipe.incr(tok_key, total_tokens)
        pipe.expire(tok_key, 130)
        req_count, _, tok_count, _ = pipe.execute()

        factor = self._get_utilization_factor()
        rpm_limit = max(1, int(settings.groq_rpm_limit_free * settings.groq_safety_factor * factor))
        tpm_limit = max(100, int(settings.groq_tpm_limit_free * settings.groq_safety_factor * factor))

        if req_count > rpm_limit:
            logger.warning("[GROQ] Admission denied by RPM: %s > %s", req_count, rpm_limit)
            self._emit_counter("denied", 1)
            self._emit_counter("denied_rpm", 1)
            self._emit_event("reserve_denied", {"reason": "rpm_exceeded", "req_count": req_count, "rpm_limit": rpm_limit})
            return RateReservation(False, "rpm_exceeded", input_tokens, output_tokens)
        if tok_count > tpm_limit:
            logger.warning("[GROQ] Admission denied by TPM: %s > %s", tok_count, tpm_limit)
            self._emit_counter("denied", 1)
            self._emit_counter("denied_tpm", 1)
            self._emit_event("reserve_denied", {"reason": "tpm_exceeded", "tok_count": tok_count, "tpm_limit": tpm_limit})
            return RateReservation(False, "tpm_exceeded", input_tokens, output_tokens)

        self._emit_counter("admitted", 1)
        self._emit_counter("tokens_reserved", total_tokens)
        return RateReservation(True, "ok", input_tokens, output_tokens)

    def reserve_for_entity(
        self,
        *,
        entity_key: str,
        input_tokens: int,
        output_tokens: int,
    ) -> RateReservation:
        """Reserve global + per-entity minute budget for fairness across tenants/projects."""
        base = self.reserve(input_tokens, output_tokens)
        if not base.granted:
            return base

        safe_entity = self._sanitize_entity_key(entity_key)
        total_tokens = max(1, input_tokens + output_tokens)
        now_minute = int(time.time() // 60)

        req_key = f"{self._REQ_KEY}:entity:{safe_entity}:{now_minute}"
        tok_key = f"{self._TOK_KEY}:entity:{safe_entity}:{now_minute}"

        pipe = self._redis.pipeline()
        pipe.incr(req_key, 1)
        pipe.expire(req_key, 130)
        pipe.incr(tok_key, total_tokens)
        pipe.expire(tok_key, 130)
        req_count, _, tok_count, _ = pipe.execute()

        factor = self._get_utilization_factor()
        global_rpm = max(1, int(settings.groq_rpm_limit_free * settings.groq_safety_factor * factor))
        global_tpm = max(100, int(settings.groq_tpm_limit_free * settings.groq_safety_factor * factor))

        entity_rpm = max(
            settings.groq_fairness_entity_min_rpm,
            int(global_rpm * settings.groq_fairness_entity_share),
        )
        entity_tpm = max(
            settings.groq_fairness_entity_min_tpm,
            int(global_tpm * settings.groq_fairness_entity_share),
        )

        if req_count > entity_rpm:
            self._emit_counter("denied_entity_rpm", 1)
            self._emit_event(
                "reserve_denied",
                {
                    "reason": "entity_rpm_exceeded",
                    "entity": safe_entity,
                    "req_count": req_count,
                    "entity_rpm": entity_rpm,
                },
            )
            return RateReservation(False, "entity_rpm_exceeded", input_tokens, output_tokens)
        if tok_count > entity_tpm:
            self._emit_counter("denied_entity_tpm", 1)
            self._emit_event(
                "reserve_denied",
                {
                    "reason": "entity_tpm_exceeded",
                    "entity": safe_entity,
                    "tok_count": tok_count,
                    "entity_tpm": entity_tpm,
                },
            )
            return RateReservation(False, "entity_tpm_exceeded", input_tokens, output_tokens)

        self._emit_counter("admitted_entity", 1)
        return RateReservation(True, "ok", input_tokens, output_tokens)

    def remember_headers(self, headers: dict[str, Any]) -> None:
        """Store last observed rate headers for diagnostics and future adaptation."""
        if not headers:
            return
        observed = {
            "x-ratelimit-limit-tokens": headers.get("x-ratelimit-limit-tokens"),
            "x-ratelimit-remaining-tokens": headers.get("x-ratelimit-remaining-tokens"),
            "x-ratelimit-reset-tokens": headers.get("x-ratelimit-reset-tokens"),
            "x-ratelimit-limit-requests": headers.get("x-ratelimit-limit-requests"),
            "x-ratelimit-remaining-requests": headers.get("x-ratelimit-remaining-requests"),
            "x-ratelimit-reset-requests": headers.get("x-ratelimit-reset-requests"),
            "retry-after": headers.get("retry-after"),
            "captured_at": int(time.time()),
        }
        self._redis.setex(self._HDR_KEY, 3600, json.dumps(observed))
        self._emit_counter("headers_captured", 1)
        if settings.groq_enable_adaptive_rate_from_headers:
            self._apply_header_feedback(observed)

    def record_success(self) -> None:
        """Update adaptive and breaker state on successful request."""
        state = self._read_state()
        state["consecutive_failures"] = 0
        # additive increase
        state["utilization_factor"] = min(1.0, float(state.get("utilization_factor", 1.0)) + 0.02)
        self._write_state(state)
        self._emit_counter("success", 1)

    def record_failure(self, error_text: str) -> None:
        """Update adaptive and breaker state on failed request."""
        state = self._read_state()
        failures = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = failures

        if "429" in (error_text or ""):
            # multiplicative decrease for rate-limit pressure
            cur = float(state.get("utilization_factor", 1.0))
            state["utilization_factor"] = max(settings.groq_min_utilization_factor, cur * 0.85)
            self._emit_counter("errors_429", 1)

        if failures >= settings.groq_circuit_breaker_failures:
            open_until = int(time.time()) + settings.groq_circuit_breaker_open_seconds
            state["circuit_open_until"] = open_until
            self._redis.setex(self._CB_KEY, settings.groq_circuit_breaker_open_seconds, str(open_until))
            self._emit_counter("circuit_open", 1)
            self._emit_event("circuit_open", {"open_until": open_until, "failures": failures})

        self._write_state(state)
        self._emit_counter("failure", 1)

    def is_circuit_open(self) -> bool:
        raw = self._redis.get(self._CB_KEY)
        if not raw:
            return False
        try:
            return int(raw) > int(time.time())
        except Exception:
            return False

    def compute_retry_delay(self, error_text: str, attempt: int) -> float:
        """Compute retry delay with retry-after parsing + exponential backoff + jitter."""
        retry_after = self._parse_retry_after(error_text)
        if retry_after is not None:
            self._emit_counter("retry_delay_retry_after", 1)
            return retry_after + random.uniform(0.05, 0.8)

        base = max(0.1, settings.groq_backoff_base_seconds)
        delay = min(15.0, base * (2 ** max(0, attempt)))
        self._emit_counter("retry_delay_backoff", 1)
        return delay + random.uniform(0.05, 0.6)

    def _parse_retry_after(self, error_text: str) -> float | None:
        # Accept patterns like "retry-after: 2" and "retry-after=2"
        match = re.search(r"retry-after\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", error_text or "", re.IGNORECASE)
        if not match:
            return None
        try:
            return max(0.0, float(match.group(1)))
        except Exception:
            return None

    def _apply_header_feedback(self, observed: dict[str, Any]) -> None:
        state = self._read_state()
        try:
            limit_t = observed.get("x-ratelimit-limit-tokens")
            rem_t = observed.get("x-ratelimit-remaining-tokens")
            if limit_t is not None and rem_t is not None:
                limit_v = float(limit_t)
                rem_v = float(rem_t)
                if limit_v > 0:
                    pressure = max(0.0, min(1.0, rem_v / limit_v))
                    # Use square-root damping: strong near exhaustion, gentle otherwise
                    target = max(settings.groq_min_utilization_factor, pressure ** 0.5)
                    current = float(state.get("utilization_factor", 1.0))
                    state["utilization_factor"] = min(current, target)
        except Exception:
            pass
        self._write_state(state)

    def _sanitize_entity_key(self, key: str) -> str:
        base = (key or "unknown").strip().lower()
        base = re.sub(r"[^a-z0-9:_-]", "_", base)
        return base[:120] if base else "unknown"

    def _emit_counter(self, metric: str, amount: int = 1) -> None:
        if not settings.groq_telemetry_enabled:
            return
        try:
            today = int(time.time() // 86400)
            key = f"{settings.groq_telemetry_prefix}:c:{metric}:{today}"
            pipe = self._redis.pipeline()
            pipe.incr(key, int(amount))
            pipe.expire(key, 172800)
            pipe.execute()
        except Exception:
            pass

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not settings.groq_telemetry_enabled:
            return
        try:
            item = {
                "ts": int(time.time()),
                "type": event_type,
                "payload": payload,
            }
            pipe = self._redis.pipeline()
            pipe.lpush(self._TELEMETRY_EVENTS_KEY, json.dumps(item))
            pipe.ltrim(self._TELEMETRY_EVENTS_KEY, 0, 999)
            pipe.expire(self._TELEMETRY_EVENTS_KEY, 172800)
            pipe.execute()
        except Exception:
            pass

    def _read_state(self) -> dict[str, Any]:
        raw = self._redis.get(self._STATE_KEY)
        if not raw:
            return {
                "utilization_factor": 1.0,
                "consecutive_failures": 0,
                "circuit_open_until": 0,
            }
        try:
            return json.loads(raw)
        except Exception:
            return {
                "utilization_factor": 1.0,
                "consecutive_failures": 0,
                "circuit_open_until": 0,
            }

    def _write_state(self, state: dict[str, Any]) -> None:
        self._redis.setex(self._STATE_KEY, 86400, json.dumps(state))

    def _get_utilization_factor(self) -> float:
        state = self._read_state()
        value = float(state.get("utilization_factor", 1.0))
        return max(settings.groq_min_utilization_factor, min(1.0, value))

    def get_last_headers(self) -> dict[str, Any] | None:
        raw = self._redis.get(self._HDR_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None


groq_rate_controller = GroqRateController()
