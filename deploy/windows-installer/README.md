# CM TechMap Windows Installer

This module provides a complete Windows installation experience for CM TechMap backend bootstrap:

- desktop GUI app (`CmTechMapInstaller.exe`) with modern UX,
- classic setup wizard package (`CmTechMapInstaller-Setup.exe`) via Inno Setup,
- distributable ZIP bundle (`CmTechMapInstaller-Windows-Bundle.zip`) ready to send to target machine,
- CI pipeline that builds and publishes both artifacts.

Build trigger note: updates in this directory trigger the Windows installer workflow.

## Included Components

- GUI app source:
	- `src/CmTechMapInstaller/CmTechMapInstaller.csproj`
	- `src/CmTechMapInstaller/Program.cs`
	- `src/CmTechMapInstaller/MainForm.cs`
	- `src/CmTechMapInstaller/InstallerSettings.cs`
- Setup packaging:
	- `packaging/CmTechMapInstaller.iss`
- Local build helper:
	- `build-installer.ps1`

## Runtime Architecture

The Windows app does not run Docker workloads directly in native Windows mode.
Instead, it orchestrates the bootstrap via WSL Ubuntu:

1. App collects user configuration in GUI.
2. App runs `zero-touch-bootstrap.ps1`.
3. PowerShell wrapper validates WSL + Docker and forwards to Linux bootstrap.
4. Linux bootstrap installs dependencies, clones/updates project, and runs `install-agent.sh`.

Bridge scripts:

- `deploy/all-in-one/zero-touch-bootstrap.ps1`
- `deploy/all-in-one/zero-touch-bootstrap.sh`

## Build Locally On Windows

Requirements:

- Windows 10/11
- .NET SDK 8+
- Inno Setup 6+

From repo root:

```powershell
cd deploy/windows-installer
powershell -ExecutionPolicy Bypass -File .\build-installer.ps1
```

Artifacts:

- `deploy/windows-installer/src/CmTechMapInstaller/bin/Release/net8.0-windows/win-x64/publish/CmTechMapInstaller.exe`
- `deploy/windows-installer/packaging/CmTechMapInstaller-Setup.exe`

## Build In CI (GitHub Actions)

Workflow:

- `.github/workflows/build-windows-installer.yml`

Triggers:

- manual (`workflow_dispatch`),
- push on `main` for installer/bootstrap path changes.

CI outputs artifact bundle:

- `CmTechMapInstaller.exe`
- `CmTechMapInstaller-Setup.exe`
- `SHA256SUMS.txt`
- `CmTechMapInstaller-Windows-Bundle.zip`
- `README-INSTALL.txt`

On GitHub Releases (`release: published`), the workflow also uploads all artifacts
as release assets automatically.

## Distribution Modes

### 1) Direct EXE (portable)

- User double-clicks `CmTechMapInstaller.exe`.
- Good for internal/admin teams.

### 2) Setup Wizard (recommended for broad users)

- User runs `CmTechMapInstaller-Setup.exe`.
- Standard `Next > Install > Finish` experience.
- Adds Start Menu shortcut and optional desktop shortcut.
- Bundles local bootstrap script (no runtime download dependency for the first installer stage).

## Ready-To-Send Delivery Flow

Goal: send one file to Windows machine and start local server quickly.

1. From CI artifacts/release, download `CmTechMapInstaller-Windows-Bundle.zip`.
2. Transfer ZIP to target Windows machine.
3. Extract ZIP.
4. Double-click `CmTechMapInstaller-Setup.exe`.
5. Open installed app and click `Install Now` after filling required fields.
6. Wait for success message in GUI log/status.

After that, backend stack is running locally and the application can use it.

## Cadastro Inicial Sem Keycloak Manual

O instalador agora pode criar o primeiro usuario automaticamente, sem abrir o painel do Keycloak.

Na tela inicial do instalador:

1. Marque `Criar usuario inicial automaticamente`.
2. Preencha:
	- `Nome completo (usuario inicial)`
	- `Email (usuario inicial)`
	- `Login (usuario inicial)`
	- `Senha inicial` (minimo 8 caracteres)
3. (Opcional) marque `Dar permissao de administrador para este usuario`.
4. Clique em `Install Now`.

Observacao importante de login:

- Na tela de login da aplicacao, use o `email` do usuario inicial como identificador.
- Mesmo que exista campo de login no instalador, o fluxo de autenticacao atual valida pelo email.

Fluxo automatico feito pelo instalador:

1. Sobe o ambiente completo (backend + Keycloak + banco + servicos auxiliares).
2. Autentica no Keycloak com credenciais de admin do ambiente.
3. Cria o usuario informado (ou reutiliza se ja existir).
4. Define a senha inicial.
5. Aplica permissao de admin quando selecionado.

Atualizacao sem parar manualmente o backend antigo:

- O instalador faz reconciliacao automatica da stack CM TechMap antes do deploy.
- Essa reconciliacao usa `docker compose down --remove-orphans` e preserva volumes de dados.
- Resultado: voce pode rodar o novo instalador direto, sem parar processos manualmente.

Se ocorrer erro, o popup de falha mostra os logs recentes logo abaixo da mensagem para diagnostico rapido.

## Silent Installation

Inno Setup supports silent switches:

```powershell
CmTechMapInstaller-Setup.exe /VERYSILENT /NORESTART
```

Silent with log:

```powershell
CmTechMapInstaller-Setup.exe /VERYSILENT /NORESTART /LOG="C:\temp\cm-techmap-setup.log"
```

## End-User Prerequisites

The installer flow expects:

- Docker Desktop installed and running,
- WSL Ubuntu available (installer attempts to install if absent),
- internet access to GitHub and package registries,
- admin privileges for first-time environment setup.

## Security and Operational Notes

- Sensitive inputs (for example tunnel token) are masked in GUI.
- GUI stores user settings under `%LOCALAPPDATA%\\CmTechMapInstaller\\settings.json`.
- Do not commit runtime secrets from deployed machine back to repository.
- Prefer token tunnel + fixed hostname for production-like stability.

## Integrity Verification (recommended)

After downloading artifacts, validate SHA-256:

```powershell
Get-FileHash -Algorithm SHA256 .\CmTechMapInstaller.exe
Get-FileHash -Algorithm SHA256 .\CmTechMapInstaller-Setup.exe
```

Compare with `SHA256SUMS.txt` from CI artifact/release.

## Optional Code Signing

For enterprise distribution and better SmartScreen reputation, sign both binaries.

Suggested process:

1. Use EV or OV code-signing certificate.
2. Sign `CmTechMapInstaller.exe` and `CmTechMapInstaller-Setup.exe`.
3. Timestamp signature.
4. Publish only signed binaries.

Example (manual, Windows host):

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a .\CmTechMapInstaller.exe
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a .\CmTechMapInstaller-Setup.exe
```

## Automatic Code Signing In CI (recommended)

The workflow supports automatic signing when secrets are configured.
If secrets are not present, build still succeeds and publishes unsigned binaries.

### Required GitHub Actions Secrets

Set these repository secrets:

1. `WIN_CODE_SIGN_CERT_BASE64`
: Base64 content of your PFX certificate.
2. `WIN_CODE_SIGN_CERT_PASSWORD`
: Password for the PFX certificate.

Generate base64 from PFX (Windows PowerShell):

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\certs\codesign.pfx")) | Set-Clipboard
```

Then paste clipboard content into `WIN_CODE_SIGN_CERT_BASE64` secret.

### CI Signing Behavior

1. Build `CmTechMapInstaller.exe`.
2. Build `CmTechMapInstaller-Setup.exe`.
3. If secrets exist:
	- imports PFX on runner,
	- signs both binaries with timestamp,
	- verifies Authenticode signature.
4. Generates `SHA256SUMS.txt` for final artifacts (already signed when signing is enabled).
5. Uploads artifacts to workflow and release.

### Post-Release Signature Verification

Verify signatures on download host:

```powershell
Get-AuthenticodeSignature .\CmTechMapInstaller.exe | Format-List Status,SignerCertificate,TimeStamperCertificate
Get-AuthenticodeSignature .\CmTechMapInstaller-Setup.exe | Format-List Status,SignerCertificate,TimeStamperCertificate
```

Expected `Status`: `Valid`.

## Operational Rollout Checklist

1. Merge installer changes to `main`.
2. Configure signing secrets (optional but recommended).
3. Run workflow manually or publish release tag.
4. Download artifacts and validate checksums.
5. Validate Authenticode signature if signing enabled.
6. Pilot install on clean Windows machine.
7. Promote to broader distribution.
8. Keep previous known-good setup for rollback.

## Recommended Release Process

1. Tag release branch/version.
2. Run GitHub workflow.
3. Download artifacts.
4. Optionally sign binaries (code signing certificate).
5. Publish setup package to internal distribution channel.
6. Keep a rollback artifact from previous version.

## Troubleshooting

Common failures:

- `Docker CLI not found`: install Docker Desktop.
- `Docker unavailable`: start Docker Desktop and enable WSL integration.
- WSL install requested reboot: reboot and rerun installer.
- Git access denied: use repository visibility/credentials compatible with target machine.

If bootstrap fails, inspect GUI log output and rerun after fixing prerequisite issue.
