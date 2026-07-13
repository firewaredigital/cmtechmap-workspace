#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8000}"
KEYCLOAK_URL="${2:-http://127.0.0.1:8080}"
MINIO_URL="${3:-http://127.0.0.1:9000}"
FLOWER_URL="${4:-http://127.0.0.1:5555}"
FLOWER_BASIC_AUTH="${FLOWER_BASIC_AUTH:-}"

check() {
  local name="$1"
  local url="$2"
  echo "[CHECK] ${name} -> ${url}"
  if curl -fsS "$url" >/dev/null; then
    echo "[OK] ${name}"
  else
    echo "[FAIL] ${name}" >&2
    exit 1
  fi
}

check_with_retry() {
  local name="$1"
  local url="$2"
  local retries="${3:-20}"
  local delay="${4:-3}"
  local i

  echo "[CHECK] ${name} -> ${url} (retries=${retries}, delay=${delay}s)"
  for i in $(seq 1 "$retries"); do
    if curl -fsS "$url" >/dev/null; then
      echo "[OK] ${name}"
      return 0
    fi
    sleep "$delay"
  done
  echo "[FAIL] ${name}" >&2
  exit 1
}

check "API health" "${BASE_URL}/api/v1/health"
check "API ready" "${BASE_URL}/api/v1/health/ready"
check "Metrics" "${BASE_URL}/metrics"
check_with_retry "Keycloak realm" "${KEYCLOAK_URL}/realms/master/.well-known/openid-configuration" 30 3
check "MinIO live" "${MINIO_URL}/minio/health/live"

if [[ -n "$FLOWER_BASIC_AUTH" ]]; then
  echo "[CHECK] Flower (with basic auth) -> ${FLOWER_URL}"
  if curl -fsS -u "$FLOWER_BASIC_AUTH" "$FLOWER_URL" >/dev/null; then
    echo "[OK] Flower"
  else
    echo "[FAIL] Flower" >&2
    exit 1
  fi
else
  echo "[CHECK] Flower (no credentials provided) -> ${FLOWER_URL}"
  status_code="$(curl -s -o /dev/null -w '%{http_code}' "$FLOWER_URL" || true)"
  if [[ "$status_code" == "200" || "$status_code" == "401" ]]; then
    echo "[OK] Flower (status=${status_code})"
  else
    echo "[FAIL] Flower (status=${status_code})" >&2
    exit 1
  fi
fi

echo "All smoke checks passed."
