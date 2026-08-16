@echo off
chcp 65001 >nul
setlocal EnableExtensions
set "PYTHON=D:\0Code2\py312\python.exe"
cd /d "%~dp0"

if "%~1"=="" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="/?" goto :usage

"%PYTHON%" "%~dp0main.py" cookie-login %*
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" (
  echo.
  echo FAILED
  exit /b %ERR%
)
echo.
echo DONE
exit /b 0

:usage
"%PYTHON%" "%~dp0main.py" cookie-login --help
echo.
echo Examples:
echo   cookie_login.bat -a user003@icloud.com --region cn --appleid --debug --keep-open 1200
echo   cookie_login.bat -a user003@icloud.com --region cn --debug --keep-open 1200
echo   cookie_login.bat -a user005@icloud.com --region cn --headless
echo   cookie_login.bat -a user001@icloud.com
echo.
exit /b 0
