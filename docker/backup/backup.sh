#!/bin/bash
# ==============================================================================
# CM TECHMAP — Automated PostgreSQL Backup
# Performs compressed pg_dump → uploads to MinIO/S3 → rotates old backups
# Designed to run as a cron job or Docker container
# ==============================================================================

set -euo pipefail

# ── Configuration (from environment) ─────────────────────────────────────────
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-cm_techmap}"
POSTGRES_USER="${POSTGRES_USER:-cm_techmap}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

MINIO_ENDPOINT="${MINIO_ENDPOINT:-minio:9000}"
MINIO_ACCESS_KEY="${MINIO_ROOT_USER:-cm_techmap_admin}"
MINIO_SECRET_KEY="${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"
MINIO_BUCKET="${BACKUP_S3_BUCKET:-cm-techmap-backups}"
MINIO_USE_SSL="${MINIO_USE_SSL:-false}"

RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
BACKUP_DIR="/tmp/cm-techmap-backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILENAME="cm_techmap_${TIMESTAMP}.sql.gz"
KEYCLOAK_BACKUP_FILENAME="keycloak_${TIMESTAMP}.sql.gz"

# ── Logging ──────────────────────────────────────────────────────────────────
log() { echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] $1"; }
error() { echo "[$(date -u +"%Y-%m-%d %H:%M:%S UTC")] ❌ ERROR: $1" >&2; }

# ── Preparation ──────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"
export PGPASSWORD="$POSTGRES_PASSWORD"

log "╔══════════════════════════════════════════════════════════════╗"
log "║  CM TECHMAP — Backup Starting                               ║"
log "╚══════════════════════════════════════════════════════════════╝"

# ── Step 1: Main database backup ─────────────────────────────────────────────
log "📦 Backing up main database: $POSTGRES_DB"
pg_dump \
    -h "$POSTGRES_HOST" \
    -p "$POSTGRES_PORT" \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    --format=custom \
    --compress=9 \
    --verbose \
    --no-owner \
    --no-privileges \
    --exclude-table-data='*.celery_*' \
    2>/tmp/pgdump_main.log \
    | gzip > "$BACKUP_DIR/$BACKUP_FILENAME"

MAIN_SIZE=$(du -sh "$BACKUP_DIR/$BACKUP_FILENAME" | cut -f1)
log "  ✅ Main backup: $BACKUP_FILENAME ($MAIN_SIZE)"

# ── Step 2: Keycloak database backup ─────────────────────────────────────────
KC_DB="${KC_POSTGRES_DB:-keycloak}"
log "📦 Backing up Keycloak database: $KC_DB"
pg_dump \
    -h "$POSTGRES_HOST" \
    -p "$POSTGRES_PORT" \
    -U "$POSTGRES_USER" \
    -d "$KC_DB" \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    2>/tmp/pgdump_kc.log \
    | gzip > "$BACKUP_DIR/$KEYCLOAK_BACKUP_FILENAME" || {
        log "  ⚠️  Keycloak backup skipped (database may not exist)"
    }

# ── Step 3: Upload to MinIO/S3 ───────────────────────────────────────────────
log "☁️  Uploading to MinIO: $MINIO_BUCKET"

# Configure mc (MinIO Client)
MC_ALIAS="cmbackup"
mc_scheme="http"
[ "$MINIO_USE_SSL" = "true" ] && mc_scheme="https"

mc alias set "$MC_ALIAS" "${mc_scheme}://${MINIO_ENDPOINT}" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" --api S3v4 2>/dev/null

# Ensure bucket exists
mc mb --ignore-existing "${MC_ALIAS}/${MINIO_BUCKET}" 2>/dev/null || true

# Upload main backup
mc cp "$BACKUP_DIR/$BACKUP_FILENAME" "${MC_ALIAS}/${MINIO_BUCKET}/database/${BACKUP_FILENAME}"
log "  ✅ Main backup uploaded"

# Upload Keycloak backup (if it exists)
if [ -f "$BACKUP_DIR/$KEYCLOAK_BACKUP_FILENAME" ]; then
    mc cp "$BACKUP_DIR/$KEYCLOAK_BACKUP_FILENAME" "${MC_ALIAS}/${MINIO_BUCKET}/keycloak/${KEYCLOAK_BACKUP_FILENAME}"
    log "  ✅ Keycloak backup uploaded"
fi

# ── Step 4: Create metadata ──────────────────────────────────────────────────
cat > "$BACKUP_DIR/metadata_${TIMESTAMP}.json" << EOF
{
    "timestamp": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
    "database": "$POSTGRES_DB",
    "host": "$POSTGRES_HOST",
    "main_backup": "$BACKUP_FILENAME",
    "main_size": "$MAIN_SIZE",
    "keycloak_backup": "$KEYCLOAK_BACKUP_FILENAME",
    "retention_days": $RETENTION_DAYS
}
EOF
mc cp "$BACKUP_DIR/metadata_${TIMESTAMP}.json" "${MC_ALIAS}/${MINIO_BUCKET}/metadata/metadata_${TIMESTAMP}.json" 2>/dev/null || true

# ── Step 5: Rotate old backups ───────────────────────────────────────────────
log "🔄 Rotating backups older than $RETENTION_DAYS days"

CUTOFF_DATE=$(date -d "-${RETENTION_DAYS} days" +%Y%m%d 2>/dev/null || date -v-${RETENTION_DAYS}d +%Y%m%d)

# List and remove old backups from MinIO
mc ls "${MC_ALIAS}/${MINIO_BUCKET}/database/" 2>/dev/null | while read -r line; do
    filename=$(echo "$line" | awk '{print $NF}')
    # Extract date from filename (cm_techmap_YYYYMMDD_HHMMSS.sql.gz)
    file_date=$(echo "$filename" | grep -oP '\d{8}' | head -1)
    if [ -n "$file_date" ] && [ "$file_date" -lt "$CUTOFF_DATE" ] 2>/dev/null; then
        mc rm "${MC_ALIAS}/${MINIO_BUCKET}/database/${filename}" 2>/dev/null && \
            log "  🗑️  Removed old backup: $filename"
    fi
done

# ── Step 6: Cleanup local temp files ─────────────────────────────────────────
rm -rf "$BACKUP_DIR"
rm -f /tmp/pgdump_*.log

log "═══════════════════════════════════════════════════════════════════"
log "✅ Backup complete!"
log "  Main:     $BACKUP_FILENAME ($MAIN_SIZE)"
log "  Stored:   ${MC_ALIAS}/${MINIO_BUCKET}/database/"
log "  Rotation: ${RETENTION_DAYS} days"
log "═══════════════════════════════════════════════════════════════════"
