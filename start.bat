@echo off
chcp 65001 >nul
title Winshere Printer 内网共享打印

REM 在已连接打印机的 Windows 上双击运行本脚本即可。
REM 首次运行会自动创建虚拟环境并安装依赖。

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Python, 请先安装 Python 3.10+ : https://www.python.org/downloads/
  echo        安装时请勾选 "Add Python to PATH"
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [初始化] 正在创建虚拟环境 ...
  python -m venv .venv
)

echo [初始化] 正在安装/更新依赖 ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt

echo.
".venv\Scripts\python.exe" app.py %*

pause
