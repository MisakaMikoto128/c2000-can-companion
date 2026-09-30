@echo off
rem ============================================================
rem  Create .venv and install dependencies.
rem  Run once (needs network for pip); later runs are no-ops.
rem ============================================================
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo [OK] .venv already exists, nothing to do.
    pause
    exit /b 0
)

set PY=
where python >nul 2>&1 && set PY=python
if not defined PY (
    where py >nul 2>&1 && set PY=py -3
)
if not defined PY (
    echo [ERROR] Python 3.10+ not found. Install it first: https://www.python.org/
    pause
    exit /b 1
)

echo [1/3] Creating virtual environment (.venv) ...
%PY% -m venv .venv
if errorlevel 1 (
    echo [ERROR] venv creation failed.
    pause
    exit /b 1
)

echo [2/3] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

echo [3/3] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check network / proxy.
    pause
    exit /b 1
)

echo.
echo [DONE] Environment ready. Use start_gui.bat to launch.
pause
