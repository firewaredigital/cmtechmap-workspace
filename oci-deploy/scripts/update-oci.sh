#!/bin/bash
# ==============================================================================
# CM TECHMAP — OCI Update Script
# Atualiza o deploy existente na VM OCI com novo código
# ==============================================================================
# Uso: ./oci-deploy/scripts/update-oci.sh [--rebuild]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TERRAFORM_DIR="$PROJECT_ROOT/oci-deploy/terraform"
SSH_KEY="${SSH_KEY_PATH:-$HOME/.ssh/oci_cm_techmap}"
REBUILD=false

# Parse args
[[ "${1:-}" == "--rebuild" ]] && REBUILD=true

# Get VM IP from terraform
cd "$TERRAFORM_DIR"
VM_IP=$(terraform output -raw instance_public_ip 2>/dev/null || echo "")

if [ -z "$VM_IP" ]; then
    echo "❌ Não foi possível obter o IP da VM."
    echo "   Verifique se o terraform state está disponível em $TERRAFORM_DIR"
    read -p "   Informe o IP manualmente: " VM_IP
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  CM TECHMAP — Atualizando Deploy OCI                        ║"
echo "║  VM: $VM_IP                                                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"

cd "$PROJECT_ROOT"

# 1. Sync código
echo "📦 Sincronizando código..."
rsync -avz --progress \
    --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' \
    --exclude '.venv' --exclude '.env' --exclude '.env.production' \
    --exclude '.env.oci' --exclude '*.pyc' --exclude '.next' --exclude 'dist' \
    --exclude 'cm-techmap-frontend/node_modules' --exclude 'teste-orto' \
    --exclude 'documentos-informativos' --exclude 'assets-design' \
    --exclude 'oci-deploy/terraform/.terraform' \
    --exclude 'oci-deploy/terraform/terraform.tfstate*' \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    ./ ubuntu@"$VM_IP":/opt/cm-techmap/

# 2. Rebuild ou restart
if [ "$REBUILD" = true ]; then
    echo "🔨 Rebuild completo dos containers..."
    ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "\
        cd /opt/cm-techmap && \
        docker compose -f docker-compose.oci.yml --env-file .env.oci build --parallel && \
        docker compose -f docker-compose.oci.yml --env-file .env.oci up -d --force-recreate"
else
    echo "🔄 Restart dos serviços alterados..."
    ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "\
        cd /opt/cm-techmap && \
        docker compose -f docker-compose.oci.yml --env-file .env.oci up -d --build"
fi

# 3. Migrations
echo "🗄️  Executando migrations..."
ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "\
    cd /opt/cm-techmap && \
    docker compose -f docker-compose.oci.yml --env-file .env.oci exec -T backend alembic upgrade head" \
    2>/dev/null || echo "  ⚠️  Migrations puladas"

# 4. Status
echo ""
echo "📊 Status dos serviços:"
ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "\
    cd /opt/cm-techmap && \
    docker compose -f docker-compose.oci.yml ps --format 'table {{.Name}}\t{{.Status}}'"

echo ""
echo "✅ Atualização concluída!"
echo "   🌐 http://$VM_IP"
echo "   📡 http://$VM_IP/api/v1/health"
