#!/usr/bin/env bash
# ==============================================================================
# CM TECHMAP — Gera os pacotes de instalação (Windows e Linux)
# ==============================================================================
# Produz dois arquivos autocontidos em deploy/local/dist/:
#
#   cm-techmap-local-windows.zip     → descompactar e rodar install.ps1
#   cm-techmap-local-linux.tar.gz    → descompactar e rodar install.sh
#
# Por que o pacote leva o código-fonte: o docker-compose.local.yml COMPILA as
# imagens do backend e do frontend na máquina de destino. Enviar só os
# scripts de instalação produziria um pacote que falha no primeiro build.
#
# Os pacotes NÃO contêm segredos: o .env.local é gerado na instalação, com
# senhas aleatórias diferentes em cada máquina.
#
# Uso:  ./build-package.sh
# ==============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="$(cd "$HERE/../.." && pwd)"          # .../applications
DIST="$HERE/dist"
STAGE="$DIST/.stage/cm-techmap-local"
VERSION="$(date +%Y.%m.%d)"

command -v zip >/dev/null || { echo "ERRO: 'zip' não instalado (sudo apt install zip)" >&2; exit 1; }

echo ">> Limpando área de trabalho"
rm -rf "$DIST/.stage" "$DIST/cm-techmap-local-windows.zip" "$DIST/cm-techmap-local-linux.tar.gz"
mkdir -p "$STAGE/applications/deploy/local" \
         "$STAGE/applications/docker/postgres" \
         "$STAGE/applications/docker/minio" \
         "$STAGE/applications/docker/keycloak"

# ── Instaladores e configuração ──────────────────────────────────────────────
echo ">> Copiando instaladores"
for f in docker-compose.local.yml .env.local.example install.sh install.ps1 \
         cmtechmap cmtechmap.ps1 README.md; do
    cp "$HERE/$f" "$STAGE/applications/deploy/local/"
done
cp -r "$HERE/nginx" "$STAGE/applications/deploy/local/"

# ── Código-fonte (sem artefatos de build) ────────────────────────────────────
echo ">> Copiando código do backend"
tar -C "$APPS" \
    --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='*.egg-info' --exclude='.pytest_cache' --exclude='.ruff_cache' \
    -cf - cm-techmap-backend | tar -C "$STAGE/applications" -xf -

echo ">> Copiando código do frontend"
tar -C "$APPS" \
    --exclude='node_modules' --exclude='dist' --exclude='.vite' \
    -cf - cm-techmap-frontend | tar -C "$STAGE/applications" -xf -

# ── Recursos de infraestrutura referenciados pelo compose ────────────────────
echo ">> Copiando recursos de infraestrutura"
cp -r "$APPS/docker/postgres/init-scripts" "$STAGE/applications/docker/postgres/"
cp "$APPS/docker/minio/init-buckets.sh"    "$STAGE/applications/docker/minio/"
cp "$APPS/docker/keycloak/"*.json          "$STAGE/applications/docker/keycloak/"

# ── Instruções na raiz do pacote ─────────────────────────────────────────────
cat > "$STAGE/LEIA-ME.txt" <<EOF
CM TECHMAP — Instalação Local (versão $VERSION)
================================================================

WINDOWS
-------
1. Instale o Docker Desktop e aguarde o ícone ficar verde:
   https://www.docker.com/products/docker-desktop/

2. Abra o PowerShell COMO ADMINISTRADOR
   (botão direito no PowerShell -> "Executar como administrador")

3. Vá até a pasta do instalador e execute:

     cd "CAMINHO\\ONDE\\DESCOMPACTOU\\cm-techmap-local\\applications\\deploy\\local"
     powershell -ExecutionPolicy Bypass -File .\\install.ps1

4. Ao final o navegador abre sozinho em http://cmtechmap.local


UBUNTU / LINUX
--------------
1. Instale o Docker, se ainda não tiver:

     curl -fsSL https://get.docker.com | sh
     sudo usermod -aG docker \$USER      # reabra a sessão depois

2. Vá até a pasta do instalador e execute:

     cd caminho/onde/descompactou/cm-techmap-local/applications/deploy/local
     sudo ./install.sh

3. Ao final, acesse o endereço que o instalador informar


O QUE ESPERAR
-------------
A primeira instalação demora de 10 a 20 minutos: o computador compila as
imagens e baixa os componentes. As próximas subidas levam segundos.

Requisitos: 8 GB de RAM (16 GB recomendado) e 20 GB de disco livre.

Precisa ser administrador porque o instalador registra o endereço
"cmtechmap.local" no arquivo de hosts do sistema.

Se a porta 80 estiver ocupada por outro programa, o instalador escolhe
outra automaticamente e informa o endereço final — nada é desligado.


DEPOIS DE INSTALAR
------------------
Windows:  .\\cmtechmap.ps1 {start|stop|status|logs|backup|update|uninstall}
Linux:    ./cmtechmap      {start|stop|status|logs|backup|update|uninstall}

Documentação completa: applications/deploy/local/README.md
EOF

# Instruções também em .md para quem abrir no editor
cp "$STAGE/LEIA-ME.txt" "$STAGE/LEIA-ME.md"

# ── Gera os pacotes ──────────────────────────────────────────────────────────
echo ">> Gerando pacote Windows (.zip)"
( cd "$DIST/.stage" && zip -qr "$DIST/cm-techmap-local-windows.zip" cm-techmap-local )

echo ">> Gerando pacote Linux (.tar.gz)"
# Preserva o bit de execução dos scripts .sh e do CLI
chmod +x "$STAGE/applications/deploy/local/install.sh" \
         "$STAGE/applications/deploy/local/cmtechmap"
( cd "$DIST/.stage" && tar -czf "$DIST/cm-techmap-local-linux.tar.gz" cm-techmap-local )

rm -rf "$DIST/.stage"

echo
echo "Pacotes gerados em $DIST:"
ls -lh "$DIST" | awk 'NR>1 {printf "  %-38s %s\n", $9, $5}'
echo
echo "Envie o .zip para máquinas Windows e o .tar.gz para Linux."
