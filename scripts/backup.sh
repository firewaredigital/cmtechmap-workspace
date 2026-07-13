#!/bin/bash
# ==============================================================================
# CM TECHMAP — Automated Database Backup
# Creates compressed backup and uploads to MinIO with 30-day retention.
# Usage: bash scripts/backup.sh [daily|hourly]
# ==============================================================================
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
BACKUP_BUCKET="cm-techmap-backups"
POSTGRES_USER="${POSTGRES_USER:-cm_techmap}"
POSTGRES_DB="${POSTGRES_DB:-cm_techmap}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="cm_techmap_${TIMESTAMP}.sql.gz"
LOCAL_DIR="/tmp/cm-techmap-backups"

echo "[$(date)] Starting backup: ${BACKUP_FILE}"

# Ensure local dir exists
mkdir -p "$LOCAL_DIR"

# Create backup
docker compose -f "$COMPOSE_FILE" exec -T postgres \
    pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    --format=plain --no-owner --no-privileges 2>/dev/null \
    | gzip > "${LOCAL_DIR}/${BACKUP_FILE}"

FILE_SIZE=$(du -h "${LOCAL_DIR}/${BACKUP_FILE}" | cut -f1)
echo "[$(date)] Backup created: ${BACKUP_FILE} (${FILE_SIZE})"

# Upload to MinIO
docker compose -f "$COMPOSE_FILE" exec -T minio \
    mc cp "/tmp/${BACKUP_FILE}" "local/${BACKUP_BUCKET}/${BACKUP_FILE}" 2>/dev/null || \
    echo "[$(date)] WARNING: MinIO upload failed, backup is local only"

# Retention — remove backups older than $RETENTION_DAYS days
find "$LOCAL_DIR" -name "cm_techmap_*.sql.gz" -mtime +${RETENTION_DAYS} -delete 2>/dev/null
echo "[$(date)] Cleanup: removed backups older than ${RETENTION_DAYS} days"

# Count remaining backups
BACKUP_COUNT=$(ls -1 "${LOCAL_DIR}"/cm_techmap_*.sql.gz 2>/dev/null | wc -l)
echo "[$(date)] Complete. ${BACKUP_COUNT} backups retained locally."
