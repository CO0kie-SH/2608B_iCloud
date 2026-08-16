#!/usr/bin/env sh
# Independent HME production client. HTTP + curl only.
# Quota / Apple rate-limit stay in the Python service.

set -eu

BASE_URL="${BASE_URL:-http://127.0.0.1:8770}"
ACCOUNT=""
COUNT=1
THREADS=1
INTERFACE="legacy"
LOOP_MIN=0
FOREVER=0
DO_LIST=0
DO_ALL=0
INTERVAL=2
WORKDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TMPDIR="${TMPDIR:-/tmp}/2608b-produce-$$"
LOGDIR="$WORKDIR/logs"
STOPFILE=""
CREATED_TOTAL=0
FAILED_TOTAL=0
SUBMITTED=0
DEADLINE=0
NEXT_WAIT=0

mkdir -p "$TMPDIR" "$LOGDIR"
LOG="$LOGDIR/produce-$(date +%Y%m%d-%H%M%S).log"

cleanup() {
  [ -n "$STOPFILE" ] && rm -f "$STOPFILE"
  rm -rf "$TMPDIR"
}
trap cleanup EXIT INT TERM

usage() {
  cat <<'EOF'

用法: produce.sh [选项]

  只用 curl 打主项目 HTTP 生产接口。配额/风控由 Python 服务自己管。
  默认接口: legacy （旧版，每小时 5 个）

选项:
  -a, --account EMAIL    指定 iCloud 账户
  -n, --count N          单次数量，默认 1（老接口受 13-15 分钟间隔，只能 1）
  -t, --threads N        并发线程，默认 1
  -i, --interface NAME   生产接口，默认 legacy
  -u, --url URL          服务地址，默认 http://127.0.0.1:8770
  --list                 查询账户与配额
  --all                  对 options 里所有可用账户逐个生产
  --loop MINUTES         循环提交 N 分钟；0/-1/forever = 无限
  --forever              无限循环，Ctrl+C 停止
  --interval SEC         轮次间隔秒，默认 2
  -h, --help             帮助

示例:
  ./produce.sh --list
  ./produce.sh -a user001@icloud.com
  ./produce.sh --all --loop 30
  ./produce.sh --all --forever
EOF
}

say() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

log() {
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >>"$LOG"
}

json_get() {
  # $1 file  $2 key  — first string or number
  _file=$1
  _key=$2
  _val=$(sed -n "s/.*\"${_key}\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$_file" | head -n 1)
  if [ -z "$_val" ]; then
    _val=$(sed -n "s/.*\"${_key}\"[[:space:]]*:[[:space:]]*\\(-*[0-9][0-9]*\\).*/\\1/p" "$_file" | head -n 1)
  fi
  printf '%s' "$_val"
}

json_names() {
  awk '
    BEGIN { inacc=0 }
    /"accounts"/ { inacc=1 }
    inacc && /"name"/ {
      name=""
      if (match($0, /"name"[[:space:]]*:[[:space:]]*"[^"]+"/)) {
        name=substr($0, RSTART, RLENGTH)
        sub(/.*"name"[[:space:]]*:[[:space:]]*"/, "", name)
        sub(/".*/, "", name)
      }
      invalid=0; ok=1
    }
    inacc && name != "" && /"cookie_invalid"[[:space:]]*:[[:space:]]*true/ { invalid=1 }
    inacc && name != "" && /"hme_ok"[[:space:]]*:[[:space:]]*false/ { ok=0 }
    inacc && name != "" && /}/ {
      if (name != "" && invalid==0 && ok==1) print name
      name=""
    }
  ' "$1"
}

json_hmes() {
  sed -n 's/.*"hme"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1"
}

json_retry() {
  _hit=$(printf '%s' "$1" | sed -n 's/.*retry_after[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p')
  [ -n "$_hit" ] && printf '%s' "$_hit" || printf '30'
}

hdr_retry_after() {
  _ra=$(awk 'BEGIN{IGNORECASE=1} /^Retry-After:/ {gsub("\r",""); sub(/^[^:]+:[[:space:]]*/,""); print; exit}' "$1")
  [ -n "$_ra" ] && printf '%s' "$_ra" || printf '30'
}

http_get() {
  # $1 url  $2 body  $3 codefile
  curl -sS -m 20 "$1" -o "$2" -w '%{http_code}' >"$3"
}

health() {
  _code=$(curl -sS -m 5 "$BASE_URL/api/health" -o "$TMPDIR/health.json" -w '%{http_code}' || true)
  if [ "$_code" != "200" ]; then
    say "[ERR] 服务没起来: $BASE_URL  HTTP ${_code:-000}"
    say "      先跑 start_web.bat / python main.py web"
    return 1
  fi
}

cmd_list() {
  health || return 1
  curl -sS -m 15 "$BASE_URL/api/production/options" -o "$TMPDIR/options.json"
  say "---------- 生产选项 ----------"
  cat "$TMPDIR/options.json"
  echo
}

load_accounts() {
  curl -sS -m 15 "$BASE_URL/api/production/options" -o "$TMPDIR/options.json"
  json_names "$TMPDIR/options.json"
}

sleep_chunked() {
  _left=$1
  [ "$_left" -gt 0 ] 2>/dev/null || return 0
  while [ "$_left" -gt 0 ]; do
    should_stop && return 0
    if [ "$_left" -ge 5 ]; then
      sleep 5
      _left=$((_left - 5))
    else
      sleep "$_left"
      _left=0
    fi
  done
}

is_looping() {
  [ "$FOREVER" -eq 1 ] || [ "$LOOP_MIN" -gt 0 ]
}

note_wait() {
  _w=$1
  is_looping || return 0
  [ "$_w" -gt 0 ] 2>/dev/null || return 0
  if [ "$NEXT_WAIT" -eq 0 ] || [ "$_w" -lt "$NEXT_WAIT" ]; then
    NEXT_WAIT=$_w
  fi
}

maybe_wait_rate() {
  is_looping || return 0
  if grep -q -i -e RATE_LIMIT -e rate_limited "$TMPDIR/job.json" 2>/dev/null; then
    _wait=$(json_retry "$(cat "$TMPDIR/job.json")")
    say "[NOTE] 服务风控 hint=${_wait}s，本轮继续扫其他账户"
    note_wait "$_wait"
  fi
}

should_stop() {
  [ "$FOREVER" -eq 1 ] && return 1
  [ "$LOOP_MIN" -gt 0 ] || return 1
  [ "$(date +%s)" -ge "$DEADLINE" ]
}

produce_one() {
  _acc=$1
  SUBMITTED=$((SUBMITTED + 1))
  echo
  say "[POST] $_acc  interface=$INTERFACE  count=$COUNT  threads=$THREADS"
  log "POST account=$_acc interface=$INTERFACE count=$COUNT threads=$THREADS"

  printf '{"account":"%s","interface":"%s","count":%s,"threads":%s}\n' \
    "$_acc" "$INTERFACE" "$COUNT" "$THREADS" >"$TMPDIR/req.json"

  _pcode=$(curl -sS -m 30 -X POST "$BASE_URL/api/production" \
    -H "Content-Type: application/json" \
    --data-binary "@$TMPDIR/req.json" \
    -D "$TMPDIR/resp.hdr" \
    -o "$TMPDIR/resp.json" \
    -w '%{http_code}' || true)

  if [ "$_pcode" = "429" ]; then
    _ra=$(hdr_retry_after "$TMPDIR/resp.hdr")
    say "[429] 服务限流，Retry-After=${_ra}s"
    log "429 account=$_acc retry_after=$_ra"
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    note_wait "$_ra"
    return 0
  fi

  if [ "$_pcode" = "409" ]; then
    say "[409] COOKIE_INVALID  $_acc 已标记失效，跳过"
    log "COOKIE_INVALID account=$_acc"
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    return 0
  fi

  if [ "$_pcode" != "202" ] && [ "$_pcode" != "200" ]; then
    say "[ERR] 提交失败 HTTP $_pcode"
    cat "$TMPDIR/resp.json"
    echo
    log "SUBMIT_FAIL http=$_pcode"
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    return 0
  fi

  _job=$(json_get "$TMPDIR/resp.json" job_id)
  if [ -z "$_job" ]; then
    say "[ERR] 响应里没有 job_id"
    cat "$TMPDIR/resp.json"
    echo
    FAILED_TOTAL=$((FAILED_TOTAL + 1))
    return 0
  fi
  say "[JOB] $_job  HTTP $_pcode"

  while :; do
    _jcode=$(curl -sS -m 20 "$BASE_URL/api/production/jobs/$_job" \
      -o "$TMPDIR/job.json" -w '%{http_code}' || true)
    if [ "$_jcode" != "200" ]; then
      say "[WARN] 查任务 HTTP ${_jcode:-000}，重试"
      sleep 2
      continue
    fi
    _status=$(json_get "$TMPDIR/job.json" status)
    case "$_status" in
      pending) say "[..] $_job pending"; sleep 2; continue ;;
      running) say "[..] $_job running"; sleep 2; continue ;;
    esac
    break
  done

  _created=$(json_get "$TMPDIR/job.json" created)
  _error=$(json_get "$TMPDIR/job.json" error)
  [ -n "$_created" ] || _created=0

  if [ "$_status" = "done" ]; then
    say "[OK] $_job created=$_created"
    if [ "$_created" -gt 0 ] 2>/dev/null; then
      json_hmes "$TMPDIR/job.json" | while IFS= read -r _hme; do
        [ -n "$_hme" ] || continue
        say "       + $_hme"
        log "CREATED $_hme"
      done
      CREATED_TOTAL=$((CREATED_TOTAL + _created))
    fi
    if [ -n "$_error" ]; then
      say "[NOTE] 部分失败: $_error"
      FAILED_TOTAL=$((FAILED_TOTAL + 1))
      maybe_wait_rate
    fi
    log "DONE job=$_job account=$_acc created=$_created error=$_error"
    return 0
  fi

  say "[FAIL] $_job status=$_status created=$_created"
  [ -n "$_error" ] && say "       $_error"
  log "FAIL job=$_job account=$_acc status=$_status created=$_created error=$_error"
  FAILED_TOTAL=$((FAILED_TOTAL + 1))
  maybe_wait_rate
}

# ---------- args ----------
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --list) DO_LIST=1; shift ;;
    --all) DO_ALL=1; shift ;;
    -a|--account) ACCOUNT=$2; shift 2 ;;
    -n|--count) COUNT=$2; shift 2 ;;
    -t|--threads) THREADS=$2; shift 2 ;;
    -i|--interface) INTERFACE=$2; shift 2 ;;
    -u|--url) BASE_URL=$2; shift 2 ;;
    --forever) FOREVER=1; shift ;;
    --loop)
      case "$2" in
        forever|inf|0|-1) FOREVER=1 ;;
        *) LOOP_MIN=$2 ;;
      esac
      shift 2
      ;;
    --interval) INTERVAL=$2; shift 2 ;;
    *) say "[ERR] 未知参数: $1"; usage; exit 2 ;;
  esac
done

if [ "$DO_LIST" -eq 1 ]; then
  cmd_list
  exit $?
fi

if [ "$DO_ALL" -eq 0 ] && [ -z "$ACCOUNT" ]; then
  say "[ERR] 必须指定 -a 账户，或使用 --all / --list"
  usage
  exit 2
fi

health || exit 1

if [ "$FOREVER" -eq 1 ]; then
  say "[INFO] 无限循环生产，接口=$INTERFACE，Ctrl+C 停止，日志=$LOG"
elif [ "$LOOP_MIN" -gt 0 ]; then
  DEADLINE=$(( $(date +%s) + LOOP_MIN * 60 ))
  say "[INFO] 循环生产 ${LOOP_MIN} 分钟，接口=$INTERFACE，日志=$LOG"
fi

while :; do
  should_stop && break
  NEXT_WAIT=0
  if [ "$DO_ALL" -eq 1 ]; then
    load_accounts >"$TMPDIR/accounts.txt" || true
    if [ ! -s "$TMPDIR/accounts.txt" ]; then
      echo "[WARN] options 里没有账户"
    else
      while IFS= read -r _acc; do
        [ -n "$_acc" ] || continue
        should_stop && break
        produce_one "$_acc"
      done <"$TMPDIR/accounts.txt"
    fi
  else
    produce_one "$ACCOUNT"
  fi
  is_looping || break
  should_stop && break
  _sleepfor=$INTERVAL
  if [ "$NEXT_WAIT" -gt "$_sleepfor" ]; then
    _sleepfor=$NEXT_WAIT
    say "[WAIT] 本轮最短风控 ${_sleepfor}s，扫完再睡"
  fi
  sleep_chunked "$_sleepfor"
done

echo
say "========== 汇总 =========="
say "提交任务: $SUBMITTED"
say "成功生产: $CREATED_TOTAL"
say "失败条目: $FAILED_TOTAL"
say "日志: $LOG"
[ "$CREATED_TOTAL" -gt 0 ]
