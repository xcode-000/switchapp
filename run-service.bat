@echo off
REM Started by the "SwitchDoc" scheduled task. Use start-windows.bat to run it by hand instead.
cd /d "%~dp0"
if not exist data mkdir data
venv\Scripts\waitress-serve.exe --listen=0.0.0.0:8080 app:app >> data\service.log 2>&1
