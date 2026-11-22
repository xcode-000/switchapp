@echo off
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  set "PY="
  py -3 --version >nul 2>&1 && set "PY=py -3"
  if not defined PY python --version >nul 2>&1 && set "PY=python"
)
if not exist venv\Scripts\python.exe (
  if not defined PY (
    echo Python 3 is not installed. Get it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
  )
  %PY% -m venv venv
  venv\Scripts\python -m pip install -r requirements.txt
)
echo SwitchDoc running on http://localhost:8090  (Ctrl+C to stop)
venv\Scripts\waitress-serve --listen=0.0.0.0:8090 app:app
