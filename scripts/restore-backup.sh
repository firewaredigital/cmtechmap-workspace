#!/bin/bash
# ==============================================================================
# CM TECHMAP — Disaster Recovery Restore Script
# Restores a PostgreSQL backup from MinIO
# ==============================================================================
# Usage:
#   bash scripts/restore-backup.sh latest
#   bash scripts/restore-backup.sh 2026-05-28
#   bash scripts/restore-backup.sh /path/to/backup.sql.gz
# ==============================================================================

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
BACKUP_BUCKET="${BACKUP_BUCKET:-cm-techmap-backups}"
POSTGRES_USER="${POSTGRES_USER:-cm_techmap}"
POSTGRES_DB="${POSTGRES_DB:-cm_techmap}"
RESTORE_TARGET="${1:-latest}"

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  CM TECHMAP — Disaster Recovery Restore                        ║"
echo "╠══════════════════════════════════════════════════════════════════╣"
echo "║  Target: $RESTORE_TARGET"
echo "║  Database: $POSTGRES_DB"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# ── Safety check ──────────────────────────────────────────────────────────────
echo "⚠️  WARNING: This will REPLACE the current database."
echo "   All existing data will be LOST."
echo ""
read -p "Type 'RESTORE' to confirm: " confirmation
if [ "$confirmation" != "RESTORE" ]; then
    echo "❌ Aborted."
    exit 1
fi

# ── Step 1: Get backup file ───────────────────────────────────────────────────
BACKUP_FILE=""

if [ "$RESTORE_TARGET" = "latest" ]; then
    echo "[1/6] Finding latest backup in MinIO..."
    BACKUP_FILE=$(docker compose -f "$COMPOSE_FILE" exec -T minio mc ls /data/$BACKUP_BUCKET/ 2>/dev/null \
        | grep ".sql.gz" | sort | tail -1 | awk '{print $NF}')
    
    if [ -z "$BACKUP_FILE" ]; then
        echo "❌ No backup files found in MinIO bucket '$BACKUP_BUCKET'"
        echo "   Trying local backup directory..."
        BACKUP_FILE=$(ls -t /tmp/cm-techmap-backups/*.sql.gz 2>/dev/null | head -1)
        if [ -z "$BACKUP_FILE" ]; then
            echo "❌ No backup files found anywhere."
            exit 1
        fi
    else
        echo "   Found: $BACKUP_FILE"
        echo "   Downloading from MinIO..."
        docker compose -f "$COMPOSE_FILE" exec -T minio mc cp "/data/$BACKUP_BUCKET/$BACKUP_FILE" /tmp/restore.sql.gz
        BACKUP_FILE="/tmp/restore.sql.gz"
    fi
elif [ -f "$RESTORE_TARGET" ]; then
    BACKUP_FILE="$RESTORE_TARGET"
else
    # Search by date pattern
    echo "[1/6] Searching for backup matching: $RESTORE_TARGET"
    BACKUP_FILE=$(docker compose -f "$COMPOSE_FILE" exec -T minio mc ls /data/$BACKUP_BUCKET/ 2>/dev/null \
        | grep "$RESTORE_TARGET" | grep ".sql.gz" | sort | tail -1 | awk '{print $NF}')
    
    if [ -z "$BACKUP_FILE" ]; then
        echo "❌ No backup found matching '$RESTORE_TARGET'"
        exit 1
    fi
    
    echo "   Found: $BACKUP_FILE"
    docker compose -f "$COMPOSE_FILE" exec -T minio mc cp "/data/$BACKUP_BUCKET/$BACKUP_FILE" /tmp/restore.sql.gz
    BACKUP_FILE="/tmp/restore.sql.gz"
fi

echo "✅ Backup file ready: $BACKUP_FILE"
echo ""

# ── Step 2: Stop services ────────────────────────────────────────────────────
echo "[2/6] Stopping application services..."
docker compose -f "$COMPOSE_FILE" stop backend celery-worker celery-beat 2>/dev/null || true
echo "✅ Services stopped"

# ── Step 3: Create safety backup ──────────────────────────────────────────────
echo "[3/6] Creating safety backup of current database..."
SAFETY_BACKUP="/tmp/cm-techmap-pre-restore-$(date +%Y%m%d_%H%M%S).sql.gz"
docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom | gzip > "$SAFETY_BACKUP"
echo "✅ Safety backup: $SAFETY_BACKUP"

# ── Step 4: Drop and recreate database ────────────────────────────────────────
echo "[4/6] Dropping and recreating database..."
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c "
    SELECT pg_terminate_backend(pg_stat_activity.pid)
    FROM pg_stat_activity
    WHERE pg_stat_activity.datname = '$POSTGRES_DB' AND pid <> pg_backend_pid();
"
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $POSTGRES_DB;"
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $POSTGRES_DB OWNER $POSTGRES_USER;"

# Re-enable extensions
docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_topology;
    CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";
    CREATE EXTENSION IF NOT EXISTS pgcrypto;
"
echo "✅ Database recreated with extensions"

# ── Step 5: Restore backup ────────────────────────────────────────────────────
echo "[5/6] Restoring backup..."
if [[ "$BACKUP_FILE" == *.sql.gz ]]; then
    gunzip -c "$BACKUP_FILE" | docker compose -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
elif [[ "$BACKUP_FILE" == *.dump ]]; then
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
        pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges < "$BACKUP_FILE"
else
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
        psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$BACKUP_FILE"
fi
echo "✅ Backup restored"

# ── Step 6: Verify and restart ────────────────────────────────────────────────
echo "[6/6] Verifying restore..."
TENANT_COUNT=$(docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM public.tenants;" 2>/dev/null || echo "0")
SCHEMA_COUNT=$(docker compose -f "$COMPOSE_FILE" exec -T postgres \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -t -c "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name LIKE 'tenant_%';" 2>/dev/null || echo "0")

echo "   Tenants: $TENANT_COUNT"
echo "   Schemas: $SCHEMA_COUNT"

echo ""
echo "Starting services..."
docker compose -f "$COMPOSE_FILE" up -d backend celery-worker

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "✅ RESTORE COMPLETE"
echo "   Safety backup: $SAFETY_BACKUP"
echo "   Tenants: $TENANT_COUNT | Schemas: $SCHEMA_COUNT"
echo "═══════════════════════════════════════════════════════════════════"
