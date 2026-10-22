@echo off
timeout /t 30 /nobreak >nul
rem Starts WANG Radio in the system tray with no console window.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw "%~dp0radio.py") || (start "" pyw "%~dp0radio.py")
