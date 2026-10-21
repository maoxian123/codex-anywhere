@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-node.ps1" %*
set "node_exit=%ERRORLEVEL%"
if not "%node_exit%"=="0" (
  echo.
  echo Node manager stopped with exit code %node_exit%. Read the error above.
  pause
)
exit /b %node_exit%
