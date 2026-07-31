#!/usr/bin/env bash
# ==============================================================================
# CM TECHMAP — Caçador de capacidade ARM A1 no Oracle Always Free
#
# O shape VM.Standard.A1.Flex do Always Free vive esgotado: a Oracle responde
# 500-InternalError "Out of host capacity" em TODOS os availability domains.
# Não é cota (a conta tem 2 OCPU/12 GB liberados e 0 em uso) — são as máquinas
# ARM que estão ocupadas. A capacidade é devolvida de forma intermitente,
# então a única saída é tentar repetidamente.
#
# Recursos Always Free existem SOMENTE na região de origem da conta
# (us-ashburn-1 aqui) — trocar de região não contorna o problema.
#
# Estratégia por rodada: tenta o shape maior (2 OCPU/12 GB) nos 3 ADs; se
# nenhum aceitar, tenta o menor (1 OCPU/6 GB), que às vezes encaixa onde o
# maior não cabe. Para no primeiro sucesso.
#
# Uso:   nohup ./retry-a1-capacity.sh > /dev/null 2>&1 &
#        tail -f oci-deploy/a1-retry.log
# Parar: touch oci-deploy/.stop-retry     (ou matar o processo)
# ==============================================================================
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TFDIR="$HERE/terraform"
LOG="$HERE/a1-retry.log"
STOP="$HERE/.stop-retry"
TERRAFORM="${TERRAFORM_BIN:-terraform}"
INTERVAL="${RETRY_INTERVAL:-300}"   # 5 min entre rodadas

command -v "$TERRAFORM" >/dev/null 2>&1 || {
    echo "ERRO: terraform não encontrado. Defina TERRAFORM_BIN." >&2; exit 1; }

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

set_var() {  # set_var <chave> <valor>
    sed -i "s/^${1}\( *\)=.*/${1}\1= ${2}/" "$TFDIR/terraform.tfvars"
}

log "=== iniciando busca por capacidade A1 (intervalo ${INTERVAL}s) ==="
ROUND=0
while [[ ! -f "$STOP" ]]; do
    ROUND=$((ROUND + 1))
    for SHAPE in "2 12" "1 6"; do
        read -r OCPUS MEM <<< "$SHAPE"
        set_var "instance_ocpus" "$OCPUS"
        set_var "instance_memory_gb" "$MEM"

        for AD in 0 1 2; do
            set_var "availability_domain_index" "$AD"
            OUT="$(cd "$TFDIR" && timeout 300 "$TERRAFORM" apply -input=false -auto-approve 2>&1)"

            if grep -q 'Apply complete' <<< "$OUT"; then
                IP="$(cd "$TFDIR" && "$TERRAFORM" output -raw instance_public_ip 2>/dev/null)"
                log "SUCESSO — rodada ${ROUND}, AD-$((AD + 1)), ${OCPUS} OCPU/${MEM} GB, IP ${IP}"
                echo "$IP" > "$HERE/.a1-ip"
                log "=== VM criada. Próximo passo: provisionar o stack e migrar. ==="
                exit 0
            fi

            if grep -q 'Out of host capacity' <<< "$OUT"; then
                log "rodada ${ROUND} | ${OCPUS}c/${MEM}g | AD-$((AD + 1)): sem capacidade"
            else
                # Erro diferente de capacidade (cota, credencial, config) —
                # insistir não resolve e pode mascarar um problema real.
                log "rodada ${ROUND} | ${OCPUS}c/${MEM}g | AD-$((AD + 1)): ERRO DIFERENTE:"
                grep -E 'Error:|LimitExceeded|Unauthorized|NotAuthenticated' <<< "$OUT" | head -3 >> "$LOG"
            fi
        done
    done
    log "rodada ${ROUND} sem sucesso — aguardando ${INTERVAL}s"
    sleep "$INTERVAL"
done

log "=== interrompido pelo arquivo .stop-retry (rodada ${ROUND}) ==="
