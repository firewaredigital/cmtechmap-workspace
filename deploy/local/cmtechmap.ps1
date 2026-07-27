# ==============================================================================
# CM TECHMAP - Operacao da instalacao local (Windows)
#
#   .\cmtechmap.ps1 start      sobe a plataforma
#   .\cmtechmap.ps1 stop       para (dados preservados)
#   .\cmtechmap.ps1 restart    reinicia os servicos de aplicacao
#   .\cmtechmap.ps1 status     estado dos servicos + endereco de acesso
#   .\cmtechmap.ps1 logs [svc] acompanha os logs
#   .\cmtechmap.ps1 backup     dump do banco + objetos do MinIO
#   .\cmtechmap.ps1 update     recompila e sobe a versao nova do codigo
#   .\cmtechmap.ps1 uninstall  remove tudo (pergunta antes de apagar dados)
# ==============================================================================
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = "status",
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = "Stop"
$Here        = Split-Path -Parent $MyInvocation.MyCommand.Path
$ComposeFile = Join-Path $Here "docker-compose.local.yml"
$EnvFile     = Join-Path $Here ".env.local"
$HostsFile   = "$env:SystemRoot\System32\drivers\etc\hosts"

if (-not (Test-Path $EnvFile)) {
    Write-Host "Instalacao nao encontrada. Rode primeiro: .\install.ps1" -ForegroundColor Red
    exit 1
}

function Get-EnvValue { param($k)
    $m = Select-String -Path $EnvFile -Pattern "^$k=" | Select-Object -First 1
    if ($m) { return ($m.Line -split '=', 2)[1] } else { return "" }
}
function Invoke-Dc { docker compose --env-file $EnvFile -f $ComposeFile @args }

$Domain = Get-EnvValue "CM_DOMAIN"; if ($Domain -eq "") { $Domain = "cmtechmap.local" }
$Suffix = Get-EnvValue "CM_PORT_SUFFIX"
$BaseUrl = "http://$Domain$Suffix"

switch ($Command.ToLower()) {
    "start" {
        Write-Host "Iniciando CM TECHMAP..." -ForegroundColor White
        Invoke-Dc up -d --remove-orphans
        Write-Host "  Acesse: $BaseUrl" -ForegroundColor Green
        Write-Host "  (o servico de autenticacao leva ~1 min para ficar pronto)"
    }

    "stop" {
        Write-Host "Parando CM TECHMAP... (os dados sao preservados)" -ForegroundColor White
        Invoke-Dc stop
    }

    "restart" {
        # Só os servicos de aplicacao: recriar o Keycloak custa minutos de
        # indisponibilidade porque ele refaz a augmentation do Quarkus.
        Write-Host "Reiniciando servicos de aplicacao..." -ForegroundColor White
        Invoke-Dc restart backend celery-worker celery-beat gateway frontend
    }

    "status" {
        Write-Host "Servicos:" -ForegroundColor White
        Invoke-Dc ps
        Write-Host ""
        try {
            $r = Invoke-WebRequest -Uri "$BaseUrl/api/v1/health" -UseBasicParsing -TimeoutSec 10
            Write-Host "Endereco: $BaseUrl  (no ar - HTTP $($r.StatusCode))" -ForegroundColor Green
        } catch {
            Write-Host "Endereco: $BaseUrl  (sem resposta)" -ForegroundColor Yellow
        }
    }

    "logs" {
        if ($Rest) { Invoke-Dc logs -f --tail=100 @Rest }
        else { Invoke-Dc logs -f --tail=100 }
    }

    "backup" {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $dest = Join-Path $Here "backups\$stamp"
        New-Item -ItemType Directory -Path $dest -Force | Out-Null
        Write-Host "Gerando backup em $dest..." -ForegroundColor White
        $pgUser = Get-EnvValue "POSTGRES_USER"
        $pgDb   = Get-EnvValue "POSTGRES_DB"
        # cmd /c preserva o redirecionamento binario sem a conversao de
        # encoding que o PowerShell aplicaria com '>'
        cmd /c "docker compose --env-file `"$EnvFile`" -f `"$ComposeFile`" exec -T postgres pg_dump -U $pgUser -d $pgDb > `"$dest\banco.sql`""
        Write-Host "  banco: $([math]::Round((Get-Item "$dest\banco.sql").Length / 1MB, 1)) MB"
        cmd /c "docker compose --env-file `"$EnvFile`" -f `"$ComposeFile`" exec -T minio tar -cf - -C /data . > `"$dest\objetos.tar`""
        if (Test-Path "$dest\objetos.tar") {
            Write-Host "  objetos: $([math]::Round((Get-Item "$dest\objetos.tar").Length / 1MB, 1)) MB"
        }
        Write-Host "Backup concluido." -ForegroundColor Green
    }

    "update" {
        Write-Host "Atualizando para a versao mais recente do codigo..." -ForegroundColor White
        Invoke-Dc build --pull backend frontend
        Invoke-Dc up -d backend celery-worker celery-beat frontend gateway
        Start-Sleep -Seconds 15
        Invoke-Dc exec -T backend alembic upgrade head
        Write-Host "Atualizado. Acesse: $BaseUrl" -ForegroundColor Green
    }

    "uninstall" {
        Write-Host "Removendo a instalacao local do CM TECHMAP." -ForegroundColor White
        $wipe = Read-Host "  Apagar TAMBEM os dados (banco, imagens, relatorios)? [s/N]"
        if ($wipe -eq "s" -or $wipe -eq "S") {
            Invoke-Dc down -v --remove-orphans
            Write-Host "  containers e dados removidos"
        } else {
            Invoke-Dc down --remove-orphans
            Write-Host "  containers removidos; dados preservados nos volumes cml-*"
        }
        $isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if ($isAdmin) {
            $kept = Get-Content $HostsFile | Where-Object {
                $_ -notmatch "^\s*127\.0\.0\.1\s+$([regex]::Escape($Domain))\s*$" -and
                $_ -notmatch "CM TECHMAP \(instalacao local\)"
            }
            [IO.File]::WriteAllLines($HostsFile, $kept)
            ipconfig /flushdns | Out-Null
            Write-Host "  entrada de $Domain removida do arquivo de hosts"
        } else {
            Write-Host "  execute como Administrador para remover $Domain do arquivo de hosts" -ForegroundColor Yellow
        }
        Write-Host "Desinstalado." -ForegroundColor Green
    }

    default {
        Get-Content $MyInvocation.MyCommand.Path | Select-Object -First 14 |
            ForEach-Object { $_ -replace '^#\s?', '' }
        exit 1
    }
}
