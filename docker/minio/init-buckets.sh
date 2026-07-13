#!/bin/sh
# ==============================================================================
# CM TECHMAP — MinIO Bucket Initialization
# Creates all required buckets with appropriate policies
# ==============================================================================

set -e

echo "⏳ Waiting for MinIO to be ready..."
until /usr/bin/mc alias set cmtechmap http://minio:9000 "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" 2>/dev/null; do
    echo "  MinIO not ready yet, retrying in 2s..."
    sleep 2
done
echo "✅ MinIO is ready."

echo "📦 Creating buckets..."

# Raw drone image uploads (private — only backend writes)
/usr/bin/mc mb --ignore-existing cmtechmap/raw-uploads
/usr/bin/mc policy set none cmtechmap/raw-uploads

# Processed orthomosaics — COG files (read by TiTiler)
/usr/bin/mc mb --ignore-existing cmtechmap/orthomosaics
/usr/bin/mc policy set download cmtechmap/orthomosaics

# Point clouds — LAS/LAZ files
/usr/bin/mc mb --ignore-existing cmtechmap/point-clouds
/usr/bin/mc policy set download cmtechmap/point-clouds

# Digital Elevation Models — DSM/DTM GeoTIFFs
/usr/bin/mc mb --ignore-existing cmtechmap/elevation-models
/usr/bin/mc policy set download cmtechmap/elevation-models

# 3D textured meshes — OBJ/glTF
/usr/bin/mc mb --ignore-existing cmtechmap/3d-models
/usr/bin/mc policy set download cmtechmap/3d-models

# Generated reports — PDF/Excel
/usr/bin/mc mb --ignore-existing cmtechmap/reports
/usr/bin/mc policy set none cmtechmap/reports

# Database and system backups
/usr/bin/mc mb --ignore-existing cmtechmap/backups
/usr/bin/mc policy set none cmtechmap/backups

echo "✅ All 7 buckets created successfully."

# Set lifecycle rules — auto-delete raw uploads after 90 days to save storage
/usr/bin/mc ilm rule add cmtechmap/raw-uploads \
    --expire-days 90 \
    --prefix "" \
    --tags "auto-cleanup=true" 2>/dev/null || echo "⚠️  ILM rule skipped (may require MinIO enterprise)"

echo "🎉 MinIO initialization complete."
exit 0
