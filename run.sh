#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
    # Match run.ps1: lightweight timestamped console logs.
    local ts
    ts="$(date +%H:%M:%S 2>/dev/null || true)"
    echo "[${ts}] $*"
}

RUN_DIR="${ROOT}/.run"
LOGS_DIR="${ROOT}/logs"
mkdir -p "${RUN_DIR}" "${LOGS_DIR}" 2>/dev/null || true

ACTION="${1:-start}"
ACTION="$(printf '%s' "$ACTION" | tr '[:upper:]' '[:lower:]')"
if [[ "$ACTION" == "--help" || "$ACTION" == "-h" || "$ACTION" == "/?" ]]; then
    ACTION="help"
fi
shift 1 || true

# Defaults (match run.ps1)
PORT=5000
NO_BROWSER=0
FORCE_BROWSER=0
TAIL=100
FOLLOW=0
LOG_RETENTION=20
ADMIN_TOKEN=""
SKIP_HEALTH=0
HEALTH_TIMEOUT_SEC=60
HEALTH_GRACE_SEC=45
BACKUP_DRY_RUN=0
BACKUP_TAG="manual"
BACKUP_OUT_DIR=""
UPDATE_INTERVAL_MINUTES=60
UPDATE_BRANCH="main"
UPDATE_NO_RESTART_IF_RUNNING=0
NO_BACKUP_MIGRATE=0

EXTRA_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -Port) PORT="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        -NoBrowser) NO_BROWSER=1; shift ;;
        --no-browser|-n) NO_BROWSER=1; shift ;;
        -ForceBrowser) FORCE_BROWSER=1; shift ;;
        --force-browser|-f) FORCE_BROWSER=1; shift ;;
        -Tail) TAIL="$2"; shift 2 ;;
        --tail) TAIL="$2"; shift 2 ;;
        -Follow) FOLLOW=1; shift ;;
        --follow) FOLLOW=1; shift ;;
        -LogRetention) LOG_RETENTION="$2"; shift 2 ;;
        -AdminToken) ADMIN_TOKEN="$2"; shift 2 ;;
        -SkipHealth) SKIP_HEALTH=1; shift ;;
        --skip-health) SKIP_HEALTH=1; shift ;;
        -HealthTimeoutSec) HEALTH_TIMEOUT_SEC="$2"; shift 2 ;;
        -HealthGraceSec) HEALTH_GRACE_SEC="$2"; shift 2 ;;
        -BackupDryRun) BACKUP_DRY_RUN=1; shift ;;
        -BackupTag) BACKUP_TAG="$2"; shift 2 ;;
        -BackupOutDir) BACKUP_OUT_DIR="$2"; shift 2 ;;
        -UpdateIntervalMinutes) UPDATE_INTERVAL_MINUTES="$2"; shift 2 ;;
        -UpdateBranch) UPDATE_BRANCH="$2"; shift 2 ;;
        -UpdateNoRestartIfRunning) UPDATE_NO_RESTART_IF_RUNNING=1; shift ;;
        -NoBackupMigrate) NO_BACKUP_MIGRATE=1; shift ;;
        *)
            # Keep parity with run.ps1's 'ExtraArgs' behaviour: stash unrecognised args.
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

PID_FILE="${RUN_DIR}/von_${PORT}.pid"
CURRENT_LOG="${LOGS_DIR}/von_${PORT}_current.log"
TS="$(date +%Y%m%d_%H%M%S 2>/dev/null || date +%Y%m%d_%H%M%S)"
NEW_LOG="${LOGS_DIR}/von_${PORT}_${TS}.log"
RAG_PID_FILE="${RUN_DIR}/rag_worker.pid"
RAG_LOG_FILE="${LOGS_DIR}/rag_worker_${TS}.log"
TOKEN_FILE="${RUN_DIR}/admin_token.txt"
SENTINEL_BROWSER="${RUN_DIR}/browser_opened_once"

rotate_logs() {
    # Keep last N logs per port; ignore failures.
    local n="$1"
    if ! printf '%s' "$n" | grep -qE '^[0-9]+$'; then
        return 0
    fi
    if [ "$n" -lt 1 ]; then
        return 0
    fi
    # shellcheck disable=SC2012
    local files
    files=( $(ls -1t "${LOGS_DIR}/von_${PORT}_"*.log 2>/dev/null || true) )
    local count=${#files[@]}
    if [ "$count" -le "$n" ]; then
        return 0
    fi
    local i
    for ((i=n; i<count; i++)); do
        rm -f "${files[$i]}" 2>/dev/null || true
    done
}

export PYTHONPATH="${ROOT}"
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
export VON_SKIP_BROWSER_LAUNCH=1

get_pid() {
    if [ ! -f "$PID_FILE" ]; then
        return 0
    fi
    # PID file format matches run.ps1: first line contains PID=nnn.
    local pid_line
    pid_line="$(head -n 1 "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid_line" =~ ^PID=([0-9]+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
    fi
}

write_pidfile() {
    local pid="$1"
    local start
    start="$(date -Iseconds 2>/dev/null || date)"
    printf 'PID=%s\nPORT=%s\nSTART=%s\n' "$pid" "$PORT" "$start" > "$PID_FILE"
}

remove_pidfile() {
    [ -f "$PID_FILE" ] && rm -f "$PID_FILE" || true
}

read_admin_token() {
    if [ -f "$TOKEN_FILE" ]; then
        tr -d '\r\n' < "$TOKEN_FILE" 2>/dev/null || true
        return 0
    fi
    local tok=""
    if command -v uuidgen >/dev/null 2>&1; then
        tok="$(uuidgen | tr -d '-')"
    elif command -v python3 >/dev/null 2>&1; then
        tok="$(python3 - <<'PY'
import uuid
print(uuid.uuid4().hex)
PY
)"
    else
        tok="$(dd if=/dev/urandom bs=16 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')"
    fi
    printf '%s\n' "$tok" > "$TOKEN_FILE" 2>/dev/null || true
    chmod 600 "$TOKEN_FILE" 2>/dev/null || true
    echo "$tok"
}

set_admin_token_env() {
    # Mirrors run.ps1: allow supplying a stable admin token via -AdminToken.
    local tok=""
    if [ -n "$ADMIN_TOKEN" ]; then
        tok="$ADMIN_TOKEN"
        printf '%s\n' "$tok" > "$TOKEN_FILE" 2>/dev/null || true
        chmod 600 "$TOKEN_FILE" 2>/dev/null || true
    else
        tok="$(read_admin_token)"
    fi
    export VON_ADMIN_TOKEN="$tok"
}

pdm_cmd() {
    if [ -x "${ROOT}/.venv/bin/pdm" ]; then
        echo "${ROOT}/.venv/bin/pdm"
    elif [ -x "${ROOT}/.venv/Scripts/pdm.exe" ]; then
        echo "${ROOT}/.venv/Scripts/pdm.exe"
    else
        echo "pdm"
    fi
}

python_cmd() {
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    elif command -v python >/dev/null 2>&1; then
        echo "python"
    else
        echo ""
    fi
}

now_iso() {
    date -Iseconds 2>/dev/null || date
}

read_failure_count() {
    local meta="$1"
    if [ ! -f "$meta" ]; then
        echo 0
        return 0
    fi
    local count
    count="$(grep -Eo '"failure_count"[[:space:]]*:[[:space:]]*[0-9]+' "$meta" 2>/dev/null | head -n 1 | grep -Eo '[0-9]+' || true)"
    if [ -z "$count" ]; then
        count=0
    fi
    echo "$count"
}

write_failure_count() {
    local meta="$1"
    local count="$2"
    printf '{"failure_count": %s}\n' "$count" > "$meta" 2>/dev/null || true
}

parse_iso_epoch() {
    local iso="$1"
    local epoch=""
    epoch="$(date -d "$iso" +%s 2>/dev/null || true)"
    if [ -z "$epoch" ]; then
        epoch="$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "$iso" +%s 2>/dev/null || true)"
    fi
    echo "$epoch"
}

should_run_interval() {
    local sentinel="$1"
    local interval_hours="$2"
    local force="$3"
    if [ "$force" -eq 1 ]; then
        return 0
    fi
    if [ ! -f "$sentinel" ]; then
        return 0
    fi
    local last
    last="$(cat "$sentinel" 2>/dev/null || true)"
    if [ -z "$last" ]; then
        return 0
    fi
    local last_epoch
    last_epoch="$(parse_iso_epoch "$last")"
    if [ -z "$last_epoch" ]; then
        return 0
    fi
    local now_epoch
    now_epoch="$(date +%s)"
    local elapsed_hours=$(( (now_epoch - last_epoch) / 3600 ))
    if [ "$elapsed_hours" -lt "$interval_hours" ]; then
        return 1
    fi
    return 0
}

health_ok() {
    local url="http://localhost:${PORT}/health"
    local timeout=5
    if printf '%s' "${VON_HEALTH_HTTP_TIMEOUT:-}" | grep -qE '^[0-9]+$'; then
        if [ "$VON_HEALTH_HTTP_TIMEOUT" -gt 0 ] && [ "$VON_HEALTH_HTTP_TIMEOUT" -lt 61 ]; then
            timeout="$VON_HEALTH_HTTP_TIMEOUT"
        fi
    fi
    if command -v curl >/dev/null 2>&1; then
        curl -sS --max-time "$timeout" --fail "$url" >/dev/null 2>&1
        return $?
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -q -T "$timeout" -O /dev/null "$url" >/dev/null 2>&1
        return $?
    fi
    return 1
}

open_browser() {
    local url="http://localhost:${PORT}/"
    # Prefer Chrome if available.
    if command -v google-chrome >/dev/null 2>&1; then
        google-chrome "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if command -v chromium >/dev/null 2>&1; then
        chromium "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if command -v chromium-browser >/dev/null 2>&1; then
        chromium-browser "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if command -v open >/dev/null 2>&1; then
        open -a "Google Chrome" "$url" >/dev/null 2>&1 || open "$url" >/dev/null 2>&1 || true
        return 0
    fi
    log "Browser open skipped (no opener found). URL: $url"
}

get_listening_pid_by_port() {
    # Best-effort listener PID detection across Linux/macOS.
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1
        return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        # Linux (iproute2)
        ss -lptn "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1
        return 0
    fi
    if command -v netstat >/dev/null 2>&1; then
        # Fallback: netstat output parsing is messy; keep minimal.
        netstat -anp 2>/dev/null | grep -E "[:\.]$port\s" | grep LISTEN 2>/dev/null | sed -n 's#.*/\([0-9][0-9]*\)$#\1#p' | head -n 1
        return 0
    fi
    return 0
}

start_rag_worker_bg() {
    # Mirrors run.ps1 best-effort background worker.
    if [ -f "$RAG_PID_FILE" ]; then
        local old
        old="$(sed -n 's/^PID=//p' "$RAG_PID_FILE" 2>/dev/null | head -n 1 || true)"
        if [ -n "$old" ] && kill -0 "$old" >/dev/null 2>&1; then
            log "RAG Worker already running (PID=$old)."
            return 0
        fi
        rm -f "$RAG_PID_FILE" 2>/dev/null || true
    fi
    log "Starting RAG Indexing Worker..."
    local pdm
    pdm="$(pdm_cmd)"
    nohup "$pdm" run python -u "${ROOT}/src/backend/utilities/rag_indexing_worker.py" >> "$RAG_LOG_FILE" 2>&1 &
    local pid=$!
    printf 'PID=%s\nSTART=%s\n' "$pid" "$(date -Iseconds 2>/dev/null || date)" > "$RAG_PID_FILE"
    log "RAG Worker started (PID=$pid). Log: $RAG_LOG_FILE"
}

stop_rag_worker() {
    if [ ! -f "$RAG_PID_FILE" ]; then
        return 0
    fi
    local pid
    pid="$(sed -n 's/^PID=//p' "$RAG_PID_FILE" 2>/dev/null | head -n 1 || true)"
    if [ -n "$pid" ]; then
        log "Stopping RAG Worker (PID=$pid)..."
        kill "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$RAG_PID_FILE" 2>/dev/null || true
}

start_server() {
    # Match run.ps1: default to production DB unless explicitly set.
    if [ -z "${VON_DB_NAME:-}" ] || [ "${VON_DB_NAME:-}" = "test_von_db" ]; then
        export VON_DB_NAME=von_db
    fi
    if [ "${VON_DB_NAME:-}" = "test_von_db" ]; then
        echo "ERROR: Cannot start server with test database (VON_DB_NAME=test_von_db)." >&2
        echo "To fix: export VON_DB_NAME='von_db'" >&2
        return 1
    fi

    local existing
    existing="$(get_pid || true)"
    if [ -n "$existing" ] && kill -0 "$existing" >/dev/null 2>&1; then
        log "Already running (PID=$existing). Use ./run.sh stop or restart."
        return 0
    fi

    # If PID file stale but port has a listener, assume running and sync PID file.
    local listener
    listener="$(get_listening_pid_by_port "$PORT" || true)"
    if [ -n "$listener" ]; then
        log "Port $PORT already in use by PID=$listener; assuming server already running (untracked)."
        write_pidfile "$listener"
        return 0
    fi

    set_admin_token_env

    local pdm
    pdm="$(pdm_cmd)"
    log "Starting Von server on port $PORT ..."
    : > "$NEW_LOG" || true
    nohup "$pdm" run python -u "${ROOT}/src/workflows/von/main.py" --port "$PORT" >> "$NEW_LOG" 2>&1 &
    local pid=$!
    write_pidfile "$pid"

    # Current log pointer (symlink preferred; copy fallback)
    rm -f "$CURRENT_LOG" 2>/dev/null || true
    ln -sf "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || cp -f "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || true

    rotate_logs "$LOG_RETENTION" || true

    if [ "$SKIP_HEALTH" -eq 1 ]; then
        log "Skipping health wait (use ./run.sh status to check)."
    else
        local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SEC ))
        local listening_logged=0
        local healthy=0
        while [ $(date +%s) -lt $deadline ]; do
            if health_ok; then
                healthy=1
                break
            fi
            if [ $listening_logged -eq 0 ]; then
                # We don't have a cheap cross-platform 'listening' check; log once after a brief delay.
                listening_logged=1
                log "Waiting for /health..."
            fi
            sleep 0.5
        done

        if [ $healthy -ne 1 ] && [ "$HEALTH_GRACE_SEC" -gt 0 ]; then
            log "Extending wait up to ${HEALTH_GRACE_SEC}s for /health (phase 2)..."
            local grace_deadline=$(( $(date +%s) + HEALTH_GRACE_SEC ))
            while [ $(date +%s) -lt $grace_deadline ]; do
                if health_ok; then
                    healthy=1
                    break
                fi
                sleep 0.5
            done
        fi

        if [ $healthy -eq 1 ]; then
            log "Server healthy (http://localhost:$PORT)"
            if [ $NO_BROWSER -eq 0 ]; then
                local should_open=0
                if [ $FORCE_BROWSER -eq 1 ]; then
                    should_open=1
                elif [ ! -f "$SENTINEL_BROWSER" ]; then
                    should_open=1
                fi
                if [ $should_open -eq 1 ]; then
                    open_browser
                    date -Iseconds 2>/dev/null > "$SENTINEL_BROWSER" || true
                    if [ $FORCE_BROWSER -eq 1 ]; then
                        log "Opened browser (forced)."
                    else
                        log "Opened browser (first launch)."
                    fi
                else
                    log "Browser already opened previously (use -ForceBrowser to open again)."
                fi
            fi
        else
            log "WARNING: Server not healthy after ${HEALTH_TIMEOUT_SEC}s (+${HEALTH_GRACE_SEC}s grace); check logs: $CURRENT_LOG"
        fi
    fi

    # Start RAG worker best-effort (mirrors run.ps1)
    start_rag_worker_bg || true
    log "Admin token file: $TOKEN_FILE"
}

stop_server() {
    local pid=""
    # Match run.ps1: stop supports extra args like: stop <pid> | stop force | stop force-any
    if [ ${#EXTRA_ARGS[@]} -gt 0 ]; then
        local arg="${EXTRA_ARGS[0]}"
        if printf '%s' "$arg" | grep -qE '^[0-9]+$'; then
            pid="$arg"
            log "Stopping specific PID=$pid (direct)"
        elif [ "$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]')" = "force" ]; then
            pid="$(get_listening_pid_by_port "$PORT" || true)"
            if [ -z "$pid" ]; then
                log "Not running"
                remove_pidfile
                stop_rag_worker || true
                return 0
            fi
            # Verify command line looks like Von (best-effort)
            local cmdline=""
            if command -v ps >/dev/null 2>&1; then
                cmdline="$(ps -p "$pid" -o args= 2>/dev/null || true)"
            fi
            if ! printf '%s' "$cmdline" | grep -q "src/workflows/von/main.py"; then
                log "ABORT: Detected PID $pid command line does not look like Von server. Use 'stop force-any' to override."
                return 0
            fi
            log "Force stop requested: attempting graceful+fallback with command line verification."
        elif [ "$(printf '%s' "$arg" | tr '[:upper:]' '[:lower:]')" = "force-any" ]; then
            pid="$(get_listening_pid_by_port "$PORT" || true)"
            log "Force-any stop requested: bypassing command line verification."
        fi
    fi

    if [ -z "$pid" ]; then
        pid="$(get_pid || true)"
    fi
    if [ -z "$pid" ]; then
        log "Not running"
        remove_pidfile
        stop_rag_worker || true
        return 0
    fi
    if ! kill -0 "$pid" >/dev/null 2>&1; then
        log "STALE: PID file exists but process missing."
        remove_pidfile
        stop_rag_worker || true
        return 0
    fi

    local token=""
    if [ -f "$TOKEN_FILE" ]; then
        token="$(tr -d '\r\n' < "$TOKEN_FILE" 2>/dev/null || true)"
    fi
    if [ -n "$token" ] && command -v curl >/dev/null 2>&1; then
        log "Attempting graceful shutdown (PID=$pid)..."
        curl -sS -X POST "http://localhost:${PORT}/admin/shutdown" -H "X-Admin-Token: ${token}" --max-time 5 >/dev/null 2>&1 || true
    else
        log "No admin token available or curl missing; skipping graceful attempt."
    fi

    local waited=0
    while [ $waited -lt 10 ]; do
        if ! kill -0 "$pid" >/dev/null 2>&1; then
            break
        fi
        sleep 0.5
        waited=$((waited+1))
    done
    if kill -0 "$pid" >/dev/null 2>&1; then
        log "Process PID=$pid still running; issuing force kill..."
        kill -9 "$pid" >/dev/null 2>&1 || true
    else
        log "Process exited."
    fi

    remove_pidfile
    stop_rag_worker || true
}

log_mongo_status() {
    local json=""
    if command -v curl >/dev/null 2>&1; then
        json="$(curl -sS --max-time 5 "http://localhost:${PORT}/api/system/db_status" 2>/dev/null || true)"
    elif command -v wget >/dev/null 2>&1; then
        json="$(wget -q -T 5 -O - "http://localhost:${PORT}/api/system/db_status" 2>/dev/null || true)"
    fi
    if [ -z "$json" ]; then
        log "Mongo: status unavailable"
        return 0
    fi
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        log "Mongo: status unavailable"
        return 0
    fi
    local line=""
    set +e
    line="$(printf '%s' "$json" | "$py" -c 'import json,sys
try:
    data=json.load(sys.stdin)
except Exception:
    sys.exit(1)
using_fallback=data.get("using_fallback")
atlas=data.get("atlas_detected")
host=data.get("effective_host")
if using_fallback:
    label="Mongo: local fallback"
elif atlas:
    label="Mongo: Atlas"
else:
    label="Mongo: "+(host or "unknown")
if host and label.startswith("Mongo: local fallback"):
    label=f"Mongo: local fallback ({host})"
elif host and label.startswith("Mongo: Atlas"):
    label=f"Mongo: Atlas ({host})"
elif host and label.startswith("Mongo: "):
    label=f"Mongo: {host}"
print(label)' 2>/dev/null)"
    local rc=$?
    set -e
    if [ $rc -ne 0 ] || [ -z "$line" ]; then
        log "Mongo: status unavailable"
        return 0
    fi
    log "$line"
}

parse_scan_counts() {
    local output="$1"
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 1
    fi
    local result=""
    set +e
    result="$(printf '%s' "$output" | "$py" -c 'import json,sys,re
text=sys.stdin.read()
m=re.search(r"\\{", text)
if not m:
    sys.exit(1)
data=json.loads(text[m.start():])
sync=data.get("sync_result") or {}
def cnt(val):
    return len(val) if isinstance(val, list) else 0
print(f"{cnt(sync.get(\"updated\"))}|{cnt(sync.get(\"skipped\"))}|{cnt(sync.get(\"missing\"))}|{cnt(sync.get(\"virtual\"))}|{cnt(sync.get(\"warnings\"))}")' 2>/dev/null)"
    local rc=$?
    set -e
    if [ $rc -ne 0 ]; then
        return 1
    fi
    printf '%s' "$result"
}

parse_predicate_counts() {
    local output="$1"
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 1
    fi
    local result=""
    set +e
    result="$(printf '%s' "$output" | "$py" -c 'import json,sys,re
text=sys.stdin.read()
m=re.search(r"\\{", text)
if not m:
    sys.exit(1)
data=json.loads(text[m.start():])
total=int(data.get("total_registry") or 0)
missing=len(data.get("missing") or [])
virtual=len(data.get("virtual") or [])
warnings=len(data.get("warnings") or [])
print(f"{total}|{missing}|{virtual}|{warnings}")' 2>/dev/null)"
    local rc=$?
    set -e
    if [ $rc -ne 0 ]; then
        return 1
    fi
    printf '%s' "$result"
}

parse_relation_alias_counts() {
    local output="$1"
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 1
    fi
    local result=""
    set +e
    result="$(printf '%s' "$output" | "$py" -c 'import json,sys,re
text=sys.stdin.read()
m=re.search(r"\\{", text)
if not m:
    sys.exit(1)
data=json.loads(text[m.start():])
updated=len((data.get("updated") or {}).keys())
warnings=len(data.get("warnings") or [])
print(f"{updated}|{warnings}")' 2>/dev/null)"
    local rc=$?
    set -e
    if [ $rc -ne 0 ]; then
        return 1
    fi
    printf '%s' "$result"
}

run_governance_scan() {
    if [ "${VON_GOV_SCAN_DISABLE:-}" = "1" ]; then
        return 0
    fi
    local interval_hours=24
    if printf '%s' "${VON_GOV_SCAN_INTERVAL_HOURS:-}" | grep -qE '^[0-9]+$'; then
        interval_hours="$VON_GOV_SCAN_INTERVAL_HOURS"
    fi
    local force=0
    if [ "${VON_GOV_SCAN_FORCE:-}" = "1" ]; then
        force=1
    fi
    local sentinel="${RUN_DIR}/last_governance_scan.txt"
    local meta="${RUN_DIR}/governance_scan_meta.json"
    local failure_count
    failure_count="$(read_failure_count "$meta")"
    local backoff="$interval_hours"
    if [ "$failure_count" -gt 0 ]; then
        local schedule=(1 3 6 12 24)
        local idx=$((failure_count - 1))
        if [ "$idx" -ge "${#schedule[@]}" ]; then
            idx=$((${#schedule[@]} - 1))
        fi
        backoff="${schedule[$idx]}"
    fi
    if ! should_run_interval "$sentinel" "$backoff" "$force"; then
        return 0
    fi
    local scan_script="${ROOT}/src/utilities/scan_code_concepts.py"
    if [ ! -f "$scan_script" ]; then
        log "[governance-scan] scanner script missing ($scan_script)"
        return 0
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local include_docstrings=0
    if printf '%s' "${VON_GOV_SCAN_INCLUDE_DOCSTRINGS:-}" | grep -qiE '^(1|true|yes)$'; then
        include_docstrings=1
    fi
    log "[governance-scan] Running governance concept tag scan (interval ${interval_hours}h; force=$force)"
    local output=""
    local exit_code=0
    set +e
    if [ "$include_docstrings" -eq 1 ]; then
        output="$("$pdm" run python "$scan_script" --sync-mentions --apply --include-docstrings 2>&1)"
        exit_code=$?
    else
        output="$("$pdm" run python "$scan_script" --sync-mentions --apply 2>&1)"
        exit_code=$?
    fi
    set -e
    local raw_out="${RUN_DIR}/governance_scan_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    local counts=""
    counts="$(parse_scan_counts "$output" || true)"
    if [ "$exit_code" -eq 0 ] && [ -n "$counts" ]; then
        local updated skipped missing virtual warnings
        IFS='|' read -r updated skipped missing virtual warnings <<<"$counts"
        printf '%s\n' "$(now_iso)" > "$sentinel" 2>/dev/null || true
        failure_count=0
        local status="success"
        if [ "$updated" -eq 0 ] && [ "$missing" -eq 0 ] && [ "$virtual" -eq 0 ] && [ "$warnings" -eq 0 ]; then
            status="no-op"
        fi
        log "[governance-scan] $status updated=$updated skipped=$skipped missing=$missing virtual=$virtual warnings=$warnings failures=$failure_count"
    else
        failure_count=$((failure_count + 1))
        printf '%s\n' "$(now_iso)" > "$sentinel" 2>/dev/null || true
        local hint=""
        hint="$(printf '%s\n' "$output" | grep -E 'Traceback|ERROR|Error|Exception' | head -n 1 || true)"
        if [ -z "$hint" ]; then
            hint="$(printf '%s\n' "$output" | tail -n 5 | tr '\n' '|' | sed 's/|$//' || true)"
        fi
        log "[governance-scan] FAILED failures=$failure_count hint=$hint"
        log "[governance-scan] raw_output=$raw_out"
    fi
    write_failure_count "$meta" "$failure_count"
}

run_predicate_verify() {
    local interval_hours=24
    local force=0
    if [ "${VON_CONCEPT_DATA_ABSENCE_FORCE:-}" = "1" ]; then
        force=1
    fi
    if [ "${VON_CONCEPT_DATA_ABSENCE_DISABLE:-}" = "1" ]; then
        return 0
    fi
    local sentinel="${RUN_DIR}/last_concept_data_absence_check.txt"
    if ! should_run_interval "$sentinel" "$interval_hours" "$force"; then
        return 0
    fi
    local script="${ROOT}/src/utilities/verify_predicate_concepts.py"
    if [ ! -f "$script" ]; then
        log "[predicate-verify] script missing ($script)"
        return 0
    fi
    log "[predicate-verify] Running predicate concept verification (interval ${interval_hours}h force=$force)"
    local pdm
    pdm="$(pdm_cmd)"
    local output=""
    local exit_code=0
    set +e
    output="$("$pdm" run python "$script" 2>&1)"
    exit_code=$?
    set -e
    local raw_out="${RUN_DIR}/predicate_verify_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    local counts=""
    counts="$(parse_predicate_counts "$output" || true)"
    if [ "$exit_code" -eq 0 ] && [ -n "$counts" ]; then
        local total missing virtual warnings
        IFS='|' read -r total missing virtual warnings <<<"$counts"
        printf '%s\n' "$(now_iso)" > "$sentinel" 2>/dev/null || true
        if [ "$missing" -eq 0 ] && [ "$virtual" -eq 0 ]; then
            log "[predicate-verify] OK total=$total missing=$missing virtual=$virtual warnings=$warnings"
        else
            log "[predicate-verify] MISSING total=$total missing=$missing virtual=$virtual warnings=$warnings see $raw_out"
        fi
    else
        log "[predicate-verify] ERROR exit=$exit_code see $raw_out"
    fi
}

run_relation_alias_audit() {
    local interval_hours=24
    local force=0
    if [ "${VON_RESIDUAL_TEXT_AUDIT_FORCE:-}" = "1" ]; then
        force=1
    fi
    if [ "${VON_RESIDUAL_TEXT_AUDIT_DISABLE:-}" = "1" ]; then
        return 0
    fi
    local sentinel="${RUN_DIR}/last_residual_text_audit.txt"
    if ! should_run_interval "$sentinel" "$interval_hours" "$force"; then
        return 0
    fi
    local script="${ROOT}/src/utilities/normalise_relationship_aliases.py"
    if [ ! -f "$script" ]; then
        log "[relation-alias-audit] script missing ($script)"
        return 0
    fi
    log "[relation-alias-audit] Running relationship alias audit (interval ${interval_hours}h force=$force)"
    local pdm
    pdm="$(pdm_cmd)"
    local output=""
    local exit_code=0
    set +e
    output="$("$pdm" run python "$script" --dry-run 2>&1)"
    exit_code=$?
    set -e
    local raw_out="${RUN_DIR}/relation_alias_audit_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    local counts=""
    counts="$(parse_relation_alias_counts "$output" || true)"
    if [ "$exit_code" -eq 0 ] && [ -n "$counts" ]; then
        local updated warnings
        IFS='|' read -r updated warnings <<<"$counts"
        printf '%s\n' "$(now_iso)" > "$sentinel" 2>/dev/null || true
        if [ "$updated" -eq 0 ]; then
            log "[relation-alias-audit] OK updated=$updated warnings=$warnings"
        else
            log "[relation-alias-audit] DETECTED updated=$updated warnings=$warnings see $raw_out"
        fi
    else
        log "[relation-alias-audit] ERROR exit=$exit_code see $raw_out"
    fi
}

run_relation_alias_cleanup() {
    local interval_hours=24
    local force=0
    if [ "${VON_CLEANUP_PRESERVED_FORCE:-}" = "1" ]; then
        force=1
    fi
    if [ "${VON_CLEANUP_PRESERVED_DISABLE:-}" = "1" ]; then
        return 0
    fi
    local execute=0
    if [ "${VON_RELATION_ALIAS_EXECUTE:-}" = "1" ] || [ "${VON_CLEANUP_PRESERVED_EXECUTE:-}" = "1" ]; then
        execute=1
    fi
    local sentinel="${RUN_DIR}/last_cleanup_preserved_fields.txt"
    if ! should_run_interval "$sentinel" "$interval_hours" "$force"; then
        return 0
    fi
    local script="${ROOT}/src/utilities/normalise_relationship_aliases.py"
    if [ ! -f "$script" ]; then
        log "[relation-alias-cleanup] script missing ($script)"
        return 0
    fi
    local mode="dry-run"
    if [ "$execute" -eq 1 ]; then
        mode="apply"
    fi
    log "[relation-alias-cleanup] Running normalisation (mode=$mode interval ${interval_hours}h force=$force)"
    local pdm
    pdm="$(pdm_cmd)"
    local output=""
    local exit_code=0
    set +e
    if [ "$execute" -eq 1 ]; then
        output="$("$pdm" run python "$script" 2>&1)"
        exit_code=$?
    else
        output="$("$pdm" run python "$script" --dry-run 2>&1)"
        exit_code=$?
    fi
    set -e
    local raw_out="${RUN_DIR}/relation_alias_cleanup_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    local counts=""
    counts="$(parse_relation_alias_counts "$output" || true)"
    if [ "$exit_code" -eq 0 ] && [ -n "$counts" ]; then
        local updated warnings
        IFS='|' read -r updated warnings <<<"$counts"
        printf '%s\n' "$(now_iso)" > "$sentinel" 2>/dev/null || true
        if [ "$execute" -eq 1 ]; then
            log "[relation-alias-cleanup] OK applied updated=$updated warnings=$warnings"
        elif [ "$updated" -eq 0 ]; then
            log "[relation-alias-cleanup] OK updated=$updated warnings=$warnings"
        else
            log "[relation-alias-cleanup] DETECTED updated=$updated warnings=$warnings enable VON_RELATION_ALIAS_EXECUTE=1 to apply. See $raw_out"
        fi
    else
        log "[relation-alias-cleanup] ERROR exit=$exit_code see $raw_out"
    fi
}

status_server() {
    # Match run.ps1: if PID file stale, try to sync from current listener.
    if [ -f "$PID_FILE" ]; then
        local pid_in_file
        pid_in_file="$(get_pid || true)"
        if [ -n "$pid_in_file" ] && ! kill -0 "$pid_in_file" >/dev/null 2>&1; then
            local listener
            listener="$(get_listening_pid_by_port "$PORT" || true)"
            if [ -n "$listener" ]; then
                write_pidfile "$listener"
            fi
        fi
    fi
    local pid
    pid="$(get_pid || true)"
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
        if health_ok; then
            log "RUNNING PID=$pid Healthy=true"
        else
            log "RUNNING PID=$pid Healthy=false"
        fi
        log_mongo_status
        log "Log: $CURRENT_LOG"
        run_governance_scan || true
        run_predicate_verify || true
        run_relation_alias_audit || true
        run_relation_alias_cleanup || true
    else
        if [ -f "$PID_FILE" ]; then
            log "STALE: PID file exists but process missing."
        else
            log "STOPPED"
        fi
    fi
}

show_logs() {
    if [ ! -f "$CURRENT_LOG" ]; then
        log "No log file yet."
        return 0
    fi
    if [ "$FOLLOW" -eq 1 ]; then
        tail -n "$TAIL" -f "$CURRENT_LOG"
    else
        tail -n "$TAIL" "$CURRENT_LOG"
    fi
}

check_health() {
    local pid="$(get_pid || true)"
    if [ -z "$pid" ] || ! kill -0 "$pid" >/dev/null 2>&1; then
        # Mirror run.ps1: if PID missing but port has a listener, treat as running (untracked)
        local listener
        listener="$(get_listening_pid_by_port "$PORT" || true)"
        if [ -n "$listener" ]; then
            if health_ok; then
                log "HEALTHY (listener PID=$listener no PID file)"
                exit 0
            fi
            log "UNHEALTHY (listener PID=$listener no PID file)"
            exit 2
        fi
        log "NOT RUNNING"
        exit 3
    fi
    if health_ok; then
        log "HEALTHY PID=$pid"
        exit 0
    fi
    log "UNHEALTHY PID=$pid"
    exit 2
}

run_rag_worker_foreground() {
    log "Starting RAG Indexing Worker..."
    local pdm
    pdm="$(pdm_cmd)"
    "$pdm" run python -u "${ROOT}/src/backend/utilities/rag_indexing_worker.py"
}

run_backup() {
    local backup_script="${ROOT}/scripts/backup_von_db.py"
    if [ ! -f "$backup_script" ]; then
        log "[backup] ERROR: backup script missing: $backup_script"
        return 1
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local out_dir="$BACKUP_OUT_DIR"
    if [ -z "$out_dir" ]; then
        out_dir="${VON_BACKUP_ROOT:-}"
    fi
    if [ -z "$out_dir" ]; then
        # Platform-friendly analogue of run.ps1's W: preference.
        if [ -d "/Volumes/von_backups" ] && [ -w "/Volumes/von_backups" ]; then
            out_dir="/Volumes/von_backups"
        elif [ -d "/mnt/von_backups" ] && [ -w "/mnt/von_backups" ]; then
            out_dir="/mnt/von_backups"
        else
            out_dir="${ROOT}/backups"
        fi
    fi
    mkdir -p "$out_dir" 2>/dev/null || true
    local mode="apply"
    if [ "$BACKUP_DRY_RUN" -eq 1 ]; then
        mode="dry-run"
    fi
    log "[backup] Starting backup (mode=$mode tag=$BACKUP_TAG out=$out_dir)"
    if [ "$BACKUP_DRY_RUN" -eq 1 ]; then
        "$pdm" run python "$backup_script" --out-dir "$out_dir" --tag "$BACKUP_TAG"
    else
        "$pdm" run python "$backup_script" --apply --out-dir "$out_dir" --tag "$BACKUP_TAG"
    fi
    local backup_exit=$?
    if [ "$backup_exit" -eq 0 ]; then
        if [ -z "${VON_DISABLE_CODE_MENTION_SCAN:-}" ] || ! printf '%s' "$VON_DISABLE_CODE_MENTION_SCAN" | grep -qiE '^(1|true|yes)$'; then
            local scan_script="${ROOT}/src/utilities/scan_code_concepts.py"
            if [ -f "$scan_script" ]; then
                log "[code-mention-scan] Running scan (reason=manual-backup)"
                "$pdm" run python "$scan_script" --sync-mentions --apply
                local scan_exit=$?
                if [ "$scan_exit" -eq 0 ]; then
                    log "[code-mention-scan] OK"
                else
                    log "[code-mention-scan] ERROR exit=$scan_exit"
                fi
            else
                log "[code-mention-scan] WARN: script missing ($scan_script)"
            fi
        else
            log "[code-mention-scan] disabled via VON_DISABLE_CODE_MENTION_SCAN"
        fi
        if [ -z "${VON_DISABLE_CODE_PREDICATE_SYNC:-}" ] || ! printf '%s' "$VON_DISABLE_CODE_PREDICATE_SYNC" | grep -qiE '^(1|true|yes)$'; then
            local sync_script="${ROOT}/src/utilities/sync_code_predicates.py"
            if [ -f "$sync_script" ]; then
                log "[code-predicate-sync] Running sync (reason=manual-backup)"
                "$pdm" run python "$sync_script" --retag-non-predicates
                local sync_exit=$?
                if [ "$sync_exit" -eq 0 ]; then
                    log "[code-predicate-sync] OK"
                else
                    log "[code-predicate-sync] ERROR exit=$sync_exit"
                fi
            else
                log "[code-predicate-sync] WARN: script missing ($sync_script)"
            fi
        else
            log "[code-predicate-sync] disabled via VON_DISABLE_CODE_PREDICATE_SYNC"
        fi
    fi
    return "$backup_exit"
}

run_autoupdate() {
    log "Starting auto-update loop (branch=$UPDATE_BRANCH interval=${UPDATE_INTERVAL_MINUTES}m)... Press Ctrl+C to stop."
    if ! command -v git >/dev/null 2>&1; then
        log "ERROR: git not found on PATH."
        return 1
    fi
    while true; do
        (cd "$ROOT" || exit 1
            local running_pid
            running_pid="$(get_pid || true)"
            if [ -z "$running_pid" ] || ! kill -0 "$running_pid" >/dev/null 2>&1; then
                start_server || true
            fi

            git fetch origin "$UPDATE_BRANCH" >/dev/null 2>&1 || true
            local local_sha
            local remote_sha
            local base_sha
            local_sha="$(git rev-parse HEAD 2>/dev/null || true)"
            remote_sha="$(git rev-parse "origin/${UPDATE_BRANCH}" 2>/dev/null || true)"
            if [ -n "$local_sha" ] && [ -n "$remote_sha" ] && [ "$local_sha" != "$remote_sha" ]; then
                base_sha="$(git merge-base HEAD "origin/${UPDATE_BRANCH}" 2>/dev/null || true)"
                if [ "$base_sha" = "$local_sha" ]; then
                    if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
                        log "Update available ($local_sha -> $remote_sha) but local uncommitted changes present; skipping pull."
                    else
                        log "Applying update ($local_sha -> $remote_sha)..."
                        if [ "$UPDATE_NO_RESTART_IF_RUNNING" -eq 0 ]; then
                            stop_server || true
                        fi
                        git pull --ff-only origin "$UPDATE_BRANCH" 2>&1 | while IFS= read -r line; do log "$line"; done
                        if [ "$UPDATE_NO_RESTART_IF_RUNNING" -eq 0 ]; then
                            log "Restarting server after update..."
                            start_server || true
                        else
                            log "Pulled updates without restart (UpdateNoRestartIfRunning set)."
                        fi
                    fi
                else
                    log "Local branch diverged from remote (local=$local_sha remote=$remote_sha base=$base_sha); manual merge required."
                fi
            else
                if [ -n "$local_sha" ]; then
                    log "No updates (HEAD=$local_sha)."
                else
                    log "No updates (unable to resolve HEAD)."
                fi
            fi
        )

        local m
        m=0
        while [ $m -lt "$UPDATE_INTERVAL_MINUTES" ]; do
            sleep 60
            m=$((m+1))
        done
    done
}

show_help() {
    cat <<'TXT'
Von Launcher Help
    Usage: ./run.sh [action] [options]
    Actions: start | foreground | stop | status | restart | logs | check | backup | autoupdate | rag-worker | help
    Options:
        -Port <int>
        -NoBrowser
        -ForceBrowser
        -Tail <n>
        -Follow
        -LogRetention <n>
        -AdminToken <token>
        -SkipHealth
        -HealthTimeoutSec <n>
        -HealthGraceSec <n>

    Backup options:
        -BackupDryRun
        -BackupTag <tag>
        -BackupOutDir <path>

    Autoupdate options:
        -UpdateIntervalMinutes <n>
        -UpdateBranch <name>
        -UpdateNoRestartIfRunning

    Examples:
        ./run.sh start
        ./run.sh restart -ForceBrowser
        ./run.sh logs -Tail 200 -Follow
        ./run.sh backup -BackupDryRun
        ./run.sh autoupdate -UpdateIntervalMinutes 30 -UpdateBranch main
TXT
}

case "$ACTION" in
    start)
        start_server
        ;;
    foreground)
        log "Running in foreground... (Ctrl+C to stop)"
        set_admin_token_env
        # Foreground still suppresses auto browser (launcher opens if requested)
        export VON_SKIP_BROWSER_LAUNCH=1
        "$(pdm_cmd)" run python -u "${ROOT}/src/workflows/von/main.py" --port "$PORT"
        ;;
    stop)
        stop_server
        ;;
    status)
        status_server
        ;;
    restart)
        stop_server
        start_server
        ;;
    logs)
        show_logs
        ;;
    check)
        check_health
        ;;
    backup)
        run_backup
        ;;
    autoupdate)
        run_autoupdate
        ;;
    rag-worker)
        run_rag_worker_foreground
        ;;
    help)
        show_help
        ;;
    *)
        log "Unknown action '$ACTION'"
        show_help
        exit 2
        ;;
esac
