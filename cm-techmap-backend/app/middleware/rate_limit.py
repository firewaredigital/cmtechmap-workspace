"""
CM TECHMAP — Rate Limiting Middleware
In-memory sliding-window rate limiter for sensitive endpoints (login, register).
Uses Redis when available; falls back to in-memory dict.
"""

import logging
import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("cm_techmap.rate_limit")

# ── Configuration ─────────────────────────────────────────────────────────────
# Endpoint → (max_requests, window_seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "/api/v1/auth/login": (10, 60),       # 10 attempts per minute
    "/api/v1/auth/register": (5, 300),     # 5 registrations per 5 minutes
    "/api/v1/auth/refresh": (30, 60),      # 30 refreshes per minute
    "/api/v1/uploads": (20, 60),           # 20 uploads per minute
}

# Global fallback for all other authenticated endpoints
DEFAULT_RATE_LIMIT = (120, 60)  # 120 requests per minute

# ── In-memory sliding window ─────────────────────────────────────────────────
_request_log: dict[str, list[float]] = defaultdict(list)
_CLEANUP_INTERVAL = 300  # Clean stale entries every 5 minutes
_last_cleanup = time.monotonic()


def _get_client_key(request: Request, path: str) -> str:
    """Build a rate limit key from client IP + path."""
    client_ip = request.headers.get("X-Real-IP") or (
        request.client.host if request.client else "unknown"
    )
    return f"{client_ip}:{path}"


def _cleanup_stale_entries() -> None:
    """Remove expired entries to prevent memory leaks."""
    global _last_cleanup
    now = time.monotonic()
    if now - _last_cleanup < _CLEANUP_INTERVAL:
        return
    _last_cleanup = now
    cutoff = now - 600  # Remove anything older than 10 minutes
    keys_to_delete = []
    for key, timestamps in _request_log.items():
        _request_log[key] = [t for t in timestamps if t > cutoff]
        if not _request_log[key]:
            keys_to_delete.append(key)
    for key in keys_to_delete:
        del _request_log[key]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Sliding-window rate limiter that protects sensitive endpoints.
    Returns HTTP 429 with Retry-After header when limit is exceeded.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        # Skip rate limiting for WebSocket connections (incompatible with BaseHTTPMiddleware)
        if path.startswith("/ws/"):
            return await call_next(request)

        # Only rate-limit POST/PUT/DELETE on configured paths
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return await call_next(request)

        # Find matching rate limit config
        max_requests, window = DEFAULT_RATE_LIMIT
        for endpoint, limits in RATE_LIMITS.items():
            if path.startswith(endpoint):
                max_requests, window = limits
                break

        # Only enforce stricter limits on configured endpoints
        if path not in RATE_LIMITS and path not in {p for p in RATE_LIMITS}:
            return await call_next(request)

        now = time.monotonic()
        key = _get_client_key(request, path)

        # Sliding window check
        timestamps = _request_log[key]
        cutoff = now - window
        # Remove expired timestamps
        _request_log[key] = [t for t in timestamps if t > cutoff]
        current_count = len(_request_log[key])

        if current_count >= max_requests:
            # Calculate retry-after
            oldest_in_window = min(_request_log[key]) if _request_log[key] else now
            retry_after = int(window - (now - oldest_in_window)) + 1

            logger.warning(
                f"Rate limit exceeded: {key} ({current_count}/{max_requests} "
                f"in {window}s window)"
            )

            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Muitas tentativas. Tente novamente mais tarde.",
                    "retry_after": retry_after,
                },
                headers={
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(now + retry_after)),
                },
            )

        # Record this request
        _request_log[key].append(now)

        # Periodic cleanup
        _cleanup_stale_entries()

        # Add rate limit headers to successful response
        response = await call_next(request)
        remaining = max(0, max_requests - len(_request_log[key]))
        response.headers["X-RateLimit-Limit"] = str(max_requests)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
