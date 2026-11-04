@echo off
cd /d "%~dp0"
echo Setting up Framekeep on this computer...
python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or newer is required. Install it from python.org and enable Add Python to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt -c requirements-lock.txt
if errorlevel 1 goto failed
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create-shortcuts.ps1"
if errorlevel 1 goto failed
echo Setup complete. Open the Framekeep desktop icon or Start Framekeep.cmd.
pause
exit /b 0
:failed
echo.
echo Setup did not finish. Check the error above and try again.
pause
exit /b 1
