@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ===============================================
echo   APEX Trading Platform - Build
echo  ===============================================
echo.
echo  This builds APEX.exe and then a Windows installer.
echo  It is a BIG build and can take several minutes.
echo  Do NOT close this window until it says DONE.
echo.

REM --- Step 0: Python check ---
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found on PATH. Install Python 3 and retry.
    echo.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)

REM --- Step 1: tooling + all runtime dependencies ---
echo  [1/4] Installing build tools + app dependencies (one-time, ~minutes)...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade pyinstaller pyinstaller-hooks-contrib
if errorlevel 1 (
    echo  ERROR: could not install PyInstaller. Check your internet/pip.
    echo.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo  ERROR: could not install dependencies from requirements.txt.
    echo  Check your internet connection and try again.
    echo.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)
echo  OK: tools + dependencies ready.

REM --- Step 1b: purge ALL stale build caches ---
REM Critical: a leftover APEX.spec / build\ / __pycache__ can make
REM PyInstaller bundle an OLD compiled module next to new code (causes
REM "module core.data has no attribute ..." crashes). Nuke them so every
REM module is recompiled from the current source.
echo  [1b/4] Cleaning old build caches...
if exist build      rmdir /s /q build
if exist dist        rmdir /s /q dist
if exist APEX.spec   del   /q APEX.spec
for /d /r %%d in (__pycache__) do if exist "%%d" rmdir /s /q "%%d"

REM --- Step 1c: bake version.json into core/_version.py ---
REM v3.1.6 — eliminates the recurring "header shows v1.0.0" regression
REM by skipping the runtime file lookup entirely. The Python constant
REM gets bundled the same way every other module does.
echo  [1c/4] Embedding version into core/_version.py...
python tools\embed_version.py
if errorlevel 1 (
    echo  ERROR: embed_version.py failed. Check version.json is valid JSON.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)

REM --- Step 1d: regenerate the multi-size .ico from the source PNG ---
REM v4.1.0 — picks up assets/icon.png (or assets/baptou_logo.png) and
REM produces assets/icon.ico embedded with 16/32/48/64/128/256 sizes.
REM Soft-fails if Pillow or the source PNG is missing.
echo  [1d/4] Regenerating window icon...
python tools\make_icon.py

REM --- Step 2: build the exe ---
REM Note: pandas/numpy/matplotlib are handled by PyInstaller's built-in
REM hooks - do NOT --collect-all them (huge + can hang the build).
REM --onedir (NOT --onefile): a onefile exe re-extracts the whole
REM ~hundreds-of-MB bundle to a temp dir on EVERY launch, which is why
REM it took minutes to open. --onedir launches directly = seconds.
echo  [2/4] Building APEX (be patient)...
python -m PyInstaller ^
    --onedir ^
    --windowed ^
    --clean ^
    --noconfirm ^
    --name APEX ^
    --icon "assets\icon.ico" ^
    --paths . ^
    --add-data "version.json;." ^
    --add-data "BOT_SKELETON.md;." ^
    --add-data "assets;assets" ^
    --hidden-import PyQt6.QtWebEngineWidgets ^
    --hidden-import PyQt6.QtWebEngineCore ^
    --hidden-import PyQt6.QtWebEngine ^
    --hidden-import longbot_v2 ^
    --hidden-import shortbot_v2 ^
    --hidden-import daybot ^
    --hidden-import universe_manager ^
    --hidden-import dotenv ^
    --hidden-import apex_version ^
    --collect-all PyQt6 ^
    --collect-all yfinance ^
    --collect-all anthropic ^
    --collect-all alpaca ^
    main.py

if not exist "dist\APEX\APEX.exe" (
    echo.
    echo  ERROR: build failed - dist\APEX\APEX.exe was NOT created.
    echo  Scroll up to read the PyInstaller error.
    echo.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)
echo  OK: dist\APEX\ folder created.
echo.

REM --- Step 3: build the installer with Inno Setup ---
echo  [3/4] Building the installer...
set "INNO=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%INNO%" set "INNO=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%INNO%" (
    echo  WARNING: Inno Setup 6 not found.
    echo  Download it from https://jrsoftware.org/isinfo.php , install,
    echo  then run build.bat again. For now you can still run
    echo  dist\APEX\APEX.exe directly.
    echo.
    if not defined APEX_NOPAUSE pause
    exit /b 0
)
if not exist installer mkdir installer
"%INNO%" installer.iss
if not exist "installer\APEX_Setup.exe" (
    echo.
    echo  ERROR: Inno Setup did not produce installer\APEX_Setup.exe.
    echo  Scroll up for the Inno Setup error.
    echo.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)

REM --- Step 4: done ---
echo.
echo  [4/4] DONE.
echo  ===============================================
echo   Installer: installer\APEX_Setup.exe
echo  ===============================================
echo.
echo  Run installer\APEX_Setup.exe to install APEX.
echo  After installing, type "APEX" in the Windows
echo  search bar - it will appear there.
echo.
if not defined APEX_NOPAUSE pause
endlocal
