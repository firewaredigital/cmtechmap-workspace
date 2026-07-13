#!/bin/bash
# ==============================================================================
# CM TECHMAP — OCI VM Setup Script
# Executa DENTRO da VM após o código ser sincronizado
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

log_info()    { echo -e "${BLUE}ℹ️  $1${NC}"; }
log_success() { echo -e "${GREEN}✅ $1${NC}"; }
log_warn()    { echo -e "${YELLOW}⚠️  $1${NC}"; }
log_error()   { echo -e "${RED}❌ $1${NC}"; }
log_step()    { echo -e "\n${CYAN}${BOLD}═══ $1 ═══${NC}\n"; }

APP_DIR="/opt/cm-techmap"
ENV_FILE="$APP_DIR/.env.oci"

echo -e "${CYAN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  ${BOLD}CM TECHMAP — OCI VM Setup${NC}${CYAN}                                   ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════╝${NC}"

# ── 1. Verificar ambiente ────────────────────────────────────────────────────
log_step "1/7 — Verificando Ambiente"

[ ! -d "$APP_DIR" ] && { log_error "Dir not found: $APP_DIR"; exit 1; }
docker --version >/dev/null 2>&1 || { log_error "Docker não instalado!"; exit 1; }
docker compose version >/dev/null 2>&1 || { log_error "Compose não instalado!"; exit 1; }
log_success "Docker $(docker --version | awk '{print $3}')"

if mountpoint -q /data; then
    log_success "Block Volume montado em /data ($(df -h /data | tail -1 | awk '{print $4}') livre)"
else
    log_warn "/data não é mount point. Usando disco local."
fi

# ── 2. Gerar secrets ────────────────────────────────────────────────────────
log_step "2/7 — Gerando Secrets"

if [ -f "$ENV_FILE" ]; then
    log_warn ".env.oci já existe — usando secrets existentes"
else
    gen_password() { openssl rand -base64 24 | tr -d '/+=' | head -c 24; }
    gen_hex() { openssl rand -hex "$1"; }

    APP_SECRET_KEY=$(gen_hex 32)
    POSTGRES_PASSWORD=$(gen_password)
    MINIO_ROOT_PASSWORD=$(gen_password)
    KEYCLOAK_ADMIN_PASSWORD=$(gen_password)
    KEYCLOAK_CLIENT_SECRET=$(gen_hex 32)
    GRAFANA_ADMIN_PASSWORD=$(gen_password)
    FLOWER_PASSWORD=$(gen_password)
    PUBLIC_IP=$(curl -sf https://ifconfig.me 2>/dev/null || echo "UNKNOWN")

    cat > "$ENV_FILE" << ENVEOF
# CM TECHMAP — OCI Environment — Generated $(date -u +"%Y-%m-%dT%H:%M:%SZ")
APP_ENV=production
APP_DEBUG=false
APP_SECRET_KEY=${APP_SECRET_KEY}
APP_CORS_ORIGINS=http://${PUBLIC_IP},https://${PUBLIC_IP}
API_LOG_LEVEL=warning
GUNICORN_WORKERS=2
CELERY_CONCURRENCY=2
CELERY_AUTOSCALE=4,1
POSTGRES_DB=cm_techmap
POSTGRES_USER=cm_techmap_oci
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
KC_POSTGRES_DB=keycloak
REDIS_MAX_MEMORY=256mb
MINIO_ROOT_USER=cm_techmap_oci
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
KEYCLOAK_REALM=cm-techmap
KEYCLOAK_CLIENT_ID=cm-techmap-api
KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET}
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
FLOWER_BASIC_AUTH=admin:${FLOWER_PASSWORD}
BACKUP_RETENTION_DAYS=14
BACKUP_CRON_SCHEDULE=0 3 * * *
BACKUP_S3_BUCKET=cm-techmap-backups
ENVEOF

    chmod 600 "$ENV_FILE"
    log_success "Secrets gerados: $ENV_FILE"
    log_info "  POSTGRES = ${POSTGRES_PASSWORD:0:4}...  |  KEYCLOAK = ${KEYCLOAK_ADMIN_PASSWORD:0:4}..."
fi

# ── 3. Preparar volumes de dados ────────────────────────────────────────────
log_step "3/7 — Preparando Volumes"

for dir in postgres redis minio prometheus grafana certbot flower backups docker; do
    mkdir -p "/data/$dir"
done
chown -R 999:999 /data/postgres /data/redis
chown -R 472:472 /data/grafana
chown -R 65534:65534 /data/prometheus
log_success "Diretórios criados com permissões corretas"

# ── 4. SSL self-signed ──────────────────────────────────────────────────────
log_step "4/7 — Configurando SSL"

SSL_DIR="$APP_DIR/docker/nginx/ssl"
mkdir -p "$SSL_DIR"

if [ ! -f "$SSL_DIR/fullchain.pem" ]; then
    PUBLIC_IP=$(curl -sf https://ifconfig.me 2>/dev/null || echo "localhost")
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout "$SSL_DIR/privkey.pem" -out "$SSL_DIR/fullchain.pem" \
        -subj "/C=BR/ST=GO/L=Goiania/O=CMTechMap/CN=$PUBLIC_IP" \
        -addext "subjectAltName=IP:$PUBLIC_IP,IP:127.0.0.1,DNS:localhost" 2>/dev/null
    cp "$SSL_DIR/fullchain.pem" "$SSL_DIR/chain.pem"
    log_success "Certificado self-signed para $PUBLIC_IP"
else
    log_info "Certificados existentes mantidos"
fi

# ── 5. Build containers ─────────────────────────────────────────────────────
log_step "5/7 — Build dos Containers"

cd "$APP_DIR"
log_info "Build em andamento (5-15 min no primeiro deploy)..."
docker compose -f docker-compose.oci.yml --env-file .env.oci build --parallel 2>&1 | tail -10
log_success "Build concluído"

# ── 6. Iniciar serviços ─────────────────────────────────────────────────────
log_step "6/7 — Iniciando Serviços"

docker compose -f docker-compose.oci.yml --env-file .env.oci up -d
log_info "Aguardando serviços (90s)..."
sleep 90
docker compose -f docker-compose.oci.yml ps --format "table {{.Name}}\t{{.Status}}"

# ── 7. Migrations + Health ──────────────────────────────────────────────────
log_step "7/7 — Migrations e Verificação"

docker compose -f docker-compose.oci.yml --env-file .env.oci \
    exec -T backend alembic upgrade head 2>/dev/null && \
    log_success "Migrations OK" || log_warn "Migrations puladas"

echo ""
log_info "Uso de recursos:"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}" | head -20
echo ""
free -h | grep Mem | awk '{printf "  RAM: %s/%s (%s livre)\n", $3, $2, $4}'
df -h /data | tail -1 | awk '{printf "  Disco: %s/%s (%s livre)\n", $3, $2, $4}'

PUBLIC_IP=$(curl -sf https://ifconfig.me 2>/dev/null || echo "UNKNOWN")
echo -e "\n${GREEN}${BOLD}✅ Setup concluído! IP: ${PUBLIC_IP}${NC}"
