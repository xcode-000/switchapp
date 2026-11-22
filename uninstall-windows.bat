@echo off
REM Removes the startup task and the firewall rule. Your data folder is NOT deleted.
net session >nul 2>&1
if errorlevel 1 (
  echo Please right-click uninstall-windows.bat and choose "Run as administrator".
  pause
  exit /b 1
)
powershell -NoProfile -Command "Stop-ScheduledTask -TaskName 'SwitchDoc' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'SwitchDoc' -Confirm:$false -ErrorAction SilentlyContinue"
taskkill /F /IM waitress-serve.exe >nul 2>&1
netsh advfirewall firewall delete rule name="SwitchDoc" >nul 2>&1
echo SwitchDoc service removed. The data folder (database + secret.key) was kept.
pause
