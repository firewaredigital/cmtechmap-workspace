#!/bin/bash
# ==============================================================================
# CM TECHMAP — Backup Container Entrypoint
# Sets up cron schedule and runs backup on startup
# ==============================================================================

set -euo pipefail

CRON_SCHEDULE="${BACKUP_CRON_SCHEDULE:-0 3 * * *}"

echo "[Backup] CM TECHMAP Backup Service Starting"
echo "[Backup] Schedule: $CRON_SCHEDULE"
echo "[Backup] Retention: ${BACKUP_RETENTION_DAYS:-30} days"
echo "[Backup] Target: ${MINIO_ENDPOINT:-minio:9000}/${BACKUP_S3_BUCKET:-cm-techmap-backups}"

# Export all environment variables for cron context
printenv | grep -v "no_proxy" > /etc/environment

# Create cron job
echo "$CRON_SCHEDULE /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1" > /etc/crontabs/root

# Run initial backup on startup
echo "[Backup] Running initial backup..."
/usr/local/bin/backup.sh || echo "[Backup] Initial backup failed (services may not be ready)"

# Start cron daemon in foreground
echo "[Backup] Cron daemon started — next backup at: $CRON_SCHEDULE"
exec crond -f -l 2
