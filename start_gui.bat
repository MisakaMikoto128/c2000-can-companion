@echo off
rem CAN Companion launcher (pywebview GUI): firmware upgrade + variable watch
rem Uses only the .venv interpreter; single-instance check lives in host_app.
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run setup_env.bat once to create the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m monitor_tui.host_app %*
rem Exit code 3 = another instance is running; pause keeps the message readable.
if errorlevel 3 pause
endlocal
