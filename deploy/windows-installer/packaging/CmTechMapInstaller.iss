#define AppName "CM TechMap Installer"
#define AppVersion "1.0.0"
#define AppPublisher "Fireware Digital"
#define AppExeName "CmTechMapInstaller.exe"

[Setup]
AppId={{9D216A97-8D86-4A35-9FD7-A7BB40FB2A2B}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\CM TechMap Installer
DefaultGroupName=CM TechMap Installer
DisableProgramGroupPage=yes
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64
OutputDir=.
OutputBaseFilename=CmTechMapInstaller-Setup
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:";

[Files]
Source: "..\src\CmTechMapInstaller\bin\Release\net8.0-windows\win-x64\publish\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\all-in-one\zero-touch-bootstrap.ps1"; DestDir: "{app}\bootstrap"; DestName: "zero-touch-bootstrap.ps1"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\CM TechMap Installer"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\CM TechMap Installer"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch CM TechMap Installer"; Flags: nowait postinstall skipifsilent
