#!/usr/bin/env bash
set -euo pipefail

mkdir -p /var/log/supervisor /var/lib/postgresql/data /data/minio /code/runtime
chown -R postgres:postgres /var/lib/postgresql

POSTGRES_BIN_DIR="$(dirname "$(command -v postgres)")"
export PATH="${POSTGRES_BIN_DIR}:$PATH"
INITDB_BIN="$(find /usr -type f -name initdb 2>/dev/null | head -n1 || true)"
PG_CTL_BIN="$(find /usr -type f -name pg_ctl 2>/dev/null | head -n1 || true)"

if [[ -z "$INITDB_BIN" || -z "$PG_CTL_BIN" ]]; then
  echo "Could not locate initdb/pg_ctl binaries in container image." >&2
  exit 127
fi

if [[ ! -f /var/lib/postgresql/data/PG_VERSION ]]; then
  su - postgres -s /bin/bash -c "\"${INITDB_BIN}\" -D /var/lib/postgresql/data"
fi

# Start postgres temporarily for bootstrap.
su - postgres -s /bin/bash -c "\"${PG_CTL_BIN}\" -D /var/lib/postgresql/data -w start"

# Ensure application DB user exists (idempotent).
psql -U postgres -d postgres <<SQL
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', '${POSTGRES_USER}', '${POSTGRES_PASSWORD}')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${POSTGRES_USER}')
\gexec
SQL

psql -U postgres -d postgres <<SQL
SELECT 'CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${POSTGRES_DB}')\gexec
SELECT 'CREATE DATABASE ${KC_POSTGRES_DB} OWNER ${POSTGRES_USER}'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${KC_POSTGRES_DB}')\gexec
SQL

psql -U postgres -d "${POSTGRES_DB}" -c "CREATE EXTENSION IF NOT EXISTS postgis;" || true

su - postgres -s /bin/bash -c "\"${PG_CTL_BIN}\" -D /var/lib/postgresql/data -m fast -w stop"

# Start redis/minio for one-shot bucket bootstrap.
redis-server /etc/redis/redis.conf --daemonize yes
minio server /data/minio --console-address :9001 >/tmp/minio-bootstrap.log 2>&1 &

# Wait minio and create buckets.
for _ in $(seq 1 30); do
  if curl -sf http://127.0.0.1:9000/minio/health/live >/dev/null; then
    break
  fi
  sleep 2
done

if command -v mc >/dev/null 2>&1; then
  mc alias set local http://127.0.0.1:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}"
  mc mb -p local/raw-uploads || true
  mc mb -p local/orthomosaics || true
  mc mb -p local/elevation-models || true
  mc mb -p local/point-clouds || true
  mc mb -p local/3d-models || true
  mc mb -p local/reports || true
  mc mb -p local/backups || true
fi

# Stop temporary bootstrap services.
pkill -f "minio server" || true
pkill redis-server || true

# Write runtime env file for backend/celery/keycloak in single-container mode.
cat >/code/.env <<EOF
APP_ENV=${APP_ENV:-production}
APP_DEBUG=${APP_DEBUG:-false}
APP_SECRET_KEY=${APP_SECRET_KEY:-CHANGE-ME}
APP_CORS_ORIGINS=${APP_CORS_ORIGINS:-https://your-vercel-domain.vercel.app}
DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}
DATABASE_URL_SYNC=postgresql+psycopg2://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}
REDIS_URL=redis://127.0.0.1:6379/0
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
MINIO_ENDPOINT=127.0.0.1:9000
MINIO_ROOT_USER=${MINIO_ROOT_USER}
MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}
MINIO_USE_SSL=false
KEYCLOAK_SERVER_URL=http://127.0.0.1:8080
KEYCLOAK_EXTERNAL_URL=${KEYCLOAK_EXTERNAL_URL:-http://127.0.0.1:8080}
KEYCLOAK_REALM=${KEYCLOAK_REALM:-cm-techmap}
KEYCLOAK_CLIENT_ID=${KEYCLOAK_CLIENT_ID:-cm-techmap-api}
KEYCLOAK_CLIENT_SECRET=${KEYCLOAK_CLIENT_SECRET:-CHANGE-ME}
KEYCLOAK_ADMIN_USERNAME=${KEYCLOAK_ADMIN_USERNAME}
KEYCLOAK_ADMIN_PASSWORD=${KEYCLOAK_ADMIN_PASSWORD}
NODEODM_HOST=${NODEODM_HOST:-127.0.0.1}
NODEODM_PORT=${NODEODM_PORT:-3000}
TITILER_URL=${TITILER_URL:-http://127.0.0.1:18888}
MARTIN_URL=${MARTIN_URL:-http://127.0.0.1:13001}
GROQ_ENABLED=${GROQ_ENABLED:-false}
AI_PROVIDER=${AI_PROVIDER:-hybrid}
GROQ_API_KEY=${GROQ_API_KEY:-}
EOF

# Wait services and apply migrations on runtime startup.
wait_for_port() {
  local host="$1"
  local port="$2"
  local retries="${3:-45}"
  for _ in $(seq 1 "$retries"); do
    if (echo >"/dev/tcp/${host}/${port}") >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

# Boot services under supervisor in background to run migrations after readiness.
/usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf &
SUP_PID=$!

wait_for_port 127.0.0.1 5432 60
wait_for_port 127.0.0.1 6379 60
wait_for_port 127.0.0.1 9000 60
wait_for_port 127.0.0.1 8000 90

# Apply Alembic and SQL runtime migrations (safe re-run).
cd /code
alembic upgrade head || true
psql "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}" -f /code/migrations/001_fiscal_virtual.sql || true
psql "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}" -f /code/migrations/002_ai_measurements.sql || true
psql "postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:5432/${POSTGRES_DB}" -f /code/migrations/003_report_config_presets.sql || true

wait "$SUP_PID"
