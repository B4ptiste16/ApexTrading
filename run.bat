@echo off
cd /d "%~dp0"
echo.
echo  ===============================================
echo   APEX Trading Platform
echo  ===============================================
echo.
echo  Starting... loading libraries.
echo  The app window can take up to ~1-2 minutes to
echo  appear on the FIRST launch. Please wait and do
echo  NOT close this window.
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python was not found on PATH.
    echo  Install Python 3, then run this again.
    echo.
    pause
    exit /b 1
)

REM First-run dependency bootstrap: only installs if something is missing
REM (fast no-op afterwards). Source-only - the installed .exe bundles all.
python -c "import alpaca, PyQt6, pandas, yfinance, anthropic" 1>nul 2>nul
if errorlevel 1 (
    echo  Installing dependencies (first run only, can take a few minutes)...
    python -m pip install --quiet --disable-pip-version-check -r requirements.txt
)

python main.py

echo.
echo  APEX has exited. If the window never appeared,
echo  read any error message shown above.
echo.
pause