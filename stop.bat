@echo off
rem Closes FocusGate and its watchdog cleanly. It starts again next time you run start.bat or sign in.
cd /d "%~dp0"
".venv\Scripts\python.exe" "run_focusgate.pyw" --stop
timeout /t 8 >nul
