@echo off
chcp 65001 >nul
setlocal
set "PYTHON=D:\0Code2\py312\python.exe"

cd /d "%~dp0.."
if errorlevel 1 (
  echo ERROR: Failed to enter the project directory.
  pause
  exit /b 1
)
if not exist "%PYTHON%" (
  echo ERROR: Python not found at "%PYTHON%".
  pause
  exit /b 1
)

echo Checking DuckDuckGo extension releases...
echo Downloads use HTTP_PROXY / HTTPS_PROXY if configured.
echo Close the DuckDuckGo test browser before updating.
"%PYTHON%" -u -m tools.duckduckgo_extension update
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" echo ERROR: Extension updater exited with code %EXIT_CODE%.
echo.
pause
exit /b %EXIT_CODE%
