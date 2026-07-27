#!/usr/bin/env bash
# ==============================================================================
# CM TECHMAP — Deploy do frontend na Vercel via REST API
#
# Por que não usar o auto-deploy do git: a conta Vercel (firewaredigital-6923,
# plano Hobby) está logada com uma identidade GitHub diferente do autor dos
# commits (GustavoSaraivap). Deploys criados pelo webhook do GitHub entram em
# estado BLOCKED aguardando autorização manual no dashboard. Deploys criados
# pela API com o token do dono da conta não passam por essa checagem.
#
# Token: variável VERCEL_TOKEN, ou o arquivo ~/.config/cm-techmap/vercel-token.
# Uso:   ./deploy-vercel.sh [branch]   (padrão: main — deploya o HEAD no GitHub)
# ==============================================================================
set -euo pipefail

PROJECT_ID="prj_PC1l2kXebZdNMsmUK4Vz4A4pnFe2"
PROJECT_NAME="cm-techmap-frontend"
REPO_ID=1260718990   # firewaredigital/cm-techmap-frontend
BRANCH="${1:-main}"
TOKEN_FILE="$HOME/.config/cm-techmap/vercel-token"
PUBLIC_URL="https://cm-techmap-frontend.vercel.app"

TOKEN="${VERCEL_TOKEN:-}"
if [[ -z "$TOKEN" && -f "$TOKEN_FILE" ]]; then
    TOKEN="$(<"$TOKEN_FILE")"
fi
if [[ -z "$TOKEN" ]]; then
    echo "ERRO: defina VERCEL_TOKEN ou grave o token em $TOKEN_FILE" >&2
    echo "      (crie um em https://vercel.com/account/settings/tokens)" >&2
    exit 1
fi

command -v jq >/dev/null || { echo "ERRO: jq é obrigatório" >&2; exit 1; }

echo ">> Criando deploy de produção da branch ${BRANCH} (HEAD no GitHub)..."
RESP="$(curl -sS -X POST "https://api.vercel.com/v13/deployments" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$PROJECT_NAME\",\"project\":\"$PROJECT_ID\",\"target\":\"production\",\"gitSource\":{\"type\":\"github\",\"repoId\":$REPO_ID,\"ref\":\"$BRANCH\"}}")"

DEPLOY_ID="$(jq -r '.id // empty' <<<"$RESP")"
if [[ -z "$DEPLOY_ID" ]]; then
    echo "ERRO ao criar o deploy:" >&2
    jq '.error // .' <<<"$RESP" >&2
    exit 1
fi
echo "   id:       $DEPLOY_ID"
echo "   inspetor: $(jq -r '.inspectorUrl // "-"' <<<"$RESP")"

echo ">> Aguardando build..."
STATE="QUEUED"
for _ in $(seq 1 90); do
    STATE="$(curl -sS "https://api.vercel.com/v13/deployments/$DEPLOY_ID" \
        -H "Authorization: Bearer $TOKEN" | jq -r '.readyState // "?"')"
    printf '   %-14s\r' "$STATE"
    case "$STATE" in READY|ERROR|CANCELED|BLOCKED) break ;; esac
    sleep 5
done
printf '\n'

if [[ "$STATE" != "READY" ]]; then
    echo "ERRO: deploy terminou em estado $STATE (veja o inspetor acima)" >&2
    exit 1
fi

echo ">> READY. Verificando a URL pública..."
BUNDLE="$(curl -s -m 30 "$PUBLIC_URL/" | grep -oE 'index-[A-Za-z0-9_-]+\.js' | head -1 || true)"
HEALTH="$(curl -s -m 30 -o /dev/null -w '%{http_code}' "$PUBLIC_URL/api/v1/health" || true)"
echo "   bundle: ${BUNDLE:-?} | /api/v1/health: HTTP ${HEALTH:-?}"
echo "OK: produção publicada em $PUBLIC_URL"
