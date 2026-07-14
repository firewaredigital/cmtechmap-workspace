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
  --create-initial-user        Create first application user automatically.
  --initial-user-name <name>   Full name for initial user.
  --initial-user-email <mail>  Email for initial user.
  --initial-user-username <u>  Username/login for initial user.
  --initial-user-password <p>  Initial password for initial user.
  --initial-user-admin         Grant admin privileges to initial user.
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

read_env_var() {
  local env_file="$1"
  local key="$2"
  local value
  value="$(grep -E "^${key}=" "$env_file" | tail -n1 | sed "s/^${key}=//" || true)"
  echo "$value"
}

strip_quotes() {
  local v="$1"
  v="${v%\"}"
  v="${v#\"}"
  v="${v%\'}"
  v="${v#\'}"
  echo "$v"
}

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/ }"
  echo "$value"
}

extract_json_string() {
  local json="$1"
  local key="$2"
  printf '%s' "$json" | sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" | head -n1
}

keycloak_request() {
  local method="$1"
  local url="$2"
  local token="$3"
  local data="${4:-}"

  if [[ -n "$data" ]]; then
    curl -sS -X "$method" "$url" -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" -d "$data"
  else
    curl -sS -X "$method" "$url" -H "Authorization: Bearer ${token}"
  fi
}

keycloak_request_status() {
  local method="$1"
  local url="$2"
  local token="$3"
  local data="${4:-}"

  if [[ -n "$data" ]]; then
    curl -sS -o /tmp/cmtechmap-keycloak-response.json -w "%{http_code}" -X "$method" "$url" -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" -d "$data"
  else
    curl -sS -o /tmp/cmtechmap-keycloak-response.json -w "%{http_code}" -X "$method" "$url" -H "Authorization: Bearer ${token}"
  fi
}

resolve_keycloak_base_and_token() {
  local host_port="$1"
  local admin_user="$2"
  local admin_password="$3"

  local base
  local response
  local token
  for base in "http://127.0.0.1:${host_port}" "http://127.0.0.1:${host_port}/auth"; do
    response="$(curl -sS --max-time 10 -X POST "${base}/realms/master/protocol/openid-connect/token" \
      -H "Content-Type: application/x-www-form-urlencoded" \
      --data-urlencode "grant_type=password" \
      --data-urlencode "client_id=admin-cli" \
      --data-urlencode "username=${admin_user}" \
      --data-urlencode "password=${admin_password}" || true)"
    token="$(extract_json_string "$response" "access_token")"
    if [[ -n "$token" ]]; then
      echo "${base}|${token}"
      return 0
    fi
  done

  return 1
}

create_initial_keycloak_user() {
  local env_file="$1"
  local name="$2"
  local email="$3"
  local username="$4"
  local password="$5"
  local is_admin="$6"

  local host_keycloak_port
  local keycloak_admin_username
  local keycloak_admin_password
  local keycloak_realm
  local base_and_token
  local keycloak_base
  local keycloak_token
  local users_json
  local user_id
  local escaped_name
  local escaped_email
  local escaped_username
  local escaped_password
  local create_payload
  local status
  local clients_json
  local realm_mgmt_client_id
  local role_json

  host_keycloak_port="$(strip_quotes "$(read_env_var "$env_file" "HOST_KEYCLOAK_PORT")")"
  keycloak_admin_username="$(strip_quotes "$(read_env_var "$env_file" "KEYCLOAK_ADMIN_USERNAME")")"
  keycloak_admin_password="$(strip_quotes "$(read_env_var "$env_file" "KEYCLOAK_ADMIN_PASSWORD")")"
  keycloak_realm="$(strip_quotes "$(read_env_var "$env_file" "KEYCLOAK_REALM")")"

  if [[ -z "$host_keycloak_port" ]]; then
    host_keycloak_port="8080"
  fi
  if [[ -z "$keycloak_admin_username" || -z "$keycloak_admin_password" || -z "$keycloak_realm" ]]; then
    echo "Nao foi possivel carregar credenciais do Keycloak para criar usuario inicial." >&2
    return 1
  fi

  echo "Configurando usuario inicial no Keycloak..."
  if ! base_and_token="$(resolve_keycloak_base_and_token "$host_keycloak_port" "$keycloak_admin_username" "$keycloak_admin_password")"; then
    echo "Falha ao autenticar no Keycloak Admin API. Verifique se o Keycloak subiu corretamente." >&2
    return 1
  fi

  keycloak_base="${base_and_token%%|*}"
  keycloak_token="${base_and_token#*|}"

  users_json="$(curl -sS -G "${keycloak_base}/admin/realms/${keycloak_realm}/users" \
    -H "Authorization: Bearer ${keycloak_token}" \
    --data-urlencode "username=${username}" \
    --data-urlencode "exact=true")"
  user_id="$(printf '%s' "$users_json" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"

  if [[ -z "$user_id" ]]; then
    escaped_name="$(json_escape "$name")"
    escaped_email="$(json_escape "$email")"
    escaped_username="$(json_escape "$username")"
    create_payload="{\"enabled\":true,\"emailVerified\":true,\"firstName\":\"${escaped_name}\",\"username\":\"${escaped_username}\",\"email\":\"${escaped_email}\"}"

    status="$(keycloak_request_status "POST" "${keycloak_base}/admin/realms/${keycloak_realm}/users" "$keycloak_token" "$create_payload")"
    if [[ "$status" != "201" && "$status" != "409" ]]; then
      echo "Falha ao criar usuario inicial (HTTP ${status})." >&2
      cat /tmp/cmtechmap-keycloak-response.json >&2 || true
      return 1
    fi

    users_json="$(curl -sS -G "${keycloak_base}/admin/realms/${keycloak_realm}/users" \
      -H "Authorization: Bearer ${keycloak_token}" \
      --data-urlencode "username=${username}" \
      --data-urlencode "exact=true")"
    user_id="$(printf '%s' "$users_json" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
  fi

  if [[ -z "$user_id" ]]; then
    echo "Nao foi possivel obter o ID do usuario inicial no Keycloak." >&2
    return 1
  fi

  escaped_password="$(json_escape "$password")"
  status="$(keycloak_request_status "PUT" "${keycloak_base}/admin/realms/${keycloak_realm}/users/${user_id}/reset-password" "$keycloak_token" "{\"type\":\"password\",\"temporary\":false,\"value\":\"${escaped_password}\"}")"
  if [[ "$status" != "204" ]]; then
    echo "Falha ao definir senha do usuario inicial (HTTP ${status})." >&2
    cat /tmp/cmtechmap-keycloak-response.json >&2 || true
    return 1
  fi

  if [[ "$is_admin" == "true" ]]; then
    clients_json="$(curl -sS -G "${keycloak_base}/admin/realms/${keycloak_realm}/clients" -H "Authorization: Bearer ${keycloak_token}" --data-urlencode "clientId=realm-management")"
    realm_mgmt_client_id="$(printf '%s' "$clients_json" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
    if [[ -z "$realm_mgmt_client_id" ]]; then
      echo "Aviso: nao foi possivel localizar client realm-management para aplicar permissao admin." >&2
    else
      role_json="$(keycloak_request "GET" "${keycloak_base}/admin/realms/${keycloak_realm}/clients/${realm_mgmt_client_id}/roles/realm-admin" "$keycloak_token")"
      status="$(keycloak_request_status "POST" "${keycloak_base}/admin/realms/${keycloak_realm}/users/${user_id}/role-mappings/clients/${realm_mgmt_client_id}" "$keycloak_token" "[${role_json}]")"
      if [[ "$status" != "204" ]]; then
        echo "Aviso: falha ao aplicar permissao de administrador (HTTP ${status})." >&2
      fi
    fi
  fi

  echo "Usuario inicial '${username}' configurado com sucesso."
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
CREATE_INITIAL_USER=false
INITIAL_USER_NAME=""
INITIAL_USER_EMAIL=""
INITIAL_USER_USERNAME=""
INITIAL_USER_PASSWORD=""
INITIAL_USER_ADMIN=false

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
    --create-initial-user)
      CREATE_INITIAL_USER=true
      shift
      ;;
    --initial-user-name)
      INITIAL_USER_NAME="${2:-}"
      shift 2
      ;;
    --initial-user-email)
      INITIAL_USER_EMAIL="${2:-}"
      shift 2
      ;;
    --initial-user-username)
      INITIAL_USER_USERNAME="${2:-}"
      shift 2
      ;;
    --initial-user-password)
      INITIAL_USER_PASSWORD="${2:-}"
      shift 2
      ;;
    --initial-user-admin)
      INITIAL_USER_ADMIN=true
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

if [[ "$CREATE_INITIAL_USER" == "true" ]]; then
  if [[ -z "$INITIAL_USER_NAME" || -z "$INITIAL_USER_EMAIL" || -z "$INITIAL_USER_USERNAME" || -z "$INITIAL_USER_PASSWORD" ]]; then
    echo "Para --create-initial-user, informe nome, email, login e senha." >&2
    exit 1
  fi
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

if [[ "$CREATE_INITIAL_USER" == "true" ]]; then
  create_initial_keycloak_user "$ENV_FILE" "$INITIAL_USER_NAME" "$INITIAL_USER_EMAIL" "$INITIAL_USER_USERNAME" "$INITIAL_USER_PASSWORD" "$INITIAL_USER_ADMIN"
fi
