@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

rem Independent HME production client. HTTP + curl only.
rem Quota / Apple rate-limit stay in the Python service.

set "BASE_URL=http://127.0.0.1:8770"
set "ACCOUNT="
set "COUNT=1"
set "THREADS=1"
set "INTERFACE=legacy"
set "LOOP_MIN=0"
set "FOREVER=0"
set "DO_LIST=0"
set "DO_ALL=0"
set "INTERVAL=2"
set "WORKDIR=%~dp0"
set "JSONJS=%WORKDIR%scripts\produce_json.js"
set "TMPDIR=%TEMP%\2608b-produce"
set "DEADLINE=0"
set "NEXT_WAIT=0"
if not exist "%TMPDIR%" mkdir "%TMPDIR%"
if not exist "%WORKDIR%logs" mkdir "%WORKDIR%logs"

for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"
set "LOG=%WORKDIR%logs\produce-%STAMP%.log"

call :parse_args %*
if errorlevel 1 exit /b 1

if "%DO_LIST%"=="1" (
  call :cmd_list
  exit /b !ERRORLEVEL!
)

if "%DO_ALL%"=="0" if not defined ACCOUNT (
  echo [ERR] 必须指定 -a 账户，或使用 --all / --list
  call :usage
  exit /b 2
)

call :health
if errorlevel 1 exit /b 1

set "CREATED_TOTAL=0"
set "FAILED_TOTAL=0"
set "SUBMITTED=0"

if "%FOREVER%"=="1" (
  echo [INFO] 无限循环生产，接口=%INTERFACE%，Ctrl+C 停止，日志=%LOG%
) else if %LOOP_MIN% GTR 0 (
  set /a LOOP_SEC=%LOOP_MIN%*60
  call :now_epoch START_EPOCH
  set /a DEADLINE=!START_EPOCH!+!LOOP_SEC!
  echo [INFO] 循环生产 %LOOP_MIN% 分钟，接口=%INTERFACE%，日志=%LOG%
)

:round
call :deadline_hit
if !DEADLINE_HIT! EQU 1 goto :finish
set "NEXT_WAIT=0"

set "ROUND_ACCOUNTS="
if "%DO_ALL%"=="1" (
  call :load_accounts
  if errorlevel 1 goto :wait_or_stop
) else (
  set "ROUND_ACCOUNTS=%ACCOUNT%"
)

for %%A in (!ROUND_ACCOUNTS!) do (
  call :deadline_hit
  if !DEADLINE_HIT! EQU 1 goto :finish
  call :produce_one "%%~A"
)

:wait_or_stop
if "%FOREVER%"=="0" if %LOOP_MIN% LEQ 0 goto :finish
call :deadline_hit
if !DEADLINE_HIT! EQU 1 goto :finish
set "SLEEPFOR=%INTERVAL%"
if !NEXT_WAIT! GTR !SLEEPFOR! set "SLEEPFOR=!NEXT_WAIT!"
if !SLEEPFOR! GTR %INTERVAL% echo [WAIT] 本轮最短风控 !SLEEPFOR!s，扫完再睡
call :sleep !SLEEPFOR!
goto :round

:finish
echo.
echo ========== 汇总 ==========
echo 提交任务: %SUBMITTED%
echo 成功生产: %CREATED_TOTAL%
echo 失败条目: %FAILED_TOTAL%
echo 日志: %LOG%
if %CREATED_TOTAL% GTR 0 (exit /b 0) else (exit /b 1)

rem ------------------------------------------------------------------
:parse_args
if "%~1"=="" goto :args_done
if /i "%~1"=="-h" goto :show_usage
if /i "%~1"=="--help" goto :show_usage
if /i "%~1"=="--list" set "DO_LIST=1" & shift & goto :parse_args
if /i "%~1"=="--all" set "DO_ALL=1" & shift & goto :parse_args
if /i "%~1"=="-a" set "ACCOUNT=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--account" set "ACCOUNT=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="-n" set "COUNT=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--count" set "COUNT=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="-t" set "THREADS=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--threads" set "THREADS=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="-i" set "INTERFACE=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--interface" set "INTERFACE=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="-u" set "BASE_URL=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--url" set "BASE_URL=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--forever" set "FOREVER=1" & shift & goto :parse_args
if /i "%~1"=="--loop" (
  if /i "%~2"=="forever" set "FOREVER=1" & shift & shift & goto :parse_args
  if /i "%~2"=="inf" set "FOREVER=1" & shift & shift & goto :parse_args
  if /i "%~2"=="0" set "FOREVER=1" & shift & shift & goto :parse_args
  if /i "%~2"=="-1" set "FOREVER=1" & shift & shift & goto :parse_args
  set "LOOP_MIN=%~2"
  shift & shift & goto :parse_args
)
if /i "%~1"=="--interval" set "INTERVAL=%~2" & shift & shift & goto :parse_args
echo [ERR] 未知参数: %~1
call :usage
exit /b 2
:show_usage
call :usage
exit /b 1
:args_done
if not defined INTERFACE set "INTERFACE=legacy"
if not defined COUNT set "COUNT=1"
if not defined THREADS set "THREADS=1"
if not defined LOOP_MIN set "LOOP_MIN=0"
if not defined FOREVER set "FOREVER=0"
if not defined INTERVAL set "INTERVAL=2"
exit /b 0

:usage
echo.
echo 用法: produce.bat [选项]
echo.
echo   只用 curl 打主项目 HTTP 生产接口。配额/风控由 Python 服务自己管。
echo   默认接口: legacy （旧版，每小时 5 个）
echo.
echo 选项:
echo   -a, --account EMAIL    指定 iCloud 账户
echo   -n, --count N          单次数量，默认 1（老接口受 13-15 分钟间隔，只能 1）
echo   -t, --threads N        并发线程，默认 1
echo   -i, --interface NAME   生产接口，默认 legacy
echo   -u, --url URL          服务地址，默认 http://127.0.0.1:8770
echo   --list                 查询账户与配额
echo   --all                  对 options 里所有可用账户逐个生产
echo   --loop MINUTES         循环提交 N 分钟；0/-1/forever = 无限
echo   --forever              无限循环，Ctrl+C 停止
echo   --interval SEC         轮次间隔秒，默认 2
echo   -h, --help             帮助
echo.
echo 示例:
echo   produce.bat --list
echo   produce.bat -a user001@icloud.com
echo   produce.bat --all --loop 30
echo   produce.bat --all --forever
exit /b 0

:health
curl.exe -sS -m 5 "%BASE_URL%/api/health" -o "%TMPDIR%\health.json" -w "%%{http_code}" > "%TMPDIR%\health.code"
set /p HCODE=<"%TMPDIR%\health.code"
if not "%HCODE%"=="200" (
  echo [ERR] 服务没起来: %BASE_URL%  HTTP %HCODE%
  echo       先跑 start_web.bat
  exit /b 1
)
exit /b 0

:cmd_list
call :health
if errorlevel 1 exit /b 1
curl.exe -sS -m 15 "%BASE_URL%/api/production/options" -o "%TMPDIR%\options.json"
if errorlevel 1 (
  echo [ERR] 拉 options 失败
  exit /b 1
)
echo ---------- 生产选项 ----------
type "%TMPDIR%\options.json"
echo.
exit /b 0

:load_accounts
curl.exe -sS -m 15 "%BASE_URL%/api/production/options" -o "%TMPDIR%\options.json"
if errorlevel 1 (
  echo [ERR] 拉 options 失败
  exit /b 1
)
set "ROUND_ACCOUNTS="
for /f "usebackq delims=" %%N in (`cscript //nologo "%JSONJS%" names "%TMPDIR%\options.json"`) do (
  set "ROUND_ACCOUNTS=!ROUND_ACCOUNTS! %%N"
)
if not defined ROUND_ACCOUNTS (
  echo [WARN] options 里没有账户
  exit /b 1
)
exit /b 0

:produce_one
set "ACC=%~1"
set /a SUBMITTED+=1
echo.
echo [POST] %ACC%  interface=%INTERFACE%  count=%COUNT%  threads=%THREADS%
call :log "POST account=%ACC% interface=%INTERFACE% count=%COUNT% threads=%THREADS%"

> "%TMPDIR%\req.json" echo {"account":"%ACC%","interface":"%INTERFACE%","count":%COUNT%,"threads":%THREADS%}

curl.exe -sS -m 30 -X POST "%BASE_URL%/api/production" ^
  -H "Content-Type: application/json" ^
  --data-binary "@%TMPDIR%\req.json" ^
  -D "%TMPDIR%\resp.hdr" ^
  -o "%TMPDIR%\resp.json" ^
  -w "%%{http_code}" > "%TMPDIR%\resp.code"

set /p PCODE=<"%TMPDIR%\resp.code"
set "JOB_ID="
set "RETRY_AFTER="

if "%PCODE%"=="429" (
  call :hdr_retry_after
  echo [429] 服务限流，Retry-After=!RETRY_AFTER!s
  call :log "429 account=%ACC% retry_after=!RETRY_AFTER!"
  set /a FAILED_TOTAL+=1
  call :note_wait !RETRY_AFTER!
  exit /b 0
)

if "%PCODE%"=="409" (
  echo [409] COOKIE_INVALID  %ACC% 已标记失效，跳过
  call :log "COOKIE_INVALID account=%ACC%"
  set /a FAILED_TOTAL+=1
  exit /b 0
)

if not "%PCODE%"=="202" if not "%PCODE%"=="200" (
  echo [ERR] 提交失败 HTTP %PCODE%
  type "%TMPDIR%\resp.json"
  echo.
  call :log "SUBMIT_FAIL http=%PCODE%"
  set /a FAILED_TOTAL+=1
  exit /b 0
)

for /f "usebackq delims=" %%V in (`cscript //nologo "%JSONJS%" get "%TMPDIR%\resp.json" job_id`) do set "JOB_ID=%%V"
if not defined JOB_ID (
  echo [ERR] 响应里没有 job_id
  type "%TMPDIR%\resp.json"
  echo.
  set /a FAILED_TOTAL+=1
  exit /b 0
)
echo [JOB] !JOB_ID!  HTTP %PCODE%

:poll_job
curl.exe -sS -m 20 "%BASE_URL%/api/production/jobs/!JOB_ID!" -o "%TMPDIR%\job.json" -w "%%{http_code}" > "%TMPDIR%\job.code"
set /p JCODE=<"%TMPDIR%\job.code"
if not "!JCODE!"=="200" (
  echo [WARN] 查任务 HTTP !JCODE!，重试
  call :sleep 2
  goto :poll_job
)
set "JSTATUS="
for /f "usebackq delims=" %%V in (`cscript //nologo "%JSONJS%" get "%TMPDIR%\job.json" status`) do set "JSTATUS=%%V"
if /i "!JSTATUS!"=="pending" (
  echo [..] !JOB_ID! pending
  call :sleep 2
  goto :poll_job
)
if /i "!JSTATUS!"=="running" (
  echo [..] !JOB_ID! running
  call :sleep 2
  goto :poll_job
)

set "JCREATED="
set "JERROR="
for /f "usebackq delims=" %%V in (`cscript //nologo "%JSONJS%" get "%TMPDIR%\job.json" created`) do set "JCREATED=%%V"
for /f "usebackq delims=" %%V in (`cscript //nologo "%JSONJS%" get "%TMPDIR%\job.json" error`) do set "JERROR=%%V"
if not defined JCREATED set "JCREATED=0"

if /i "!JSTATUS!"=="done" (
  echo [OK] !JOB_ID! created=!JCREATED!
  if !JCREATED! GTR 0 (
    for /f "usebackq delims=" %%H in (`cscript //nologo "%JSONJS%" hmes "%TMPDIR%\job.json"`) do (
      echo        + %%H
      call :log "CREATED %%H"
    )
    set /a CREATED_TOTAL+=JCREATED
  )
  if defined JERROR if not "!JERROR!"=="" (
    echo [NOTE] 部分失败: !JERROR!
    call :maybe_wait_rate
    set /a FAILED_TOTAL+=1
  )
  call :log "DONE job=!JOB_ID! account=%ACC% created=!JCREATED! error=!JERROR!"
  exit /b 0
)

echo [FAIL] !JOB_ID! status=!JSTATUS! created=!JCREATED!
if defined JERROR echo        !JERROR!
call :log "FAIL job=!JOB_ID! account=%ACC% status=!JSTATUS! created=!JCREATED! error=!JERROR!"
set /a FAILED_TOTAL+=1
call :maybe_wait_rate
exit /b 0

:maybe_wait_rate
if "%FOREVER%"=="0" if %LOOP_MIN% LEQ 0 exit /b 0
findstr /i /c:"RATE_LIMIT" /c:"rate_limited" "%TMPDIR%\job.json" >nul
if errorlevel 1 exit /b 0
set "WAITSEC="
for /f "usebackq delims=" %%R in (`cscript //nologo "%JSONJS%" retryfile "%TMPDIR%\job.json"`) do set "WAITSEC=%%R"
if not defined WAITSEC set "WAITSEC=30"
echo [NOTE] 服务风控 hint=!WAITSEC!s，本轮继续扫其他账户
call :note_wait !WAITSEC!
exit /b 0

:note_wait
if "%FOREVER%"=="0" if "%LOOP_MIN%"=="0" exit /b 0
set /a _W=%~1
if !_W! LSS 1 exit /b 0
if !NEXT_WAIT! EQU 0 (
  set "NEXT_WAIT=!_W!"
) else if !_W! LSS !NEXT_WAIT! (
  set "NEXT_WAIT=!_W!"
)
exit /b 0

:hdr_retry_after
set "RETRY_AFTER="
for /f "usebackq tokens=1,* delims=:" %%H in ("%TMPDIR%\resp.hdr") do (
  if /i "%%H"=="Retry-After" (
    set "RETRY_AFTER=%%I"
    set "RETRY_AFTER=!RETRY_AFTER: =!"
  )
)
if not defined RETRY_AFTER set "RETRY_AFTER=30"
exit /b 0

:log
>> "%LOG%" echo [%DATE% %TIME%] %~1
exit /b 0

:now_epoch
for /f %%T in ('powershell -NoProfile -Command "[int][double]::Parse((Get-Date -UFormat %%s))"') do set "%~1=%%T"
exit /b 0

:deadline_hit
set "DEADLINE_HIT=0"
if "%FOREVER%"=="1" exit /b 0
if %LOOP_MIN% LEQ 0 exit /b 0
if %DEADLINE% LEQ 0 exit /b 0
call :now_epoch NOW
if !NOW! GEQ !DEADLINE! set "DEADLINE_HIT=1"
exit /b 0

:sleep
set /a _S=%~1
if !_S! LSS 1 set _S=1
:sleep_chunk
call :deadline_hit
if !DEADLINE_HIT! EQU 1 exit /b 0
if !_S! LEQ 0 exit /b 0
if !_S! GEQ 5 (
  ping -n 6 127.0.0.1 >nul
  set /a _S-=5
  goto :sleep_chunk
)
set /a _P=_S+1
ping -n !_P! 127.0.0.1 >nul
set /a _S=0
exit /b 0
