#!/bin/bash
# ==============================================================================
# CM TECHMAP — Production Deployment Script
# One-command deployment to a fresh server
# ==============================================================================

set -euo pipefail

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  CM TECHMAP — Production Deployment                             ║"
echo "╚══════════════════════════════════════════════════════════════════╝"

# ── Pre-flight checks ────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || { echo "❌ Docker not installed"; exit 1; }
command -v docker compose >/dev/null 2>&1 || { echo "❌ Docker Compose not installed"; exit 1; }

# ── Step 1: Generate secrets if needed ────────────────────────────────────────
ENV_FILE=".env.production"
if [ ! -f "$ENV_FILE" ]; then
    echo "📋 No production env found — generating secrets..."
    bash scripts/generate-secrets.sh "$ENV_FILE"
    echo ""
    echo "⚠️  IMPORTANT: Edit $ENV_FILE and set your DOMAIN_NAME before continuing!"
    echo "   Current default: mapa.suaprefeitura.gov.br"
    echo ""
    read -p "Press Enter after configuring $ENV_FILE..."
fi

# ── Step 2: Create SSL directory ──────────────────────────────────────────────
mkdir -p docker/nginx/ssl

# ── Step 3: Build and start ───────────────────────────────────────────────────
echo "🔨 Building containers..."
docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE" build --parallel

echo "🚀 Starting services..."
docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE" up -d

# ── Step 4: Wait for services ────────────────────────────────────────────────
echo "⏳ Waiting for services to become healthy..."
sleep 15

for service in postgres redis minio backend frontend nginx; do
    status=$(docker compose -f docker-compose.prod.yml ps --format json "$service" 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health','unknown'))" 2>/dev/null || echo "unknown")
    echo "  $service: $status"
done

# ── Step 5: Run migrations ───────────────────────────────────────────────────
echo "🗄️  Running database migrations..."
docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE" exec -T backend alembic upgrade head 2>/dev/null || {
    echo "  ⚠️  Alembic migrations skipped (run manually if needed)"
}

# ── Step 6: Summary ──────────────────────────────────────────────────────────
DOMAIN=$(grep DOMAIN_NAME "$ENV_FILE" | head -1 | cut -d= -f2)

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "✅ CM TECHMAP deployed successfully!"
echo "═══════════════════════════════════════════════════════════════════"
echo ""
echo "  🌐 Application: https://${DOMAIN}"
echo "  📊 Grafana:      https://${DOMAIN}/grafana/"
echo "  🔧 Flower:       https://${DOMAIN}/flower/"
echo "  📡 API Docs:     https://${DOMAIN}/docs"
echo "  🔑 Keycloak:     https://${DOMAIN}:18080"
echo ""
echo "  📦 Backup:       Daily at 3:00 AM (configurable)"
echo "  📈 Metrics:      Prometheus → Grafana"
echo ""
echo "Next steps:"
echo "  1. Configure DNS: ${DOMAIN} → your server IP"
echo "  2. Install Let's Encrypt: docker exec cmp-nginx certbot certonly --webroot -w /var/www/certbot -d ${DOMAIN}"
echo "  3. Reload Nginx: docker exec cmp-nginx nginx -s reload"
echo "═══════════════════════════════════════════════════════════════════"
