@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PYTHON="
if exist "D:\0Code2\py312\python.exe" set "PYTHON=D:\0Code2\py312\python.exe"
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python314\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python314\python.exe"
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYTHON if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYTHON (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON=py -3"
)
if not defined PYTHON (
  where python >nul 2>nul
  if not errorlevel 1 set "PYTHON=python"
)
if not defined PYTHON (
  echo FAILED: Python not found. Install Python 3.11+ or edit PYTHON in this bat.
  exit /b 1
)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8770"
echo Using: %PYTHON%
echo Starting web UI on http://127.0.0.1:%PORT%
%PYTHON% "%~dp0main.py" web --port %PORT%
if errorlevel 1 (
  echo.
  echo FAILED
  exit /b 1
)
exit /b 0
