#!/usr/bin/env bash
# ==============================================================================
# CM TECHMAP — Deploy do backend na VM OCI de produção
#
# A VM (/opt/cm-techmap) é um ESPELHO por rsync, não um clone git — rodar
# `git pull` lá falha com "not a git repository". Todo comando docker compose
# precisa de `--env-file .env.oci`, senão a interpolação aborta.
#
# O build é lançado com nohup + polling do log porque a compilação consome
# toda a RAM de 1 GB e derruba a sessão SSH — um build em foreground morre
# junto com a conexão e a imagem antiga fica no ar silenciosamente.
#
# Uso:  ./deploy-vm.sh [--skip-build]
# Env:  VM_HOST (padrão 143.47.116.9), VM_USER (ubuntu), SSH_KEY (~/.ssh/oci_cm_techmap_rsa)
# ==============================================================================
set -euo pipefail

VM_HOST="${VM_HOST:-143.47.116.9}"
VM_USER="${VM_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/oci_cm_techmap_rsa}"
REMOTE_DIR="/opt/cm-techmap"
COMPOSE="docker-compose.oci-micro.yml"
SKIP_BUILD="${1:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # applications/
SSH_OPTS=(-i "$SSH_KEY" -o ConnectTimeout=30 -o StrictHostKeyChecking=accept-new)
ssh_vm() { ssh "${SSH_OPTS[@]}" "${VM_USER}@${VM_HOST}" "$@"; }

echo ">> 1/5 Sincronizando código para ${VM_HOST}..."
rsync -az --delete \
    -e "ssh ${SSH_OPTS[*]}" \
    --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' \
    "$HERE/cm-techmap-backend/" "${VM_USER}@${VM_HOST}:${REMOTE_DIR}/cm-techmap-backend/"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$HERE/$COMPOSE" "${VM_USER}@${VM_HOST}:${REMOTE_DIR}/$COMPOSE"
rsync -az -e "ssh ${SSH_OPTS[*]}" \
    "$HERE/docker/nginx/" "${VM_USER}@${VM_HOST}:${REMOTE_DIR}/docker/nginx/"

echo ">> 2/5 Validando compose na VM..."
ssh_vm "cd $REMOTE_DIR && docker compose --env-file .env.oci -f $COMPOSE config --quiet"

if [[ "$SKIP_BUILD" != "--skip-build" ]]; then
    echo ">> 3/5 Build das imagens (background — SSH cai sob pressão de memória)..."
    ssh_vm "cd $REMOTE_DIR && rm -f /tmp/deploy-build.log && \
        nohup sh -c 'docker compose --env-file .env.oci -f $COMPOSE build backend celery-worker celery-beat nginx >> /tmp/deploy-build.log 2>&1 && echo BUILD_DONE >> /tmp/deploy-build.log || echo BUILD_FAIL >> /tmp/deploy-build.log' > /dev/null 2>&1 & echo iniciado"

    for _ in $(seq 1 90); do
        MARK="$(ssh_vm "grep -oE 'BUILD_DONE|BUILD_FAIL' /tmp/deploy-build.log 2>/dev/null | head -1" || true)"
        [[ -n "$MARK" ]] && break
        sleep 20
    done
    if [[ "${MARK:-}" != "BUILD_DONE" ]]; then
        echo "ERRO: build terminou em ${MARK:-timeout}. Log:" >&2
        ssh_vm "tail -30 /tmp/deploy-build.log" >&2
        exit 1
    fi
    echo "   build OK"
else
    echo ">> 3/5 Build pulado (--skip-build)"
fi

echo ">> 4/5 Subindo serviços de aplicação e aplicando migrações..."
# Só os serviços que mudam a cada deploy. Recriar o Keycloak custa ~20 min de
# autenticação fora do ar nesta VM: ao detectar mudança de configuração ele
# refaz a augmentation do Quarkus (~7 min) e ainda precisa aquecer o JIT.
# Postgres/Redis/MinIO têm estado e não devem ser reciclados à toa.
ssh_vm "cd $REMOTE_DIR && \
    docker compose --env-file .env.oci -f $COMPOSE up -d backend celery-worker celery-beat nginx && \
    sleep 20 && \
    docker compose --env-file .env.oci -f $COMPOSE exec -T backend alembic upgrade head"

echo ">> 5/5 Verificando saúde..."
for i in $(seq 1 30); do
    CODE="$(curl -s -m 20 -o /dev/null -w '%{http_code}' "http://${VM_HOST}/api/v1/health" || true)"
    [[ "$CODE" == "200" ]] && { echo "   health: 200 (tentativa $i)"; break; }
    sleep 10
done
[[ "${CODE:-}" == "200" ]] || { echo "ERRO: backend não respondeu (último: ${CODE:-sem resposta})" >&2; exit 1; }

ssh_vm "docker ps --format '{{.Names}} {{.Status}}' | grep '^cmo-'"
echo "OK: backend publicado em http://${VM_HOST}"
