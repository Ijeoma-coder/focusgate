@echo off
rem Shows errors in a console window. Use this if start.bat seems to do nothing.
cd /d "%~dp0"
".venv\Scripts\python.exe" "run_focusgate.pyw"
pause
