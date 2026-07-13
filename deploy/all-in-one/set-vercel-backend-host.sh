#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <backend-host>"
  echo "Example: $0 https://api.my-domain.com"
  exit 1
fi

BACKEND_HOST="${1%/}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
VERCEL_JSON="$ROOT_DIR/applications/cm-techmap-frontend/vercel.json"

if [[ ! -f "$VERCEL_JSON" ]]; then
  echo "vercel.json not found at $VERCEL_JSON" >&2
  exit 1
fi

# Replace destination host for api/auth/realms rewrites, regardless of previous value.
sed -E -i "s|\"destination\": \"https://[^\"]+/api/:path\*\"|\"destination\": \"${BACKEND_HOST}/api/:path*\"|g" "$VERCEL_JSON"
sed -E -i "s|\"destination\": \"https://[^\"]+/realms/:path\*\"|\"destination\": \"${BACKEND_HOST}/realms/:path*\"|g" "$VERCEL_JSON"
sed -E -i "s|\"destination\": \"https://[^\"]+/auth/:path\*\"|\"destination\": \"${BACKEND_HOST}/auth/:path*\"|g" "$VERCEL_JSON"

echo "Updated vercel rewrites to backend host: ${BACKEND_HOST}"
