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

# Defaults (match run.ps1): macOS avoids AirPlay Receiver's common port 5000.
DEFAULT_PORT=5000
if [[ "$(uname -s 2>/dev/null || true)" == "Darwin" ]]; then
    DEFAULT_PORT=5001
fi
PORT="$DEFAULT_PORT"
PORT_EXPLICIT=0
AGENT_TEST=0
ISOLATED_TEST_INSTANCE=0
AGENT_TEST_INSTANCE=0
NO_BROWSER=0
FORCE_BROWSER=0
CHROME_BETA=0
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
RESTORE_BACKUP_PATH=""
RESTORE_TARGET_DB_NAME=""
RESTORE_APPLY=0
RESTORE_DROP_TARGET=0
UPDATE_INTERVAL_MINUTES=60
UPDATE_BRANCH="main"
UPDATE_NO_RESTART_IF_RUNNING=0
NO_BACKUP_MIGRATE=0
DISABLE_LOG_READY=0
HEALTH_DEBUG=0
SHOW_RELATION_COVERAGE=0
STATUS_RUN_MAINTENANCE=0
BACKUP_FLAGS_SET=0
READY_LOG_PATTERNS=("Running with Waitress" "Press CTRL+C to quit" "Flask app running")

EXTRA_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        -Port) PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
        --port|--Port) PORT="$2"; PORT_EXPLICIT=1; shift 2 ;;
        -AgentTest|--AgentTest) AGENT_TEST=1; shift ;;
        -IsolatedTestInstance|--IsolatedTestInstance) ISOLATED_TEST_INSTANCE=1; shift ;;
        -NoBrowser|--NoBrowser) NO_BROWSER=1; shift ;;
        --no-browser|-n) NO_BROWSER=1; shift ;;
        -ForceBrowser|--ForceBrowser) FORCE_BROWSER=1; shift ;;
        --force-browser|-f) FORCE_BROWSER=1; shift ;;
        -ChromeBeta|--ChromeBeta) CHROME_BETA=1; shift ;;
        -Tail) TAIL="$2"; shift 2 ;;
        --tail|--Tail) TAIL="$2"; shift 2 ;;
        -Follow) FOLLOW=1; shift ;;
        --follow) FOLLOW=1; shift ;;
        -LogRetention) LOG_RETENTION="$2"; shift 2 ;;
        -AdminToken) ADMIN_TOKEN="$2"; shift 2 ;;
        -SkipHealth|--SkipHealth) SKIP_HEALTH=1; shift ;;
        --skip-health) SKIP_HEALTH=1; shift ;;
        -HealthTimeoutSec) HEALTH_TIMEOUT_SEC="$2"; shift 2 ;;
        -HealthGraceSec) HEALTH_GRACE_SEC="$2"; shift 2 ;;
        -ReadyLogPatterns)
            if [ $# -ge 2 ]; then
                IFS=',' read -r -a READY_LOG_PATTERNS <<<"$2"
                # Trim whitespace on each pattern.
                for i in "${!READY_LOG_PATTERNS[@]}"; do
                    local_pattern="${READY_LOG_PATTERNS[$i]}"
                    local_pattern="${local_pattern#"${local_pattern%%[![:space:]]*}"}"
                    local_pattern="${local_pattern%"${local_pattern##*[![:space:]]}"}"
                    READY_LOG_PATTERNS[$i]="$local_pattern"
                done
            fi
            shift 2
            ;;
        -DisableLogReady) DISABLE_LOG_READY=1; shift ;;
        -HealthDebug) HEALTH_DEBUG=1; shift ;;
        -ShowRelationCoverage) SHOW_RELATION_COVERAGE=1; shift ;;
        -StatusRunMaintenance) STATUS_RUN_MAINTENANCE=1; shift ;;
        -BackupDryRun) BACKUP_DRY_RUN=1; BACKUP_FLAGS_SET=1; shift ;;
        -BackupTag) BACKUP_TAG="$2"; BACKUP_FLAGS_SET=1; shift 2 ;;
        -BackupOutDir) BACKUP_OUT_DIR="$2"; BACKUP_FLAGS_SET=1; shift 2 ;;
        -RestoreBackupPath) RESTORE_BACKUP_PATH="$2"; shift 2 ;;
        -RestoreTargetDbName) RESTORE_TARGET_DB_NAME="$2"; shift 2 ;;
        -RestoreApply) RESTORE_APPLY=1; shift ;;
        -RestoreDropTarget) RESTORE_DROP_TARGET=1; shift ;;
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

if [ "$AGENT_TEST" -eq 1 ] || [ "$ISOLATED_TEST_INSTANCE" -eq 1 ]; then
    AGENT_TEST_INSTANCE=1
fi

if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
    if [ "$PORT_EXPLICIT" -eq 0 ]; then
        PORT=5010
    fi
    if [ "$FORCE_BROWSER" -eq 0 ]; then
        NO_BROWSER=1
    fi
fi

PID_FILE="${RUN_DIR}/von_${PORT}.pid"
CURRENT_LOG="${LOGS_DIR}/von_${PORT}_current.log"
TS="$(date +%Y%m%d_%H%M%S 2>/dev/null || date +%Y%m%d_%H%M%S)"
NEW_LOG="${LOGS_DIR}/von_${PORT}_${TS}.log"
SERVER_ERR_LOG="${NEW_LOG}.err"
RAG_PID_FILE="${RUN_DIR}/rag_worker.pid"
RAG_LOG_FILE="${LOGS_DIR}/rag_worker_${TS}.log"
RAG_ERR_LOG_FILE="${RAG_LOG_FILE}.err"
CONCEPT_INDEX_PID_FILE="${RUN_DIR}/concept_index_worker.pid"
CONCEPT_INDEX_LOG_FILE="${LOGS_DIR}/concept_index_worker_${TS}.log"
CONCEPT_INDEX_ERR_LOG_FILE="${CONCEPT_INDEX_LOG_FILE}.err"
TOKEN_FILE="${RUN_DIR}/admin_token.txt"
BROWSER_SENTINEL_NAME="browser_opened_once"
if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
    BROWSER_SENTINEL_NAME="browser_opened_once_${PORT}"
fi
SENTINEL_BROWSER="${RUN_DIR}/${BROWSER_SENTINEL_NAME}"
LOCAL_BACKUPS="${ROOT}/backups"
BACKUP_ROOT=""
REMOTE_BACKUP_ROOT=""
REPAIR_ATTEMPTED=0
ALLOW_REPO_BACKUP_OUTPUT=0
case "${VON_ALLOW_BACKUP_IN_REPO:-}" in
    1|true|TRUE|yes|YES|y|Y|on|ON) ALLOW_REPO_BACKUP_OUTPUT=1 ;;
esac

same_path() {
    local a="$1"
    local b="$2"
    if [ -z "$a" ] || [ -z "$b" ]; then
        return 1
    fi
    local ra=""
    local rb=""
    ra="$(cd "$a" 2>/dev/null && pwd -P)" || return 1
    rb="$(cd "$b" 2>/dev/null && pwd -P)" || return 1
    [ "$ra" = "$rb" ]
}

normalise_path() {
    local input_path="$1"
    if [ -z "$input_path" ]; then
        return 1
    fi
    local expanded="$input_path"
    if [ "${expanded#\~}" != "$expanded" ]; then
        expanded="${HOME}${expanded#\~}"
    fi

    local dir_part
    local base_part
    dir_part="$(dirname "$expanded")"
    base_part="$(basename "$expanded")"
    if [ "$dir_part" = "." ]; then
        dir_part="$PWD"
    fi

    local abs_dir
    abs_dir="$(cd "$dir_part" 2>/dev/null && pwd -P)" || return 1
    printf '%s/%s' "$abs_dir" "$base_part"
}

is_path_inside_root() {
    local candidate="$1"
    local root_path="$2"
    local candidate_abs=""
    local root_abs=""
    candidate_abs="$(normalise_path "$candidate")" || return 1
    root_abs="$(normalise_path "$root_path")" || return 1
    if [ "$candidate_abs" = "$root_abs" ]; then
        return 0
    fi
    case "$candidate_abs" in
        "$root_abs"/*) return 0 ;;
    esac
    return 1
}

backup_output_path_allowed() {
    local path="$1"
    local context="$2"
    local apply_mode="$3"
    if ! is_path_inside_root "$path" "$ROOT"; then
        return 0
    fi

    local resolved
    resolved="$(normalise_path "$path" 2>/dev/null || printf '%s' "$path")"
    if [ "$ALLOW_REPO_BACKUP_OUTPUT" -eq 1 ]; then
        log "[$context] WARN: backup output resolves inside repo root ($resolved), but continuing due to VON_ALLOW_BACKUP_IN_REPO=1."
        return 0
    fi

    if [ "$apply_mode" = "1" ]; then
        log "[$context] ERROR: refusing backup apply mode with output under repo root ($resolved). Set VON_BACKUP_ROOT/-BackupOutDir outside repo, or override with VON_ALLOW_BACKUP_IN_REPO=1."
        return 1
    fi

    log "[$context] WARN: backup output is under repo root ($resolved). Dry-run allowed, apply mode remains blocked unless VON_ALLOW_BACKUP_IN_REPO=1."
    return 0
}

resolve_backup_root() {
    local preferred=""
    if [ -n "${VON_BACKUP_ROOT:-}" ]; then
        preferred="${VON_BACKUP_ROOT}"
        preferred="${preferred#\"}"
        preferred="${preferred%\"}"
        if mkdir -p "$preferred" 2>/dev/null; then
            BACKUP_ROOT="$preferred"
        else
            log "[backup] WARN: Cannot create/write VON_BACKUP_ROOT='${preferred}'; falling back to automatic selection."
        fi
    fi

    REMOTE_BACKUP_ROOT=""
    if [ -d "/Volumes/von_backups" ] && [ -w "/Volumes/von_backups" ]; then
        REMOTE_BACKUP_ROOT="/Volumes/von_backups"
    elif [ -d "/mnt/von_backups" ] && [ -w "/mnt/von_backups" ]; then
        REMOTE_BACKUP_ROOT="/mnt/von_backups"
    fi

    if [ -z "$BACKUP_ROOT" ]; then
        if [ -n "$REMOTE_BACKUP_ROOT" ]; then
            BACKUP_ROOT="$REMOTE_BACKUP_ROOT"
        else
            BACKUP_ROOT="$LOCAL_BACKUPS"
        fi
    fi

    mkdir -p "$BACKUP_ROOT" 2>/dev/null || true
}

resolve_backup_out_dir() {
    local requested="$1"
    local fallback="$2"
    local reason="$3"
    local effective="$requested"
    if [ -z "$effective" ]; then
        effective="$fallback"
    fi
    if ! mkdir -p "$effective" 2>/dev/null; then
        log "[backup] WARN: Cannot create/write ${effective} (${reason}); falling back to local backups: ${fallback}"
        effective="$fallback"
        mkdir -p "$effective" 2>/dev/null || true
    fi
    printf '%s' "$effective"
}

mtime_epoch() {
    local path="$1"
    if command -v stat >/dev/null 2>&1; then
        if stat -c %Y "$path" >/dev/null 2>&1; then
            stat -c %Y "$path" 2>/dev/null || true
            return 0
        fi
        if stat -f %m "$path" >/dev/null 2>&1; then
            stat -f %m "$path" 2>/dev/null || true
            return 0
        fi
    fi
    return 1
}

migrate_local_backups() {
    local moved=0
    local copied=0
    local remote_root="$REMOTE_BACKUP_ROOT"
    if [ -z "$remote_root" ]; then
        log "[backup-migrate] summary moved=$moved copied=$copied (no remote backup root)"
        return 0
    fi

    if [ ! -d "$LOCAL_BACKUPS" ]; then
        mkdir -p "$LOCAL_BACKUPS" 2>/dev/null || {
            log "[backup-migrate] summary moved=$moved copied=$copied (cannot create local backups dir)"
            return 0
        }
    fi

    if same_path "$LOCAL_BACKUPS" "$remote_root"; then
        log "[backup-migrate] summary moved=$moved copied=$copied (remote backup root is local)"
        return 0
    fi

    if ! mkdir -p "$remote_root" 2>/dev/null; then
        log "[backup-migrate] summary moved=$moved copied=$copied (create dest failed)"
        return 0
    fi

    local candidates=()
    while IFS= read -r entry; do
        if [ -z "$entry" ]; then
            continue
        fi
        if printf '%s' "$entry" | grep -qE '_[0-9]{8}_[0-9]{6}Z?'; then
            if [ -d "${LOCAL_BACKUPS}/${entry}" ] || printf '%s' "$entry" | grep -qE '\.zip(\.enc)?$'; then
                candidates+=("$entry")
            fi
        fi
    done < <(ls -1t "$LOCAL_BACKUPS" 2>/dev/null || true)

    local newest=""
    if [ "${#candidates[@]}" -gt 0 ]; then
        newest="${candidates[0]}"
    fi

    if [ "${#candidates[@]}" -gt 1 ]; then
        local i
        for ((i=1; i<${#candidates[@]}; i++)); do
            local item="${candidates[$i]}"
            if [ -e "${remote_root}/${item}" ]; then
                continue
            fi
            log "[backup-migrate] Moving ${item} -> ${remote_root}"
            if mv "${LOCAL_BACKUPS}/${item}" "${remote_root}/" 2>/dev/null; then
                moved=$((moved+1))
            else
                log "[backup-migrate] WARN move failed ${item}"
            fi
        done
    fi

    if [ -n "$newest" ] && [ ! -e "${remote_root}/${newest}" ]; then
        log "[backup-migrate] Copying newest ${newest} to backup root (preserve local copy)"
        if [ -d "${LOCAL_BACKUPS}/${newest}" ]; then
            if cp -a "${LOCAL_BACKUPS}/${newest}" "${remote_root}/" 2>/dev/null; then
                copied=$((copied+1))
            fi
        else
            if cp -a "${LOCAL_BACKUPS}/${newest}" "${remote_root}/" 2>/dev/null; then
                copied=$((copied+1))
            fi
        fi
    fi

    local remote_candidates=()
    while IFS= read -r entry; do
        if [ -z "$entry" ]; then
            continue
        fi
        if printf '%s' "$entry" | grep -qE '_[0-9]{8}_[0-9]{6}Z?'; then
            if [ -d "${remote_root}/${entry}" ] || printf '%s' "$entry" | grep -qE '\.zip(\.enc)?$'; then
                remote_candidates+=("$entry")
            fi
        fi
    done < <(ls -1t "$remote_root" 2>/dev/null || true)

    local remote_newest=""
    if [ "${#remote_candidates[@]}" -gt 0 ]; then
        remote_newest="${remote_candidates[0]}"
    fi

    if [ -n "$remote_newest" ]; then
        local local_path="${LOCAL_BACKUPS}/${remote_newest}"
        local remote_path="${remote_root}/${remote_newest}"
        local needs_downsync=0
        if [ ! -e "$local_path" ]; then
            needs_downsync=1
        else
            local remote_mtime
            local local_mtime
            remote_mtime="$(mtime_epoch "$remote_path" || true)"
            local_mtime="$(mtime_epoch "$local_path" || true)"
            if [ -n "$remote_mtime" ] && [ -n "$local_mtime" ] && [ "$remote_mtime" -gt "$local_mtime" ]; then
                needs_downsync=1
            fi
        fi
        if [ "$needs_downsync" -eq 1 ]; then
            log "[backup-migrate] Down-sync newer ${remote_newest} -> local backups"
            if [ -d "$remote_path" ]; then
                cp -a "$remote_path" "$LOCAL_BACKUPS/" 2>/dev/null || log "[backup-migrate] WARN down-sync failed ${remote_newest}"
            else
                cp -a "$remote_path" "$LOCAL_BACKUPS/" 2>/dev/null || log "[backup-migrate] WARN down-sync failed ${remote_newest}"
            fi
        fi
    fi

    log "[backup-migrate] summary moved=$moved copied=$copied"
}

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

# Resolve backup root and migrate local backups if configured.
resolve_backup_root
if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
    log "[backup-migrate] disabled via -AgentTest"
elif [ "$NO_BACKUP_MIGRATE" -eq 0 ]; then
    migrate_local_backups
else
    log "[backup-migrate] disabled via -NoBackupMigrate"
fi

# Backward-compatible convenience: if start action is combined with backup flags,
# run the backup action instead of starting the server.
if [ "$ACTION" = "start" ] && [ "$BACKUP_FLAGS_SET" -eq 1 ]; then
    log "Start requested with backup flags; running backup action only."
    ACTION="backup"
fi

load_env_from_dotenv() {
    local env_path="$1"
    if [ -z "$env_path" ] || [ ! -f "$env_path" ]; then
        return 0
    fi

    local applied=0
    local line=""
    local trimmed=""
    local key=""
    local value=""
    local first_char=""
    local last_char=""

    while IFS= read -r line || [ -n "$line" ]; do
        # Handle CRLF files safely.
        line="${line%$'\r'}"

        trimmed="$line"
        trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
        trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
        if [ -z "$trimmed" ]; then
            continue
        fi
        if [[ "$trimmed" == \#* ]]; then
            continue
        fi

        if [[ "$trimmed" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"

            value="${value#"${value%%[![:space:]]*}"}"
            value="${value%"${value##*[![:space:]]}"}"

            if [ "${#value}" -ge 2 ]; then
                first_char="${value:0:1}"
                last_char="${value:${#value}-1:1}"
                if [[ "$first_char" == '"' && "$last_char" == '"' ]] || [[ "$first_char" == "'" && "$last_char" == "'" ]]; then
                    value="${value:1:${#value}-2}"
                fi
            fi

            printf -v "$key" '%s' "$value"
            export "$key"
            applied=$((applied + 1))
        fi
    done < "$env_path"

    if [ "$applied" -gt 0 ]; then
        log "Loaded $applied .env override(s) from $env_path"
    fi
}

# Load .env overrides without executing arbitrary shell content.
load_env_from_dotenv "${ROOT}/.env"

if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
    export VON_AGENT_TEST_INSTANCE=1
else
    unset VON_AGENT_TEST_INSTANCE || true
fi

export PYTHONPATH="${ROOT}"
export PYTHONUNBUFFERED=1
export PYTHONFAULTHANDLER="${PYTHONFAULTHANDLER:-1}"
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
    if [ -x "${ROOT}/.venv/bin/python" ]; then
        echo "${ROOT}/.venv/bin/python"
    elif [ -x "${ROOT}/.venv/Scripts/python.exe" ]; then
        echo "${ROOT}/.venv/Scripts/python.exe"
    elif command -v python3 >/dev/null 2>&1; then
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

is_truthy() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|y|Y) return 0 ;;
        *) return 1 ;;
    esac
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

pidfile_uptime_minutes() {
    if [ ! -f "$PID_FILE" ]; then
        return 1
    fi
    local start_line=""
    start_line="$(grep -E '^START=' "$PID_FILE" 2>/dev/null | head -n 1 || true)"
    if [ -z "$start_line" ]; then
        return 1
    fi
    local start_value="${start_line#START=}"
    if [ -z "$start_value" ]; then
        return 1
    fi
    local start_epoch=""
    start_epoch="$(parse_iso_epoch "$start_value")"
    if [ -z "$start_epoch" ]; then
        return 1
    fi
    local now_epoch=""
    now_epoch="$(date +%s 2>/dev/null || true)"
    if [ -z "$now_epoch" ]; then
        return 1
    fi
    local diff=$(( (now_epoch - start_epoch) / 60 ))
    if [ "$diff" -lt 0 ]; then
        diff=0
    fi
    printf '%s' "$diff"
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

get_health_payload() {
    local timeout=2
    if printf '%s' "${VON_HEALTH_HTTP_TIMEOUT:-}" | grep -qE '^[0-9]+$'; then
        if [ "$VON_HEALTH_HTTP_TIMEOUT" -gt 0 ] && [ "$VON_HEALTH_HTTP_TIMEOUT" -lt 61 ]; then
            timeout="$VON_HEALTH_HTTP_TIMEOUT"
        fi
    fi
    local hosts=("127.0.0.1")
    if [ -n "${VON_HEALTH_HOSTS:-}" ]; then
        IFS=',' read -r -a hosts <<<"${VON_HEALTH_HOSTS}"
        local i
        for i in "${!hosts[@]}"; do
            hosts[$i]="${hosts[$i]#"${hosts[$i]%%[![:space:]]*}"}"
            hosts[$i]="${hosts[$i]%"${hosts[$i]##*[![:space:]]}"}"
        done
    elif is_truthy "${VON_HEALTH_INCLUDE_LOCALHOST:-}"; then
        hosts+=("localhost")
    fi
    local host
    for host in "${hosts[@]}"; do
        if [ -z "$host" ]; then
            continue
        fi
        local url="http://${host}:${PORT}/health"
        if command -v curl >/dev/null 2>&1; then
            local body
            body="$(curl -fsS --max-time "$timeout" "$url" 2>/dev/null || true)"
            if [ -n "$body" ]; then
                printf '%s' "$body"
                return 0
            fi
            if [ "$HEALTH_DEBUG" -eq 1 ]; then
                log "Health attempt failed $url"
            fi
            continue
        fi
        if command -v wget >/dev/null 2>&1; then
            local body
            body="$(wget -q -T "$timeout" -O - "$url" 2>/dev/null || true)"
            if [ -n "$body" ]; then
                printf '%s' "$body"
                return 0
            fi
            if [ "$HEALTH_DEBUG" -eq 1 ]; then
                log "Health attempt failed $url"
            fi
            continue
        fi
    done
    return 1
}

health_ok() {
    get_health_payload >/dev/null
}

health_payload_field() {
    local payload="$1"
    local field="$2"
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 1
    fi
    HEALTH_PAYLOAD="$payload" "$py" - "$field" <<'PY' 2>/dev/null
import json
import os
import sys

field = sys.argv[1]
try:
    payload = json.loads(os.environ.get("HEALTH_PAYLOAD", ""))
except Exception:
    raise SystemExit(1)

value = payload.get(field)
if isinstance(value, bool):
    print("true" if value else "false")
elif value is not None:
    print(value)
else:
    raise SystemExit(1)
PY
}

agent_test_health_payload_valid() {
    local payload="$1"
    local log_failure="${2:-0}"
    if [ -z "$payload" ]; then
        if [ "$log_failure" = "1" ]; then
            log "Agent test health read-back rejected: /health returned no JSON payload."
        fi
        return 1
    fi

    local marker
    marker="$(health_payload_field "$payload" "agent_test_instance" || true)"
    if [ "$marker" != "true" ]; then
        if [ "$log_failure" = "1" ]; then
            log "Agent test health read-back rejected: agent_test_instance marker was not true."
        fi
        return 1
    fi

    local listener
    listener="$(get_listening_pid_by_port "$PORT" || true)"
    if [ -z "$listener" ]; then
        if [ "$log_failure" = "1" ]; then
            log "Agent test health read-back rejected: no listener remained on port $PORT."
        fi
        return 1
    fi

    local payload_pid
    payload_pid="$(health_payload_field "$payload" "pid" || true)"
    if ! printf '%s' "$payload_pid" | grep -qE '^[0-9]+$'; then
        if [ "$log_failure" = "1" ]; then
            log "Agent test health read-back rejected: /health payload did not include a valid pid."
        fi
        return 1
    fi
    if [ "$payload_pid" != "$listener" ]; then
        if [ "$log_failure" = "1" ]; then
            log "Agent test health read-back rejected: /health pid $payload_pid did not match listener PID $listener."
        fi
        return 1
    fi
    return 0
}

launcher_health_ready() {
    local log_failure="${1:-0}"
    local payload
    payload="$(get_health_payload || true)"
    if [ -z "$payload" ]; then
        if [ "$log_failure" = "1" ] && [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
            log "Agent test health read-back not ready: /health did not return a payload."
        fi
        return 1
    fi
    if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
        agent_test_health_payload_valid "$payload" "$log_failure"
        return $?
    fi
    return 0
}

confirm_health_stable() {
    local attempts=4
    local i=0
    while [ "$i" -lt "$attempts" ]; do
        sleep 0.5
        if ! launcher_health_ready "$([ "$i" -eq $((attempts - 1)) ] && echo 1 || echo 0)"; then
            return 1
        fi
        i=$((i + 1))
    done
    return 0
}

show_server_start_failure_tail() {
    log "Showing last 40 log lines for startup diagnostics:"
    if [ -f "$CURRENT_LOG" ]; then
        local log_tail
        log_tail="$(tail -n 40 "$CURRENT_LOG" 2>/dev/null || true)"
        if [ -n "$log_tail" ]; then
            printf '%s\n' "$log_tail"
        fi
    fi
    if [ -f "$SERVER_ERR_LOG" ]; then
        log "Last 40 stderr log lines:"
        local err_tail
        err_tail="$(tail -n 40 "$SERVER_ERR_LOG" 2>/dev/null || true)"
        if [ -n "$err_tail" ]; then
            printf '%s\n' "$err_tail"
        fi
    fi
}

launch_detached_process() {
    local launcher_py="$1"
    local stdout_log="$2"
    local stderr_log="$3"
    local working_dir="$4"
    local executable="$5"
    shift 5

    "$launcher_py" - "$stdout_log" "$stderr_log" "$working_dir" "$executable" "$@" <<'PY'
import subprocess
import sys

stdout_path = sys.argv[1]
stderr_path = sys.argv[2]
working_dir = sys.argv[3]
executable = sys.argv[4]
args = sys.argv[5:]

stdout = open(stdout_path, "ab", buffering=0)
stderr = open(stderr_path, "ab", buffering=0)
process = subprocess.Popen(
    [executable, *args],
    cwd=working_dir,
    stdin=subprocess.DEVNULL,
    stdout=stdout,
    stderr=stderr,
    close_fds=True,
    start_new_session=True,
)
print(process.pid)
PY
}

open_browser() {
    local url="http://localhost:${PORT}/"
    # Match run.ps1 behaviour: prefer Chrome Beta, with optional Beta-only mode.
    if command -v google-chrome-beta >/dev/null 2>&1; then
        google-chrome-beta "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if command -v chromium-beta >/dev/null 2>&1; then
        chromium-beta "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if [ "$CHROME_BETA" -eq 1 ] && command -v open >/dev/null 2>&1; then
        open -a "Google Chrome Beta" "$url" >/dev/null 2>&1 || true
        return 0
    fi
    if [ "$CHROME_BETA" -eq 1 ]; then
        log "Browser open skipped (-ChromeBeta requested, but Chrome Beta was not found). URL: $url"
        return 0
    fi

    # Stable/browser fallbacks.
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
        open -a "Google Chrome Beta" "$url" >/dev/null 2>&1 || open -a "Google Chrome" "$url" >/dev/null 2>&1 || open "$url" >/dev/null 2>&1 || true
        return 0
    fi
    log "Browser open skipped (no opener found). URL: $url"
}

open_browser_if_needed() {
    if [ "$NO_BROWSER" -eq 1 ]; then
        return 0
    fi

    local should_open=0
    if [ "$FORCE_BROWSER" -eq 1 ]; then
        should_open=1
    elif [ ! -f "$SENTINEL_BROWSER" ]; then
        should_open=1
    fi

    if [ "$should_open" -eq 1 ]; then
        open_browser
        date -Iseconds 2>/dev/null > "$SENTINEL_BROWSER" || true
        if [ "$FORCE_BROWSER" -eq 1 ]; then
            log "Opened browser (forced)."
        else
            log "Opened browser (first launch)."
        fi
    else
        log "Browser already opened previously (use -ForceBrowser to open again)."
    fi
}

get_listening_pid_by_port() {
    # Best-effort listener PID detection across Windows/Linux/macOS.
    local port="$1"
    if command -v netstat.exe >/dev/null 2>&1; then
        netstat.exe -ano -p tcp 2>/dev/null | awk -v p="$port" 'BEGIN { IGNORECASE=1 } $1 == "TCP" { local=$2; state=$4; pid=$5; if (state == "LISTENING" && local ~ ":" p "$" && pid ~ /^[0-9]+$/) { print pid; exit } }'
        return 0
    fi
    if command -v ss >/dev/null 2>&1; then
        # Linux (iproute2)
        ss -lptn "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1
        return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1
        return 0
    fi
    if command -v netstat >/dev/null 2>&1; then
        # Fallback: netstat output parsing is messy; keep minimal.
        netstat -anp 2>/dev/null | grep -E "[:\.]$port\s" | grep LISTEN 2>/dev/null | sed -n 's#.*/\([0-9][0-9]*\)$#\1#p' | head -n 1
        return 0
    fi
    return 0
}

get_process_commandline() {
    local pid="${1:-}"
    if [ -z "$pid" ]; then
        return 0
    fi
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 0
    fi
    "$py" -c 'import psutil,sys
pid=int(sys.argv[1])
try:
    cmd=psutil.Process(pid).cmdline()
except Exception:
    raise SystemExit(1)
print(" ".join(str(p) for p in cmd))' "$pid" 2>/dev/null || true
}

process_exists() {
    local pid="${1:-}"
    if ! printf '%s' "$pid" | grep -qE '^[0-9]+$'; then
        return 1
    fi
    if kill -0 "$pid" >/dev/null 2>&1; then
        return 0
    fi
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 1
    fi
    "$py" -c 'import psutil,sys
pid=int(sys.argv[1])
raise SystemExit(0 if psutil.pid_exists(pid) else 1)' "$pid" >/dev/null 2>&1
}

is_von_main_process() {
    local pid="${1:-}"
    local cmdline=""
    cmdline="$(get_process_commandline "$pid")"
    if [ -z "$cmdline" ]; then
        return 1
    fi
    local cmd_norm=""
    cmd_norm="$(printf '%s' "$cmdline" | tr '[:upper:]' '[:lower:]' | sed 's#\\#/#g')"
    if ! printf '%s' "$cmd_norm" | grep -F -q "src/workflows/von/main.py"; then
        return 1
    fi

    local root_norm=""
    root_norm="$(printf '%s' "$ROOT" | tr '[:upper:]' '[:lower:]' | sed 's#\\#/#g')"
    local root_drive_norm="$root_norm"
    if printf '%s' "$root_norm" | grep -qE '^/[a-z]/'; then
        root_drive_norm="$(printf '%s' "$root_norm" | sed -E 's#^/([a-z])/#\1:/#')"
    fi

    if printf '%s' "$cmd_norm" | grep -F -q "$root_norm"; then
        return 0
    fi
    if printf '%s' "$cmd_norm" | grep -F -q "$root_drive_norm"; then
        return 0
    fi
    if printf '%s' "$cmd_norm" | grep -F -q "/strong-ai-lab/von/"; then
        return 0
    fi

    # Launchers start the server from $ROOT with a relative script argument.
    # In that case the command line may not contain the absolute repo path.
    local cwd_norm=""
    cwd_norm="$(get_process_cwd "$pid" | tr '[:upper:]' '[:lower:]' | sed 's#\\#/#g')" || true
    if [ -n "$cwd_norm" ] && [ "$cwd_norm" = "$root_norm" ]; then
        return 0
    fi
    return 1
}

get_process_cwd() {
    local pid="${1:-}"
    if [ -z "$pid" ]; then
        return 1
    fi
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 1
    fi
    "$py" -c 'import os,sys,psutil
pid=int(sys.argv[1])
try:
    print(os.path.normpath(psutil.Process(pid).cwd()))
except Exception:
    raise SystemExit(1)' "$pid" 2>/dev/null
}

stop_process_with_escalation() {
    local pid="${1:-}"
    if [ -z "$pid" ]; then
        return 1
    fi

    kill "$pid" >/dev/null 2>&1 || true

    local waited=0
    while [ "$waited" -lt 20 ]; do
        if ! process_exists "$pid"; then
            return 0
        fi
        sleep 0.5
        waited=$((waited + 1))
    done

    kill -9 "$pid" >/dev/null 2>&1 || true
    sleep 0.2
    if process_exists "$pid"; then
        return 1
    fi
    return 0
}

stop_process_tree_with_escalation() {
    local pid="${1:-}"
    if [ -z "$pid" ]; then
        return 1
    fi

    if command -v taskkill.exe >/dev/null 2>&1; then
        taskkill.exe //PID "$pid" //T >/dev/null 2>&1 || true
        sleep 0.5
        taskkill.exe //PID "$pid" //T //F >/dev/null 2>&1 || true
    else
        stop_process_with_escalation "$pid" || true
    fi

    # Verify via psutil for consistent cross-platform existence checks.
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 0
    fi
    if "$py" -c 'import psutil,sys
pid=int(sys.argv[1])
raise SystemExit(0 if psutil.pid_exists(pid) else 1)' "$pid" 2>/dev/null; then
        return 1
    fi
    return 0
}

stop_python_processes_by_script() {
    local script_relative_path="${1:-}"
    local label="${2:-python-script}"
    local exclude_pid="${3:-0}"
    if [ -z "$script_relative_path" ]; then
        return 0
    fi
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 0
    fi
    local script_path="${ROOT}/${script_relative_path}"
    local killed
    killed="$("$py" -c 'import os,sys,psutil
target=os.path.normcase(os.path.normpath(sys.argv[1]))
exclude=int(sys.argv[2]) if len(sys.argv) > 2 else 0
current=os.getpid()
fragment=target.lower().replace("\\\\","/")
name=os.path.basename(target).lower()
killed=[]
for proc in psutil.process_iter(["pid","cmdline"]):
    try:
        pid=int(proc.info.get("pid") or 0)
        if pid <= 0 or pid == current or pid == exclude:
            continue
        cmdline=[str(part) for part in (proc.info.get("cmdline") or [])]
        if not cmdline:
            continue
        joined=" ".join(cmdline)
        norm=joined.lower().replace("\\\\","/")
        if fragment not in norm and name not in norm:
            continue
        try:
            proc.terminate()
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        killed.append(str(pid))
    except Exception:
        continue
print(" ".join(killed))' "$script_path" "$exclude_pid" 2>/dev/null || true)"
    if [ -n "$killed" ]; then
        log "Stopped stale ${label} process(es): ${killed}"
    fi
}

restart_port_takeover() {
    local listener="${1:-}"
    if [ -z "$listener" ]; then
        return 0
    fi

    if ! is_von_main_process "$listener"; then
        log "Port $PORT is owned by PID=$listener, which does not look like this Von server. Aborting restart takeover."
        return 1
    fi

    log "Restart takeover: stopping untracked Von listener PID=$listener on port $PORT..."
    if ! stop_process_with_escalation "$listener"; then
        log "Failed to stop PID=$listener; restart cannot continue safely."
        return 1
    fi

    local remaining=""
    remaining="$(get_listening_pid_by_port "$PORT" || true)"
    if [ -n "$remaining" ]; then
        log "Port $PORT is still in use by PID=$remaining after takeover attempt; aborting start."
        return 1
    fi

    log "Restart takeover succeeded; port $PORT is clear."
    return 0
}

port_listening() {
    local pid
    pid="$(get_listening_pid_by_port "$PORT" || true)"
    [ -n "$pid" ]
}

get_von_main_pid_by_port() {
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 0
    fi
    local script_path="${ROOT}/src/workflows/von/main.py"
    "$py" -c 'import os,sys,psutil
target=os.path.normcase(os.path.normpath(sys.argv[1]))
port=str(sys.argv[2])
target_norm=target.lower().replace("\\\\","/")
target_name=os.path.basename(target).lower()
matches=[]
for proc in psutil.process_iter(["pid","cmdline","create_time"]):
    try:
        cmdline=[str(part) for part in (proc.info.get("cmdline") or [])]
        if not cmdline:
            continue
        exe_name=os.path.basename(cmdline[0]).lower()
        if "python" not in exe_name:
            continue
        norm=[part.lower().replace("\\\\","/") for part in cmdline]
        joined=" ".join(norm)
        if target_norm not in joined and not any(arg.endswith(target_name) for arg in norm):
            continue
        port_match=False
        for i,arg in enumerate(norm):
            if arg == "--port" and i + 1 < len(norm) and norm[i + 1] == port:
                port_match=True
                break
            if arg.startswith("--port=") and arg.split("=", 1)[1] == port:
                port_match=True
                break
        if not port_match:
            continue
        matches.append((float(proc.info.get("create_time") or 0.0), int(proc.info.get("pid") or 0)))
    except Exception:
        continue
if not matches:
    raise SystemExit(1)
matches.sort(reverse=True)
print(matches[0][1])' "$script_path" "$PORT" 2>/dev/null || true
}

log_ready() {
    if [ "$DISABLE_LOG_READY" -eq 1 ]; then
        return 1
    fi
    local log_path="$1"
    if [ ! -f "$log_path" ]; then
        return 1
    fi
    local tail=""
    tail="$(tail -n 400 "$log_path" 2>/dev/null || true)"
    local pattern
    for pattern in "${READY_LOG_PATTERNS[@]}"; do
        if [ -n "$pattern" ] && printf '%s' "$tail" | grep -F -q "$pattern"; then
            return 0
        fi
    done
    return 1
}

sync_pidfile_to_listener() {
    local listener
    listener="$(get_listening_pid_by_port "$PORT" || true)"
    if [ -z "$listener" ]; then
        listener="$(get_von_main_pid_by_port || true)"
    fi
    if [ -z "$listener" ]; then
        return 0
    fi
    if ! is_von_main_process "$listener"; then
        return 0
    fi
    local current=""
    current="$(get_pid || true)"
    if [ -z "$current" ] || [ "$current" != "$listener" ]; then
        write_pidfile "$listener"
        log "Synchronized PID file to listener PID=${listener}"
    fi
}

refine_pid_to_child() {
    local parent_pid="$1"
    if [ -z "$parent_pid" ]; then
        return 0
    fi
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 0
    fi
    local child
    child="$("$py" -c 'import psutil,sys
parent=int(sys.argv[1])
target="src/workflows/von/main.py"
try:
    proc=psutil.Process(parent)
except Exception:
    raise SystemExit(1)
for child in proc.children(recursive=True):
    try:
        cmd=" ".join(str(p) for p in (child.cmdline() or []))
    except Exception:
        continue
    if target in cmd.replace("\\\\","/"):
        print(child.pid)
        raise SystemExit(0)
raise SystemExit(1)' "$parent_pid" 2>/dev/null || true)"
    if [ -n "$child" ] && [ "$child" != "$parent_pid" ]; then
        write_pidfile "$child"
        log "Updated PID file to python process PID=$child (was wrapper PID=$parent_pid)."
    fi
    return 0
}

start_rag_worker_bg() {
    # Mirrors run.ps1 best-effort background worker.
    if [ -f "$RAG_PID_FILE" ]; then
        local old
        old="$(sed -n 's/^PID=//p' "$RAG_PID_FILE" 2>/dev/null | head -n 1 || true)"
        if [ -n "$old" ] && process_exists "$old"; then
            log "RAG Worker already running (PID=$old)."
            return 0
        fi
        rm -f "$RAG_PID_FILE" 2>/dev/null || true
    fi
    stop_python_processes_by_script "src/backend/utilities/rag_indexing_worker.py" "RAG Worker"

    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        log "ERROR: No python executable found for RAG Worker launch."
        return 1
    fi

    log "Starting RAG Indexing Worker..."
    rm -f "$RAG_LOG_FILE" "$RAG_ERR_LOG_FILE" 2>/dev/null || true
    nohup "$py" -u "${ROOT}/src/backend/utilities/rag_indexing_worker.py" >> "$RAG_LOG_FILE" 2>> "$RAG_ERR_LOG_FILE" &
    local pid=$!
    printf 'PID=%s\nSTART=%s\n' "$pid" "$(date -Iseconds 2>/dev/null || date)" > "$RAG_PID_FILE"
    log "RAG Worker started (PID=$pid). Logs: $RAG_LOG_FILE, $RAG_ERR_LOG_FILE"
}

stop_rag_worker() {
    local pid_to_kill=0
    if [ -f "$RAG_PID_FILE" ]; then
        local content
        content="$(cat "$RAG_PID_FILE" 2>/dev/null || true)"
        if printf '%s' "$content" | grep -qE 'PID=[0-9]+'; then
            pid_to_kill="$(printf '%s' "$content" | sed -n 's/^PID=//p' | head -n 1 || true)"
        fi
        if [ "$pid_to_kill" -gt 0 ] 2>/dev/null; then
            log "Stopping RAG Worker (PID=$pid_to_kill)..."
            if ! stop_process_tree_with_escalation "$pid_to_kill"; then
                log "WARN: RAG Worker PID=$pid_to_kill may still be running."
            fi
        fi
    fi
    stop_python_processes_by_script "src/backend/utilities/rag_indexing_worker.py" "RAG Worker" "$pid_to_kill"
    rm -f "$RAG_PID_FILE" 2>/dev/null || true
}

start_concept_index_worker_bg() {
    if [ -f "$CONCEPT_INDEX_PID_FILE" ]; then
        local old
        old="$(sed -n 's/^PID=//p' "$CONCEPT_INDEX_PID_FILE" 2>/dev/null | head -n 1 || true)"
        if [ -n "$old" ] && process_exists "$old"; then
            log "Concept Index Worker already running (PID=$old)."
            return 0
        fi
        rm -f "$CONCEPT_INDEX_PID_FILE" 2>/dev/null || true
    fi
    stop_python_processes_by_script "src/backend/utilities/concept_index_worker.py" "Concept Index Worker"

    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        log "ERROR: No python executable found for Concept Index Worker launch."
        return 1
    fi

    local script_path="${ROOT}/src/backend/utilities/concept_index_worker.py"
    if [ ! -f "$script_path" ]; then
        log "[concept-index-worker] WARN: script missing: $script_path"
        return 0
    fi

    log "Starting Concept Index Worker..."
    rm -f "$CONCEPT_INDEX_LOG_FILE" "$CONCEPT_INDEX_ERR_LOG_FILE" 2>/dev/null || true
    nohup "$py" -u "$script_path" >> "$CONCEPT_INDEX_LOG_FILE" 2>> "$CONCEPT_INDEX_ERR_LOG_FILE" &
    local pid=$!
    printf 'PID=%s\nSTART=%s\n' "$pid" "$(date -Iseconds 2>/dev/null || date)" > "$CONCEPT_INDEX_PID_FILE"
    log "Concept Index Worker started (PID=$pid). Logs: $CONCEPT_INDEX_LOG_FILE, $CONCEPT_INDEX_ERR_LOG_FILE"
}

stop_concept_index_worker() {
    local pid_to_kill=0
    if [ -f "$CONCEPT_INDEX_PID_FILE" ]; then
        local content
        content="$(cat "$CONCEPT_INDEX_PID_FILE" 2>/dev/null || true)"
        if printf '%s' "$content" | grep -qE 'PID=[0-9]+'; then
            pid_to_kill="$(printf '%s' "$content" | sed -n 's/^PID=//p' | head -n 1 || true)"
        fi
        if [ "$pid_to_kill" -gt 0 ] 2>/dev/null; then
            log "Stopping Concept Index Worker (PID=$pid_to_kill)..."
            if ! stop_process_tree_with_escalation "$pid_to_kill"; then
                log "WARN: Concept Index Worker PID=$pid_to_kill may still be running."
            fi
        fi
    fi
    stop_python_processes_by_script "src/backend/utilities/concept_index_worker.py" "Concept Index Worker" "$pid_to_kill"
    rm -f "$CONCEPT_INDEX_PID_FILE" 2>/dev/null || true
}

start_server() {
    local restart_takeover="${1:-0}"

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
    if [ -z "$existing" ]; then
        existing="$(get_von_main_pid_by_port || true)"
    fi
    if [ -n "$existing" ] && process_exists "$existing"; then
        if [ "$restart_takeover" = "1" ]; then
            restart_port_takeover "$existing" || return 1
        else
            log "Already running (PID=$existing). Use ./run.sh stop or restart."
            write_pidfile "$existing"
            open_browser_if_needed
            return 0
        fi
    fi

    if [ "$restart_takeover" = "1" ]; then
        local takeover_listener=""
        takeover_listener="$(get_listening_pid_by_port "$PORT" || true)"
        if [ -n "$takeover_listener" ]; then
            restart_port_takeover "$takeover_listener" || return 1
        fi
    fi

    # If PID file stale but port has a listener, assume running and sync PID file.
    local listener
    listener="$(get_listening_pid_by_port "$PORT" || true)"
    if [ -n "$listener" ]; then
        if [ "$AGENT_TEST_INSTANCE" -eq 1 ] && ! is_von_main_process "$listener"; then
            log "Agent test mode: port $PORT is owned by non-Von PID=$listener; aborting instead of adopting it."
            return 1
        fi
        log "Port $PORT already in use by PID=$listener; assuming server already running (untracked)."
        write_pidfile "$listener"
        open_browser_if_needed
        return 0
    fi

    set_admin_token_env
    if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
        log "Agent test mode: preserving other Von server processes; only port $PORT is managed."
    else
        stop_python_processes_by_script "src/workflows/von/main.py" "Von Server"
    fi

    local py
    py="$(python_cmd)"
    local launch_mode="direct-python"
    if [ -z "$py" ]; then
        py="$(pdm_cmd)"
        launch_mode="pdm-fallback"
    fi

    local purity_script="${ROOT}/scripts/check_workflow_purity.py"
    if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
        log "Agent test mode: skipping workflow purity check."
    elif [ -f "$purity_script" ]; then
        log "Running Workflow Purity Check (warn-only)..."
        if [ "$launch_mode" = "pdm-fallback" ]; then
            "$py" run python "$purity_script" --quiet-on-pass 2>&1 | while IFS= read -r line; do log "[purity-check] $line"; done || true
        else
            "$py" "$purity_script" --quiet-on-pass 2>&1 | while IFS= read -r line; do log "[purity-check] $line"; done || true
        fi
    fi

    log "Starting Von server on port $PORT (mode=$launch_mode)..."
    rm -f "$NEW_LOG" "$SERVER_ERR_LOG" 2>/dev/null || true
    : > "$NEW_LOG" 2>/dev/null || true
    : > "$SERVER_ERR_LOG" 2>/dev/null || true

    local server_exe="$py"
    local server_args=()
    if [ "$launch_mode" = "pdm-fallback" ]; then
        server_args=(run python -u "${ROOT}/src/workflows/von/main.py" --port "$PORT")
    else
        server_args=(-u "${ROOT}/src/workflows/von/main.py" --port "$PORT")
    fi

    local detacher_py
    detacher_py="$(python_cmd)"
    local pid=""
    if [ -n "$detacher_py" ]; then
        pid="$(
            launch_detached_process \
                "$detacher_py" \
                "$NEW_LOG" \
                "$SERVER_ERR_LOG" \
                "$ROOT" \
                "$server_exe" \
                "${server_args[@]}" \
                2>>"$SERVER_ERR_LOG" \
            || true
        )"
        if ! printf '%s' "$pid" | grep -qE '^[0-9]+$'; then
            log "ERROR: Detached launcher did not return a child PID. Showing stderr:"
            show_server_start_failure_tail
            return 1
        fi
    else
        nohup "$server_exe" "${server_args[@]}" >> "$NEW_LOG" 2>> "$SERVER_ERR_LOG" &
        pid=$!
    fi
    log "Launched PID=$pid. Logs: $NEW_LOG ; stderr: $SERVER_ERR_LOG"
    write_pidfile "$pid"

    # Current log pointer (symlink preferred; copy fallback)
    rm -f "$CURRENT_LOG" 2>/dev/null || true
    ln -sf "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || cp -f "$NEW_LOG" "$CURRENT_LOG" 2>/dev/null || true

    rotate_logs "$LOG_RETENTION" || true

    if [ "$SKIP_HEALTH" -eq 1 ]; then
        log "Skipping health wait (use ./run.sh status to check)."
    else
        local max_attempts=$((HEALTH_TIMEOUT_SEC * 2))
        local attempt=0
        local healthy=0
        local listening_logged=0
        while [ "$attempt" -lt "$max_attempts" ]; do
            sleep 0.5

            if [ "$listening_logged" -eq 0 ] && ! process_exists "$pid"; then
                log "ERROR: Server process exited early before listening on port $PORT. Showing last 40 log lines:"
                local log_tail=""
                local err_tail=""
                if [ -f "$CURRENT_LOG" ]; then
                    log_tail="$(tail -n 40 "$CURRENT_LOG" 2>/dev/null || true)"
                    if [ -n "$log_tail" ]; then
                        printf '%s\n' "$log_tail"
                    fi
                fi
                if [ -f "$SERVER_ERR_LOG" ]; then
                    log "Last 40 stderr log lines:"
                    err_tail="$(tail -n 40 "$SERVER_ERR_LOG" 2>/dev/null || true)"
                    if [ -n "$err_tail" ]; then
                        printf '%s\n' "$err_tail"
                    fi
                fi

                if [ "$REPAIR_ATTEMPTED" -eq 0 ] && printf '%s\n%s' "$log_tail" "$err_tail" | grep -qE "ModuleNotFoundError|ImportError"; then
                    local repair_script="${ROOT}/setup_py.sh"
                    if [ -f "$repair_script" ]; then
                        log "Detected missing dependencies. Attempting auto-repair..."
                        REPAIR_ATTEMPTED=1
                        set +e
                        bash "$repair_script"
                        local repair_exit=$?
                        set -e
                        if [ "$repair_exit" -eq 0 ]; then
                            log "Repair completed successfully. Retrying server start..."
                            start_server
                            return 0
                        fi
                        log "Repair failed."
                    fi
                fi

                log "Aborting start. (Use -HealthDebug for verbose retries)"
                return 1
            fi

            if [ "$listening_logged" -eq 0 ] && port_listening; then
                log "Port $PORT is listening; waiting for /health..."
                listening_logged=1
            fi

            if launcher_health_ready 0; then
                healthy=1
                break
            fi
            if [ "$HEALTH_DEBUG" -eq 1 ] && [ $((attempt % 4)) -eq 0 ]; then
                log "Health not ready yet (attempt ${attempt}/${max_attempts})"
            fi
            if log_ready "$CURRENT_LOG"; then
                if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
                    if [ "$HEALTH_DEBUG" -eq 1 ]; then
                        log "Detected readiness log pattern; Agent test mode still requires /health marker read-back."
                    fi
                else
                    log "Detected readiness log pattern; marking healthy (log shortcut)."
                    healthy=1
                    break
                fi
            fi
            attempt=$((attempt + 1))
        done

        if [ "$healthy" -ne 1 ] && [ "$listening_logged" -eq 1 ] && [ "$HEALTH_GRACE_SEC" -gt 0 ]; then
            log "Extending wait up to ${HEALTH_GRACE_SEC}s for /health (phase 2)..."
            local grace_attempts=$((HEALTH_GRACE_SEC * 2))
            local g=0
            while [ "$g" -lt "$grace_attempts" ] && [ "$healthy" -ne 1 ]; do
                sleep 0.5
                if launcher_health_ready 0; then
                    healthy=1
                    break
                fi
                if [ "$HEALTH_DEBUG" -eq 1 ] && [ $((g % 10)) -eq 0 ]; then
                    log "Grace wait health not ready ($((g / 2))s/${HEALTH_GRACE_SEC}s)"
                fi
                if log_ready "$CURRENT_LOG"; then
                    if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
                        if [ "$HEALTH_DEBUG" -eq 1 ]; then
                            log "Detected readiness log pattern during grace; Agent test mode still requires /health marker read-back."
                        fi
                    else
                        log "Detected readiness log pattern during grace; marking healthy (log shortcut)."
                        healthy=1
                        break
                    fi
                fi
                if [ $((g % 10)) -eq 0 ]; then
                    log "Still waiting for /health... $((g / 2))s/${HEALTH_GRACE_SEC}s grace"
                fi
                g=$((g + 1))
            done
        fi

        if [ "$healthy" -eq 1 ] && ! confirm_health_stable; then
            healthy=0
            log "ERROR: /health read-back did not remain stable after initial readiness."
            if ! process_exists "$pid"; then
                remove_pidfile
            fi
            show_server_start_failure_tail
            return 1
        fi

        if [ "$healthy" -eq 1 ]; then
            log "Server healthy (http://localhost:$PORT)"
            open_browser_if_needed
        elif [ "$listening_logged" -eq 1 ]; then
            if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
                launcher_health_ready 1 || true
                log "ERROR: Agent test port is listening but /health marker read-back did not validate in $((HEALTH_TIMEOUT_SEC + HEALTH_GRACE_SEC))s."
                show_server_start_failure_tail
                return 1
            else
                log "WARNING: Port is listening but /health did not respond in $((HEALTH_TIMEOUT_SEC + HEALTH_GRACE_SEC))s; continuing (service may still be initialising)."
                log_mongo_status
            fi
        else
            log "WARNING: Server not healthy after initial ${HEALTH_TIMEOUT_SEC}s (port not listening); check logs: $CURRENT_LOG and $SERVER_ERR_LOG"
        fi
    fi

    # Best-effort PID refinement to the python child only when using a wrapper launch.
    if [ "$launch_mode" = "pdm-fallback" ]; then
        sleep 0.4
        refine_pid_to_child "$pid"
    fi
    sync_pidfile_to_listener

    log "Admin token file: $TOKEN_FILE"
    if [ "$SHOW_RELATION_COVERAGE" -eq 1 ]; then
        run_relation_coverage_summary || true
    fi
    if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
        log "Agent test mode: skipping startup maintenance and shared workers."
    else
        run_daily_backup_if_due || true
        run_test_db_refresh_if_due || true

        # Start RAG worker best-effort (mirrors run.ps1)
        start_rag_worker_bg || true
        # Start Concept Index worker best-effort (mirrors run.ps1)
        start_concept_index_worker_bg || true
    fi
}

stop_server() {
    local pid=""
    local graceful=0
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
                if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
                    log "Agent test mode: preserving shared RAG and concept-index workers."
                else
                    stop_rag_worker || true
                    stop_concept_index_worker || true
                fi
                return 0
            fi
            # Verify command line looks like this Von server.
            if ! is_von_main_process "$pid"; then
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
        pid="$(get_von_main_pid_by_port || true)"
    fi
    if [ -z "$pid" ]; then
        log "Not running"
        remove_pidfile
        if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
            log "Agent test mode: preserving shared RAG and concept-index workers."
        else
            stop_rag_worker || true
            stop_concept_index_worker || true
        fi
        return 0
    fi
    if ! process_exists "$pid"; then
        log "STALE: PID file exists but process missing."
        remove_pidfile
        if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
            log "Agent test mode: preserving shared RAG and concept-index workers."
        else
            stop_rag_worker || true
            stop_concept_index_worker || true
        fi
        return 0
    fi
    if ! is_von_main_process "$pid"; then
        local listener=""
        listener="$(get_listening_pid_by_port "$PORT" || true)"
        if [ -n "$listener" ] && is_von_main_process "$listener"; then
            log "PID file pointed to non-Von PID=$pid; switching to listener PID=$listener."
            pid="$listener"
            write_pidfile "$pid"
        else
            log "STALE: PID file PID=$pid does not match this Von server."
            remove_pidfile
            if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
                log "Agent test mode: preserving shared RAG and concept-index workers."
            else
                stop_rag_worker || true
                stop_concept_index_worker || true
            fi
            return 0
        fi
    fi

    local token=""
    if [ -f "$TOKEN_FILE" ]; then
        token="$(tr -d '\r\n' < "$TOKEN_FILE" 2>/dev/null || true)"
    fi
    if [ -n "$token" ] && command -v curl >/dev/null 2>&1; then
        log "Attempting graceful shutdown (PID=$pid)..."
        if curl -sS -X POST "http://localhost:${PORT}/admin/shutdown" -H "X-Admin-Token: ${token}" --max-time 5 >/dev/null 2>&1; then
            graceful=1
        fi
    else
        log "No admin token available or curl missing; skipping graceful attempt."
    fi

    local waited=0
    while [ "$waited" -lt 20 ]; do
        if ! process_exists "$pid"; then
            break
        fi
        sleep 0.5
        waited=$((waited + 1))
    done
    if process_exists "$pid"; then
        log "Process PID=$pid still running; issuing force kill..."
        if ! stop_process_tree_with_escalation "$pid"; then
            log "Force kill failed: PID=$pid remained alive after escalation."
        fi
    else
        if [ "$graceful" -eq 1 ]; then
            log "Graceful shutdown completed."
        else
            log "Process exited."
        fi
    fi

    remove_pidfile
    if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
        log "Agent test mode: preserving other Von server processes and shared RAG/concept-index workers."
    else
        stop_python_processes_by_script "src/workflows/von/main.py" "Von Server" "$pid"
        stop_rag_worker || true
        stop_concept_index_worker || true
    fi
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

run_code_mention_scan() {
    local reason="${1:-backup}"
    if is_truthy "${VON_DISABLE_CODE_MENTION_SCAN:-}"; then
        log "[code-mention-scan] disabled via VON_DISABLE_CODE_MENTION_SCAN"
        return 0
    fi
    local script_path="${ROOT}/src/utilities/scan_code_concepts.py"
    if [ ! -f "$script_path" ]; then
        log "[code-mention-scan] WARN: script missing ($script_path)"
        return 0
    fi
    local pdm
    pdm="$(pdm_cmd)"
    log "[code-mention-scan] Running scan (reason=$reason)"
    local output=""
    local exit_code=0
    set +e
    output="$("$pdm" run python "$script_path" --sync-mentions --apply 2>&1)"
    exit_code=$?
    set -e
    local raw_out="${RUN_DIR}/code_mention_scan_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    if [ "$exit_code" -eq 0 ]; then
        log "[code-mention-scan] OK"
    else
        log "[code-mention-scan] ERROR exit=$exit_code"
    fi
}

run_code_predicate_sync() {
    local reason="${1:-backup}"
    if is_truthy "${VON_DISABLE_CODE_PREDICATE_SYNC:-}"; then
        log "[code-predicate-sync] disabled via VON_DISABLE_CODE_PREDICATE_SYNC"
        return 0
    fi
    local script_path="${ROOT}/src/utilities/sync_code_predicates.py"
    if [ ! -f "$script_path" ]; then
        log "[code-predicate-sync] WARN: script missing ($script_path)"
        return 0
    fi
    local pdm
    pdm="$(pdm_cmd)"
    log "[code-predicate-sync] Running sync (reason=$reason)"
    local output=""
    local exit_code=0
    set +e
    output="$("$pdm" run python "$script_path" --retag-non-predicates 2>&1)"
    exit_code=$?
    set -e
    local raw_out="${RUN_DIR}/code_predicate_sync_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    if [ "$exit_code" -eq 0 ]; then
        log "[code-predicate-sync] OK"
    else
        log "[code-predicate-sync] ERROR exit=$exit_code"
    fi
}

cron_due() {
    local schedule="$1"
    local last_iso="$2"
    local now_iso="$3"
    local py
    py="$(python_cmd)"
    if [ -z "$py" ]; then
        return 2
    fi
    local script
    script=$'import sys,datetime,re\nschedule=sys.argv[1]\nlast_iso=sys.argv[2]\nnow_iso=sys.argv[3]\n'
    script+=$'def parse_dt(value):\n    if not value:\n        return None\n    txt=value.strip()\n    if txt.endswith("Z"):\n        txt=txt[:-1] + "+00:00"\n    try:\n        dt=datetime.datetime.fromisoformat(txt)\n    except Exception:\n        return None\n    if dt.tzinfo is None:\n        dt=dt.replace(tzinfo=datetime.timezone.utc)\n    return dt.astimezone(datetime.timezone.utc)\n'
    script+=$'def token_to_int(tok,min_v,max_v,name_map=None,is_dow=False):\n    tok=tok.strip()\n    if tok.isdigit():\n        v=int(tok)\n        if is_dow and v==7:\n            v=0\n        if v<min_v or v>max_v:\n            raise ValueError(tok)\n        return v\n    if name_map:\n        key=tok.upper()\n        if key in name_map:\n            v=int(name_map[key])\n            if is_dow and v==7:\n                v=0\n            if v<min_v or v>max_v:\n                raise ValueError(tok)\n            return v\n    raise ValueError(tok)\n'
    script+=$'def parse_field(field,min_v,max_v,name_map=None,is_dow=False):\n    allowed=[False]*(max_v+1)\n    is_star=False\n    field=field.strip()\n    if not field:\n        raise ValueError("empty")\n    if field=="*":\n        is_star=True\n        for v in range(min_v,max_v+1):\n            allowed[v]=True\n    else:\n        for part in field.split(","):\n            part=part.strip()\n            if not part:\n                continue\n            m=re.match(r"([^/]+)(?:/(\\d+))?$",part)\n            if not m:\n                raise ValueError(part)\n            base=m.group(1).strip()\n            step=int(m.group(2)) if m.group(2) else 1\n            if step<1:\n                raise ValueError(part)\n            if base=="*":\n                start=min_v\n                end=max_v\n            elif "-" in base:\n                a,b=base.split("-",1)\n                start=token_to_int(a,min_v,max_v,name_map,is_dow)\n                end=token_to_int(b,min_v,max_v,name_map,is_dow)\n            else:\n                start=token_to_int(base,min_v,max_v,name_map,is_dow)\n                end=start\n            if start<min_v or end>max_v or start>end:\n                raise ValueError(part)\n            v=start\n            while v<=end:\n                vv=v\n                if is_dow and vv==7:\n                    vv=0\n                allowed[vv]=True\n                v+=step\n    values=[i for i in range(min_v,max_v+1) if allowed[i]]\n    if not values:\n        raise ValueError("empty")\n    return {"allowed":allowed,"values":values,"is_star":is_star}\n'
    script+=$'def cron_match(cron,dt):\n    if not cron["month"]["allowed"][dt.month]:\n        return False\n    dom_ok=cron["dom"]["allowed"][dt.day]\n    dow=dt.weekday()+1\n    if dow==7:\n        dow=0\n    dow_ok=cron["dow"]["allowed"][dow]\n    if cron["dom"]["is_star"] and cron["dow"]["is_star"]:\n        day_ok=True\n    elif cron["dom"]["is_star"]:\n        day_ok=dow_ok\n    elif cron["dow"]["is_star"]:\n        day_ok=dom_ok\n    else:\n        day_ok=dom_ok or dow_ok\n    if not day_ok:\n        return False\n    if not cron["hour"]["allowed"][dt.hour]:\n        return False\n    if not cron["minute"]["allowed"][dt.minute]:\n        return False\n    return True\n'
    script+=$'tokens=schedule.strip().split()\nif len(tokens)!=5:\n    sys.exit(2)\nmonth_names={"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,"JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}\ndow_names={"SUN":0,"MON":1,"TUE":2,"WED":3,"THU":4,"FRI":5,"SAT":6}\ntry:\n    cron={\n        "minute":parse_field(tokens[0],0,59),\n        "hour":parse_field(tokens[1],0,23),\n        "dom":parse_field(tokens[2],1,31),\n        "month":parse_field(tokens[3],1,12,month_names),\n        "dow":parse_field(tokens[4],0,7,dow_names,True),\n    }\nexcept Exception:\n    sys.exit(2)\nnow=parse_dt(now_iso) or datetime.datetime.now(datetime.timezone.utc)\nlast=parse_dt(last_iso)\nif last is None:\n    last=now-datetime.timedelta(days=370)\ndt=(last.replace(second=0,microsecond=0)+datetime.timedelta(minutes=1))\nlimit=now+datetime.timedelta(days=370)\nwhile dt<=limit:\n    if cron_match(cron,dt):\n        sys.stdout.write("1" if dt<=now else "0")\n        sys.exit(0)\n    dt+=datetime.timedelta(minutes=1)\nsys.exit(2)\n'
    local out=""
    set +e
    out="$("$py" -c "$script" "$schedule" "$last_iso" "$now_iso" 2>/dev/null)"
    local exit_code=$?
    set -e
    if [ "$exit_code" -ne 0 ]; then
        return 2
    fi
    if [ "$out" = "1" ]; then
        return 0
    fi
    return 1
}

run_daily_backup_if_due() {
    if is_truthy "${VON_DISABLE_DAILY_BACKUP:-}"; then
        return 0
    fi
    local schedule="${VON_BACKUP_SCHEDULE:-}"
    if [ -n "$schedule" ]; then
        schedule="${schedule#\"}"
        schedule="${schedule%\"}"
    fi
    local interval_hours=24
    if printf '%s' "${VON_BACKUP_INTERVAL_HOURS:-}" | grep -qE '^[0-9]+$'; then
        interval_hours="${VON_BACKUP_INTERVAL_HOURS}"
    fi
    if [ "$interval_hours" -lt 1 ]; then
        interval_hours=24
    fi
    local sentinel="${RUN_DIR}/last_backup_utc.txt"
    local last=""
    if [ -f "$sentinel" ]; then
        last="$(tr -d '\r\n' < "$sentinel" 2>/dev/null || true)"
    fi
    local now
    now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    local due=1
    if [ -n "$schedule" ]; then
        if cron_due "$schedule" "$last" "$now"; then
            due=1
        else
            local cron_rc=$?
            if [ "$cron_rc" -eq 2 ]; then
                log "[daily-backup] WARN: Unsupported VON_BACKUP_SCHEDULE='${schedule}'; falling back to interval ${interval_hours}h."
                schedule=""
            else
                due=0
            fi
        fi
    fi
    if [ -z "$schedule" ]; then
        if [ -n "$last" ]; then
            local last_epoch
            last_epoch="$(parse_iso_epoch "$last")"
            if [ -n "$last_epoch" ]; then
                local now_epoch
                now_epoch="$(date -u +%s)"
                local elapsed_hours=$(( (now_epoch - last_epoch) / 3600 ))
                if [ "$elapsed_hours" -lt "$interval_hours" ]; then
                    due=0
                fi
            fi
        fi
    fi
    if [ "$due" -eq 0 ]; then
        return 0
    fi

    local pid_file="${RUN_DIR}/daily_backup.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(tr -d '\r\n' < "$pid_file" 2>/dev/null || true)"
        if [ -n "$pid" ] && process_exists "$pid"; then
            return 0
        fi
        rm -f "$pid_file" 2>/dev/null || true
    fi

    local backup_script="${ROOT}/scripts/backup_von_db.py"
    if [ ! -f "$backup_script" ]; then
        log "[daily-backup] WARN: backup script missing: $backup_script (skipping)"
        return 0
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local out_dir
    out_dir="$(resolve_backup_out_dir "$BACKUP_ROOT" "$LOCAL_BACKUPS" "daily-backup")"
    if ! backup_output_path_allowed "$out_dir" "daily-backup" 1; then
        log "[daily-backup] WARN: skipping scheduled backup until VON_BACKUP_ROOT points outside repo (or VON_ALLOW_BACKUP_IN_REPO=1)."
        return 0
    fi
    log "[daily-backup] Launching background backup (interval ${interval_hours}h)..."
    (
        set +e
        cd "$ROOT" || exit 1
        export MONGO_ALLOW_LOCAL_FALLBACK=0
        local output_log="${RUN_DIR}/daily_backup_last_output.log"
        "$pdm" run python "$backup_script" --apply --out-dir "$out_dir" --tag auto-daily >> "$output_log" 2>&1
        local exit_code=$?
        if [ "$exit_code" -ne 0 ]; then
            log "[daily-backup] ERROR exit=$exit_code"
            exit 0
        fi
        run_code_mention_scan "daily-backup"
        run_code_predicate_sync "daily-backup"
        date -u +"%Y-%m-%dT%H:%M:%SZ" > "$sentinel" 2>/dev/null || true
        log "[daily-backup] Completed."
    ) &
    printf '%s\n' "$!" > "$pid_file" 2>/dev/null || true
}

trigger_test_db_refresh() {
    local reason="${1:-scheduled}"
    if is_truthy "${VON_DISABLE_TEST_DB_REFRESH:-}"; then
        return 0
    fi
    local refresh_script="${ROOT}/scripts/maintenance/refresh_test_db.py"
    if [ ! -f "$refresh_script" ]; then
        return 0
    fi
    local pid_file="${RUN_DIR}/test_db_refresh.pid"
    if [ -f "$pid_file" ]; then
        local pid
        pid="$(tr -d '\r\n' < "$pid_file" 2>/dev/null || true)"
        if [ -n "$pid" ] && process_exists "$pid"; then
            return 0
        fi
        rm -f "$pid_file" 2>/dev/null || true
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local apply_flag=""
    if is_truthy "${VON_TEST_DB_REFRESH_APPLY:-}"; then
        apply_flag="--apply"
    fi
    log "[test-db-refresh] Launching background refresh (${reason})..."
    (
        set +e
        cd "$ROOT" || exit 1
        local output_log="${RUN_DIR}/test_db_refresh_last_output.log"
        "$pdm" run python "$refresh_script" --drop-target $apply_flag >> "$output_log" 2>&1
        local exit_code=$?
        if [ "$exit_code" -eq 0 ]; then
            date -u +"%Y-%m-%dT%H:%M:%SZ" > "${RUN_DIR}/last_test_db_refresh_utc.txt" 2>/dev/null || true
            log "[test-db-refresh] Completed."
        else
            log "[test-db-refresh] ERROR exit=$exit_code"
        fi
    ) &
    printf '%s\n' "$!" > "$pid_file" 2>/dev/null || true
}

run_test_db_refresh_if_due() {
    if is_truthy "${VON_DISABLE_TEST_DB_REFRESH:-}"; then
        return 0
    fi
    local interval_hours=4
    if printf '%s' "${VON_TEST_DB_REFRESH_INTERVAL_HOURS:-}" | grep -qE '^[0-9]+$'; then
        interval_hours="${VON_TEST_DB_REFRESH_INTERVAL_HOURS}"
    fi
    if [ "$interval_hours" -lt 1 ]; then
        interval_hours=4
    fi
    local sentinel="${RUN_DIR}/last_test_db_refresh_utc.txt"
    local last=""
    if [ -f "$sentinel" ]; then
        last="$(tr -d '\r\n' < "$sentinel" 2>/dev/null || true)"
    fi
    if [ -n "$last" ]; then
        local last_epoch
        last_epoch="$(parse_iso_epoch "$last")"
        if [ -n "$last_epoch" ]; then
            local now_epoch
            now_epoch="$(date -u +%s)"
            local elapsed_hours=$(( (now_epoch - last_epoch) / 3600 ))
            if [ "$elapsed_hours" -lt "$interval_hours" ]; then
                return 0
            fi
        fi
    fi
    local count_script="${ROOT}/scripts/maintenance/count_concepts.py"
    local refresh_script="${ROOT}/scripts/maintenance/refresh_test_db.py"
    if [ ! -f "$count_script" ] || [ ! -f "$refresh_script" ]; then
        return 0
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local count_raw=""
    set +e
    count_raw="$("$pdm" run python "$count_script" 2>/dev/null)"
    local exit_code=$?
    set -e
    if [ "$exit_code" -ne 0 ] || [ -z "$count_raw" ]; then
        return 0
    fi
    if ! printf '%s' "$count_raw" | grep -qE '^[0-9]+$'; then
        return 0
    fi
    local count="$count_raw"
    if [ "$count" -lt 1000 ]; then
        return 0
    fi
    log "[test-db-refresh] Launching background refresh (concepts=${count} interval=${interval_hours}h)..."
    trigger_test_db_refresh "scheduled"
}

run_relation_coverage_summary() {
    if is_truthy "${VON_RELATION_COVERAGE_DISABLE:-}"; then
        return 0
    fi
    local script="${ROOT}/scripts/maintenance/relation_coverage_summary.py"
    if [ ! -f "$script" ]; then
        log "[relation-coverage] script missing ($script)"
        return 0
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local test_db_count=""
    local count_script="${ROOT}/scripts/maintenance/count_concepts.py"
    if [ -f "$count_script" ]; then
        set +e
        test_db_count="$(VON_DB_NAME=test_von_db "$pdm" run python "$count_script" 2>/dev/null)"
        set -e
        if ! printf '%s' "$test_db_count" | grep -qE '^[0-9]+$'; then
            test_db_count=""
        fi
    fi
    local output=""
    local exit_code=0
    set +e
    output="$("$pdm" run python "$script" 2>&1)"
    exit_code=$?
    set -e
    if [ "$exit_code" -ne 0 ]; then
        log "[relation-coverage] ERROR exit=$exit_code"
        return 0
    fi
    local line
    while IFS= read -r line; do
        if printf '%s' "$line" | grep -q '^Total concepts:'; then
            if [ -n "$test_db_count" ]; then
                log "[relation-coverage] ${line} | test_db=${test_db_count}"
                if ! is_truthy "${VON_DISABLE_TEST_DB_REFRESH:-}"; then
                    local primary_count=""
                    primary_count="$(printf '%s' "$line" | grep -Eo '[0-9]+' | head -n 1 || true)"
                    if [ -n "$primary_count" ] && [ "$test_db_count" -lt "$primary_count" ]; then
                        trigger_test_db_refresh "mismatch"
                    fi
                fi
            else
                log "[relation-coverage] ${line}"
            fi
        elif printf '%s' "$line" | grep -qE '^(predicate|---)'; then
            log "[relation-coverage] ${line}"
        elif printf '%s' "$line" | grep -qE '^(hasDescription|hasNote|hasContent)'; then
            log "[relation-coverage] ${line}"
        fi
    done <<<"$output"
}

run_residual_legacy_text_audit() {
    if is_truthy "${VON_RESIDUAL_TEXT_AUDIT_DISABLE:-}"; then
        return 0
    fi
    local interval_hours=24
    local force=0
    if is_truthy "${VON_RESIDUAL_TEXT_AUDIT_FORCE:-}"; then
        force=1
    fi
    local sentinel="${RUN_DIR}/last_residual_text_audit.txt"
    if ! should_run_interval "$sentinel" "$interval_hours" "$force"; then
        return 0
    fi
    local script="${ROOT}/scripts/maintenance/audit_residual_preserved_fields.py"
    if [ ! -f "$script" ]; then
        log "[residual-text-audit] script missing ($script)"
        return 0
    fi
    log "[residual-text-audit] Running residual legacy text audit (interval ${interval_hours}h force=$force)"
    local pdm
    pdm="$(pdm_cmd)"
    local output=""
    local exit_code=0
    set +e
    output="$("$pdm" run python "$script" 2>&1)"
    exit_code=$?
    set -e
    local raw_out="${RUN_DIR}/residual_text_audit_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    if [ "$exit_code" -eq 0 ]; then
        date -u +"%Y-%m-%dT%H:%M:%SZ" > "$sentinel" 2>/dev/null || true
        log "[residual-text-audit] OK"
    elif [ "$exit_code" -eq 2 ]; then
        log "[residual-text-audit] VIOLATION: residual legacy text fields detected see $raw_out"
    else
        log "[residual-text-audit] ERROR exit=$exit_code see $raw_out"
    fi
}

run_cleanup_preserved_fields() {
    if is_truthy "${VON_CLEANUP_PRESERVED_DISABLE:-}"; then
        return 0
    fi
    local interval_hours=24
    local force=0
    if is_truthy "${VON_CLEANUP_PRESERVED_FORCE:-}"; then
        force=1
    fi
    local execute=0
    if is_truthy "${VON_CLEANUP_PRESERVED_EXECUTE:-}"; then
        execute=1
    fi
    local sentinel="${RUN_DIR}/last_cleanup_preserved_fields.txt"
    if ! should_run_interval "$sentinel" "$interval_hours" "$force"; then
        return 0
    fi
    local script="${ROOT}/scripts/maintenance/cleanup_preserved_fields.py"
    if [ ! -f "$script" ]; then
        log "[cleanup-preserved-fields] script missing ($script)"
        return 0
    fi
    local mode="dry-run"
    if [ "$execute" -eq 1 ]; then
        mode="execute"
    fi
    log "[cleanup-preserved-fields] Running cleanup (mode=$mode interval ${interval_hours}h force=$force)"
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
    local raw_out="${RUN_DIR}/cleanup_preserved_fields_last_output.log"
    printf '%s\n' "$output" > "$raw_out" 2>/dev/null || true
    if [ "$exit_code" -eq 0 ]; then
        date -u +"%Y-%m-%dT%H:%M:%SZ" > "$sentinel" 2>/dev/null || true
        if [ "$execute" -eq 1 ]; then
            log "[cleanup-preserved-fields] OK executed"
        else
            log "[cleanup-preserved-fields] OK no legacy fields detected"
        fi
    elif [ "$exit_code" -eq 2 ] && [ "$execute" -eq 0 ]; then
        log "[cleanup-preserved-fields] DETECTED legacy preserved_fields enable VON_CLEANUP_PRESERVED_EXECUTE=1 to migrate. See $raw_out"
    else
        log "[cleanup-preserved-fields] ERROR exit=$exit_code see $raw_out"
    fi
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
        if is_truthy "${VON_GOV_SCAN_DEBUG:-}"; then
            local line
            while IFS= read -r line; do
                log "[governance-scan][debug] $line"
            done <<<"$output"
        fi
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
    sync_pidfile_to_listener
    local run_maintenance=0
    if [ "$STATUS_RUN_MAINTENANCE" -eq 1 ] || is_truthy "${VON_STATUS_RUN_MAINTENANCE:-}"; then
        run_maintenance=1
    fi
    local pid
    pid="$(get_pid || true)"
    if [ -n "$pid" ] && process_exists "$pid" && ! is_von_main_process "$pid"; then
        local listener=""
        listener="$(get_listening_pid_by_port "$PORT" || true)"
        if [ -n "$listener" ] && is_von_main_process "$listener"; then
            write_pidfile "$listener"
            pid="$listener"
            log "Synchronized PID file to listener PID=${listener}"
        else
            pid=""
        fi
    fi
    if [ -n "$pid" ] && process_exists "$pid"; then
        local uptime=""
        uptime="$(pidfile_uptime_minutes || true)"
        if health_ok; then
            if [ -n "$uptime" ]; then
                log "RUNNING PID=$pid Uptime=${uptime} Healthy=true"
            else
                log "RUNNING PID=$pid Healthy=true"
            fi
        else
            if [ -n "$uptime" ]; then
                log "RUNNING PID=$pid Uptime=${uptime} Healthy=false"
            else
                log "RUNNING PID=$pid Healthy=false"
            fi
        fi
        log_mongo_status
        log "Log: $CURRENT_LOG"
        if [ "$run_maintenance" -eq 1 ]; then
            run_governance_scan || true
            run_predicate_verify || true
            run_residual_legacy_text_audit || true
            run_cleanup_preserved_fields || true
            run_relation_alias_audit || true
            run_relation_alias_cleanup || true
        elif [ "$HEALTH_DEBUG" -eq 1 ]; then
            log "Status maintenance checks skipped (set -StatusRunMaintenance or VON_STATUS_RUN_MAINTENANCE=1 to enable)."
        fi
        if [ "$SHOW_RELATION_COVERAGE" -eq 1 ]; then
            run_relation_coverage_summary || true
        fi
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
    sync_pidfile_to_listener
    local pid="$(get_pid || true)"
    if [ -z "$pid" ] || ! process_exists "$pid"; then
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
    if ! is_truthy "${VON_ENABLE_BACKUP_ACTION:-}"; then
        log "[backup] ERROR: manual backup action is disabled by default. Set VON_ENABLE_BACKUP_ACTION=1 to acknowledge admin-level access and enable this action."
        return 1
    fi

    local backup_script="${ROOT}/scripts/backup_von_db.py"
    if [ ! -f "$backup_script" ]; then
        log "[backup] ERROR: backup script missing: $backup_script"
        return 1
    fi
    local pdm
    pdm="$(pdm_cmd)"
    local out_dir="$BACKUP_OUT_DIR"
    if [ -z "$out_dir" ]; then
        out_dir="$BACKUP_ROOT"
    fi
    out_dir="$(resolve_backup_out_dir "$out_dir" "$LOCAL_BACKUPS" "manual-backup")"
    local mode="apply"
    if [ "$BACKUP_DRY_RUN" -eq 1 ]; then
        mode="dry-run"
    fi
    local apply_mode=1
    if [ "$BACKUP_DRY_RUN" -eq 1 ]; then
        apply_mode=0
    fi
    if ! backup_output_path_allowed "$out_dir" "backup" "$apply_mode"; then
        return 1
    fi
    log "[backup] Starting backup (mode=$mode tag=$BACKUP_TAG out=$out_dir)"
    if [ "$BACKUP_DRY_RUN" -eq 1 ]; then
        "$pdm" run python "$backup_script" --out-dir "$out_dir" --tag "$BACKUP_TAG"
    else
        "$pdm" run python "$backup_script" --apply --out-dir "$out_dir" --tag "$BACKUP_TAG"
    fi
    local backup_exit=$?
    if [ "$backup_exit" -eq 0 ]; then
        log "[backup] OK"
        run_code_mention_scan "manual-backup"
        run_code_predicate_sync "manual-backup"
        if [ "$AGENT_TEST_INSTANCE" -eq 1 ]; then
            log "[backup-migrate] disabled via -AgentTest"
        elif [ "$NO_BACKUP_MIGRATE" -eq 0 ]; then
            migrate_local_backups
        else
            log "[backup-migrate] disabled via -NoBackupMigrate"
        fi
    else
        log "[backup] ERROR exit=$backup_exit"
    fi
    return "$backup_exit"
}

run_restore_backup() {
    if ! is_truthy "${VON_ENABLE_RESTORE_ACTION:-}"; then
        log "[restore-backup] ERROR: restore action is disabled by default. Set VON_ENABLE_RESTORE_ACTION=1 to acknowledge destructive restore risk and enable this action."
        return 1
    fi
    if [ -z "$RESTORE_BACKUP_PATH" ]; then
        log "[restore-backup] ERROR: specify -RestoreBackupPath with a backup artefact or receipt path."
        return 1
    fi

    local restore_script="${ROOT}/scripts/restore_von_db.py"
    if [ ! -f "$restore_script" ]; then
        log "[restore-backup] ERROR: restore script missing: $restore_script"
        return 1
    fi

    local pdm
    pdm="$(pdm_cmd)"
    local args=(run python "$restore_script" --backup-path "$RESTORE_BACKUP_PATH")
    if [ -n "$RESTORE_TARGET_DB_NAME" ]; then
        args+=(--target-db-name "$RESTORE_TARGET_DB_NAME")
    fi
    if [ "$RESTORE_DROP_TARGET" -eq 1 ]; then
        args+=(--drop-target)
    fi
    if [ "$RESTORE_APPLY" -eq 1 ]; then
        args+=(--apply)
    fi

    local mode="dry-run"
    if [ "$RESTORE_APPLY" -eq 1 ]; then
        mode="apply"
    fi
    local drop="false"
    if [ "$RESTORE_DROP_TARGET" -eq 1 ]; then
        drop="true"
    fi
    local target_summary="<auto restore probe>"
    if [ -n "$RESTORE_TARGET_DB_NAME" ]; then
        target_summary="$RESTORE_TARGET_DB_NAME"
    fi

    log "[restore-backup] Starting restore (mode=$mode backup=$RESTORE_BACKUP_PATH target_db=$target_summary drop_target=$drop)"
    "$pdm" "${args[@]}"
    local restore_exit=$?
    if [ "$restore_exit" -eq 0 ]; then
        log "[restore-backup] OK"
    else
        log "[restore-backup] ERROR exit=$restore_exit"
    fi
    return "$restore_exit"
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
            if [ -z "$running_pid" ] || ! process_exists "$running_pid"; then
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
    Actions: start | foreground | stop | status | restart | logs | check | backup | restore-backup | autoupdate | rag-worker | help
    Options:
        -Port <int>            Server port (default 5001 on macOS, 5000 elsewhere; -AgentTest defaults to 5010)
        -AgentTest             Isolated coding-agent test instance mode
        -IsolatedTestInstance  Alias for -AgentTest
        -NoBrowser
        -ForceBrowser
        -ChromeBeta
        -Tail <n>
        -Follow
        -LogRetention <n>
        -AdminToken <token>
        -SkipHealth
        -HealthTimeoutSec <n>
        -HealthGraceSec <n>
        -ReadyLogPatterns <p>
        -DisableLogReady
        -HealthDebug
        -StatusRunMaintenance
        -ShowRelationCoverage
        -NoBackupMigrate

    Backup options:
        -BackupDryRun
        -BackupTag <tag>
        -BackupOutDir <path>

    Restore options:
        -RestoreBackupPath <path>
        -RestoreTargetDbName <name>
        -RestoreApply
        -RestoreDropTarget

    Backup safety environment variables:
        VON_ENABLE_BACKUP_ACTION=1
        VON_ENABLE_RESTORE_ACTION=1
        VON_ALLOW_BACKUP_IN_REPO=1

    Autoupdate options:
        -UpdateIntervalMinutes <n>
        -UpdateBranch <name>
        -UpdateNoRestartIfRunning

    Examples:
        ./run.sh start
        ./run.sh restart -ForceBrowser
        ./run.sh restart -AgentTest -HealthTimeoutSec 180
        ./run.sh logs -Tail 200 -Follow
        ./run.sh backup -BackupDryRun
        ./run.sh restore-backup -RestoreBackupPath /path/to/last_successful_backup_receipt.json
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
        start_server 1
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
    restore-backup)
        run_restore_backup
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
