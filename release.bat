@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ===============================================
echo   APEX - One-Click Release
echo  ===============================================
echo.
echo  This will: bump the version, build the installer,
echo  push to GitHub, and publish a Release. Installed
echo  apps then self-update overnight automatically.
echo  It is SLOW (the build takes several minutes).
echo.

REM --- 0. sanity: tools present ---
where git >nul 2>&1 || (echo ERROR: git not found & pause & exit /b 1)
where gh  >nul 2>&1 || (echo ERROR: GitHub CLI ^(gh^) not found. Install it ^& run 'gh auth login'. & pause & exit /b 1)

REM --- 1. bump patch version in version.json ---
python -c "import json;p='version.json';d=json.load(open(p));v=[int(x) for x in str(d.get('version','1.0.0')).split('.')];v=(v+[0,0,0])[:3];v[2]+=1;d['version']='.'.join(map(str,v));json.dump(d,open(p,'w'),indent=2)" || (echo ERROR: could not bump version.json & pause & exit /b 1)
for /f "delims=" %%v in ('python -c "import json;print(json.load(open('version.json'))['version'])"') do set "VER=%%v"
echo  New version: v%VER%
echo.

REM --- 2. build the installer (non-interactive) ---
set APEX_NOPAUSE=1
REM Force fresh build: delete any stale installer first so the existence
REM check below is meaningful regardless of what happened in build.bat.
if exist "installer\APEX_Setup.exe" del /q "installer\APEX_Setup.exe"
call "%~dp0build.bat"
if errorlevel 1 (
    echo.
    echo  ERROR: build.bat returned an error - release aborted.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)
if not exist "installer\APEX_Setup.exe" (
    echo.
    echo  ERROR: build did not produce installer\APEX_Setup.exe - release aborted.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)

REM --- 3. commit + push source (version bump + any code changes) ---
git add -A
git commit -m "Release v%VER%"
git push origin main
if errorlevel 1 (
    echo  ERROR: git push failed - release aborted.
    if not defined APEX_NOPAUSE pause
    exit /b 1
)

REM --- 4. publish GitHub Release with the installer attached ---
gh release create "v%VER%" "installer\APEX_Setup.exe" --title "APEX v%VER%" --notes "Automated release v%VER%"
if errorlevel 1 (
    echo  ERROR: gh release failed. Is gh authenticated ^(gh auth status^)?
    if not defined APEX_NOPAUSE pause
    exit /b 1
)

echo.
echo  ===============================================
echo   DONE - v%VER% published.
echo   Installed apps self-update overnight.
echo  ===============================================
echo.
if not defined APEX_NOPAUSE pause
endlocal
