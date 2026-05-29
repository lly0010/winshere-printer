@echo off
REM Pure ASCII so cmd.exe parses it under any code page.
REM Launches the desktop control panel (start/stop service, set port).
chcp 65001 >nul
title Winshere Printer Control Panel

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10+ first:
  echo         https://www.python.org/downloads/  ^(check "Add Python to PATH"^)
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [SETUP] Creating virtual environment ...
  python -m venv .venv
)

echo [SETUP] Installing / updating dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Failed to install dependencies. Check your network / pip.
  pause
  exit /b 1
)

REM Use pythonw.exe so the control panel runs without a console window.
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" control_panel.py
) else (
  ".venv\Scripts\python.exe" control_panel.py
)
