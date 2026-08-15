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
echo.
echo 用法: cookie_login.bat [选项]
echo.
echo   有头登录 iCloud / Apple 账户，采集 Cookie 写回 YAML。
echo   实验性质：会话写入 db\cookie\，下次自动注入。
echo.
echo 选项:
echo   -a, --account EMAIL    指定账户（多账户时必填）
echo   --timeout SEC          等待登录超时，默认 600
echo   --url URL              起始页，默认 https://www.^<domain^>/
echo   --appleid              先登录 iCloud，再打开 /settings/ 采 Apple 账户页 session
echo   --keep-open SEC        采完后再挂 SEC 秒再关，默认 0
echo   --debug                页面内容变化落盘 HTML/文本，并按页记 cookie
echo   --region cn            中国区，打开 icloud.com.cn
echo   --suffix cn            同上
echo   --no-reuse-session     不注入 db\cookie\ 里的上次会话
echo   --no-backup            写回 YAML 前不生成 .bak
echo   -h, --help             本帮助
echo.
echo 示例:
echo   cookie_login.bat -a maohongwei003@icloud.com --region cn --appleid --debug --keep-open 1200
echo   cookie_login.bat -a maohongwei003@icloud.com --region cn --debug --keep-open 1200
echo   cookie_login.bat -a maohongwei001@icloud.com
echo.
exit /b 0
