#!/bin/bash
# ==============================================================================
# CM TECHMAP — PgBouncer Entrypoint
# Generates userlist.txt from environment variables
# ==============================================================================

set -euo pipefail

USERLIST="/etc/pgbouncer/userlist.txt"
PG_USER="${POSTGRES_USER:-cm_techmap_prod}"
PG_PASS="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}"

echo "[PgBouncer] Generating userlist..."

# Generate MD5 password hash (PgBouncer format)
MD5_HASH=$(echo -n "${PG_PASS}${PG_USER}" | md5sum | cut -d' ' -f1)

cat > "$USERLIST" << EOF
"${PG_USER}" "md5${MD5_HASH}"
"pgbouncer_admin" "md5$(echo -n "${PG_PASS}pgbouncer_admin" | md5sum | cut -d' ' -f1)"
"pgbouncer_stats" "md5$(echo -n "${PG_PASS}pgbouncer_stats" | md5sum | cut -d' ' -f1)"
EOF

chmod 600 "$USERLIST"

echo "[PgBouncer] Userlist generated for: ${PG_USER}, pgbouncer_admin, pgbouncer_stats"
echo "[PgBouncer] Starting pgbouncer..."

exec pgbouncer /etc/pgbouncer/pgbouncer.ini
