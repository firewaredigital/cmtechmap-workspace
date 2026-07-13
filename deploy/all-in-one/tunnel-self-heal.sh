#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env.single"
VERCEL_JSON="${ROOT_DIR}/applications/cm-techmap-frontend/vercel.json"
STATE_DIR="${SCRIPT_DIR}/.state"
LOG_FILE="${STATE_DIR}/tunnel-self-heal.log"
PID_FILE="${STATE_DIR}/tunnel-self-heal.pid"
STATE_FILE="${STATE_DIR}/tunnel-self-heal.state"

INTERVAL_SECONDS="60"
FAIL_THRESHOLD="3"
RUN_ONCE="false"
DAEMON_MODE="false"
RUN_LOOP_MODE="false"
AUTO_VERCEL_DEPLOY="false"
VERCEL_DEPLOY_COMMAND=""

usage() {
  cat <<'EOF'
CM TECHMAP tunnel self-heal daemon

Usage:
  ./tunnel-self-heal.sh [options]

Options:
  --interval <seconds>          Poll interval (default: 60).
  --fail-threshold <count>      Consecutive public-health failures before restart (default: 3).
  --run-once                    Run one cycle and exit.
  --daemon                      Start in background and return.
  --run-loop                    Internal loop mode (used by daemon launcher).
  --auto-vercel-deploy          Run Vercel deploy command when URL changes.
  --vercel-deploy-command <cmd> Custom command for redeploy.
  --help                        Show this help.

Behavior:
  1) Reads tunnel URL (fixed vars or cloudflared logs).
  2) Compares with PUBLIC_BACKEND_URL in .env.single.
  3) If changed, updates env + Keycloak external URL + frontend rewrites.
  4) Verifies public health endpoint.
  5) On repeated failures, restarts cloudflared and gateway.
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

ensure_state_dir() {
  mkdir -p "$STATE_DIR"
}

read_env_var() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 | sed "s/^${key}=//" || true)"
  echo "$value"
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

patch_vercel_rewrites() {
  local backend_url="$1"
  if [[ ! -f "$VERCEL_JSON" ]]; then
    echo "[WARN] vercel.json not found at ${VERCEL_JSON}" >&2
    return 0
  fi

  sed -E -i "s|\"destination\": \"https://[^\"]+/api/:path\*\"|\"destination\": \"${backend_url}/api/:path*\"|g" "$VERCEL_JSON"
  sed -E -i "s|\"destination\": \"https://[^\"]+/realms/:path\*\"|\"destination\": \"${backend_url}/realms/:path*\"|g" "$VERCEL_JSON"
  sed -E -i "s|\"destination\": \"https://[^\"]+/auth/:path\*\"|\"destination\": \"${backend_url}/auth/:path*\"|g" "$VERCEL_JSON"
}

extract_trycloudflare_from_logs() {
  local logs="$1"
  local found
  found="$(printf '%s\n' "$logs" | grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' | tail -n1 || true)"
  echo "$found"
}

discover_tunnel_url() {
  local fixed_public
  local fixed_hostname
  local logs
  local discovered
  local container_name

  fixed_public="$(strip_quotes "$(read_env_var "CLOUDFLARE_TUNNEL_PUBLIC_URL")")"
  if [[ -n "$fixed_public" ]]; then
    echo "$(ensure_url_scheme "$fixed_public")"
    return 0
  fi

  fixed_hostname="$(strip_quotes "$(read_env_var "CLOUDFLARE_TUNNEL_HOSTNAME")")"
  if [[ -n "$fixed_hostname" ]]; then
    echo "$(ensure_url_scheme "$fixed_hostname")"
    return 0
  fi

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

  return 1
}

write_state() {
  local key="$1"
  local value="$2"
  touch "$STATE_FILE"
  if grep -qE "^${key}=" "$STATE_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$STATE_FILE"
  else
    echo "${key}=${value}" >>"$STATE_FILE"
  fi
}

read_state() {
  local key="$1"
  if [[ ! -f "$STATE_FILE" ]]; then
    echo ""
    return 0
  fi
  grep -E "^${key}=" "$STATE_FILE" | tail -n1 | sed "s/^${key}=//" || true
}

wait_for_public_health() {
  local base_url="$1"
  if curl -fsS --max-time 5 "${base_url}/api/v1/health" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

maybe_deploy_vercel() {
  if [[ "$AUTO_VERCEL_DEPLOY" != "true" ]]; then
    return 0
  fi

  if [[ -z "$VERCEL_DEPLOY_COMMAND" ]]; then
    VERCEL_DEPLOY_COMMAND="cd \"${ROOT_DIR}/applications/cm-techmap-frontend\" && vercel deploy --prod"
  fi

  if ! command -v vercel >/dev/null 2>&1; then
    echo "[WARN] vercel CLI not found; skipping auto deploy."
    return 0
  fi

  echo "[INFO] Running Vercel deploy command..."
  bash -lc "$VERCEL_DEPLOY_COMMAND" || echo "[WARN] Vercel deploy command failed."
}

restart_tunnel_components() {
  echo "[WARN] Restarting tunnel components (cloudflared + gateway)."
  docker restart cm-techmap-cloudflared-quick >/dev/null 2>&1 || true
  docker restart cm-techmap-cloudflared-token >/dev/null 2>&1 || true
  docker restart cm-techmap-gateway >/dev/null 2>&1 || true
}

process_cycle() {
  ensure_state_dir

  local detected_url
  local current_url
  local fail_count

  detected_url="$(discover_tunnel_url || true)"
  detected_url="$(ensure_url_scheme "${detected_url:-}")"
  current_url="$(strip_quotes "$(read_env_var "PUBLIC_BACKEND_URL")")"
  current_url="${current_url%/}"

  if [[ -z "$detected_url" ]]; then
    echo "[WARN] Could not determine tunnel URL this cycle."
    fail_count="$(read_state "public_health_failures")"
    fail_count="${fail_count:-0}"
    fail_count="$((fail_count + 1))"
    write_state "public_health_failures" "$fail_count"
    if (( fail_count >= FAIL_THRESHOLD )); then
      restart_tunnel_components
      write_state "public_health_failures" "0"
    fi
    return 0
  fi

  if [[ "$detected_url" != "$current_url" ]]; then
    echo "[INFO] Tunnel URL changed: '${current_url:-<empty>}' -> '${detected_url}'"
    upsert_env_var "PUBLIC_BACKEND_URL" "$detected_url"
    upsert_env_var "KEYCLOAK_EXTERNAL_URL" "${detected_url}/auth"
    patch_vercel_rewrites "$detected_url"
    write_state "last_url" "$detected_url"
    maybe_deploy_vercel
  fi

  if wait_for_public_health "$detected_url"; then
    echo "[OK] Public health reachable at ${detected_url}/api/v1/health"
    write_state "public_health_failures" "0"
  else
    echo "[WARN] Public health failed at ${detected_url}/api/v1/health"
    fail_count="$(read_state "public_health_failures")"
    fail_count="${fail_count:-0}"
    fail_count="$((fail_count + 1))"
    write_state "public_health_failures" "$fail_count"
    if (( fail_count >= FAIL_THRESHOLD )); then
      restart_tunnel_components
      write_state "public_health_failures" "0"
    fi
  fi
}

start_daemon() {
  ensure_state_dir

  if [[ -f "$PID_FILE" ]]; then
    old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
      echo "Self-heal daemon already running with PID ${old_pid}."
      return 0
    fi
  fi

  local daemon_args=()

  daemon_args+=(--run-loop --interval "$INTERVAL_SECONDS" --fail-threshold "$FAIL_THRESHOLD")
  if [[ "$AUTO_VERCEL_DEPLOY" == "true" ]]; then
    daemon_args+=(--auto-vercel-deploy)
  fi
  if [[ -n "$VERCEL_DEPLOY_COMMAND" ]]; then
    daemon_args+=(--vercel-deploy-command "$VERCEL_DEPLOY_COMMAND")
  fi

  echo "Starting self-heal daemon..."
  nohup "$0" "${daemon_args[@]}" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  echo "Self-heal daemon started with PID $(cat "$PID_FILE")."
  echo "Log file: $LOG_FILE"
}

run_loop() {
  ensure_state_dir
  echo "$$" >"$PID_FILE"
  while true; do
    process_cycle
    if [[ "$RUN_ONCE" == "true" ]]; then
      break
    fi
    sleep "$INTERVAL_SECONDS"
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --interval)
      INTERVAL_SECONDS="${2:-}"
      shift 2
      ;;
    --fail-threshold)
      FAIL_THRESHOLD="${2:-}"
      shift 2
      ;;
    --run-once)
      RUN_ONCE="true"
      shift
      ;;
    --daemon)
      DAEMON_MODE="true"
      shift
      ;;
    --run-loop)
      RUN_LOOP_MODE="true"
      shift
      ;;
    --auto-vercel-deploy)
      AUTO_VERCEL_DEPLOY="true"
      shift
      ;;
    --vercel-deploy-command)
      VERCEL_DEPLOY_COMMAND="${2:-}"
      shift 2
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

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing env file: $ENV_FILE" >&2
  exit 1
fi

if [[ "$DAEMON_MODE" == "true" ]]; then
  start_daemon
  exit 0
fi

RUN_ONCE_VALUE="$RUN_ONCE"
if [[ "$RUN_LOOP_MODE" == "true" ]]; then
  RUN_ONCE="$RUN_ONCE_VALUE"
  run_loop
else
  process_cycle
fi
