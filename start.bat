@echo off
chcp 65001 >nul 2>nul
rem ETF 分析工作台 - 一键启动
rem 启动后浏览器打开 http://127.0.0.1:5001

cd /d "%~dp0"

rem 依次尝试:项目内 venv -> WorkBuddy 管理 venv -> 环境变量 ETF_PYTHON -> 系统 python
set PY=
if exist "%~dp0venv\Scripts\python.exe" set PY="%~dp0venv\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set PY="%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY if defined ETF_PYTHON set PY="%ETF_PYTHON%"
if not defined PY (
  where python >nul 2>nul && set PY=python
)

if not defined PY (
  echo [ERROR] Python not found.
  echo   1^) Create a venv:  python -m venv venv
  echo   2^) Install deps:   venv\Scripts\python.exe -m pip install -r requirements.txt
  echo   Or set ETF_PYTHON to your python.exe path.
  pause
  exit /b 1
)

echo Starting ETF analysis workbench...
echo Open http://127.0.0.1:5001 in your browser.
echo Press Ctrl+C to stop.
echo.

%PY% app.py

pause
