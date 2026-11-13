@echo off
rem One-time setup: creates a private Python environment and installs what FocusGate needs.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (set PY=python)
%PY% -m venv .venv
if errorlevel 1 (
  echo Could not create the environment. Install Python 3.10 or newer from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Something failed to install. Check your internet connection and run setup.bat again.
  pause
  exit /b 1
)
echo.
echo Setup finished. Double-click start.bat to open FocusGate.
pause
