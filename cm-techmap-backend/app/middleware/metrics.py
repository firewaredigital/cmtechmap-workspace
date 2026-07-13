"""
CM TECHMAP — Prometheus Metrics Middleware
Exposes request count, latency histogram, and active connections for monitoring.
"""

import time
import logging
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, PlainTextResponse

logger = logging.getLogger("cm_techmap.metrics")

# ── Metrics storage ──────────────────────────────────────────────────────────
_request_count: dict[str, int] = defaultdict(int)
_request_latency_sum: dict[str, float] = defaultdict(float)
_request_latency_count: dict[str, int] = defaultdict(int)
_active_requests: int = 0
_total_requests: int = 0
_error_count: dict[str, int] = defaultdict(int)

# Histogram buckets (seconds)
_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
_latency_bucket_counts: dict[str, dict[float, int]] = defaultdict(lambda: {b: 0 for b in _BUCKETS})


class PrometheusMetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware that collects HTTP metrics for Prometheus scraping.
    Tracks: request count, latency histogram, error rate, active connections.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        global _active_requests, _total_requests

        # Skip metrics endpoint itself to avoid recursion
        if request.url.path == "/metrics":
            return await call_next(request)

        # Skip WebSocket connections (incompatible with BaseHTTPMiddleware)
        if request.url.path.startswith("/ws/"):
            return await call_next(request)

        method = request.method
        path = self._normalize_path(request.url.path)
        label = f'{method}:{path}'

        _active_requests += 1
        _total_requests += 1
        start = time.perf_counter()

        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            duration = time.perf_counter() - start
            _active_requests -= 1

            status_class = f"{status // 100}xx"
            key = f'{label}:{status_class}'

            _request_count[key] += 1
            _request_latency_sum[label] += duration
            _request_latency_count[label] += 1

            if status >= 400:
                _error_count[f'{label}:{status}'] += 1

            # Update histogram buckets
            for bucket in _BUCKETS:
                if duration <= bucket:
                    _latency_bucket_counts[label][bucket] += 1

        return response

    @staticmethod
    def _normalize_path(path: str) -> str:
        """
        Normalize path to reduce cardinality.
        Replace UUIDs and IDs with {id} placeholder.
        """
        import re
        # UUID pattern
        path = re.sub(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            '{id}', path, flags=re.IGNORECASE,
        )
        # Numeric IDs
        path = re.sub(r'/\d+(?=/|$)', '/{id}', path)
        return path


def generate_metrics_text() -> str:
    """
    Generate Prometheus-compatible metrics text output.
    Format: https://prometheus.io/docs/instrumenting/exposition_formats/
    """
    lines: list[str] = []

    # ── Request count ─────────────────────────────────────────────────────
    lines.append("# HELP cm_techmap_http_requests_total Total HTTP requests")
    lines.append("# TYPE cm_techmap_http_requests_total counter")
    for key, count in sorted(_request_count.items()):
        parts = key.rsplit(":", 2)
        if len(parts) == 3:
            method_path, status = ":".join(parts[:2]), parts[2]
            method, path = method_path.split(":", 1) if ":" in method_path else ("UNKNOWN", method_path)
        else:
            method, path, status = "UNKNOWN", key, "unknown"
        lines.append(
            f'cm_techmap_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {count}'
        )

    # ── Request latency ───────────────────────────────────────────────────
    lines.append("")
    lines.append("# HELP cm_techmap_http_request_duration_seconds HTTP request latency")
    lines.append("# TYPE cm_techmap_http_request_duration_seconds histogram")
    for label, bucket_counts in sorted(_latency_bucket_counts.items()):
        method, path = label.split(":", 1) if ":" in label else ("UNKNOWN", label)
        cumulative = 0
        for bucket in _BUCKETS:
            cumulative += bucket_counts.get(bucket, 0)
            lines.append(
                f'cm_techmap_http_request_duration_seconds_bucket{{method="{method}",path="{path}",le="{bucket}"}} {cumulative}'
            )
        total_count = _request_latency_count.get(label, 0)
        total_sum = _request_latency_sum.get(label, 0)
        lines.append(
            f'cm_techmap_http_request_duration_seconds_bucket{{method="{method}",path="{path}",le="+Inf"}} {total_count}'
        )
        lines.append(
            f'cm_techmap_http_request_duration_seconds_sum{{method="{method}",path="{path}"}} {total_sum:.6f}'
        )
        lines.append(
            f'cm_techmap_http_request_duration_seconds_count{{method="{method}",path="{path}"}} {total_count}'
        )

    # ── Active requests ───────────────────────────────────────────────────
    lines.append("")
    lines.append("# HELP cm_techmap_active_requests Current active HTTP requests")
    lines.append("# TYPE cm_techmap_active_requests gauge")
    lines.append(f"cm_techmap_active_requests {_active_requests}")

    # ── Total requests ────────────────────────────────────────────────────
    lines.append("")
    lines.append("# HELP cm_techmap_total_requests Total HTTP requests processed")
    lines.append("# TYPE cm_techmap_total_requests counter")
    lines.append(f"cm_techmap_total_requests {_total_requests}")

    # ── Error count ───────────────────────────────────────────────────────
    lines.append("")
    lines.append("# HELP cm_techmap_http_errors_total Total HTTP errors")
    lines.append("# TYPE cm_techmap_http_errors_total counter")
    for key, count in sorted(_error_count.items()):
        parts = key.rsplit(":", 1)
        if len(parts) == 2:
            label, status = parts
            method, path = label.split(":", 1) if ":" in label else ("UNKNOWN", label)
        else:
            method, path, status = "UNKNOWN", key, "unknown"
        lines.append(
            f'cm_techmap_http_errors_total{{method="{method}",path="{path}",status="{status}"}} {count}'
        )

    lines.append("")
    return "\n".join(lines)
