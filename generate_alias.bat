@echo off
chcp 65001 >nul
setlocal
set "PYTHON=D:\0Code2\py312\python.exe"
cd /d "%~dp0"
set "ACCOUNT=%~1"
set "LABEL=%~2"
if "%ACCOUNT%"=="" (
  "%PYTHON%" "%~dp0scripts\generate_alias.py"
) else if "%LABEL%"=="" (
  "%PYTHON%" "%~dp0scripts\generate_alias.py" -a "%ACCOUNT%"
) else (
  "%PYTHON%" "%~dp0scripts\generate_alias.py" -a "%ACCOUNT%" -l "%LABEL%"
)
if errorlevel 1 (
  echo.
  echo FAILED
  exit /b 1
)
echo.
echo DONE
exit /b 0