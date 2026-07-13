param(
    [Parameter(Mandatory = $false)]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Join-Path $root "src\CmTechMapInstaller"
$issFile = Join-Path $root "packaging\CmTechMapInstaller.iss"

Write-Host "[build] Restoring..."
Push-Location $projectDir
try {
    dotnet restore
    Write-Host "[build] Publishing single-file exe..."
    dotnet publish -c $Configuration -r win-x64 --self-contained true /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true
}
finally {
    Pop-Location
}

$innoCompiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $innoCompiler)) {
    throw "Inno Setup not found at '$innoCompiler'. Install Inno Setup 6 and rerun."
}

Write-Host "[build] Building setup..."
& $innoCompiler $issFile

Write-Host "[build] Done. Artifacts:"
Write-Host "- $root\src\CmTechMapInstaller\bin\$Configuration\net8.0-windows\win-x64\publish\CmTechMapInstaller.exe"
Write-Host "- $root\packaging\CmTechMapInstaller-Setup.exe"
