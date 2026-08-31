@echo off
cd /d "%~dp0"

rem Find a working Python: PATH first, then the codex runtime, then py launcher.
set "CRYPTOBOT_PY="
for %%P in (python.exe) do if not defined CRYPTOBOT_PY set "CRYPTOBOT_PY=%%~$PATH:P"
if defined CRYPTOBOT_PY (
  "%CRYPTOBOT_PY%" -c "import sys" 1>nul 2>nul || set "CRYPTOBOT_PY="
)
if not defined CRYPTOBOT_PY if exist "%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" set "CRYPTOBOT_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not defined CRYPTOBOT_PY (
  py -3 --version 1>nul 2>nul && set "CRYPTOBOT_PY=py -3"
)
if not defined CRYPTOBOT_PY (
  echo Python not found. Install Python 3.11+ and re-run.
  pause
  exit /b 1
)

start "" http://127.0.0.1:8765
%CRYPTOBOT_PY% server.py
pause
