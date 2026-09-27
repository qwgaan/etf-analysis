@echo off
chcp 65001 >nul 2>nul
rem ETF 分析工作台 - 一键启动
rem 启动后浏览器打开 http://127.0.0.1:5001

cd /d "%~dp0"

set "PY="
set "WHY="

rem 候选 Python（第一个能 import flask 的胜出）
set "C1=%~dp0venv\Scripts\python.exe"
set "C2=%~dp0.venv\Scripts\python.exe"
set "C3=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

if "%PY%"=="" if exist "%C1%" (
  "%C1%" -c "import flask" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%C1%"
    set "WHY=项目 venv"
  )
)
if "%PY%"=="" if exist "%C2%" (
  "%C2%" -c "import flask" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%C2%"
    set "WHY=项目 .venv"
  )
)
if "%PY%"=="" if exist "%C3%" (
  "%C3%" -c "import flask" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%C3%"
    set "WHY=WorkBuddy 管理 venv"
  )
)
if "%PY%"=="" if defined ETF_PYTHON (
  "%ETF_PYTHON%" -c "import flask" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%ETF_PYTHON%"
    set "WHY=环境变量 ETF_PYTHON"
  )
)
if "%PY%"=="" (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    "%%i" -c "import flask" >nul 2>nul
    if not errorlevel 1 (
      set "PY=%%i"
      set "WHY=系统 python"
      goto :found
    )
  )
)
:found

if "%PY%"=="" (
  echo [ERROR] 未找到带 flask 的 Python。
  echo   1^) 创建虚拟环境:  python -m venv venv
  echo   2^) 安装依赖:       venv\Scripts\python.exe -m pip install -r requirements.txt
  echo   或设置环境变量 ETF_PYTHON 指向带 flask 的 python.exe
  pause
  exit /b 1
)

echo 使用 Python: %PY%  ^(%WHY%^)
echo.

rem ---- 端口占用预检:5001 已被占用说明实例已在运行,直接打开浏览器即可 ----
set "PORT_FREE=1"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"TCP.*:5001 .*LISTENING"') do (
  set "PORT_FREE=0"
  set "OLD_PID=%%p"
)
if "%PORT_FREE%"=="0" (
  echo [提示] 端口 5001 已被占用^(PID %OLD_PID%^),说明本程序已在运行。
  echo        不会重复启动,直接为你打开浏览器。
  echo        如需重启:先关闭那个进程,或执行  taskkill /PID %OLD_PID% /F
  echo.
  start "" "http://127.0.0.1:5001"
  timeout /t 3 >nul
  exit /b 0
)

echo Starting ETF analysis workbench...
echo Open http://127.0.0.1:5001 in your browser.
echo Press Ctrl+C to stop.
echo.

"%PY%" app.py

rem 若 app.py 异常退出,保留窗口以便查看报错
echo.
echo [程序已退出] 若上方有报错信息,请截图反馈。
pause
