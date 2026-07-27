#!/usr/bin/env bash
# ==============================================================================
# CM TECHMAP — Instalador da plataforma LOCAL (Ubuntu / Debian / derivados)
# ==============================================================================
# Sobe a plataforma inteira nesta máquina e a publica sob um domínio próprio
# (padrão http://cmtechmap.local) — sem localhost, sem portas na URL.
#
# O que ele faz:
#   1. Confere pré-requisitos (Docker, Compose, RAM, disco)
#   2. Gera .env.local com segredos aleatórios (só na primeira vez)
#   3. Resolve conflito da porta 80, se houver
#   4. Registra o domínio em /etc/hosts
#   5. Compila e sobe os 12 serviços
#   6. Aplica as migrações do banco
#   7. Valida ponta a ponta e só então declara sucesso
#
# Uso:  sudo ./install.sh [--domain NOME] [--port N] [--yes]
# ==============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HERE/docker-compose.local.yml"
ENV_FILE="$HERE/.env.local"
ENV_EXAMPLE="$HERE/.env.local.example"
HOSTS_FILE="/etc/hosts"
HOSTS_MARKER="# CM TECHMAP (instalacao local)"

DOMAIN=""
PORT=""
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --port)   PORT="$2";   shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -20; exit 0 ;;
        *) echo "Opção desconhecida: $1" >&2; exit 1 ;;
    esac
done

# ── Saída ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; RESET=""
fi
step()  { echo; echo "${BOLD}▸ $*${RESET}"; }
ok()    { echo "  ${GREEN}✓${RESET} $*"; }
warn()  { echo "  ${YELLOW}!${RESET} $*"; }
die()   { echo "  ${RED}✗ $*${RESET}" >&2; exit 1; }
ask() { # ask <pergunta> ; retorna 0 para sim
    [[ $ASSUME_YES -eq 1 ]] && return 0
    local r; read -r -p "  $1 [s/N] " r </dev/tty || return 1
    [[ "$r" =~ ^[sSyY]$ ]]
}

# ══════════════════════════════════════════════════════════════════════════════
step "1/7 Verificando pré-requisitos"

[[ $EUID -eq 0 ]] || die "Rode com sudo: precisa editar $HOSTS_FILE e usar a porta 80."

# Usuário real por trás do sudo — o Docker costuma estar no grupo dele
REAL_USER="${SUDO_USER:-root}"

command -v docker >/dev/null 2>&1 || die "Docker não encontrado.
    Instale com:  curl -fsSL https://get.docker.com | sh
    Depois:       sudo usermod -aG docker $REAL_USER  (e reabra a sessão)"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 não encontrado.
    Instale o pacote docker-compose-plugin da sua distribuição."
docker info >/dev/null 2>&1 || die "O daemon do Docker não está rodando: sudo systemctl start docker"
ok "Docker $(docker --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1) e Compose OK"

TOTAL_RAM_GB=$(free -g | awk '/^Mem:/ {print $2}')
if (( TOTAL_RAM_GB < 8 )); then
    warn "RAM total: ${TOTAL_RAM_GB} GB. O NodeODM (fotogrametria) precisa de folga."
    warn "Com menos de 8 GB, reduza NODEODM_MEMORY_LIMIT no .env.local."
    ask "Continuar mesmo assim?" || die "Instalação cancelada."
else
    ok "RAM: ${TOTAL_RAM_GB} GB"
fi

FREE_DISK_GB=$(df -BG --output=avail "$HERE" | tail -1 | tr -dc '0-9')
(( FREE_DISK_GB >= 20 )) || die "Disco livre: ${FREE_DISK_GB} GB. São necessários ao menos 20 GB (imagens + dados)."
ok "Disco livre: ${FREE_DISK_GB} GB"

# ══════════════════════════════════════════════════════════════════════════════
step "2/7 Preparando configuração"

gen_secret() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32; }

if [[ -f "$ENV_FILE" ]]; then
    ok "Reaproveitando $ENV_FILE (segredos preservados)"
else
    # Volumes de uma instalação anterior guardam as senhas ANTIGAS: o
    # POSTGRES_PASSWORD só vale na primeira inicialização do banco. Gerar
    # segredos novos sobre dados antigos deixa Postgres/Keycloak/Martin em
    # loop de "password authentication failed" — falhar aqui é mais honesto.
    STALE="$(docker volume ls -q --filter 'name=^cml-' 2>/dev/null | tr '\n' ' ')"
    if [[ -n "${STALE// }" ]]; then
        echo "  ${YELLOW}Encontrei dados de uma instalação anterior:${RESET} ${STALE}"
        echo "  Mas o arquivo de segredos ($ENV_FILE) não existe mais, e as senhas"
        echo "  gravadas nesses volumes não podem ser recuperadas."
        echo
        echo "  Opções:"
        echo "    • recuperar o .env.local antigo (backup?) e rodar de novo"
        echo "    • apagar os dados e instalar do zero:"
        echo "        docker volume rm ${STALE}"
        echo "        sudo ./install.sh"
        die "Instalação interrompida para não corromper dados existentes."
    fi
    [[ -f "$ENV_EXAMPLE" ]] || die "Modelo não encontrado: $ENV_EXAMPLE"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    # Um segredo diferente por linha marcada com GERAR
    while grep -q '=GERAR$' "$ENV_FILE"; do
        sed -i "0,/=GERAR$/s//=$(gen_secret)/" "$ENV_FILE"
    done
    chmod 600 "$ENV_FILE"
    chown "$REAL_USER":"$REAL_USER" "$ENV_FILE" 2>/dev/null || true
    ok "$ENV_FILE criado com segredos aleatórios"
fi

set_env() { # set_env <chave> <valor>
    if grep -qE "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
get_env() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

[[ -n "$DOMAIN" ]] && set_env CM_DOMAIN "$DOMAIN"
DOMAIN="$(get_env CM_DOMAIN)"; DOMAIN="${DOMAIN:-cmtechmap.local}"

# .dev e .app estão na lista HSTS pré-carregada dos navegadores: o Chrome
# força HTTPS e a instalação local (HTTP) ficaria inacessível.
case "$DOMAIN" in
    *.dev|*.app) die "O domínio '$DOMAIN' usa um TLD com HTTPS obrigatório (HSTS).
    Use algo como cmtechmap.local — o navegador recusaria HTTP em .dev/.app." ;;
esac
ok "Domínio: $DOMAIN"

# ══════════════════════════════════════════════════════════════════════════════
step "3/7 Escolhendo a porta HTTP"

# Seleção DINÂMICA: nunca para serviços de terceiros para tomar a porta.
# Se a preferida estiver ocupada (Apache, IIS, outro projeto), procura a
# próxima livre e segue — o domínio continua limpo, só ganha o sufixo.
port_busy() { ss -tln 2>/dev/null | grep -qE "[:.]$1[[:space:]]"; }

port_holder() {
    ss -tlnp 2>/dev/null | grep -E "[:.]$1[[:space:]]" \
        | grep -oE 'users:\(\("[^"]+' | head -1 | cut -d'"' -f2
}

if [[ -n "$PORT" ]]; then
    # Porta pedida explicitamente com --port: respeitar ou falhar claramente
    port_busy "$PORT" && die "A porta $PORT (pedida com --port) já está em uso por: $(port_holder "$PORT")"
    set_env CM_HTTP_PORT "$PORT"
else
    PREFERRED="$(get_env CM_HTTP_PORT)"; PREFERRED="${PREFERRED:-80}"
    if ! port_busy "$PREFERRED"; then
        PORT="$PREFERRED"
        ok "Porta $PORT livre — a URL fica sem sufixo"
    else
        HOLDER="$(port_holder "$PREFERRED")"
        warn "Porta $PREFERRED ocupada${HOLDER:+ por '$HOLDER'} — mantida intacta"
        PORT=""
        for CANDIDATE in 8080 8090 8100 8200 8888 9080 9090; do
            port_busy "$CANDIDATE" || { PORT="$CANDIDATE"; break; }
        done
        if [[ -z "$PORT" ]]; then
            # Nenhuma das preferidas: varrer faixa alta até achar uma livre
            PORT=8300
            while port_busy "$PORT"; do
                PORT=$((PORT + 1))
                (( PORT > 8500 )) && die "Nenhuma porta livre entre 8300 e 8500."
            done
        fi
        set_env CM_HTTP_PORT "$PORT"
        ok "Porta $PORT escolhida automaticamente"
    fi
fi
PORT="$(get_env CM_HTTP_PORT)"

# O Keycloak monta o emissor dos tokens a partir desta URL; se o sufixo de
# porta não acompanhar, todo token é recusado com "Invalid token issuer".
if [[ "$PORT" == "80" ]]; then
    set_env CM_PORT_SUFFIX ""
    BASE_URL="http://$DOMAIN"
else
    set_env CM_PORT_SUFFIX ":$PORT"
    BASE_URL="http://$DOMAIN:$PORT"
fi
ok "Endereço de acesso: $BASE_URL"

# ══════════════════════════════════════════════════════════════════════════════
step "4/7 Registrando o domínio no sistema"

if grep -qE "^[^#]*[[:space:]]$DOMAIN([[:space:]]|$)" "$HOSTS_FILE"; then
    ok "$DOMAIN já consta em $HOSTS_FILE"
else
    cp "$HOSTS_FILE" "${HOSTS_FILE}.cmtechmap.bak"
    printf '\n%s\n127.0.0.1\t%s\n' "$HOSTS_MARKER" "$DOMAIN" >> "$HOSTS_FILE"
    ok "$DOMAIN → 127.0.0.1 (backup em ${HOSTS_FILE}.cmtechmap.bak)"
fi

# Em .local o mDNS (avahi) pode sequestrar a resolução. /etc/hosts costuma
# ter prioridade, mas isso depende do nsswitch.conf — conferir é barato.
RESOLVED="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1)"
if [[ "$RESOLVED" != "127.0.0.1" ]]; then
    die "O nome $DOMAIN resolve para '${RESOLVED:-nada}' em vez de 127.0.0.1.
    Provável interferência do mDNS (avahi) em domínios .local.
    Soluções: garanta 'files' antes de 'mdns' em /etc/nsswitch.conf,
    ou reinstale com outro sufixo:  sudo ./install.sh --domain cmtechmap.interno"
fi
ok "Resolução verificada: $DOMAIN → 127.0.0.1"

# ══════════════════════════════════════════════════════════════════════════════
step "5/7 Compilando e subindo os serviços (demora alguns minutos)"

cd "$HERE"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" config --quiet \
    || die "Configuração do compose inválida."

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" build --pull 2>&1 \
    | grep -E 'ERROR|error:|Built|Building' | tail -5 || true

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --remove-orphans \
    || die "Falha ao subir os serviços. Veja: docker compose -f $COMPOSE_FILE logs"
ok "Serviços iniciados"

# ══════════════════════════════════════════════════════════════════════════════
step "6/7 Aguardando o banco e aplicando migrações"

for i in $(seq 1 60); do
    if docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
         exec -T backend curl -sf http://localhost:8000/api/v1/health >/dev/null 2>&1; then
        ok "Backend respondendo (tentativa $i)"
        break
    fi
    sleep 5
done

docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
    exec -T backend alembic upgrade head 2>&1 | tail -3 \
    || die "As migrações do banco falharam."
ok "Banco migrado"

# ══════════════════════════════════════════════════════════════════════════════
step "7/7 Validando a instalação"

FAILURES=0
check() { # check <descrição> <url> <código esperado>
    local code
    # `|| true` (não `|| echo 000`): em falha o curl JÁ imprime 000, e sem o
    # `|| true` seu código de saída mataria o script sob `set -e`.
    code="$(curl -s -m 30 -o /dev/null -w '%{http_code}' "$2" 2>/dev/null || true)"
    code="${code:-000}"
    if [[ "$code" == "$3" ]]; then
        ok "$1"
    else
        warn "$1 — recebido HTTP $code (esperado $3)"
        FAILURES=$((FAILURES + 1))
    fi
}

# O Keycloak leva minutos para subir na primeira vez (importa o realm)
echo "  aguardando o serviço de autenticação (pode levar ~3 min na 1ª vez)..."
for i in $(seq 1 60); do
    curl -sf -m 10 "$BASE_URL/realms/$(get_env KEYCLOAK_REALM)/.well-known/openid-configuration" >/dev/null 2>&1 && break
    sleep 10
done

check "Interface web responde"        "$BASE_URL/"                    200
check "API de saúde"                  "$BASE_URL/api/v1/health"       200
check "Autenticação (OIDC)"           "$BASE_URL/realms/$(get_env KEYCLOAK_REALM)/.well-known/openid-configuration" 200
check "Rota de callback do login"     "$BASE_URL/auth/callback"       200

echo
if (( FAILURES == 0 )); then
    echo "${GREEN}${BOLD}════════════════════════════════════════════════════${RESET}"
    echo "${GREEN}${BOLD}  CM TECHMAP instalado e funcionando${RESET}"
    echo "${GREEN}${BOLD}════════════════════════════════════════════════════${RESET}"
    echo
    echo "  Acesse:  ${BOLD}$BASE_URL${RESET}"
    echo
    echo "  Usuário de demonstração:"
    echo "    superadmin@cmtechmap.com.br / SuperAdmin@2026"
    echo "    ${YELLOW}(troque em $BASE_URL/admin — é senha pública de exemplo)${RESET}"
    echo
    echo "  Operação:  ./cmtechmap {start|stop|status|logs|backup|uninstall}"
else
    echo "${YELLOW}${BOLD}Instalação concluída com $FAILURES verificação(ões) falhando.${RESET}"
    echo "  Alguns serviços podem ainda estar iniciando. Reveja em alguns minutos:"
    echo "    ./cmtechmap status"
    echo "    ./cmtechmap logs"
    exit 1
fi
