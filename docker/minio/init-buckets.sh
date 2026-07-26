#!/bin/sh
# ==============================================================================
# CM TECHMAP — MinIO Bucket Initialization
# Creates all required buckets with appropriate policies.
#
# NOTE: current `mc` releases removed `mc policy set` in favor of
# `mc anonymous set`. The old command aborted this script (set -e) after the
# first bucket, leaving 6 of 7 buckets uncreated.
# ==============================================================================

set -e

echo "⏳ Waiting for MinIO to be ready..."
until /usr/bin/mc alias set cmtechmap http://minio:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" 2>/dev/null; do
    echo "  MinIO not ready yet, retrying in 2s..."
    sleep 2
done
echo "✅ MinIO is ready."

# set_policy <policy> <bucket> — works with both modern (anonymous) and
# legacy (policy) mc CLIs, and never aborts the script.
set_policy() {
    /usr/bin/mc anonymous set "$1" "cmtechmap/$2" 2>/dev/null \
        || /usr/bin/mc policy set "$1" "cmtechmap/$2" 2>/dev/null \
        || echo "⚠️  Could not set policy '$1' on bucket '$2'"
}

echo "📦 Creating buckets..."

# Raw drone image uploads (private — only backend writes)
/usr/bin/mc mb --ignore-existing cmtechmap/raw-uploads
set_policy none raw-uploads

# Processed orthomosaics — COG files (read by TiTiler)
/usr/bin/mc mb --ignore-existing cmtechmap/orthomosaics
set_policy download orthomosaics

# Point clouds — LAS/LAZ files
/usr/bin/mc mb --ignore-existing cmtechmap/point-clouds
set_policy download point-clouds

# Digital Elevation Models — DSM/DTM GeoTIFFs
/usr/bin/mc mb --ignore-existing cmtechmap/elevation-models
set_policy download elevation-models

# 3D textured meshes — OBJ/glTF
/usr/bin/mc mb --ignore-existing cmtechmap/3d-models
set_policy download 3d-models

# Generated reports — PDF/Excel
/usr/bin/mc mb --ignore-existing cmtechmap/reports
set_policy none reports

# Database and system backups (both names: `backups` is the backend default,
# `cm-techmap-backups` is what the standalone backup container targets)
/usr/bin/mc mb --ignore-existing cmtechmap/backups
set_policy none backups
/usr/bin/mc mb --ignore-existing cmtechmap/cm-techmap-backups
set_policy none cm-techmap-backups

echo "✅ All buckets created successfully."

# Set lifecycle rules — auto-delete raw uploads after 90 days to save storage
/usr/bin/mc ilm rule add cmtechmap/raw-uploads \
    --expire-days 90 \
    --prefix "" \
    --tags "auto-cleanup=true" 2>/dev/null || echo "⚠️  ILM rule skipped (may require MinIO enterprise)"

echo "🎉 MinIO initialization complete."
exit 0
