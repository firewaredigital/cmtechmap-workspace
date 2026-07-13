#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.single.yml"
ENV_FILE="${SCRIPT_DIR}/.env.single"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.single.example"
VERCEL_JSON="${ROOT_DIR}/applications/cm-techmap-frontend/vercel.json"
SELF_HEAL_SCRIPT="${SCRIPT_DIR}/tunnel-self-heal.sh"

usage() {
  cat <<'EOF'
CM TECHMAP install agent

Usage:
  ./install-agent.sh [options]

Options:
  --with-tunnel                 Enable gateway + cloudflared profile.
  --public-backend-url <url>    Force public URL (overrides auto-detection).
  --public-backend-port <port>  Preferred host port for backend mapping (default: 8000).
  --tunnel-timeout <seconds>    Wait timeout for tunnel URL discovery (default: 90).
  --enable-self-heal            Start tunnel self-heal daemon after deploy (tunnel mode only).
  --disable-self-heal           Disable self-heal daemon even in tunnel mode.
  --self-heal-interval <sec>    Poll interval for self-heal daemon (default: 60).
  --self-heal-fail-threshold <n> Consecutive failures before tunnel restart (default: 3).
  --self-heal-auto-vercel-deploy Trigger Vercel redeploy when tunnel URL changes.
  --self-heal-vercel-command <cmd> Custom deploy command for self-heal mode.
  --frontend-url <url>          Sets APP_CORS_ORIGINS in .env.single.
  --skip-frontend-patch         Do not modify vercel.json.
  --skip-smoke                  Skip smoke checks.
  --help                        Show this help.

Behavior:
  1) Validates host dependencies.
  2) Creates .env.single from template if missing.
  3) Resolves free host ports if defaults are occupied.
  4) Starts local runtime stack.
  4) Detects and persists PUBLIC_BACKEND_URL automatically.
  5) Runs smoke checks.
  6) Optionally patches frontend rewrites to the public backend URL.
  7) Optionally starts self-heal daemon for continuous tunnel repair.
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

declare -A ASSIGNED_HOST_PORTS
RESOLVED_PORT=""

is_port_free() {
  local port="$1"
  if ss -ltn "( sport = :${port} )" | grep -q ":${port}"; then
    return 1
  fi
  return 0
}

is_port_already_assigned() {
  local port="$1"
  [[ -n "${ASSIGNED_HOST_PORTS[$port]:-}" ]]
}

mark_port_assigned() {
  local port="$1"
  ASSIGNED_HOST_PORTS[$port]=1
}

next_free_port() {
  local start_port="$1"
  local p="$start_port"
  while [[ "$p" -le 65535 ]]; do
    if is_port_free "$p" && ! is_port_already_assigned "$p"; then
      echo "$p"
      return 0
    fi
    p=$((p + 1))
  done
  echo "No free port found starting from ${start_port}" >&2
  return 1
}

resolve_mapped_port() {
  local env_key="$1"
  local preferred="$2"
  local current
  local free

  current="$(strip_quotes "$(read_env_var "$env_key")")"
  if [[ -z "$current" ]]; then
    current="$preferred"
  fi

  if is_port_free "$current" && ! is_port_already_assigned "$current"; then
    mark_port_assigned "$current"
    RESOLVED_PORT="$current"
    return 0
  fi

  free="$(next_free_port "$preferred")"
  mark_port_assigned "$free"
  RESOLVED_PORT="$free"
}

upsert_env_var() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >>"$ENV_FILE"
  fi
}

patch_vercel_rewrites() {
  local backend_url="$1"
  if [[ ! -f "$VERCEL_JSON" ]]; then
    echo "Frontend vercel.json not found: $VERCEL_JSON" >&2
    return 1
  fi

  sed -E -i "s|\"destination\": \"https://[^\"]+/api/:path\*\"|\"destination\": \"${backend_url}/api/:path*\"|g" "$VERCEL_JSON"
  sed -E -i "s|\"destination\": \"https://[^\"]+/realms/:path\*\"|\"destination\": \"${backend_url}/realms/:path*\"|g" "$VERCEL_JSON"
  sed -E -i "s|\"destination\": \"https://[^\"]+/auth/:path\*\"|\"destination\": \"${backend_url}/auth/:path*\"|g" "$VERCEL_JSON"
  echo "Patched frontend rewrites to ${backend_url}"
}

read_env_var() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | sed "s/^${key}=//" || true)"
  echo "$value"
}

wait_for_http() {
  local url="$1"
  local timeout_seconds="$2"
  local started_at
  local now

  started_at="$(date +%s)"
  while true; do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - started_at >= timeout_seconds )); then
      return 1
    fi
    sleep 2
  done
}

strip_quotes() {
  local v="$1"
  v="${v%\"}"
  v="${v#\"}"
  v="${v%\'}"
  v="${v#\'}"
  echo "$v"
}

ensure_url_scheme() {
  local maybe_url="$1"
  if [[ -z "$maybe_url" ]]; then
    echo ""
    return 0
  fi
  if [[ "$maybe_url" =~ ^https?:// ]]; then
    echo "${maybe_url%/}"
    return 0
  fi
  echo "https://${maybe_url%/}"
}

extract_trycloudflare_from_logs() {
  local logs="$1"
  local found
  found="$(printf '%s\n' "$logs" | grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | tail -n1 || true)"
  echo "$found"
}

discover_tunnel_url_from_cloudflared() {
  local timeout_seconds="$1"
  local start
  local now
  local logs
  local discovered
  local container_name

  start="$(date +%s)"
  while true; do
    container_name="cm-techmap-cloudflared-quick"
    if ! docker ps -a --format '{{.Names}}' | grep -q '^cm-techmap-cloudflared-quick$'; then
      container_name="cm-techmap-cloudflared-token"
    fi
    logs="$(docker logs "$container_name" 2>&1 || true)"
    discovered="$(extract_trycloudflare_from_logs "$logs")"
    if [[ -n "$discovered" ]]; then
      echo "${discovered%/}"
      return 0
    fi

    now="$(date +%s)"
    if (( now - start >= timeout_seconds )); then
      return 1
    fi
    sleep 2
  done
}

fetch_public_ipv4() {
  local ip
  ip="$(curl -4fsS --max-time 5 https://api.ipify.org || true)"
  if [[ -n "$ip" ]]; then
    echo "$ip"
    return 0
  fi
  ip="$(curl -4fsS --max-time 5 https://ifconfig.me || true)"
  if [[ -n "$ip" ]]; then
    echo "$ip"
    return 0
  fi
  return 1
}

fetch_local_ipv4() {
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  if [[ -n "$ip" ]]; then
    echo "$ip"
    return 0
  fi
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++){if($i=="src"){print $(i+1); exit}}}' || true)"
  if [[ -n "$ip" ]]; then
    echo "$ip"
    return 0
  fi
  return 1
}

detect_backend_url_local_mode() {
  local forced_url="$1"
  local fallback_port="$2"
  local from_env
  local pub_ip
  local local_ip

  if [[ -n "$forced_url" ]]; then
    echo "${forced_url%/}"
    return 0
  fi

  from_env="$(strip_quotes "$(read_env_var "PUBLIC_BACKEND_URL")")"
  if [[ -n "$from_env" ]]; then
    echo "${from_env%/}"
    return 0
  fi

  if pub_ip="$(fetch_public_ipv4)"; then
    echo "http://${pub_ip}:${fallback_port}"
    return 0
  fi

  if local_ip="$(fetch_local_ipv4)"; then
    echo "http://${local_ip}:${fallback_port}"
    return 0
  fi

  echo "http://127.0.0.1:${fallback_port}"
  return 0
}

detect_backend_url_tunnel_mode() {
  local forced_url="$1"
  local timeout_seconds="$2"
  local tunnel_public
  local tunnel_hostname
  local discovered

  if [[ -n "$forced_url" ]]; then
    echo "$(ensure_url_scheme "${forced_url%/}")"
    return 0
  fi

  tunnel_public="$(strip_quotes "$(read_env_var "CLOUDFLARE_TUNNEL_PUBLIC_URL")")"
  if [[ -n "$tunnel_public" ]]; then
    echo "$(ensure_url_scheme "${tunnel_public%/}")"
    return 0
  fi

  tunnel_hostname="$(strip_quotes "$(read_env_var "CLOUDFLARE_TUNNEL_HOSTNAME")")"
  if [[ -n "$tunnel_hostname" ]]; then
    echo "$(ensure_url_scheme "${tunnel_hostname%/}")"
    return 0
  fi

  if discovered="$(discover_tunnel_url_from_cloudflared "$timeout_seconds")"; then
    echo "${discovered%/}"
    return 0
  fi

  echo "Could not auto-detect tunnel public URL from cloudflared logs." >&2
  echo "Set CLOUDFLARE_TUNNEL_PUBLIC_URL or CLOUDFLARE_TUNNEL_HOSTNAME in .env.single." >&2
  return 1
}

WITH_TUNNEL=false
PUBLIC_BACKEND_URL=""
PUBLIC_BACKEND_PORT="8000"
TUNNEL_TIMEOUT="90"
HOST_BACKEND_PORT=""
HOST_KEYCLOAK_PORT=""
HOST_MINIO_API_PORT=""
HOST_MINIO_CONSOLE_PORT=""
HOST_FLOWER_PORT=""
HOST_GATEWAY_PORT=""
FRONTEND_URL=""
SKIP_FRONTEND_PATCH=false
SKIP_SMOKE=false
ENABLE_SELF_HEAL=false
SELF_HEAL_EXPLICIT_SET=false
SELF_HEAL_INTERVAL="60"
SELF_HEAL_FAIL_THRESHOLD="3"
SELF_HEAL_AUTO_VERCEL_DEPLOY=false
SELF_HEAL_VERCEL_COMMAND=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-tunnel)
      WITH_TUNNEL=true
      shift
      ;;
    --public-backend-url)
      PUBLIC_BACKEND_URL="${2:-}"
      shift 2
      ;;
    --public-backend-port)
      PUBLIC_BACKEND_PORT="${2:-}"
      shift 2
      ;;
    --tunnel-timeout)
      TUNNEL_TIMEOUT="${2:-}"
      shift 2
      ;;
    --enable-self-heal)
      ENABLE_SELF_HEAL=true
      SELF_HEAL_EXPLICIT_SET=true
      shift
      ;;
    --disable-self-heal)
      ENABLE_SELF_HEAL=false
      SELF_HEAL_EXPLICIT_SET=true
      shift
      ;;
    --self-heal-interval)
      SELF_HEAL_INTERVAL="${2:-}"
      shift 2
      ;;
    --self-heal-fail-threshold)
      SELF_HEAL_FAIL_THRESHOLD="${2:-}"
      shift 2
      ;;
    --self-heal-auto-vercel-deploy)
      SELF_HEAL_AUTO_VERCEL_DEPLOY=true
      shift
      ;;
    --self-heal-vercel-command)
      SELF_HEAL_VERCEL_COMMAND="${2:-}"
      shift 2
      ;;
    --frontend-url)
      FRONTEND_URL="${2:-}"
      shift 2
      ;;
    --skip-frontend-patch)
      SKIP_FRONTEND_PATCH=true
      shift
      ;;
    --skip-smoke)
      SKIP_SMOKE=true
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

require_cmd docker
require_cmd curl
require_cmd ss

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin is required." >&2
  exit 1
fi

if [[ "$ENABLE_SELF_HEAL" == "true" ]]; then
  if [[ ! -x "$SELF_HEAL_SCRIPT" ]]; then
    echo "Self-heal script not found or not executable: $SELF_HEAL_SCRIPT" >&2
    exit 1
  fi
fi

cd "$SCRIPT_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  echo "Created .env.single from template."
fi

env_self_heal_enabled="$(strip_quotes "$(read_env_var "SELF_HEAL_ENABLED")")"
env_self_heal_interval="$(strip_quotes "$(read_env_var "SELF_HEAL_INTERVAL")")"
env_self_heal_fail_threshold="$(strip_quotes "$(read_env_var "SELF_HEAL_FAIL_THRESHOLD")")"
env_self_heal_auto_deploy="$(strip_quotes "$(read_env_var "SELF_HEAL_AUTO_VERCEL_DEPLOY")")"
env_self_heal_vercel_cmd="$(strip_quotes "$(read_env_var "SELF_HEAL_VERCEL_COMMAND")")"

if [[ -n "$env_self_heal_interval" ]]; then
  SELF_HEAL_INTERVAL="$env_self_heal_interval"
fi
if [[ -n "$env_self_heal_fail_threshold" ]]; then
  SELF_HEAL_FAIL_THRESHOLD="$env_self_heal_fail_threshold"
fi
if [[ "$env_self_heal_auto_deploy" == "true" ]]; then
  SELF_HEAL_AUTO_VERCEL_DEPLOY=true
fi
if [[ -n "$env_self_heal_vercel_cmd" ]]; then
  SELF_HEAL_VERCEL_COMMAND="$env_self_heal_vercel_cmd"
fi

if [[ "$SELF_HEAL_EXPLICIT_SET" != "true" ]]; then
  if [[ "$WITH_TUNNEL" == "true" ]]; then
    ENABLE_SELF_HEAL=true
  else
    ENABLE_SELF_HEAL=false
  fi
fi

resolve_mapped_port "HOST_BACKEND_PORT" "$PUBLIC_BACKEND_PORT"
HOST_BACKEND_PORT="$RESOLVED_PORT"
resolve_mapped_port "HOST_KEYCLOAK_PORT" "8080"
HOST_KEYCLOAK_PORT="$RESOLVED_PORT"
resolve_mapped_port "HOST_MINIO_API_PORT" "9000"
HOST_MINIO_API_PORT="$RESOLVED_PORT"
resolve_mapped_port "HOST_MINIO_CONSOLE_PORT" "9001"
HOST_MINIO_CONSOLE_PORT="$RESOLVED_PORT"
resolve_mapped_port "HOST_FLOWER_PORT" "5555"
HOST_FLOWER_PORT="$RESOLVED_PORT"
resolve_mapped_port "HOST_GATEWAY_PORT" "8088"
HOST_GATEWAY_PORT="$RESOLVED_PORT"

upsert_env_var "HOST_BACKEND_PORT" "$HOST_BACKEND_PORT"
upsert_env_var "HOST_KEYCLOAK_PORT" "$HOST_KEYCLOAK_PORT"
upsert_env_var "HOST_MINIO_API_PORT" "$HOST_MINIO_API_PORT"
upsert_env_var "HOST_MINIO_CONSOLE_PORT" "$HOST_MINIO_CONSOLE_PORT"
upsert_env_var "HOST_FLOWER_PORT" "$HOST_FLOWER_PORT"
upsert_env_var "HOST_GATEWAY_PORT" "$HOST_GATEWAY_PORT"

if [[ -n "$FRONTEND_URL" ]]; then
  upsert_env_var "APP_CORS_ORIGINS" "$FRONTEND_URL"
  upsert_env_var "FRONTEND_PUBLIC_URL" "$FRONTEND_URL"
fi

COMPOSE_ARGS=( -f "$COMPOSE_FILE" --env-file "$ENV_FILE" )

if [[ "$WITH_TUNNEL" == "true" ]]; then
  token_line="$(grep -E '^CLOUDFLARE_TUNNEL_TOKEN=' "$ENV_FILE" || true)"
  token_value="${token_line#CLOUDFLARE_TUNNEL_TOKEN=}"
  quick_line="$(grep -E '^CLOUDFLARE_TUNNEL_QUICK=' "$ENV_FILE" || true)"
  quick_value="${quick_line#CLOUDFLARE_TUNNEL_QUICK=}"
  quick_value="$(strip_quotes "$quick_value")"
  if [[ -z "$token_value" && "$quick_value" != "true" ]]; then
    echo "Tunnel mode requires either CLOUDFLARE_TUNNEL_TOKEN or CLOUDFLARE_TUNNEL_QUICK=true in .env.single." >&2
    exit 1
  fi

  echo "Starting stack with tunnel profile..."
  if [[ -n "$token_value" && "$quick_value" != "true" ]]; then
    docker compose "${COMPOSE_ARGS[@]}" --profile tunnel-token up -d --build --remove-orphans
  else
    docker compose "${COMPOSE_ARGS[@]}" --profile tunnel-quick up -d --build --remove-orphans
  fi

  PUBLIC_BACKEND_URL="$(detect_backend_url_tunnel_mode "$PUBLIC_BACKEND_URL" "$TUNNEL_TIMEOUT")"
  PUBLIC_BACKEND_URL="$(ensure_url_scheme "$PUBLIC_BACKEND_URL")"
else
  echo "Starting stack without tunnel profile..."
  docker compose "${COMPOSE_ARGS[@]}" up -d --build --remove-orphans

  PUBLIC_BACKEND_URL="$(detect_backend_url_local_mode "$PUBLIC_BACKEND_URL" "$HOST_BACKEND_PORT")"
fi

if [[ "$ENABLE_SELF_HEAL" == "true" ]]; then
  if [[ "$WITH_TUNNEL" != "true" ]]; then
    echo "--enable-self-heal requires --with-tunnel." >&2
    exit 1
  fi

  SELF_HEAL_ARGS=(
    --daemon
    --interval "$SELF_HEAL_INTERVAL"
    --fail-threshold "$SELF_HEAL_FAIL_THRESHOLD"
  )

  if [[ "$SELF_HEAL_AUTO_VERCEL_DEPLOY" == "true" ]]; then
    SELF_HEAL_ARGS+=(--auto-vercel-deploy)
  fi

  if [[ -n "$SELF_HEAL_VERCEL_COMMAND" ]]; then
    SELF_HEAL_ARGS+=(--vercel-deploy-command "$SELF_HEAL_VERCEL_COMMAND")
  fi

  echo "Starting tunnel self-heal daemon..."
  "$SELF_HEAL_SCRIPT" "${SELF_HEAL_ARGS[@]}"
fi

upsert_env_var "SELF_HEAL_ENABLED" "${ENABLE_SELF_HEAL}"
upsert_env_var "SELF_HEAL_INTERVAL" "${SELF_HEAL_INTERVAL}"
upsert_env_var "SELF_HEAL_FAIL_THRESHOLD" "${SELF_HEAL_FAIL_THRESHOLD}"
upsert_env_var "SELF_HEAL_AUTO_VERCEL_DEPLOY" "${SELF_HEAL_AUTO_VERCEL_DEPLOY}"
upsert_env_var "SELF_HEAL_VERCEL_COMMAND" "${SELF_HEAL_VERCEL_COMMAND}"

echo "Resolved PUBLIC_BACKEND_URL=${PUBLIC_BACKEND_URL}"
upsert_env_var "PUBLIC_BACKEND_URL" "$PUBLIC_BACKEND_URL"
upsert_env_var "KEYCLOAK_EXTERNAL_URL" "$PUBLIC_BACKEND_URL/auth"

if [[ "$WITH_TUNNEL" == "true" ]]; then
  if ! wait_for_http "${PUBLIC_BACKEND_URL}/api/v1/health" "$TUNNEL_TIMEOUT"; then
    echo "Tunnel endpoint did not become healthy in ${TUNNEL_TIMEOUT}s: ${PUBLIC_BACKEND_URL}/api/v1/health" >&2
  fi
fi

if [[ "$SKIP_SMOKE" != "true" ]]; then
  echo "Running smoke checks..."
  ./smoke-check.sh \
    "http://127.0.0.1:${HOST_BACKEND_PORT}" \
    "http://127.0.0.1:${HOST_KEYCLOAK_PORT}" \
    "http://127.0.0.1:${HOST_MINIO_API_PORT}" \
    "http://127.0.0.1:${HOST_FLOWER_PORT}"
fi

if [[ "$SKIP_FRONTEND_PATCH" != "true" ]]; then
  backend_url="$PUBLIC_BACKEND_URL"
  if [[ -n "$backend_url" ]]; then
    patch_vercel_rewrites "$backend_url"
  else
    echo "PUBLIC_BACKEND_URL is empty. Skipping vercel.json patch."
  fi
fi

echo
echo "Install agent completed."
echo "Local API: http://127.0.0.1:${HOST_BACKEND_PORT}"
echo "Local Keycloak: http://127.0.0.1:${HOST_KEYCLOAK_PORT}"
echo "Local MinIO API: http://127.0.0.1:${HOST_MINIO_API_PORT}"
echo "Local MinIO Console: http://127.0.0.1:${HOST_MINIO_CONSOLE_PORT}"
echo "Local Flower: http://127.0.0.1:${HOST_FLOWER_PORT}"
echo "Gateway (tunnel profile): http://127.0.0.1:${HOST_GATEWAY_PORT}"
echo "Public Backend URL: ${PUBLIC_BACKEND_URL}"
if [[ "$WITH_TUNNEL" == "true" ]]; then
  echo "Tunnel profile enabled."
fi
