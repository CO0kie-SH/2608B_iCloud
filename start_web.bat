@echo off
chcp 65001 >nul
setlocal
set "PYTHON=D:\0Code2\py312\python.exe"
cd /d "%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8770"
echo Starting web UI on http://127.0.0.1:%PORT%
"%PYTHON%" "%~dp0main.py" web --port %PORT%
if errorlevel 1 (
  echo.
  echo FAILED
  exit /b 1
)
exit /b 0
