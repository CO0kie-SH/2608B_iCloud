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

echo Starting Patchright: https://mayips.com/
echo Browser resources: "%CD%\browsers\patchright"
echo Close the browser window to end this test.
"%PYTHON%" -u "%CD%\scripts\patchright_browser.py" open "https://mayips.com/"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo.
  echo ERROR: Patchright test exited with code %EXIT_CODE%.
  pause
)
exit /b %EXIT_CODE%
