#!/bin/bash
# ==============================================================================
# CM TECHMAP — Multi-Tenant Schema Migration
# Applies database migrations to ALL tenant schemas idempotently
# ==============================================================================
# Usage:
#   docker compose exec backend python -m scripts.migrate_all_tenants
#   # Or from host:
#   bash scripts/migrate-tenants.sh
# ==============================================================================

set -euo pipefail

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  CM TECHMAP — Multi-Tenant Schema Migration                    ║"
echo "╚══════════════════════════════════════════════════════════════════╝"

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.production}"

# Check if running inside container
if [ -f "/code/app/main.py" ]; then
    echo "[MIGRATE] Running inside container..."
    python -c "
import asyncio
from app.core.database import get_direct_db_session, engine_direct
from app.services.tenant_lifecycle import migrate_all_tenants
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

async def run():
    factory = async_sessionmaker(engine_direct, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        results = await migrate_all_tenants(session)
        for r in results:
            status = r.get('status', 'unknown')
            schema = r.get('schema', '?')
            if status == 'migrated':
                print(f'  ✅ {schema}: v{r[\"from_version\"]} → v{r[\"to_version\"]}')
            elif status == 'up_to_date':
                print(f'  ✔️  {schema}: already at v{r[\"version\"]}')
            else:
                print(f'  ❌ {schema}: {status} — {r.get(\"error\", \"\")}')
        print(f'\\nTotal: {len(results)} tenant(s) processed')

asyncio.run(run())
"
else
    echo "[MIGRATE] Running from host — executing in backend container..."
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" exec -T backend python -c "
import asyncio
from app.core.database import engine_direct
from app.services.tenant_lifecycle import migrate_all_tenants
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

async def run():
    factory = async_sessionmaker(engine_direct, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        results = await migrate_all_tenants(session)
        for r in results:
            status = r.get('status', 'unknown')
            schema = r.get('schema', '?')
            if status == 'migrated':
                print(f'  ✅ {schema}: v{r.get(\"from_version\",0)} → v{r.get(\"to_version\",0)}')
            elif status == 'up_to_date':
                print(f'  ✔️  {schema}: already at v{r.get(\"version\",0)}')
            else:
                print(f'  ❌ {schema}: {status}')
        print(f'\\nTotal: {len(results)} tenant(s) processed')

asyncio.run(run())
"
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "✅ Migration complete"
echo "═══════════════════════════════════════════════════════════════════"
