#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
CM TECHMAP zero-touch bootstrap

Usage:
  ./zero-touch-bootstrap.sh [options]

Options:
  --repo-url <url>             Git repository URL (required).
  --branch <name>              Git branch/tag to checkout (default: main).
  --workspace-dir <path>       Target workspace directory (default: $HOME/cmtechmap-workspace).
  --frontend-url <url>         Frontend URL used for CORS bootstrap.
  --with-tunnel                Enable tunnel mode (default).
  --without-tunnel             Disable tunnel mode.
  --quick-tunnel               Force quick tunnel mode (default).
  --tunnel-token <token>       Cloudflare tunnel token (stable hostname mode).
  --tunnel-hostname <host>     Cloudflare tunnel hostname (without scheme).
  --tunnel-public-url <url>    Fixed public URL for tunnel mode.
  --skip-frontend-patch        Do not patch frontend vercel rewrites.
  --skip-smoke                 Skip smoke checks.
  --help                       Show this help.

Examples:
  ./zero-touch-bootstrap.sh \
    --repo-url https://github.com/your-org/your-repo.git \
    --frontend-url https://your-frontend.vercel.app

  ./zero-touch-bootstrap.sh \
    --repo-url https://github.com/your-org/your-repo.git \
    --frontend-url https://your-frontend.vercel.app \
    --tunnel-token <SECRET> \
    --tunnel-hostname api.example.com
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

maybe_sudo() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "This step requires root privileges and sudo is not available." >&2
    exit 1
  fi
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return 0
  fi

  echo "Installing Docker (engine + compose plugin)..."
  require_cmd curl
  maybe_sudo sh -c "curl -fsSL https://get.docker.com | sh"
  maybe_sudo systemctl enable docker >/dev/null 2>&1 || true
  maybe_sudo systemctl start docker >/dev/null 2>&1 || true
}

install_base_packages() {
  require_cmd uname

  if command -v apt-get >/dev/null 2>&1; then
    echo "Installing base packages via apt..."
    maybe_sudo apt-get update -y
    maybe_sudo DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git iproute2 procps
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    echo "Installing base packages via dnf..."
    maybe_sudo dnf install -y ca-certificates curl git iproute procps-ng
    return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    echo "Installing base packages via yum..."
    maybe_sudo yum install -y ca-certificates curl git iproute procps-ng
    return 0
  fi

  echo "Unsupported package manager. Install curl/git/docker manually and retry." >&2
  exit 1
}

normalize_url() {
  local value="$1"
  if [[ -z "$value" ]]; then
    echo ""
    return 0
  fi
  if [[ "$value" =~ ^https?:// ]]; then
    echo "${value%/}"
  else
    echo "https://${value%/}"
  fi
}

upsert_env_var() {
  local env_file="$1"
  local key="$2"
  local value="$3"
  if grep -qE "^${key}=" "$env_file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$env_file"
  else
    echo "${key}=${value}" >>"$env_file"
  fi
}

gen_secret() {
  require_cmd openssl
  openssl rand -hex 24
}

ensure_secret_if_placeholder() {
  local env_file="$1"
  local key="$2"
  local current
  current="$(grep -E "^${key}=" "$env_file" | tail -n1 | cut -d= -f2- || true)"
  if [[ -z "$current" || "$current" == CHANGE-ME* ]]; then
    upsert_env_var "$env_file" "$key" "$(gen_secret)"
  fi
}

ensure_flower_auth_if_placeholder() {
  local env_file="$1"
  local current
  current="$(grep -E '^FLOWER_BASIC_AUTH=' "$env_file" | tail -n1 | cut -d= -f2- || true)"
  if [[ -z "$current" || "$current" == CHANGE-ME* || "$current" != *:* ]]; then
    upsert_env_var "$env_file" "FLOWER_BASIC_AUTH" "admin:$(gen_secret)"
  fi
}

REPO_URL=""
BRANCH="main"
WORKSPACE_DIR="${HOME}/cmtechmap-workspace"
FRONTEND_URL=""
WITH_TUNNEL=true
QUICK_TUNNEL=true
TUNNEL_TOKEN=""
TUNNEL_HOSTNAME=""
TUNNEL_PUBLIC_URL=""
SKIP_FRONTEND_PATCH=false
SKIP_SMOKE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)
      REPO_URL="${2:-}"
      shift 2
      ;;
    --branch)
      BRANCH="${2:-}"
      shift 2
      ;;
    --workspace-dir)
      WORKSPACE_DIR="${2:-}"
      shift 2
      ;;
    --frontend-url)
      FRONTEND_URL="${2:-}"
      shift 2
      ;;
    --with-tunnel)
      WITH_TUNNEL=true
      shift
      ;;
    --without-tunnel)
      WITH_TUNNEL=false
      shift
      ;;
    --quick-tunnel)
      QUICK_TUNNEL=true
      shift
      ;;
    --tunnel-token)
      TUNNEL_TOKEN="${2:-}"
      shift 2
      ;;
    --tunnel-hostname)
      TUNNEL_HOSTNAME="${2:-}"
      shift 2
      ;;
    --tunnel-public-url)
      TUNNEL_PUBLIC_URL="${2:-}"
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

if [[ -z "$REPO_URL" ]]; then
  echo "--repo-url is required." >&2
  usage
  exit 1
fi

FRONTEND_URL="$(normalize_url "$FRONTEND_URL")"
TUNNEL_PUBLIC_URL="$(normalize_url "$TUNNEL_PUBLIC_URL")"

install_base_packages
install_docker
require_cmd git
require_cmd docker
require_cmd curl
require_cmd openssl

if [[ ! -d "$WORKSPACE_DIR/.git" ]]; then
  echo "Cloning repository into $WORKSPACE_DIR ..."
  rm -rf "$WORKSPACE_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$WORKSPACE_DIR"
else
  echo "Repository already exists at $WORKSPACE_DIR. Updating..."
  git -C "$WORKSPACE_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$WORKSPACE_DIR" checkout "$BRANCH"
  if ! git -C "$WORKSPACE_DIR" pull --ff-only origin "$BRANCH"; then
    echo "Could not fast-forward existing repository at $WORKSPACE_DIR." >&2
    echo "Use a clean workspace-dir or resolve local changes and rerun." >&2
    exit 1
  fi
fi

AGENT_DIR="$WORKSPACE_DIR/applications/deploy/all-in-one"
ENV_FILE="$AGENT_DIR/.env.single"
ENV_EXAMPLE="$AGENT_DIR/.env.single.example"

if [[ ! -d "$AGENT_DIR" ]]; then
  echo "Install agent directory not found: $AGENT_DIR" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
fi

ensure_secret_if_placeholder "$ENV_FILE" "APP_SECRET_KEY"
ensure_secret_if_placeholder "$ENV_FILE" "POSTGRES_PASSWORD"
ensure_secret_if_placeholder "$ENV_FILE" "MINIO_ROOT_PASSWORD"
ensure_secret_if_placeholder "$ENV_FILE" "KEYCLOAK_ADMIN_PASSWORD"
ensure_secret_if_placeholder "$ENV_FILE" "KEYCLOAK_CLIENT_SECRET"
ensure_flower_auth_if_placeholder "$ENV_FILE"

if [[ -n "$FRONTEND_URL" ]]; then
  upsert_env_var "$ENV_FILE" "APP_CORS_ORIGINS" "$FRONTEND_URL"
  upsert_env_var "$ENV_FILE" "FRONTEND_PUBLIC_URL" "$FRONTEND_URL"
fi

if [[ -n "$TUNNEL_TOKEN" ]]; then
  upsert_env_var "$ENV_FILE" "CLOUDFLARE_TUNNEL_TOKEN" "$TUNNEL_TOKEN"
  QUICK_TUNNEL=false
fi

if [[ "$QUICK_TUNNEL" == "true" ]]; then
  upsert_env_var "$ENV_FILE" "CLOUDFLARE_TUNNEL_QUICK" "true"
else
  upsert_env_var "$ENV_FILE" "CLOUDFLARE_TUNNEL_QUICK" "false"
fi

if [[ -n "$TUNNEL_HOSTNAME" ]]; then
  upsert_env_var "$ENV_FILE" "CLOUDFLARE_TUNNEL_HOSTNAME" "$TUNNEL_HOSTNAME"
fi

if [[ -n "$TUNNEL_PUBLIC_URL" ]]; then
  upsert_env_var "$ENV_FILE" "CLOUDFLARE_TUNNEL_PUBLIC_URL" "$TUNNEL_PUBLIC_URL"
fi

INSTALL_ARGS=()
if [[ "$WITH_TUNNEL" == "true" ]]; then
  INSTALL_ARGS+=(--with-tunnel)
fi
if [[ -n "$FRONTEND_URL" ]]; then
  INSTALL_ARGS+=(--frontend-url "$FRONTEND_URL")
fi
if [[ "$SKIP_FRONTEND_PATCH" == "true" ]]; then
  INSTALL_ARGS+=(--skip-frontend-patch)
fi
if [[ "$SKIP_SMOKE" == "true" ]]; then
  INSTALL_ARGS+=(--skip-smoke)
fi

echo "Running install-agent with args: ${INSTALL_ARGS[*]:-(none)}"
cd "$AGENT_DIR"
chmod +x ./install-agent.sh ./smoke-check.sh ./tunnel-self-heal.sh ./tunnel-self-heal-stop.sh
./install-agent.sh "${INSTALL_ARGS[@]}"
