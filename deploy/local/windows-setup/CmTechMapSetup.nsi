; =============================================================================
; CM TECHMAP — Instalador gráfico para Windows
; =============================================================================
; Gera um .exe que o usuário abre com duplo clique. Ele faz TUDO sozinho:
; verifica o Docker Desktop, copia os arquivos, sorteia senhas, registra o
; domínio no arquivo de hosts, escolhe uma porta livre, compila as imagens,
; sobe os serviços, migra o banco e cria os atalhos. O usuário não digita
; nenhum comando.
;
; Compilado no Linux com:  makensis CmTechMapSetup.nsi
; =============================================================================

Unicode true

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"

!define APP_NAME      "CM TECHMAP"
!define APP_VERSION   "1.0.0"
!define APP_PUBLISHER "Fireware Digital"
!define APP_KEY       "CmTechMapLocal"
!define REG_UNINST    "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_KEY}"

Name "${APP_NAME}"
OutFile "..\dist\CM-TECHMAP-Instalador.exe"
InstallDir "$PROGRAMFILES64\CM TECHMAP"
InstallDirRegKey HKLM "Software\${APP_KEY}" "InstallDir"
; Precisa de administrador: edita o arquivo de hosts do Windows
RequestExecutionLevel admin
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUnInstDetails show

Var AccessUrl
Var LocalDir

; ── Aparência ───────────────────────────────────────────────────────────────
!define MUI_ABORTWARNING
!define MUI_ICON   "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!define MUI_WELCOMEPAGE_TITLE "Instalação do ${APP_NAME}"
!define MUI_WELCOMEPAGE_TEXT  "Este assistente instala a plataforma ${APP_NAME} neste computador.$\r$\n$\r$\nTudo funciona localmente: as imagens de drone, o banco de dados e os mapas ficam nesta máquina. Ao final, o sistema estará acessível pelo navegador em um endereço próprio.$\r$\n$\r$\nÉ necessário o Docker Desktop instalado e em execução. A primeira instalação leva de 10 a 20 minutos.$\r$\n$\r$\nClique em Avançar para começar."

; ── Páginas ─────────────────────────────────────────────────────────────────
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

!define MUI_FINISHPAGE_TITLE "${APP_NAME} instalado"
!define MUI_FINISHPAGE_TEXT  "A plataforma está no ar neste computador.$\r$\n$\r$\nUse o atalho 'Abrir ${APP_NAME}' na Área de Trabalho ou no Menu Iniciar sempre que quiser acessar."
!define MUI_FINISHPAGE_RUN_TEXT "Abrir o ${APP_NAME} agora"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION AbrirSistema
!define MUI_FINISHPAGE_NOREBOOTSUPPORT
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "PortugueseBR"

; =============================================================================
; Funções auxiliares
; =============================================================================

; Lê a URL de acesso gravada pelo install.ps1 (a porta pode ter sido
; escolhida dinamicamente, então não dá para presumir).
Function LerUrlDeAcesso
    StrCpy $AccessUrl "http://cmtechmap.local"
    ${If} ${FileExists} "$LocalDir\.access-url"
        FileOpen $0 "$LocalDir\.access-url" r
        FileRead $0 $1
        FileClose $0
        ${If} $1 != ""
            StrCpy $AccessUrl $1
        ${EndIf}
    ${EndIf}
FunctionEnd

Function AbrirSistema
    Call LerUrlDeAcesso
    ExecShell "open" "$AccessUrl"
FunctionEnd

; =============================================================================
; Verificação de pré-requisitos — antes de qualquer alteração no sistema
; =============================================================================
Function .onInit
    ${IfNot} ${AtLeastWin10}
        MessageBox MB_ICONSTOP "O ${APP_NAME} requer Windows 10 ou superior."
        Abort
    ${EndIf}

    ; O Docker precisa existir E estar em execução. Verificar as duas coisas
    ; separadamente permite dar a orientação certa em cada caso.
    nsExec::ExecToStack 'cmd /c docker --version'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONSTOP|MB_OKCANCEL \
            "O Docker Desktop não foi encontrado.$\r$\n$\r$\nEle é necessário para executar o ${APP_NAME}.$\r$\n$\r$\nClique em OK para abrir a página de download." \
            IDOK abrir_download IDCANCEL sair
        abrir_download:
            ExecShell "open" "https://www.docker.com/products/docker-desktop/"
        sair:
            Abort
    ${EndIf}

    nsExec::ExecToStack 'cmd /c docker info'
    Pop $0
    ${If} $0 != 0
        MessageBox MB_ICONSTOP \
            "O Docker Desktop está instalado, mas não está em execução.$\r$\n$\r$\nAbra o Docker Desktop, aguarde o ícone ficar verde (Running) e execute este instalador novamente."
        Abort
    ${EndIf}

    ; Instalação anterior feita por este instalador? O NSIS já aponta
    ; $INSTDIR para a mesma pasta (InstallDirRegKey). Avisar que é uma
    ; ATUALIZAÇÃO: a versão em execução será parada e os dados preservados
    ; (a migração/parada em si acontece no install.ps1, passo 2/8).
    ReadRegStr $0 HKLM "Software\${APP_KEY}" "InstallDir"
    StrCmp $0 "" fim_deteccao
        MessageBox MB_ICONINFORMATION|MB_OKCANCEL \
            "Uma instalação do ${APP_NAME} já existe em:$\r$\n$0$\r$\n$\r$\nEste instalador vai ATUALIZÁ-LA para a nova versão:$\r$\n  • a versão em execução será parada automaticamente$\r$\n  • os dados (projetos, imagens, banco) serão preservados$\r$\n$\r$\nDeseja continuar com a atualização?" \
            IDOK fim_deteccao
        Abort
    fim_deteccao:
FunctionEnd

; =============================================================================
; Instalação
; =============================================================================
Section "Plataforma ${APP_NAME}" SecPrincipal
    SectionIn RO
    SetOutPath "$INSTDIR"

    DetailPrint "Copiando arquivos da plataforma..."
    ; Todo o conteúdo do pacote (instaladores + código-fonte necessário
    ; para compilar as imagens na máquina de destino).
    ; `payload\*` e não `payload\*.*`: este último pode deixar de fora
    ; arquivos sem extensão, e o pacote ficaria incompleto em silêncio.
    File /r "payload\*"

    StrCpy $LocalDir "$INSTDIR\applications\deploy\local"

    ; ── Execução do provisionamento ─────────────────────────────────────────
    DetailPrint ""
    DetailPrint "════════════════════════════════════════════════════════"
    DetailPrint " Preparando a plataforma — de 10 a 20 minutos"
    DetailPrint " O computador vai compilar os componentes. É normal"
    DetailPrint " demorar; não feche esta janela."
    DetailPrint "════════════════════════════════════════════════════════"
    DetailPrint ""

    ; ExecToLog mostra a saída do PowerShell na janela de detalhes, para o
    ; usuário ver o progresso em vez de uma barra parada.
    nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$LocalDir\install.ps1" -Yes -Silent'
    Pop $0
    ${If} $0 != 0
        DetailPrint ""
        DetailPrint "A instalação encontrou um problema (código $0)."
        ; O install.ps1 grava o motivo exato da falha em .install-error —
        ; mostrá-lo evita o diálogo genérico que esconde a causa real.
        StrCpy $1 ""
        ${If} ${FileExists} "$LocalDir\.install-error"
            FileOpen $2 "$LocalDir\.install-error" r
            FileRead $2 $1
            FileClose $2
            DetailPrint "Motivo: $1"
        ${EndIf}
        ${If} $1 != ""
            MessageBox MB_ICONEXCLAMATION \
                "A instalação não foi concluída.$\r$\n$\r$\nMotivo:$\r$\n$1$\r$\n$\r$\nO detalhamento completo está na janela do instalador."
        ${Else}
            MessageBox MB_ICONEXCLAMATION \
                "A instalação não foi concluída.$\r$\n$\r$\nCausas comuns:$\r$\n  • Docker Desktop parou durante o processo$\r$\n  • Memória insuficiente (mínimo 8 GB)$\r$\n  • Sem espaço em disco (mínimo 20 GB)$\r$\n$\r$\nO detalhamento está na janela do instalador."
        ${EndIf}
        Abort "Instalação interrompida."
    ${EndIf}

    Call LerUrlDeAcesso
    DetailPrint ""
    DetailPrint "Endereço de acesso: $AccessUrl"

    ; ── Atalhos ─────────────────────────────────────────────────────────────
    DetailPrint "Criando atalhos..."

    ; Abrir o sistema: um .url aponta para o endereço final, seja qual for a
    ; porta escolhida
    FileOpen $0 "$INSTDIR\Abrir CM TECHMAP.url" w
    FileWrite $0 "[InternetShortcut]$\r$\n"
    FileWrite $0 "URL=$AccessUrl$\r$\n"
    FileClose $0

    ; Scripts de operação sem terminal visível para o usuário
    FileOpen $0 "$INSTDIR\Iniciar.cmd" w
    FileWrite $0 '@echo off$\r$\n'
    FileWrite $0 'powershell -NoProfile -ExecutionPolicy Bypass -File "$LocalDir\cmtechmap.ps1" start$\r$\n'
    FileWrite $0 'start "" "$AccessUrl"$\r$\n'
    FileClose $0

    FileOpen $0 "$INSTDIR\Parar.cmd" w
    FileWrite $0 '@echo off$\r$\n'
    FileWrite $0 'powershell -NoProfile -ExecutionPolicy Bypass -File "$LocalDir\cmtechmap.ps1" stop$\r$\n'
    FileClose $0

    FileOpen $0 "$INSTDIR\Ver situacao.cmd" w
    FileWrite $0 '@echo off$\r$\n'
    FileWrite $0 'powershell -NoProfile -ExecutionPolicy Bypass -File "$LocalDir\cmtechmap.ps1" status$\r$\n'
    FileWrite $0 'pause$\r$\n'
    FileClose $0

    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\Abrir ${APP_NAME}.lnk"  "$INSTDIR\Abrir CM TECHMAP.url"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\Iniciar servicos.lnk"   "$INSTDIR\Iniciar.cmd"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\Parar servicos.lnk"     "$INSTDIR\Parar.cmd"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\Ver situacao.lnk"       "$INSTDIR\Ver situacao.cmd"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\Desinstalar.lnk"        "$INSTDIR\Desinstalar.exe"
    CreateShortCut "$DESKTOP\Abrir ${APP_NAME}.lnk"                 "$INSTDIR\Abrir CM TECHMAP.url"

    ; ── Registro (Adicionar/Remover Programas) ──────────────────────────────
    WriteRegStr HKLM "Software\${APP_KEY}" "InstallDir" "$INSTDIR"
    WriteRegStr HKLM "Software\${APP_KEY}" "AccessUrl"  "$AccessUrl"
    WriteRegStr HKLM "${REG_UNINST}" "DisplayName"     "${APP_NAME}"
    WriteRegStr HKLM "${REG_UNINST}" "DisplayVersion"  "${APP_VERSION}"
    WriteRegStr HKLM "${REG_UNINST}" "Publisher"       "${APP_PUBLISHER}"
    WriteRegStr HKLM "${REG_UNINST}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "${REG_UNINST}" "UninstallString" '"$INSTDIR\Desinstalar.exe"'
    WriteRegDWORD HKLM "${REG_UNINST}" "NoModify" 1
    WriteRegDWORD HKLM "${REG_UNINST}" "NoRepair" 1
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKLM "${REG_UNINST}" "EstimatedSize" "$0"

    WriteUninstaller "$INSTDIR\Desinstalar.exe"

    DetailPrint ""
    DetailPrint "Instalação concluída. Acesse: $AccessUrl"
SectionEnd

; =============================================================================
; Desinstalação
; =============================================================================
Section "Uninstall"
    StrCpy $LocalDir "$INSTDIR\applications\deploy\local"

    MessageBox MB_YESNO|MB_ICONQUESTION \
        "Deseja apagar TAMBÉM os dados (projetos, imagens, relatórios)?$\r$\n$\r$\nEscolha Não para manter os dados caso pretenda reinstalar." \
        IDYES apagar_dados IDNO manter_dados

    apagar_dados:
        DetailPrint "Removendo serviços e dados..."
        nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "cd \"$LocalDir\"; docker compose --env-file .env.local -f docker-compose.local.yml down -v --remove-orphans"'
        Pop $0
        Goto limpar_hosts

    manter_dados:
        DetailPrint "Removendo serviços (dados preservados)..."
        nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "cd \"$LocalDir\"; docker compose --env-file .env.local -f docker-compose.local.yml down --remove-orphans"'
        Pop $0

    limpar_hosts:
        DetailPrint "Removendo o endereço do arquivo de hosts..."
        nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$h=\"$SYSDIR\drivers\etc\hosts\"; $$k=Get-Content $$h | Where-Object { $$_ -notmatch \"cmtechmap\" -and $$_ -notmatch \"CM TECHMAP\" }; [IO.File]::WriteAllLines($$h, $$k); ipconfig /flushdns"'
        Pop $0

    Delete "$DESKTOP\Abrir ${APP_NAME}.lnk"
    RMDir /r "$SMPROGRAMS\${APP_NAME}"
    RMDir /r "$INSTDIR"

    DeleteRegKey HKLM "${REG_UNINST}"
    DeleteRegKey HKLM "Software\${APP_KEY}"
SectionEnd
