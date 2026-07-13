param(
    [Parameter(Mandatory = $false)]
    [string]$RepoUrl = "https://github.com/firewaredigital/cmtechmap-workspace.git",

    [Parameter(Mandatory = $false)]
    [string]$Branch = "main",

    [Parameter(Mandatory = $false)]
    [string]$WorkspaceDir = "",

    [Parameter(Mandatory = $false)]
    [string]$FrontendUrl = "",

    [Parameter(Mandatory = $false)]
    [switch]$WithoutTunnel,

    [Parameter(Mandatory = $false)]
    [switch]$QuickTunnel,

    [Parameter(Mandatory = $false)]
    [string]$TunnelToken = "",

    [Parameter(Mandatory = $false)]
    [string]$TunnelHostname = "",

    [Parameter(Mandatory = $false)]
    [string]$TunnelPublicUrl = "",

    [Parameter(Mandatory = $false)]
    [switch]$SkipSmoke,

    [Parameter(Mandatory = $false)]
    [switch]$SkipFrontendPatch
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host "[bootstrap] $Message"
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Escape-BashSingleQuoted {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) {
        return "''"
    }
    return "'" + ($Value -replace "'", "'\"'\"'") + "'"
}

function Ensure-WslAvailable {
    if (Test-CommandExists "wsl") {
        return
    }

    Write-Step "WSL not found. Attempting install (requires Administrator privileges)."
    wsl --install -d Ubuntu
    throw "WSL installation started. Reboot Windows and run this command again."
}

function Ensure-UbuntuDistro {
    $list = wsl -l -q 2>$null
    if ($null -eq $list) {
        wsl --install -d Ubuntu
        throw "Ubuntu distribution installation started. Reboot Windows and run this command again."
    }

    $hasUbuntu = $false
    foreach ($line in $list) {
        $name = ($line | ForEach-Object { $_.Trim() })
        if ($name -eq "Ubuntu") {
            $hasUbuntu = $true
            break
        }
    }

    if (-not $hasUbuntu) {
        Write-Step "Ubuntu distro not found. Installing Ubuntu for WSL."
        wsl --install -d Ubuntu
        throw "Ubuntu installation started. Reboot Windows and run this command again."
    }
}

function Ensure-DockerCli {
    if (Test-CommandExists "docker") {
        return
    }

    throw "Docker CLI not found. Install Docker Desktop and enable WSL integration, then rerun installer."
}

function Ensure-DockerAccessible {
    try {
        docker version | Out-Null
    }
    catch {
        throw "Docker is installed but unavailable. Start Docker Desktop and ensure WSL integration is enabled."
    }
}

Write-Step "Validating WSL runtime..."
Ensure-WslAvailable
Ensure-UbuntuDistro
Write-Step "Validating Docker runtime..."
Ensure-DockerCli
Ensure-DockerAccessible

$argsList = @()
$argsList += "--repo-url"
$argsList += $RepoUrl
$argsList += "--branch"
$argsList += $Branch

if (-not [string]::IsNullOrWhiteSpace($FrontendUrl)) {
    $argsList += "--frontend-url"
    $argsList += $FrontendUrl
}

if (-not [string]::IsNullOrWhiteSpace($WorkspaceDir)) {
    $argsList += "--workspace-dir"
    $argsList += $WorkspaceDir
}

if ($WithoutTunnel.IsPresent) {
    $argsList += "--without-tunnel"
}

if ($QuickTunnel.IsPresent) {
    $argsList += "--quick-tunnel"
}

if (-not [string]::IsNullOrWhiteSpace($TunnelToken)) {
    $argsList += "--tunnel-token"
    $argsList += $TunnelToken
}

if (-not [string]::IsNullOrWhiteSpace($TunnelHostname)) {
    $argsList += "--tunnel-hostname"
    $argsList += $TunnelHostname
}

if (-not [string]::IsNullOrWhiteSpace($TunnelPublicUrl)) {
    $argsList += "--tunnel-public-url"
    $argsList += $TunnelPublicUrl
}

if ($SkipSmoke.IsPresent) {
    $argsList += "--skip-smoke"
}

if ($SkipFrontendPatch.IsPresent) {
    $argsList += "--skip-frontend-patch"
}

$escapedArgs = @()
foreach ($arg in $argsList) {
    $escapedArgs += (Escape-BashSingleQuoted -Value $arg)
}

$linuxBootstrapUrl = "https://raw.githubusercontent.com/firewaredigital/cmtechmap-workspace/main/applications/deploy/all-in-one/zero-touch-bootstrap.sh"
$linuxCommand = "curl -fsSL $linuxBootstrapUrl | bash -s -- " + ($escapedArgs -join " ")

Write-Step "Starting Linux bootstrap inside WSL Ubuntu..."
wsl -d Ubuntu -- bash -lc $linuxCommand

Write-Step "Done. Backend bootstrap completed via WSL."
