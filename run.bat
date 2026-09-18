@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Використання: run.bat ^<script.py^>
  echo Напр.:        run.bat check_wallets.py
  pause
  exit /b 1
)

rem --- Find a working Python (same order as start.bat) ---
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
  echo [X] Python 3.11+ не знайдено.
  pause
  exit /b 1
)

echo [i] Python: %CRYPTOBOT_PY%
%CRYPTOBOT_PY% -c "import ccxt" 1>nul 2>nul || %CRYPTOBOT_PY% -m pip install "ccxt>=4.4,<5"
echo.
%CRYPTOBOT_PY% "%~1"
echo.
pause
