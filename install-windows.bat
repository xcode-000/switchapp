@echo off
REM ================================================================
REM  SwitchDoc - Windows installation
REM  Right-click this file and choose "Run as administrator".
REM  - creates a private Python environment and installs the packages
REM  - opens TCP port 8080 in Windows Firewall (domain + private networks)
REM  - creates a scheduled task "SwitchDoc" that starts the app when Windows
REM    starts (runs as SYSTEM, restarts automatically if it stops)
REM ================================================================
setlocal
cd /d "%~dp0"
set PORT=8080

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Please right-click install-windows.bat and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

REM Find a real Python (the Windows "python" shortcut that opens the Microsoft Store does not count)
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo  Python 3 is not installed on this computer.
  echo.
  echo  1. Download it from https://www.python.org/downloads/
  echo  2. Run the installer, choose "Customize installation"
  echo  3. Tick "Add python.exe to PATH" and, on the next page, "Install for all users"
  echo  4. Close this window and run install-windows.bat again as administrator
  echo.
  start "" https://www.python.org/downloads/
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo  Found %%v

echo [1/4] Creating the Python environment...
if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe --version >nul 2>&1 || rmdir /s /q venv
)
if not exist venv\Scripts\python.exe (
  if exist venv rmdir /s /q venv
  %PY% -m venv venv || goto :error
)
venv\Scripts\python -m pip install --upgrade pip >nul
echo [2/4] Installing packages (needs internet)...
venv\Scripts\python -m pip install -r requirements.txt || goto :error
if not exist data mkdir data

REM Pick a free port: another program may already use 8080
schtasks /End /TN SwitchDoc >nul 2>&1
taskkill /F /IM waitress-serve.exe >nul 2>&1
:checkport
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo  Port %PORT% is already used by another program - trying the next one.
  set /a PORT=%PORT%+10
  goto :checkport
)
(
  echo @echo off
  echo REM Started by the "SwitchDoc" scheduled task. Use start-windows.bat to run it by hand instead.
  echo cd /d "%%~dp0"
  echo if not exist data mkdir data
  echo venv\Scripts\waitress-serve.exe --listen=0.0.0.0:%PORT% app:app ^>^> data\service.log 2^>^&1
) > run-service.bat

echo [3/4] Opening port %PORT% in Windows Firewall...
netsh advfirewall firewall delete rule name="SwitchDoc" >nul 2>&1
netsh advfirewall firewall add rule name="SwitchDoc" dir=in action=allow protocol=TCP localport=%PORT% profile=domain,private >nul || goto :error

echo [4/4] Creating the "SwitchDoc" startup task...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$a = New-ScheduledTaskAction -Execute '%~dp0run-service.bat' -WorkingDirectory '%~dp0';" ^
  "$t = New-ScheduledTaskTrigger -AtStartup;" ^
  "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable;" ^
  "$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest;" ^
  "Unregister-ScheduledTask -TaskName 'SwitchDoc' -Confirm:$false -ErrorAction SilentlyContinue;" ^
  "Register-ScheduledTask -TaskName 'SwitchDoc' -Action $a -Trigger $t -Settings $s -Principal $p -Description 'SwitchDoc network switch documentation' | Out-Null;" ^
  "Start-ScheduledTask -TaskName 'SwitchDoc'" || goto :error

timeout /t 5 /nobreak >nul
echo.
echo  ============================================================
echo   SwitchDoc is installed and running.
echo   On this PC:      http://localhost:%PORT%
echo   From the LAN:    http://%COMPUTERNAME%:%PORT%
echo   Database:        %~dp0data\switchdoc.db
echo   Log file:        %~dp0data\service.log
echo  ============================================================
echo.
start "" http://localhost:%PORT%
pause
exit /b 0

:error
echo.
echo  Installation failed - see the message above.
pause
exit /b 1
