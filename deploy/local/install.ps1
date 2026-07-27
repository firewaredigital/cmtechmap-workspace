# ==============================================================================
# CM TECHMAP - Instalador da plataforma LOCAL (Windows 10/11)
# ==============================================================================
# Sobe a plataforma inteira nesta maquina e a publica sob um dominio proprio
# (padrao http://cmtechmap.local) - sem localhost, sem portas na URL.
#
# Pre-requisito: Docker Desktop instalado e em execucao.
#   https://www.docker.com/products/docker-desktop/
#
# COMO EXECUTAR (precisa ser como Administrador - edita o arquivo de hosts):
#   1. Clique com o botao direito no PowerShell -> "Executar como administrador"
#   2. cd caminho\para\applications\deploy\local
#   3. powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Parametros:  -Domain cmtechmap.local  -Port 80  -Yes
# ==============================================================================
[CmdletBinding()]
param(
    [string]$Domain = "",
    [int]$Port = 0,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
$Here        = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $Here "docker-compose.local.yml"
$EnvFile     = Join-Path $Here ".env.local"
$EnvExample  = Join-Path $Here ".env.local.example"
$HostsFile   = "$env:SystemRoot\System32\drivers\etc\hosts"
$HostsMarker = "# CM TECHMAP (instalacao local)"

function Write-Step { param($m) Write-Host ""; Write-Host "> $m" -ForegroundColor White -BackgroundColor DarkBlue }
function Write-Ok   { param($m) Write-Host "  [ok] $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Stop-Fail  { param($m) Write-Host "  [X]  $m" -ForegroundColor Red; exit 1 }
function Ask {
    param($q)
    if ($Yes) { return $true }
    $r = Read-Host "  $q [s/N]"
    return ($r -eq "s" -or $r -eq "S" -or $r -eq "y" -or $r -eq "Y")
}

# ==============================================================================
Write-Step "1/7 Verificando pre-requisitos"

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Stop-Fail "Execute o PowerShell como Administrador (precisa editar o arquivo de hosts)."
}

try { docker --version | Out-Null } catch {
    Stop-Fail "Docker nao encontrado. Instale o Docker Desktop:`n    https://www.docker.com/products/docker-desktop/"
}
try { docker compose version | Out-Null } catch {
    Stop-Fail "Docker Compose v2 nao encontrado. Atualize o Docker Desktop."
}
try { docker info 2>&1 | Out-Null } catch {
    Stop-Fail "O Docker Desktop nao esta em execucao. Abra-o e aguarde ficar pronto."
}
Write-Ok "Docker e Compose disponiveis"

# No Windows a RAM util e a alocada para o backend do Docker (WSL2)
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if ($ramGB -lt 8) {
    Write-Warn "RAM total: $ramGB GB. O NodeODM (fotogrametria) precisa de folga."
    Write-Warn "Reduza NODEODM_MEMORY_LIMIT no .env.local apos a instalacao."
    if (-not (Ask "Continuar mesmo assim?")) { Stop-Fail "Instalacao cancelada." }
} else {
    Write-Ok "RAM: $ramGB GB"
}

$freeGB = [math]::Round((Get-PSDrive -Name ($Here.Substring(0,1))).Free / 1GB)
if ($freeGB -lt 20) { Stop-Fail "Disco livre: $freeGB GB. Sao necessarios ao menos 20 GB." }
Write-Ok "Disco livre: $freeGB GB"

# ==============================================================================
Write-Step "2/7 Preparando configuracao"

function New-Secret {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) -replace '[^A-Za-z0-9]', '').Substring(0, 24)
}

if (Test-Path $EnvFile) {
    Write-Ok "Reaproveitando .env.local (segredos preservados)"
} else {
    # Volumes de uma instalacao anterior guardam as senhas ANTIGAS: o
    # POSTGRES_PASSWORD so vale na primeira inicializacao do banco. Gerar
    # segredos novos sobre dados antigos deixa Postgres/Keycloak/Martin em
    # loop de "password authentication failed" - falhar aqui e mais honesto.
    $stale = (docker volume ls -q --filter "name=^cml-" 2>$null) -join " "
    if ($stale.Trim() -ne "") {
        Write-Host "  Encontrei dados de uma instalacao anterior: $stale" -ForegroundColor Yellow
        Write-Host "  Mas o arquivo de segredos (.env.local) nao existe mais, e as senhas"
        Write-Host "  gravadas nesses volumes nao podem ser recuperadas."
        Write-Host ""
        Write-Host "  Opcoes:"
        Write-Host "    - recuperar o .env.local antigo (backup?) e rodar de novo"
        Write-Host "    - apagar os dados e instalar do zero:"
        Write-Host "        docker volume rm $stale"
        Write-Host "        .\install.ps1"
        Stop-Fail "Instalacao interrompida para nao corromper dados existentes."
    }
    if (-not (Test-Path $EnvExample)) { Stop-Fail "Modelo nao encontrado: $EnvExample" }
    # Preserva UTF-8 sem BOM: o Docker Compose nao interpreta BOM em .env
    $lines = Get-Content $EnvExample
    $out = foreach ($line in $lines) {
        if ($line -match '=GERAR$') { $line -replace '=GERAR$', "=$(New-Secret)" } else { $line }
    }
    [IO.File]::WriteAllLines($EnvFile, $out, (New-Object Text.UTF8Encoding($false)))
    Write-Ok ".env.local criado com segredos aleatorios"
}

function Get-EnvValue { param($k)
    $m = Select-String -Path $EnvFile -Pattern "^$k=" | Select-Object -First 1
    if ($m) { return ($m.Line -split '=', 2)[1] } else { return "" }
}
function Set-EnvValue { param($k, $v)
    $content = Get-Content $EnvFile
    if ($content -match "^$k=") {
        $content = $content -replace "^$k=.*", "$k=$v"
    } else {
        $content += "$k=$v"
    }
    [IO.File]::WriteAllLines($EnvFile, $content, (New-Object Text.UTF8Encoding($false)))
}

if ($Domain -ne "") { Set-EnvValue "CM_DOMAIN" $Domain }
$Domain = Get-EnvValue "CM_DOMAIN"
if ($Domain -eq "") { $Domain = "cmtechmap.local" }

# .dev e .app estao na lista HSTS pre-carregada: o navegador forcaria HTTPS
if ($Domain -match '\.(dev|app)$') {
    Stop-Fail "O dominio '$Domain' usa TLD com HTTPS obrigatorio (HSTS).`n    Use algo como cmtechmap.local"
}
Write-Ok "Dominio: $Domain"

# ==============================================================================
Write-Step "3/7 Definindo a porta HTTP"

function Test-PortBusy { param($p)
    $c = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue
    return ($null -ne $c)
}
function Get-PortHolder { param($p)
    $conn = Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $conn) { return "" }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) { return $proc.ProcessName } else { return "desconhecido" }
}

# Selecao DINAMICA: nunca para servicos de terceiros para tomar a porta.
# Se a preferida estiver ocupada (IIS, Skype, outro projeto), procura a
# proxima livre e segue.
if ($Port -gt 0) {
    # Porta pedida explicitamente com -Port: respeitar ou falhar claramente
    if (Test-PortBusy $Port) {
        Stop-Fail "A porta $Port (pedida com -Port) ja esta em uso por: $(Get-PortHolder $Port)"
    }
    Set-EnvValue "CM_HTTP_PORT" $Port
    $PortValue = $Port
} else {
    $preferred = [int](Get-EnvValue "CM_HTTP_PORT")
    if ($preferred -eq 0) { $preferred = 80 }
    if (-not (Test-PortBusy $preferred)) {
        $PortValue = $preferred
        Write-Ok "Porta $PortValue livre - a URL fica sem sufixo"
    } else {
        $holder = Get-PortHolder $preferred
        Write-Warn "Porta $preferred ocupada$(if ($holder) { " por '$holder'" }) - mantida intacta"
        $PortValue = 0
        foreach ($c in @(8080, 8090, 8100, 8200, 8888, 9080, 9090)) {
            if (-not (Test-PortBusy $c)) { $PortValue = $c; break }
        }
        if ($PortValue -eq 0) {
            $PortValue = 8300
            while ((Test-PortBusy $PortValue) -and ($PortValue -le 8500)) { $PortValue++ }
            if ($PortValue -gt 8500) { Stop-Fail "Nenhuma porta livre entre 8300 e 8500." }
        }
        Set-EnvValue "CM_HTTP_PORT" $PortValue
        Write-Ok "Porta $PortValue escolhida automaticamente"
    }
}

# O Keycloak monta o emissor dos tokens a partir desta URL; sem o sufixo de
# porta correto, todo token e recusado com "Invalid token issuer".
if ($PortValue -eq 80) {
    Set-EnvValue "CM_PORT_SUFFIX" ""
    $BaseUrl = "http://$Domain"
} else {
    Set-EnvValue "CM_PORT_SUFFIX" ":$PortValue"
    $BaseUrl = "http://${Domain}:$PortValue"
}
Write-Ok "Endereco de acesso: $BaseUrl"

# ==============================================================================
Write-Step "4/7 Registrando o dominio no sistema"

$hostsContent = Get-Content $HostsFile -ErrorAction SilentlyContinue
if ($hostsContent -match "^\s*[^#].*\s$([regex]::Escape($Domain))\s*$") {
    Write-Ok "$Domain ja consta no arquivo de hosts"
} else {
    Copy-Item $HostsFile "$HostsFile.cmtechmap.bak" -Force
    Add-Content -Path $HostsFile -Value "`r`n$HostsMarker`r`n127.0.0.1`t$Domain"
    Write-Ok "$Domain -> 127.0.0.1 (backup em $HostsFile.cmtechmap.bak)"
}

# O Windows mantem cache de DNS; sem limpar, o nome recem-criado pode falhar
ipconfig /flushdns | Out-Null
$resolved = (Resolve-DnsName $Domain -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
if ($resolved -ne "127.0.0.1") {
    Stop-Fail "O nome $Domain resolve para '$resolved' em vez de 127.0.0.1.`n    Verifique o arquivo de hosts: $HostsFile"
}
Write-Ok "Resolucao verificada: $Domain -> 127.0.0.1"

# ==============================================================================
Write-Step "5/7 Compilando e subindo os servicos (demora alguns minutos)"

Push-Location $Here
try {
    docker compose --env-file $EnvFile -f $ComposeFile config --quiet
    if ($LASTEXITCODE -ne 0) { Stop-Fail "Configuracao do compose invalida." }

    docker compose --env-file $EnvFile -f $ComposeFile build --pull
    if ($LASTEXITCODE -ne 0) { Stop-Fail "Falha ao compilar as imagens." }

    docker compose --env-file $EnvFile -f $ComposeFile up -d --remove-orphans
    if ($LASTEXITCODE -ne 0) { Stop-Fail "Falha ao subir os servicos." }
    Write-Ok "Servicos iniciados"

    # ==========================================================================
    Write-Step "6/7 Aguardando o banco e aplicando migracoes"

    $ready = $false
    for ($i = 1; $i -le 60; $i++) {
        docker compose --env-file $EnvFile -f $ComposeFile exec -T backend `
            curl -sf http://localhost:8000/api/v1/health 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; Write-Ok "Backend respondendo (tentativa $i)"; break }
        Start-Sleep -Seconds 5
    }
    if (-not $ready) { Write-Warn "Backend demorou a responder; seguindo assim mesmo." }

    docker compose --env-file $EnvFile -f $ComposeFile exec -T backend alembic upgrade head
    if ($LASTEXITCODE -ne 0) { Stop-Fail "As migracoes do banco falharam." }
    Write-Ok "Banco migrado"
}
finally { Pop-Location }

# ==============================================================================
Write-Step "7/7 Validando a instalacao"

$realm = Get-EnvValue "KEYCLOAK_REALM"
if ($realm -eq "") { $realm = "cm-techmap" }

Write-Host "  aguardando o servico de autenticacao (pode levar ~3 min na 1a vez)..."
for ($i = 1; $i -le 60; $i++) {
    try {
        Invoke-WebRequest -Uri "$BaseUrl/realms/$realm/.well-known/openid-configuration" `
            -UseBasicParsing -TimeoutSec 10 | Out-Null
        break
    } catch { Start-Sleep -Seconds 10 }
}

$failures = 0
function Test-Endpoint { param($desc, $url, $expected)
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 30
        if ($r.StatusCode -eq $expected) { Write-Ok $desc }
        else { Write-Warn "$desc - HTTP $($r.StatusCode) (esperado $expected)"; $script:failures++ }
    } catch {
        Write-Warn "$desc - sem resposta"; $script:failures++
    }
}

Test-Endpoint "Interface web responde"    "$BaseUrl/"              200
Test-Endpoint "API de saude"              "$BaseUrl/api/v1/health" 200
Test-Endpoint "Autenticacao (OIDC)"       "$BaseUrl/realms/$realm/.well-known/openid-configuration" 200
Test-Endpoint "Rota de callback do login" "$BaseUrl/auth/callback" 200

Write-Host ""
if ($failures -eq 0) {
    Write-Host "====================================================" -ForegroundColor Green
    Write-Host "  CM TECHMAP instalado e funcionando" -ForegroundColor Green
    Write-Host "====================================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Acesse:  $BaseUrl" -ForegroundColor White
    Write-Host ""
    Write-Host "  Usuario de demonstracao:"
    Write-Host "    superadmin@cmtechmap.com.br / SuperAdmin@2026"
    Write-Host "    (troque em $BaseUrl/admin - e senha publica de exemplo)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Operacao:  .\cmtechmap.ps1 {start|stop|status|logs|backup|uninstall}"
    Start-Process $BaseUrl
} else {
    Write-Host "Instalacao concluida com $failures verificacao(oes) falhando." -ForegroundColor Yellow
    Write-Host "  Alguns servicos podem ainda estar iniciando. Reveja com:"
    Write-Host "    .\cmtechmap.ps1 status"
    exit 1
}
