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
#define MyAppVersion   "4.6.141"
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
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; v4.6.37 — wipe the bundled-libs dir before copying so a previous install
; whose files were locked (e.g. APEX was running during an /VERYSILENT in-app
; update) can't leave half-populated sub-packages behind. Specifically this
; ensures sklearn/__check_build (the v4.6.36 install-skip culprit) and any
; future newly-added subpackage get freshly created.
Type: filesandordirs; Name: "{app}\_internal"

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
; V4.6.23 — Launch APEX in BOTH normal and silent install modes.
; The 'skipifsilent' flag used to suppress the post-install launch
; during /VERYSILENT installs, which is exactly when the in-app
; updater runs us — meaning users were stranded with no APEX window
; after clicking Update.
; - Normal install: checkbox in the wizard offers to launch.
; - Silent install: launches automatically (runasoriginaluser so it
;   runs as the user who triggered the install, not LocalSystem).
Filename: "{app}\{#MyAppExeName}"; \
  Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
  Flags: nowait postinstall
Filename: "{app}\{#MyAppExeName}"; \
  Flags: nowait runasoriginaluser; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// v4.6.37 — force-kill any running APEX.exe BEFORE files are copied so a
// silent in-app update can never leave a half-installed bundle (which is
// what made v4.6.36 ship sklearn without its __check_build subpackage on
// machines where APEX was still open).  Inno's built-in 'close apps'
// prompt is skipped under /VERYSILENT, so we do the force-kill ourselves.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  // v4.6.98 — kill via PowerShell Stop-Process FIRST. On Windows-on-ARM
  // `taskkill /F` fails ("operation not supported") for the x64-emulated
  // APEX.exe, so the running app stayed alive, locked its files, and the
  // bundle was never replaced -> the app relaunched at the old version and the
  // updater looped. Stop-Process uses TerminateProcess (like Task Manager) and
  // works on ARM. Loop until APEX is gone (up to 20s), then taskkill as backup.
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -ExecutionPolicy Bypass -Command "$d=(Get-Date).AddSeconds(20); while((Get-Process APEX -ErrorAction SilentlyContinue) -and (Get-Date) -lt $d){ Get-Process APEX -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep -Milliseconds 400 }"',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'),
       '/F /IM APEX.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // brief pause so Windows releases handles before we start copying
  Sleep(800);
  Result := '';
end;
