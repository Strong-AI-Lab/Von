#!/bin/bash

SCRIPT_DIR_ABS="$(cd "$(dirname "$0")" && pwd)" # Absolute path of the script's directory

echo "[Debug run.sh] Script started."

# Set PYTHONPATH to the script's directory (project root)
export PYTHONPATH="$SCRIPT_DIR_ABS"
echo "[Debug run.sh] PYTHONPATH set to: $PYTHONPATH"
# Portable helpers: convert ISO timestamp to epoch seconds and get epoch ms.
# Tries GNU date (date -d), then python3, then gdate (coreutils). Falls back
# to a best-effort seconds timestamp when millisecond precision isn't available.
iso_to_epoch() {
    iso="$1"
    # Try GNU date first (Linux)
    if epoch=$(date -d "$iso" +%s 2>/dev/null); then
        echo "$epoch"
        return 0
    fi
    # Try python3
    if command -v python3 >/dev/null 2>&1; then
        epoch=$(python3 - <<PY
import sys,datetime
s=sys.argv[1]
try:
    if s.endswith('Z'):
        s=s[:-1]+'+00:00'
    dt=datetime.datetime.fromisoformat(s)
    print(int(dt.timestamp()))
except Exception:
    sys.exit(2)
PY
"$iso" 2>/dev/null)
        if [ $? -eq 0 ] && [ -n "$epoch" ]; then
            echo "$epoch"
            return 0
        fi
    fi
    # Try gdate (GNU coreutils on macOS via brew)
    if command -v gdate >/dev/null 2>&1; then
        if epoch=$(gdate -d "$iso" +%s 2>/dev/null); then
            echo "$epoch"
            return 0
        fi
    fi
    return 1
}

epoch_ms() {
    # Try GNU date with milliseconds and ensure it's numeric (Linux). On
    # macOS the format may produce non-digit tokens; validate before using.
    if val=$(date +%s%3N 2>/dev/null); then
        if printf '%s' "$val" | grep -qE '^[0-9]+$'; then
            echo "$val"
            return 0
        fi
    fi
    # Python fallback (reliable across platforms)
    if command -v python3 >/dev/null 2>&1; then
        ms=$(python3 - <<'PY'
import time
print(int(time.time()*1000))
PY
)
        if printf '%s' "$ms" | grep -qE '^[0-9]+$'; then
            echo "$ms"
            return 0
        fi
    fi
    # Last-resort: seconds * 1000
    echo "$(( $(date +%s) * 1000 ))"
}

# Governance daily scan function
run_governance_scan() {
    if [ "${VON_GOV_SCAN_DISABLE}" = "1" ]; then
        return 0
    fi
    local interval_hours=${VON_GOV_SCAN_INTERVAL_HOURS:-24}
    local force=${VON_GOV_SCAN_FORCE:-0}
    local sentinel_dir="${SCRIPT_DIR_ABS}/.run"
    local sentinel_file="${sentinel_dir}/last_governance_scan.txt"
    mkdir -p "${sentinel_dir}" 2>/dev/null || true
    local now_epoch
    now_epoch=$(date +%s)
    local should_run=1
    if [ "$force" != "1" ] && [ -f "$sentinel_file" ]; then
        last_iso=$(cat "$sentinel_file" 2>/dev/null || true)
        if last_epoch=$(iso_to_epoch "$last_iso" 2>/dev/null); then
            elapsed=$(( (now_epoch - last_epoch) / 3600 ))
            if [ "$elapsed" -lt "$interval_hours" ]; then
                should_run=0
            fi
        fi
    fi
    if [ "$should_run" -eq 0 ] && [ "$force" != "1" ]; then
        return 0
    fi
    echo "[governance-scan] Running governance concept tag scan (interval ${interval_hours}h; force=${force})"
    local allow_local_flag=""
    if [ "${VON_GOV_SCAN_ALLOW_LOCAL}" = "1" ]; then
        allow_local_flag="--allow-local"
    fi
    local start_epoch
    start_epoch=$(epoch_ms)
    # Run quietly (no progress) to keep logs clean
    if output=$(pdm run python scripts/mark_code_referenced_concepts.py --execute ${allow_local_flag} 2>&1); then
        # Attempt to parse JSON tail - removed unused 'added' variable
        # Fallback simple parse by counting occurrences inside JSON arrays is imprecise; we keep summary generic
        echo "$now_epoch" > "$sentinel_file" 2>/dev/null || true
        end_epoch=$(epoch_ms)
        elapsed_ms=$(( end_epoch - start_epoch ))
        echo "[governance-scan] success elapsed=${elapsed_ms}ms"
    else
        end_epoch=$(epoch_ms)
        elapsed_ms=$(( end_epoch - start_epoch ))
        echo "[governance-scan] FAILED elapsed=${elapsed_ms}ms"
        if [ "${VON_GOV_SCAN_DEBUG}" = "1" ]; then
            echo "$output" | sed 's/^/[governance-scan][debug] /'
        fi
    fi
}

# Create run dir and log layout
RUN_DIR="$SCRIPT_DIR_ABS/.run"
LOGS_DIR="$SCRIPT_DIR_ABS/logs"
mkdir -p "$RUN_DIR" "$LOGS_DIR" 2>/dev/null || true
PID_FILE="$RUN_DIR/von.pid"
CURRENT_LOG="$LOGS_DIR/von_current.log"
TIMESTAMP=$(date +%Y%m%d_%H%M%S 2>/dev/null || date +%Y%m%d_%H%M%S)
NEW_LOG="$LOGS_DIR/von_${TIMESTAMP}.log"

# Defaults
ACTION=${1:-start}
shift 1 || true
PORT=${PORT:-5000}
NO_BROWSER=0
FORCE_BROWSER=0

while [ $# -gt 0 ]; do
    case "$1" in
        --no-browser|-n) NO_BROWSER=1; shift ;;
        --force-browser|-f) FORCE_BROWSER=1; shift ;;
        --port) PORT="$2"; shift 2 ;;
        --help|-h) echo "Usage: $0 [start|foreground|stop|status|restart|logs|check] [--no-browser|--force-browser] [--port N]"; exit 0 ;;
        *) echo "Unknown option: $1"; shift ;;
    esac
done

write_pid() { echo "$1" > "$PID_FILE"; }
remove_pid() { [ -f "$PID_FILE" ] && rm -f "$PID_FILE"; }
get_pid() { [ -f "$PID_FILE" ] && cat "$PID_FILE" || echo ""; }

# Read or generate an admin token persisted in $RUN_DIR/admin_token.txt
read_admin_token() {
    token_file="$RUN_DIR/admin_token.txt"
    if [ -f "$token_file" ]; then
        cat "$token_file" 2>/dev/null || echo ""
        return 0
    fi
    # Try uuidgen, else python3 fallback
    if command -v uuidgen >/dev/null 2>&1; then
        tok=$(uuidgen | tr -d '-')
    elif command -v python3 >/dev/null 2>&1; then
        tok=$(python3 - <<'PY'
import uuid
print(uuid.uuid4().hex)
PY
)
    else
        # Fallback to random hex from /dev/urandom
        tok=$(dd if=/dev/urandom bs=16 count=1 2>/dev/null | od -An -tx1 | tr -d ' \n')
    fi
    # Persist token (best-effort)
    echo "$tok" > "$token_file" 2>/dev/null || true
    chmod 600 "$token_file" 2>/dev/null || true
    echo "$tok"
}

start_bg() {
    # Safety check: prevent running server with test database
    if [ "$VON_DB_NAME" = "test_von_db" ]; then
        echo "ERROR: Cannot start server with test database (VON_DB_NAME=test_von_db)." >&2
        echo "This prevents accidental data corruption from running production server against test data." >&2
        echo "To fix: export VON_DB_NAME='von_db'" >&2
        return 1
    fi

    if [ -n "$(get_pid)" ] && ps -p "$(get_pid)" > /dev/null 2>&1; then
        echo "Already running (PID=$(get_pid))."; return 0
    fi
    # Run governance scan first (best-effort)
    run_governance_scan || true
    echo "Starting Von application in background (port $PORT)..."
    if [ "$NO_BROWSER" -eq 1 ]; then
        export VON_SKIP_BROWSER_LAUNCH=1
    else
        export VON_SKIP_BROWSER_LAUNCH=0
    fi
    if [ "$FORCE_BROWSER" -eq 1 ]; then
        export VON_FORCE_BROWSER=1
    else
        export VON_FORCE_BROWSER=0
    fi
    # Ensure admin token is present for graceful shutdown
    ADM_TOKEN=$(read_admin_token)
    export VON_ADMIN_TOKEN="$ADM_TOKEN"
    PDM_CMD="pdm"
    if [ -x "$SCRIPT_DIR_ABS/.venv/bin/pdm" ]; then
        PDM_CMD="$SCRIPT_DIR_ABS/.venv/bin/pdm"
    fi
    # Start in background via nohup so it detaches; write pid
    nohup $PDM_CMD run python -u "$SCRIPT_DIR_ABS/src/workflows/von/main.py" --port "$PORT" > "$NEW_LOG" 2>&1 &
    child=$!
    # Give short time for process to start
    sleep 0.4
    if ps -p $child > /dev/null 2>&1; then
        write_pid $child
        # Update current log pointer
        ln -f "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || cp -f "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || true
        echo "Started (PID=$child). Log: $NEW_LOG"
        # If force-browser requested, wait for /health then open browser
        if [ "$FORCE_BROWSER" -eq 1 ]; then
            url="http://localhost:$PORT/"
            echo "Force browser requested: waiting for $url to become available..."
            # prefer curl, fallback to wget, else skip
            checker=""
            if command -v curl >/dev/null 2>&1; then
                checker="curl -sS --max-time 1 --fail"
            elif command -v wget >/dev/null 2>&1; then
                checker="wget -q -T 1 -O -"
            fi
            attempts=0
            while [ $attempts -lt 60 ]; do
                if [ -n "$checker" ]; then
                    if $checker "$url" >/dev/null 2>&1; then
                        echo "Service ready; opening browser: $url"
                        # Try Chrome-specific commands first
                        if command -v google-chrome >/dev/null 2>&1; then
                            google-chrome "$url" || true
                        elif command -v chromium >/dev/null 2>&1; then
                            chromium "$url" || true
                        elif command -v chromium-browser >/dev/null 2>&1; then
                            chromium-browser "$url" || true
                        elif command -v open >/dev/null 2>&1; then
                            # macOS: try to open with Chrome explicitly
                            open -a "Google Chrome" "$url" 2>/dev/null || open "$url" || true
                        elif [ -f "/c/Program Files/Google/Chrome/Application/chrome.exe" ]; then
                            # Windows Git Bash
                            "/c/Program Files/Google/Chrome/Application/chrome.exe" "$url" || true
                        elif command -v xdg-open >/dev/null 2>&1; then
                            xdg-open "$url" || true
                        fi
                        break
                    fi
                fi
                attempts=$((attempts+1))
                sleep 0.5
            done
            if [ $attempts -ge 60 ]; then
                echo "Warning: service did not become ready within timeout; browser not opened."
            fi
        fi
    else
        echo "Failed to start process; see $NEW_LOG"
    fi
}

start_foreground() {
    # Safety check: prevent running server with test database
    if [ "$VON_DB_NAME" = "test_von_db" ]; then
        echo "ERROR: Cannot start server with test database (VON_DB_NAME=test_von_db)." >&2
        echo "This prevents accidental data corruption from running production server against test data." >&2
        echo "To fix: export VON_DB_NAME='von_db'" >&2
        return 1
    fi

    run_governance_scan || true
    echo "Running in foreground (port $PORT)... Ctrl+C to stop"
    if [ "$NO_BROWSER" -eq 1 ]; then
        export VON_SKIP_BROWSER_LAUNCH=1
    else
        export VON_SKIP_BROWSER_LAUNCH=0
    fi
    if [ "$FORCE_BROWSER" -eq 1 ]; then
        export VON_FORCE_BROWSER=1
    else
        export VON_FORCE_BROWSER=0
    fi
    ADM_TOKEN=$(read_admin_token)
    export VON_ADMIN_TOKEN="$ADM_TOKEN"
    PDM_CMD="pdm"
    if [ -x "$SCRIPT_DIR_ABS/.venv/bin/pdm" ]; then
        PDM_CMD="$SCRIPT_DIR_ABS/.venv/bin/pdm"
    fi
    # Start the server as a child process, write logs to NEW_LOG, and tail it to the console
    "$PDM_CMD" run python -u "$SCRIPT_DIR_ABS/src/workflows/von/main.py" --port "$PORT" > "$NEW_LOG" 2>&1 &
    child=$!
    write_pid $child
    # If force-browser requested, wait for localhost readiness then open
    if [ "$FORCE_BROWSER" -eq 1 ]; then
        url="http://localhost:$PORT/"
        echo "Force browser requested: waiting for $url to become available..."
        checker=""
        if command -v curl >/dev/null 2>&1; then
            checker="curl -sS --max-time 1 --fail"
        elif command -v wget >/dev/null 2>&1; then
            checker="wget -q -T 1 -O -"
        fi
        attempts=0
        while [ $attempts -lt 60 ]; do
            if [ -n "$checker" ]; then
                if $checker "$url" >/dev/null 2>&1; then
                    echo "Service ready; opening browser: $url"
                    # Try Chrome-specific commands first
                    if command -v google-chrome >/dev/null 2>&1; then
                        google-chrome "$url" || true
                    elif command -v chromium >/dev/null 2>&1; then
                        chromium "$url" || true
                    elif command -v chromium-browser >/dev/null 2>&1; then
                        chromium-browser "$url" || true
                    elif command -v open >/dev/null 2>&1; then
                        # macOS: try to open with Chrome explicitly
                        open -a "Google Chrome" "$url" 2>/dev/null || open "$url" || true
                    elif [ -f "/c/Program Files/Google/Chrome/Application/chrome.exe" ]; then
                        # Windows Git Bash
                        "/c/Program Files/Google/Chrome/Application/chrome.exe" "$url" || true
                    elif command -v xdg-open >/dev/null 2>&1; then
                        xdg-open "$url" || true
                    fi
                    break
                fi
            fi
            attempts=$((attempts+1))
            sleep 0.5
        done
        if [ $attempts -ge 60 ]; then
            echo "Warning: service did not become ready within timeout; browser not opened."
        fi
    fi

    # Tail the log and forward signals so Ctrl+C stops the child
    ln -f "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || cp -f "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || true
    trap 'echo "Stopping child..."; kill -TERM $child 2>/dev/null || true; wait $child; exit' INT TERM
    tail -n +1 -f "$CURRENT_LOG" &
    tailpid=$!
    # Wait for child to exit
    wait $child
    # Cleanup tail
    kill $tailpid 2>/dev/null || true
    remove_pid
}

stop_server() {
    pid=$(get_pid)
    if [ -z "$pid" ]; then
        echo "Not running (no PID file)."
        return 0
    fi
    if ! ps -p "$pid" > /dev/null 2>&1; then
        echo "Stale PID file found; removing."; remove_pid; return 0
    fi
    echo "Stopping PID=$pid..."
    # Attempt graceful shutdown via admin endpoint
    token_file="$RUN_DIR/admin_token.txt"
    if [ -f "$token_file" ]; then
        token=$(cat "$token_file" 2>/dev/null || echo "")
        if [ -n "$token" ]; then
            echo "Attempting graceful shutdown via /admin/shutdown..."
            if command -v curl >/dev/null 2>&1; then
                curl -sS -X POST "http://localhost:$PORT/admin/shutdown" -H "X-Admin-Token: $token" --max-time 5 >/dev/null 2>&1 || true
            fi
            # Give it a short grace period to exit
            sleep 2
            if ! ps -p "$pid" > /dev/null 2>&1; then
                echo "Graceful shutdown succeeded."; remove_pid; return 0
            fi
            echo "Graceful attempt did not stop process; escalating to kill.";
        fi
    fi
    kill "$pid" 2>/dev/null || true
    sleep 1
    if ps -p "$pid" > /dev/null 2>&1; then
        echo "Process still running; force killing..."
        kill -9 "$pid" 2>/dev/null || true
    fi
    remove_pid
    echo "Stopped."
}

status_server() {
    pid=$(get_pid)
    if [ -n "$pid" ] && ps -p "$pid" > /dev/null 2>&1; then
        echo "RUNNING PID=$pid"
    else
        echo "STOPPED"
    fi
}

show_logs() {
    if [ -f "$CURRENT_LOG" ]; then
        tail -n 200 -f "$CURRENT_LOG"
    else
        echo "No log file yet: $CURRENT_LOG"
    fi
}

case "$ACTION" in
    start)
        start_bg
        ;;
    foreground)
        start_foreground
        ;;
    stop)
        stop_server
        ;;
    status)
        status_server
        ;;
    restart)
        stop_server
        start_bg
        ;;
    logs)
        show_logs
        ;;
    check)
        # Simple health check: run_governance_scan is separate; check PID and exit code 0 if running
        if [ -n "$(get_pid)" ] && ps -p "$(get_pid)" > /dev/null 2>&1; then
            echo "HEALTHY"; exit 0
        else
            echo "NOT RUNNING"; exit 3
        fi
        ;;
    *)
        echo "Unknown action: $ACTION"; exit 2
        ;;
esac

echo "[run.sh] Finished."
exit 0
