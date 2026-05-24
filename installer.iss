; APEX Trading Platform - Inno Setup Installer Script
; -----------------------------------------------------
; Built automatically by build.bat (step 3).
; Produces a per-user installer (no admin needed, like Discord):
;   - Installs to %LocalAppData%\APEX Trading Platform
;   - Start Menu shortcut  -> appears in Windows search
;   - Optional Desktop shortcut
;   - Uninstaller in Settings > Apps / Add-Remove Programs
;   - The same folder is the app's writable data dir (.env, state, charts)
; -----------------------------------------------------

#define MyAppName      "APEX Trading Platform"
#define MyAppVersion   "4.5.1"
#define MyAppPublisher "APEX"
#define MyAppExeName   "APEX.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}

; Per-user install: no admin prompt, folder is user-writable (Discord-style)
PrivilegesRequired=lowest
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes

OutputDir=installer
OutputBaseFilename=APEX_Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Whole PyInstaller --onedir folder: APEX.exe + _internal\ (bundled libs,
; bots, version.json, assets). This launches in seconds (no per-launch
; self-extraction like a onefile exe).
Source: "dist\APEX\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
; Copy the icon next to the app so the Start Menu shortcut can use it.
Source: "assets\*"; DestDir: "{app}\assets"; \
  Flags: ignoreversion recursesubdirs createallsubdirs
; NOTE: .env is NOT shipped - the user adds their API keys after install.

[Icons]
; Start Menu (this is what makes it show up in Windows search)
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  IconFilename: "{app}\assets\icon.ico"
; Quick access to the folder where the user must drop their .env
Name: "{group}\APEX Data Folder (put .env here)"; Filename: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Optional desktop shortcut
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
  IconFilename: "{app}\assets\icon.ico"; Tasks: desktopicon

[Run]
; Offer to launch APEX immediately after install
Filename: "{app}\{#MyAppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
