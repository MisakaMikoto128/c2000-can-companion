@echo off
rem CAN Companion（pywebview GUI）：CAN 固件升级 + 变量观察
rem 只用 .venv 一个解释器启动；互斥量单实例检查在 host_app 内做。
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run setup_env.bat once to create the Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m monitor_tui.host_app %*
rem 退出码 3 = 已有一个实例在跑；双击启动时 pause 让提示停留可读。
if errorlevel 3 pause
endlocal
