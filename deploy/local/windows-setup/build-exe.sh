#!/usr/bin/env bash
# ==============================================================================
# CM TECHMAP — Compila o instalador gráfico do Windows (.exe)
# ==============================================================================
# Gera deploy/local/dist/CM-TECHMAP-Instalador.exe — o arquivo que o usuário
# final abre com duplo clique. Ele faz tudo sozinho, sem terminal.
#
# Compila NO LINUX com NSIS (sudo apt install nsis), então não é preciso ter
# uma máquina Windows para produzir o instalador.
#
# Uso:  ./build-exe.sh
# ==============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_DIR="$(cd "$HERE/.." && pwd)"          # deploy/local
APPS="$(cd "$LOCAL_DIR/../.." && pwd)"       # applications
PAYLOAD="$HERE/payload"
DIST="$LOCAL_DIR/dist"

command -v makensis >/dev/null || {
    echo "ERRO: NSIS não encontrado. Instale com: sudo apt install nsis" >&2; exit 1; }

echo ">> Montando o payload (o que vai dentro do .exe)"
rm -rf "$PAYLOAD"
mkdir -p "$PAYLOAD/applications/deploy/local" \
         "$PAYLOAD/applications/docker/postgres" \
         "$PAYLOAD/applications/docker/minio" \
         "$PAYLOAD/applications/docker/keycloak"

# Instaladores e configuração
for f in docker-compose.local.yml .env.local.example install.ps1 \
         cmtechmap.ps1 README.md GUIA-DE-TESTE.md; do
    cp "$LOCAL_DIR/$f" "$PAYLOAD/applications/deploy/local/"
done
cp -r "$LOCAL_DIR/nginx" "$PAYLOAD/applications/deploy/local/"

# Código-fonte: o compose COMPILA as imagens na máquina de destino, então o
# instalador precisa levar o código junto.
echo ">> Copiando código do backend"
tar -C "$APPS" \
    --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.egg-info' --exclude='.pytest_cache' --exclude='.ruff_cache' \
    --exclude='.git' --exclude='.github' \
    -cf - cm-techmap-backend | tar -C "$PAYLOAD/applications" -xf -

echo ">> Copiando código do frontend"
tar -C "$APPS" \
    --exclude='node_modules' --exclude='dist' --exclude='.vite' \
    --exclude='.git' --exclude='.github' \
    -cf - cm-techmap-frontend | tar -C "$PAYLOAD/applications" -xf -

echo ">> Copiando recursos de infraestrutura"
cp -r "$APPS/docker/postgres/init-scripts" "$PAYLOAD/applications/docker/postgres/"
cp "$APPS/docker/minio/init-buckets.sh"    "$PAYLOAD/applications/docker/minio/"
cp "$APPS/docker/keycloak/"*.json          "$PAYLOAD/applications/docker/keycloak/"

# Nenhum segredo pode ir junto: cada instalação sorteia os seus
if find "$PAYLOAD" \( -name '.env.local' -o -name '*.pem' -o -name '.env' \) | grep -q .; then
    echo "ERRO: o payload contém segredos. Abortando." >&2
    find "$PAYLOAD" \( -name '.env.local' -o -name '*.pem' -o -name '.env' \) >&2
    exit 1
fi

echo ">> Compilando o instalador"
mkdir -p "$DIST"
makensis -V2 "$HERE/CmTechMapSetup.nsi"

rm -rf "$PAYLOAD"

EXE="$DIST/CM-TECHMAP-Instalador.exe"
[[ -f "$EXE" ]] || { echo "ERRO: o .exe não foi gerado." >&2; exit 1; }

# ── Verificação do conteúdo ──────────────────────────────────────────────────
# Um .exe compilado sobre um payload incompleto é gerado sem erro nenhum e só
# falha na máquina do usuário. Extrair e conferir é a única prova real.
if command -v 7z >/dev/null; then
    echo ">> Verificando o conteúdo do instalador"
    CHECK="$(mktemp -d)"
    trap 'rm -rf "$CHECK"' EXIT
    7z x -o"$CHECK" "$EXE" > /dev/null 2>&1

    MISSING=0
    for f in applications/deploy/local/install.ps1 \
             applications/deploy/local/cmtechmap.ps1 \
             applications/deploy/local/docker-compose.local.yml \
             applications/deploy/local/nginx/gateway.conf \
             applications/deploy/local/.env.local.example \
             applications/cm-techmap-backend/Dockerfile \
             applications/cm-techmap-frontend/package.json \
             applications/docker/keycloak/cm-techmap-realm.json \
             applications/docker/minio/init-buckets.sh; do
        [[ -f "$CHECK/$f" ]] || { echo "  FALTA: $f" >&2; MISSING=$((MISSING + 1)); }
    done

    if (( MISSING > 0 )); then
        echo "ERRO: o instalador saiu incompleto ($MISSING arquivo(s) faltando)." >&2
        rm -f "$EXE"
        exit 1
    fi
    echo "  $(find "$CHECK/applications" -type f | wc -l) arquivos verificados"
else
    echo ">> AVISO: 7z ausente — conteúdo do .exe não verificado (apt install p7zip-full)"
fi

echo
echo "Instalador gerado:"
ls -lh "$EXE" | awk '{printf "  %s  (%s)\n", $9, $5}'
file "$EXE" | cut -d: -f2- | sed 's/^/ /'
echo
echo "Envie este arquivo ao usuário Windows. Basta abrir com duplo clique."
