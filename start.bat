@echo off
rem ETF 分析工作台 - 一键启动
rem 启动后浏览器打开 http://127.0.0.1:5001

cd /d "%~dp0"

set PY="C:\Users\admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if not exist %PY% (
  echo [ERROR] Python venv not found. Please install dependencies first:
  echo   %PY% -m pip install --no-cache-dir akshare flask pandas numpy
  pause
  exit /b 1
)

echo Starting ETF analysis workbench...
echo Open http://127.0.0.1:5001 in your browser.
echo Press Ctrl+C to stop.
echo.

%PY% app.py

pause
