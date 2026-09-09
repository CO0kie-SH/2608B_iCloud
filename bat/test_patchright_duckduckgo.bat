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

echo Starting Patchright with DuckDuckGo: https://mayips.com/
echo Close this browser before updating the extension.
"%PYTHON%" -u "%CD%\scripts\patchright_browser.py" open "https://mayips.com/" --extension-dir "%CD%\browsers\extensions\duckduckgo\active" --profile-dir "%CD%\db\browser_profiles\patchright-duckduckgo" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo ERROR: Extension test exited with code %EXIT_CODE%.
  echo Run bat\update_duckduckgo_extension.bat if the extension is missing.
  pause
)
exit /b %EXIT_CODE%
