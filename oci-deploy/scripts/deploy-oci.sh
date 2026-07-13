#!/bin/bash
# ==============================================================================
# CM TECHMAP — OCI Deploy Script (máquina local → OCI)
# Provisiona infraestrutura via Terraform e deploy da aplicação via SSH
# ==============================================================================
#
# Pré-requisitos:
#   1. OCI CLI configurado (~/.oci/config)
#   2. Terraform >= 1.5 instalado
#   3. SSH key gerada (ssh-keygen -t ed25519 -f ~/.ssh/oci_cm_techmap)
#   4. terraform.tfvars preenchido (copiar de terraform.tfvars.example)
#
# Uso:
#   chmod +x oci-deploy/scripts/deploy-oci.sh
#   ./oci-deploy/scripts/deploy-oci.sh
# ==============================================================================

set -euo pipefail

# ── Cores para output ────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

log_info()    { echo -e "${BLUE}ℹ️  $1${NC}"; }
log_success() { echo -e "${GREEN}✅ $1${NC}"; }
log_warn()    { echo -e "${YELLOW}⚠️  $1${NC}"; }
log_error()   { echo -e "${RED}❌ $1${NC}"; }
log_step()    { echo -e "\n${CYAN}${BOLD}═══ $1 ═══${NC}\n"; }

# ── Diretórios ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TERRAFORM_DIR="$PROJECT_ROOT/oci-deploy/terraform"
SSH_KEY="${SSH_KEY_PATH:-$HOME/.ssh/oci_cm_techmap}"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║  ${BOLD}CM TECHMAP — Oracle Cloud Deploy (Always Free)${NC}${CYAN}                 ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# FASE 1: Pré-flight checks
# ══════════════════════════════════════════════════════════════════════════════
log_step "FASE 1/5 — Verificação de Pré-requisitos"

# Terraform
if command -v terraform &>/dev/null; then
    TF_VERSION=$(terraform version -json 2>/dev/null | jq -r '.terraform_version' 2>/dev/null || terraform version | head -1)
    log_success "Terraform: $TF_VERSION"
else
    log_error "Terraform não encontrado. Instale: https://developer.hashicorp.com/terraform/install"
    exit 1
fi

# OCI CLI (opcional mas recomendado)
if command -v oci &>/dev/null; then
    log_success "OCI CLI instalado"
else
    log_warn "OCI CLI não encontrado (opcional). Instale: https://docs.oracle.com/iaas/Content/API/SDKDocs/cliinstall.htm"
fi

# SSH Key
if [ -f "$SSH_KEY" ]; then
    log_success "SSH Key: $SSH_KEY"
else
    log_warn "SSH Key não encontrada em $SSH_KEY"
    read -p "  Deseja gerar uma nova SSH key? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ssh-keygen -t ed25519 -f "$SSH_KEY" -C "cm-techmap-oci" -N ""
        log_success "SSH Key gerada: $SSH_KEY"
    else
        log_error "SSH Key necessária para continuar."
        exit 1
    fi
fi

# Terraform vars
if [ ! -f "$TERRAFORM_DIR/terraform.tfvars" ]; then
    log_error "terraform.tfvars não encontrado!"
    log_info "Copie e preencha o arquivo de exemplo:"
    echo "  cp $TERRAFORM_DIR/terraform.tfvars.example $TERRAFORM_DIR/terraform.tfvars"
    echo "  nano $TERRAFORM_DIR/terraform.tfvars"
    exit 1
fi
log_success "terraform.tfvars encontrado"

# Docker + rsync
command -v rsync &>/dev/null || { log_error "rsync não encontrado. Instale: sudo apt install rsync"; exit 1; }
log_success "rsync disponível"

echo ""
log_success "Todos os pré-requisitos verificados!"

# ══════════════════════════════════════════════════════════════════════════════
# FASE 2: Provisionar Infraestrutura com Terraform
# ══════════════════════════════════════════════════════════════════════════════
log_step "FASE 2/5 — Provisionando Infraestrutura OCI"

cd "$TERRAFORM_DIR"

# Init
log_info "Inicializando Terraform..."
terraform init -upgrade

# Plan
log_info "Planejando recursos..."
terraform plan -out=tfplan

echo ""
read -p "  Aplicar este plano? [y/N] " -n 1 -r
echo ""
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    log_warn "Deploy cancelado pelo usuário."
    exit 0
fi

# Apply
log_info "Aplicando... (pode levar 2-5 minutos)"
terraform apply tfplan

# Extrair outputs
VM_IP=$(terraform output -raw instance_public_ip)
log_success "VM provisionada: $VM_IP"

# Limpa plan file
rm -f tfplan

# ══════════════════════════════════════════════════════════════════════════════
# FASE 3: Aguardar VM ficar pronta
# ══════════════════════════════════════════════════════════════════════════════
log_step "FASE 3/5 — Aguardando VM ficar pronta"

log_info "Aguardando cloud-init concluir (pode levar 3-8 minutos)..."
log_info "A VM está instalando Docker, configurando storage, firewall, etc."

MAX_RETRIES=40
RETRY=0
while [ $RETRY -lt $MAX_RETRIES ]; do
    if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes \
       -i "$SSH_KEY" ubuntu@"$VM_IP" \
       "test -f /var/lib/cloud/instance/boot-finished" 2>/dev/null; then
        log_success "VM pronta! Cloud-init concluído."
        break
    fi
    RETRY=$((RETRY + 1))
    echo -ne "  Tentativa $RETRY/$MAX_RETRIES...\r"
    sleep 15
done

if [ $RETRY -eq $MAX_RETRIES ]; then
    log_error "Timeout aguardando VM. Tente SSH manual:"
    echo "  ssh -i $SSH_KEY ubuntu@$VM_IP"
    exit 1
fi

# Verificar Docker
ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "docker --version && docker compose version"
log_success "Docker verificado na VM"

# ══════════════════════════════════════════════════════════════════════════════
# FASE 4: Deploy da Aplicação
# ══════════════════════════════════════════════════════════════════════════════
log_step "FASE 4/5 — Deploy da Aplicação"

cd "$PROJECT_ROOT"

# Sync do código para a VM
log_info "Sincronizando código para a VM..."
rsync -avz --progress \
    --exclude '.git' \
    --exclude 'node_modules' \
    --exclude '__pycache__' \
    --exclude '.venv' \
    --exclude '.env' \
    --exclude '.env.production' \
    --exclude '*.pyc' \
    --exclude '.next' \
    --exclude 'dist' \
    --exclude 'cm-techmap-frontend/node_modules' \
    --exclude 'teste-orto' \
    --exclude 'documentos-informativos' \
    --exclude 'assets-design' \
    --exclude 'maps-research-transcript.md' \
    --exclude 'oci-deploy/terraform/.terraform' \
    --exclude 'oci-deploy/terraform/terraform.tfstate*' \
    --exclude 'oci-deploy/terraform/tfplan' \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
    ./ ubuntu@"$VM_IP":/opt/cm-techmap/

log_success "Código sincronizado"

# Executar setup na VM
log_info "Executando setup na VM..."
ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "chmod +x /opt/cm-techmap/oci-deploy/scripts/setup-oci-vm.sh && sudo /opt/cm-techmap/oci-deploy/scripts/setup-oci-vm.sh"

# ══════════════════════════════════════════════════════════════════════════════
# FASE 5: Verificação
# ══════════════════════════════════════════════════════════════════════════════
log_step "FASE 5/5 — Verificação Final"

log_info "Aguardando serviços ficarem healthy (60s)..."
sleep 60

# Health checks
log_info "Verificando serviços..."
HEALTH_OK=true

for endpoint in "http://$VM_IP/nginx-health" "http://$VM_IP/api/v1/health"; do
    STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "$endpoint" 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ]; then
        log_success "$endpoint → HTTP $STATUS"
    else
        log_warn "$endpoint → HTTP $STATUS (pode estar iniciando)"
        HEALTH_OK=false
    fi
done

# Docker stats na VM
echo ""
log_info "Status dos containers:"
ssh -i "$SSH_KEY" ubuntu@"$VM_IP" "cd /opt/cm-techmap && docker compose -f docker-compose.oci.yml ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'" || true

# ══════════════════════════════════════════════════════════════════════════════
# RESUMO
# ══════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
if [ "$HEALTH_OK" = true ]; then
    echo -e "${GREEN}${BOLD}✅ CM TECHMAP — Deploy concluído com sucesso!${NC}"
else
    echo -e "${YELLOW}${BOLD}⚠️  CM TECHMAP — Deploy concluído (alguns serviços ainda iniciando)${NC}"
fi
echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  🌐 Aplicação:    ${BOLD}http://$VM_IP${NC}"
echo -e "  📡 API Health:   ${BOLD}http://$VM_IP/api/v1/health${NC}"
echo -e "  📄 API Docs:     ${BOLD}http://$VM_IP/docs${NC}"
echo -e "  🔑 Keycloak:     ${BOLD}http://$VM_IP:18080${NC} (via SSH tunnel)"
echo ""
echo -e "  🖥️  SSH:          ${BOLD}ssh -i $SSH_KEY ubuntu@$VM_IP${NC}"
echo -e "  📊 Docker Stats: ${BOLD}ssh -i $SSH_KEY ubuntu@$VM_IP docker stats --no-stream${NC}"
echo ""
echo -e "  ${YELLOW}Próximos passos:${NC}"
echo -e "    1. Configurar DNS (se tiver domínio): A record → $VM_IP"
echo -e "    2. Instalar Let's Encrypt:"
echo -e "       ssh -i $SSH_KEY ubuntu@$VM_IP"
echo -e "       sudo certbot certonly --webroot -w /data/certbot -d SEU_DOMINIO"
echo -e "    3. Para atualizações futuras: ./oci-deploy/scripts/update-oci.sh"
echo -e "${CYAN}═══════════════════════════════════════════════════════════════════${NC}"
