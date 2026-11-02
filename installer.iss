#define MyAppName "SinLens"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "SinLens"
#define MyAppExeName "SinLens.exe"

[Setup]
AppId={{A3F5A0E8-5B4B-4AA9-9A4D-8A6A5D2C7D11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\SinLens
DefaultGroupName=SinLens
DisableProgramGroupPage=yes
OutputDir=installer
OutputBaseFilename=SinLens-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern
SetupIconFile=assets\sinlens.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "dist\SinLens\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SinLens"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\SinLens"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch SinLens"; Flags: nowait postinstall skipifsilent
