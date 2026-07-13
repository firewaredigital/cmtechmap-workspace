#!/bin/bash
# ==============================================================================
# CM TECHMAP — Production Secrets Generator
# Generates cryptographically secure random secrets for all services
# Run this ONCE before first production deployment
# ==============================================================================

set -euo pipefail

OUTPUT_FILE="${1:-.env.production}"

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  CM TECHMAP — Production Secrets Generator                      ║"
echo "╚══════════════════════════════════════════════════════════════════╝"

# Generate random strings
gen_secret() { openssl rand -base64 "$1" | tr -d '/+=' | head -c "$1"; }
gen_password() { openssl rand -base64 24 | tr -d '/+=' | head -c 24; }
gen_hex() { openssl rand -hex "$1"; }

# Application secrets
APP_SECRET_KEY=$(gen_hex 32)
POSTGRES_PASSWORD=$(gen_password)
MINIO_ROOT_PASSWORD=$(gen_password)
KEYCLOAK_ADMIN_PASSWORD=$(gen_password)
KEYCLOAK_CLIENT_SECRET=$(gen_hex 32)
GRAFANA_ADMIN_PASSWORD=$(gen_password)
FLOWER_PASSWORD=$(gen_password)

cat > "$OUTPUT_FILE" << ENVEOF
# ==============================================================================
# CM TECHMAP — Production Environment Configuration
# Generated at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# ⚠️  KEEP THIS FILE SECRET — DO NOT COMMIT TO GIT
# ==============================================================================

# ── Application ──────────────────────────────────────────────────────────────
APP_ENV=production
APP_DEBUG=false
APP_SECRET_KEY=${APP_SECRET_KEY}
APP_CORS_ORIGINS=https://\${DOMAIN_NAME}
API_LOG_LEVEL=warning

# ── Domain ───────────────────────────────────────────────────────────────────
DOMAIN_NAME=mapa.suaprefeitura.gov.br
NGINX_PORT=80

# ── PostgreSQL ───────────────────────────────────────────────────────────────
POSTGRES_DB=cm_techmap
POSTGRES_USER=cm_techmap_prod
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
KC_POSTGRES_DB=keycloak

# ── Redis ────────────────────────────────────────────────────────────────────
REDIS_MAX_MEMORY=1gb

# ── MinIO (S3) ───────────────────────────────────────────────────────────────
MINIO_ROOT_USER=cm_techmap_prod
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}

# ── Keycloak ─────────────────────────────────────────────────────────────────
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
KEYCLOAK_REALM=cm-techmap
KEYCLOAK_CLIENT_ID=cm-techmap-api
KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET}

# ── Grafana ──────────────────────────────────────────────────────────────────
GRAFANA_ADMIN_USER=admin
GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}

# ── Flower (Celery Monitor) ──────────────────────────────────────────────────
FLOWER_BASIC_AUTH=admin:${FLOWER_PASSWORD}

# ── Backup ───────────────────────────────────────────────────────────────────
BACKUP_RETENTION_DAYS=30
BACKUP_CRON_SCHEDULE=0 3 * * *
BACKUP_S3_BUCKET=cm-techmap-backups

# ── SSL ──────────────────────────────────────────────────────────────────────
# For Let's Encrypt, set LETSENCRYPT_EMAIL
LETSENCRYPT_EMAIL=devops@suaprefeitura.gov.br
ENVEOF

chmod 600 "$OUTPUT_FILE"

echo ""
echo "✅ Production secrets generated: $OUTPUT_FILE"
echo ""
echo "Secrets summary:"
echo "  APP_SECRET_KEY    = ${APP_SECRET_KEY:0:8}...${APP_SECRET_KEY: -4}"
echo "  POSTGRES_PASSWORD = ${POSTGRES_PASSWORD:0:4}...${POSTGRES_PASSWORD: -4}"
echo "  MINIO_PASSWORD    = ${MINIO_ROOT_PASSWORD:0:4}...${MINIO_ROOT_PASSWORD: -4}"
echo "  KEYCLOAK_PASSWORD = ${KEYCLOAK_ADMIN_PASSWORD:0:4}...${KEYCLOAK_ADMIN_PASSWORD: -4}"
echo ""
echo "⚠️  IMPORTANT:"
echo "  1. Store this file securely (chmod 600 applied)"
echo "  2. DO NOT commit to version control"
echo "  3. Back up these secrets — if lost, system must be reconfigured"
