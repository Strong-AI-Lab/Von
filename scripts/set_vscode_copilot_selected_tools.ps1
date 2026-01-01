# Updates VS Code Copilot "selected tools" for a profile by editing state.vscdb.
# NZ English spelling.
#
# Safety:
# - Makes a timestamped backup of state.vscdb by default.
# - Dry-run by default (no DB writes).
# - Requires VS Code for the target profile to be closed (DB lock otherwise).

[CmdletBinding()]
param(
    [ValidateSet('Insiders', 'Stable')]
    [string]$VSCodeProfile = 'Insiders',

    # Convenience presets to avoid manual allowlists.
    # - minimal-von: Von MCP servers only (vontology + vonrag) plus built-in chat toolsets.
    # - dev-von: minimal-von + Atlassian + GitHub (MCP configs) + GitHub PR extension tools.
    # - broad-von: dev-von + MongoDB + arXiv MCP configs (still a small explicit selection).
    # - broad-useful: Keep toolsets + GitHub PR extension + ALL MCP configs + ALL selected MCP tools, capped by MaxTools.
    [ValidateSet('minimal-von', 'dev-von', 'broad-von', 'broad-useful')]
    [string]$Preset = 'broad-von',

    # Optional: provide your own allowlist (JSON array of strings).
    [string]$AllowlistJson,

    # Mode controls:
    # - By default this script runs in dry-run mode (no DB writes).
    # - Use -Apply to write changes.
    # - -DryRun is accepted for compatibility, but is parsed manually because PowerShell's
    #   argument binder can be fussy when invoking scripts via powershell.exe -File.
    [switch]$Apply,
    [object]$DryRun,
    [switch]$NoBackup,
    [switch]$IncludeWebSearch,

    # Optional: remove (not just disable) any non-allowed tool entries.
    # This may help if VS Code/Copilot re-enables previously-known tools on startup.
    [switch]$Compact,

    # Optional: change VS Code Settings Sync state for this profile.
    # This is mainly for diagnosing whether Sync is overwriting chat/selectedTools.
    [ValidateSet('Unchanged', 'Disable', 'Enable')]
    [string]$UserDataSync = 'Unchanged',

    # Optional: write a full debug dump (kept/removed lists, counts) to this path.
    # Useful when the console output truncation ("... (N more)") is not enough.
    [string]$DumpPath,

    # Copilot hard limit is 128 tools; stay below to leave headroom.
    [ValidateRange(10, 200)]
    [int]$MaxTools = 120
)

$ErrorActionPreference = 'Stop'

# NOTE: Apply mode must not run inside the VS Code integrated terminal.
# VS Code keeps chat tool selection in memory and may overwrite state.vscdb on shutdown,
# which makes DB edits appear to "not stick".
if ($Apply) {
    $isInVSCodeTerminal = $false
    if ($env:TERM_PROGRAM -eq 'vscode') { $isInVSCodeTerminal = $true }
    if ($env:VSCODE_PID) { $isInVSCodeTerminal = $true }

    if ($isInVSCodeTerminal) {
        throw "Refusing to run in apply mode from the VS Code integrated terminal. Close VS Code for the selected profile, then run this script from an external PowerShell window (or run with -DryRun 1 inside VS Code)."
    }
}

function ConvertTo-BooleanOrNull([object]$value) {
    if ($null -eq $value) {
        return $null
    }

    if ($value -is [bool]) {
        return [bool]$value
    }

    if ($value -is [int] -or $value -is [long]) {
        if ([int]$value -eq 0) { return $false }
        if ([int]$value -eq 1) { return $true }
        throw "DryRun numeric values must be 0 or 1. Got: $value"
    }

    $text = [string]$value
    $text = $text.Trim()
    if ($text.StartsWith('$')) {
        $text = $text.Substring(1)
    }

    switch ($text.ToLowerInvariant()) {
        'true' { return $true }
        'false' { return $false }
        '0' { return $false }
        '1' { return $true }
        default {
            throw "DryRun must be true/false (or 1/0). Got: '$value'"
        }
    }
}

function Get-ProfileRoot([string]$profileName) {
    switch ($profileName) {
        'Insiders' { return Join-Path $env:APPDATA 'Code - Insiders\User' }
        'Stable' { return Join-Path $env:APPDATA 'Code\User' }
        default { throw "Unsupported profile: $profileName" }
    }
}

function Assert-VSCodeNotRunning([string]$profileName) {
    # VS Code keeps chat tool selection in memory and may overwrite state.vscdb on shutdown.
    # To make DB edits stick, VS Code must be fully closed.
    $processPattern = switch ($profileName) {
        'Insiders' { '*Insiders*' }
        'Stable' { 'Code' }
        default { '*' }
    }

    $running = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -like $processPattern -and $_.ProcessName -like 'Code*' }

    if ($running) {
        $names = ($running | Select-Object -ExpandProperty ProcessName -Unique | Sort-Object)
        throw "VS Code appears to be running ($($names -join ', ')). Close VS Code for profile '$profileName' and re-run with -Apply. If it is stuck, end the process(es) in Task Manager first."
    }
}

$profileRoot = Get-ProfileRoot $VSCodeProfile
$globalStorage = Join-Path $profileRoot 'globalStorage'
$dbPath = Join-Path $globalStorage 'state.vscdb'

if (-not (Test-Path $dbPath)) {
    throw "state.vscdb not found: $dbPath"
}

$allowIds = @()
$allowPrefixes = @()

$dryRunOverride = $null
if ($PSBoundParameters.ContainsKey('DryRun')) {
    $dryRunOverride = ConvertTo-BooleanOrNull $DryRun
}

# Effective mode:
# - explicit -DryRun wins
# - else -Apply implies not dry-run
# - else default is dry-run
$effectiveDryRun = $true
if ($null -ne $dryRunOverride) {
    $effectiveDryRun = $dryRunOverride
}
elseif ($Apply) {
    $effectiveDryRun = $false
}

if ($Apply -and (-not $effectiveDryRun)) {
    Assert-VSCodeNotRunning $VSCodeProfile
}

# Always keep the core chat toolsets.
$allowIds += @(
    'custom-agent',
    'toolset:GitHub.copilot-chat/edit',
    'toolset:GitHub.copilot-chat/search',
    'toolset:GitHub.copilot-chat/web'
)

# Keep a small, explicit core set of tool entries that support normal coding
# workflows (read/search/edit files, run tasks/tests, basic terminal access).
# This helps avoid accidentally disabling essential tools when pruning MCP
# and extension tool catalogues.
$allowIds += @(
    'copilot_readFile',
    'copilot_findFiles',
    'copilot_listDirectory',
    'copilot_findTextInFiles',
    'copilot_searchCodebase',
    'copilot_listCodeUsages',
    'copilot_getErrors',
    'copilot_getChangedFiles',
    'copilot_getSearchResults',
    'copilot_createDirectory',
    'copilot_createFile',
    'copilot_editFiles',
    'run_in_terminal',
    'get_terminal_output',
    'terminal_last_command',
    'terminal_selection',
    'run_task',
    'get_task_output',
    'create_and_run_task',
    'runTests',
    'manage_todo_list',
    'copilot_getProjectSetupInfo'
)

if ($Preset -eq 'minimal-von') {
    $allowIds += @(
        'mcp.config.ws0.vontology',
        'mcp.config.ws0.vonrag'
    )
}

if ($Preset -eq 'dev-von') {
    $allowIds += @(
        'mcp.config.ws0.vontology',
        'mcp.config.ws0.vonrag',
        'mcp.config.usrlocal.atlassian',
        'mcp.config.usrlocal.github',
        # GitHub Pull Request extension tools (smaller surface than GitHub MCP tool catalogues)
        'github-pull-request_activePullRequest',
        'github-pull-request_copilot-coding-agent',
        'github-pull-request_doSearch',
        'github-pull-request_issue_fetch',
        'github-pull-request_openPullRequest',
        'github-pull-request_renderIssues',
        'github-pull-request_formSearchQuery',
        'github-pull-request_suggest-fix'
    )
}

if ($Preset -eq 'broad-von') {
    $allowIds += @(
        'mcp.config.ws0.vontology',
        'mcp.config.ws0.vonrag',
        'mcp.config.ws0.arxiv',
        'mcp.config.usrlocal.atlassian',
        'mcp.config.usrlocal.github',
        'mcp.config.usrlocal.mongodb',
        # Curated Atlassian (Jira) MCP tools
        'mcp_atlassian_getAccessibleAtlassianResources',
        'mcp_atlassian_getVisibleJiraProjects',
        'mcp_atlassian_searchJiraIssuesUsingJql',
        'mcp_atlassian_getJiraIssue',
        'mcp_atlassian_createJiraIssue',
        'mcp_atlassian_editJiraIssue',
        'mcp_atlassian_transitionJiraIssue',
        'mcp_atlassian_addCommentToJiraIssue',
        'mcp_atlassian_addWorklogToJiraIssue',
        'mcp_atlassian_lookupJiraAccountId',
        # Curated GitHub MCP tools
        'mcp_github_search_pull_requests',
        'mcp_github_push_files',
        'mcp_github_create_or_update_file',
        'mcp_github_pull_request_review_write',
        'mcp_github_assign_copilot_to_issue',
        # GitHub Pull Request extension tools (smaller surface than GitHub MCP tool catalogues)
        'github-pull-request_activePullRequest',
        'github-pull-request_copilot-coding-agent',
        'github-pull-request_doSearch',
        'github-pull-request_issue_fetch',
        'github-pull-request_openPullRequest',
        'github-pull-request_renderIssues',
        'github-pull-request_formSearchQuery',
        'github-pull-request_suggest-fix'
    )

    # Keep Von tool families broadly; external MCPs are curated via explicit ids.
    $allowPrefixes += @(
        'mcp_vontology_',
        'mcp_vonrag_'
    )
}

if ($Preset -eq 'broad-useful') {
    # Keep all MCP servers (configs) and any already-selected MCP tools.
    # This keeps capability broad while still allowing us to cap total tools.
    $allowPrefixes += @(
        'mcp.config.',
        'mcp_',
        'github-pull-request_'
    )
}

if ($IncludeWebSearch) {
    $allowIds += 'vscode-websearchforcopilot_webSearch'
}

if ($AllowlistJson) {
    try {
        $custom = $AllowlistJson | ConvertFrom-Json
    }
    catch {
        throw "AllowlistJson must be valid JSON (array of strings). Error: $($_.Exception.Message)"
    }

    if (-not ($custom -is [System.Collections.IEnumerable])) {
        throw "AllowlistJson must decode to an array."
    }

    $allowIds = @()
    $allowPrefixes = @()
    foreach ($item in $custom) {
        if ($item -isnot [string]) {
            throw "AllowlistJson must contain only strings. Got: $($item.GetType().FullName)"
        }
        $allowIds += $item
    }
}

# De-dupe allowlists preserving order.
$seenIds = [System.Collections.Generic.HashSet[string]]::new()
$allowIds = $allowIds | Where-Object { $seenIds.Add($_) }

$seenPrefixes = [System.Collections.Generic.HashSet[string]]::new()
$allowPrefixes = $allowPrefixes | Where-Object { $seenPrefixes.Add($_) }

Write-Output "=== VS Code Copilot selected tools update ==="
Write-Output "Profile: $VSCodeProfile"
Write-Output "Preset: $Preset"
Write-Output "Default preset: broad-von"
Write-Output "DB: $dbPath"
Write-Output "DryRun: $effectiveDryRun"
Write-Output "Allow ids: $($allowIds.Count)"
Write-Output "Allow prefixes: $($allowPrefixes.Count)"
Write-Output "MaxTools cap: $MaxTools"

$noArgs = ($PSBoundParameters.Count -eq 0)
if ($effectiveDryRun) {
    Write-Output "NOTE: This run is a dry-run (default). No changes will be written to VS Code's state database."
    if ($noArgs) {
        Write-Output "Tip: re-run with -Apply to apply changes (with VS Code closed for the selected profile)."
    }

    Write-Output "Usage examples:"
    Write-Output ('- Dry-run (explicit): powershell -NoProfile -ExecutionPolicy Bypass -File "{0}" -VSCodeProfile {1} -Preset {2} -DryRun 1' -f $PSCommandPath, $VSCodeProfile, $Preset)
    Write-Output ('- Apply (recommended): powershell -NoProfile -ExecutionPolicy Bypass -File "{0}" -VSCodeProfile {1} -Preset {2} -Apply' -f $PSCommandPath, $VSCodeProfile, $Preset)
    Write-Output "  (Close VS Code for that profile first, otherwise state.vscdb may be locked.)"

    $applyArgs = @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', ('"{0}"' -f $PSCommandPath),
        '-VSCodeProfile', $VSCodeProfile,
        '-Preset', $Preset,
        '-MaxTools', $MaxTools,
        '-Apply'
    )
    if ($IncludeWebSearch) { $applyArgs += '-IncludeWebSearch' }
    if ($NoBackup) { $applyArgs += '-NoBackup' }
    if ($Compact) { $applyArgs += '-Compact' }
    if ($UserDataSync -ne 'Unchanged') { $applyArgs += @('-UserDataSync', $UserDataSync) }
    if ($AllowlistJson) { $applyArgs += @('-AllowlistJson', ('"{0}"' -f $AllowlistJson.Replace('"', '\"'))) }

    Write-Output "Apply command:"
    Write-Output ("powershell {0}" -f ($applyArgs -join ' '))
    Write-Output ""
}
else {
    Write-Output "APPLY MODE: This run will update VS Code's state database (close VS Code for the selected profile first)."
}

if ((-not $NoBackup) -and (-not $effectiveDryRun)) {
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backupPath = "$dbPath.bak_$stamp"
    Copy-Item -LiteralPath $dbPath -Destination $backupPath -Force
    Write-Output "Backup: $backupPath"
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "python not found on PATH; needed to edit/query sqlite. Activate your venv or install Python."
}

$allowSpec = @{
    ids            = $allowIds
    prefixes       = $allowPrefixes
    max_tools      = $MaxTools
    dump_path      = $DumpPath
    compact        = [bool]$Compact
    user_data_sync = $UserDataSync
}

$allowJson = $allowSpec | ConvertTo-Json -Compress

$py = @'
import json
import re
import sqlite3
import sys

DB = sys.argv[1]
with open(sys.argv[2], "r", encoding="utf-8-sig") as f:
    allow_spec = json.load(f)

ALLOW_ID_LIST = list(allow_spec.get("ids") or [])
ALLOW_IDS = set(ALLOW_ID_LIST)
ALLOW_PREFIXES = list(allow_spec.get("prefixes") or [])
MAX_TOOLS = int(allow_spec.get("max_tools") or 120)
DUMP_PATH = allow_spec.get("dump_path") or None
COMPACT = bool(allow_spec.get("compact") or False)
USER_DATA_SYNC = (allow_spec.get("user_data_sync") or "Unchanged")

DRY_RUN = sys.argv[3].lower() == "true"

TOOLISH_RE = re.compile(r"[\w]+[\w\-]*([\.:/\-])[\w\-]+")

def is_toolish_string(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if len(s) == 0:
        return False
    if any(c.isspace() for c in s):
        return False
    return TOOLISH_RE.search(s) is not None

def is_allowed_tool_id(tool_id: str) -> bool:
    if tool_id in ALLOW_IDS:
        return True
    for p in ALLOW_PREFIXES:
        if tool_id.startswith(p):
            return True
    return False

KEY_FIELDS = {"id", "toolId", "tool", "name", "toolName", "command"}

def filter_obj(obj):
    # Returns (new_obj, changed)
    if isinstance(obj, list):
        new_list = []
        changed = False
        for item in obj:
            if isinstance(item, str) and is_toolish_string(item):
                if is_allowed_tool_id(item):
                    new_list.append(item)
                else:
                    changed = True
                continue

            if isinstance(item, dict):
                # If dict has an explicit id field, filter at that level.
                id_val = None
                for k in KEY_FIELDS:
                    v = item.get(k)
                    if isinstance(v, str) and is_toolish_string(v):
                        id_val = v
                        break
                if id_val is not None and not is_allowed_tool_id(id_val):
                    changed = True
                    continue

                new_item, item_changed = filter_obj(item)
                new_list.append(new_item)
                changed = changed or item_changed
                continue

            new_item, item_changed = filter_obj(item)
            new_list.append(new_item)
            changed = changed or item_changed

        return new_list, changed

    if isinstance(obj, dict):
        new_dict = {}
        changed = False
        for k, v in obj.items():
            if k in KEY_FIELDS and isinstance(v, str) and is_toolish_string(v):
                if is_allowed_tool_id(v):
                    new_dict[k] = v
                else:
                    changed = True
                continue

            new_v, v_changed = filter_obj(v)
            new_dict[k] = new_v
            changed = changed or v_changed

        return new_dict, changed

    return obj, False

def collect_toolish(obj, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in KEY_FIELDS and isinstance(v, str) and is_toolish_string(v):
                out.add(v)
            collect_toolish(v, out)
    elif isinstance(obj, list):
        for v in obj:
            collect_toolish(v, out)
    elif isinstance(obj, str):
        if is_toolish_string(obj):
            out.add(obj)

def ensure_allow_ids_present(obj):
    """Best-effort: ensure explicit allowlisted ids are present in the selection.

    VS Code/Copilot currently stores chat/selectedTools in at least two shapes:
    - Flat list of tool ids (strings)
    - Structured dict with 'toolSetEntries'/'toolEntries' as [id, enabled] pairs

    We add missing ids only when we can do so safely.
    """
    changed = False

    def ensure_in_flat_list(lst):
        nonlocal changed
        existing = set([x for x in lst if isinstance(x, str)])
        for tid in ALLOW_ID_LIST:
            if tid not in existing:
                lst.append(tid)
                existing.add(tid)
                changed = True

    def ensure_in_pair_lists(toolset_entries, tool_entries):
        nonlocal changed
        existing_toolsets = set(
            [e[0] for e in toolset_entries if isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str)]
        )
        existing_tools = set(
            [e[0] for e in tool_entries if isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str)]
        )

        def should_be_toolset_id(tid: str) -> bool:
            return tid == "custom-agent" or tid.startswith("toolset:") or tid.startswith("mcp.config.")

        for tid in ALLOW_ID_LIST:
            if tid in existing_toolsets or tid in existing_tools:
                continue
            if should_be_toolset_id(tid):
                toolset_entries.append([tid, True])
                existing_toolsets.add(tid)
            else:
                tool_entries.append([tid, True])
                existing_tools.add(tid)
            changed = True

    if isinstance(obj, list):
        ensure_in_flat_list(obj)
        return obj, changed

    if isinstance(obj, dict):
        # Structured shape.
        tse = obj.get("toolSetEntries")
        te = obj.get("toolEntries")
        if isinstance(tse, list) and isinstance(te, list):
            ensure_in_pair_lists(tse, te)
            obj["toolSetEntries"] = tse
            obj["toolEntries"] = te
            return obj, changed

        # Flat-but-nested shape.
        for k in ("selectedTools", "tools", "toolIds", "items"):
            v = obj.get(k)
            if isinstance(v, list):
                ensure_in_flat_list(v)
                obj[k] = v
                return obj, changed
        return obj, changed

    return obj, False

def is_selected_tools_structured(obj) -> bool:
    return (
        isinstance(obj, dict)
        and isinstance(obj.get("toolSetEntries"), list)
        and isinstance(obj.get("toolEntries"), list)
    )

def iter_enabled_ids_from_pairs(entries):
    for e in entries:
        if not (isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str)):
            continue
        if e[1] is True:
            yield e[0]

def count_enabled_true_flags(entries):
    count = 0
    ids = []
    for e in entries:
        if not (isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str)):
            continue
        if e[1] is True:
            count += 1
            ids.append(e[0])
    return count, ids

def apply_allowlist_to_pair_entries(entries):
    """Mutates entries in-place. Returns True if any enabled flags changed."""
    changed = False
    for e in entries:
        if not (isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str)):
            continue
        tool_id = e[0]
        current = e[1] is True
        desired = is_allowed_tool_id(tool_id)
        if current != desired:
            e[1] = bool(desired)
            changed = True
    return changed

def compact_pair_entries(entries):
    """Return a filtered list containing only allowed ids."""
    if not isinstance(entries, list):
        return entries
    out = []
    for e in entries:
        if not (isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str)):
            continue
        if is_allowed_tool_id(e[0]):
            out.append([e[0], True])
    return out

def main():
    conn = sqlite3.connect(DB)
    try:
        cur = conn.cursor()

        # Surface Settings Sync state: it can overwrite extension/global state on startup.
        try:
            cur.execute("SELECT value FROM ItemTable WHERE key = 'sync.enable' LIMIT 1;")
            sync_row = cur.fetchone()
            sync_enabled = None
            if sync_row and sync_row[0] is not None:
                sync_enabled = (sync_row[0] == "true" or sync_row[0] is True)
        except Exception:
            sync_enabled = None

        print(f"Settings Sync enabled (sync.enable): {sync_enabled}")

        if USER_DATA_SYNC in ("Disable", "Enable"):
            desired = "true" if USER_DATA_SYNC == "Enable" else "false"
            if DRY_RUN:
                print(f"Dry-run: would set sync.enable to {desired}.")
            else:
                cur.execute(
                    "UPDATE ItemTable SET value = ? WHERE key = 'sync.enable';",
                    (desired,),
                )
                conn.commit()
                print(f"Updated sync.enable to {desired}.")
        elif USER_DATA_SYNC != "Unchanged":
            print(f"WARNING: Unrecognised UserDataSync value: {USER_DATA_SYNC!r}. No sync change applied.")

        cur.execute("SELECT value FROM ItemTable WHERE key = 'chat/selectedTools' LIMIT 1;")
        row = cur.fetchone()
        if not row:
            print("chat/selectedTools not found; nothing to do")
            return 0

        raw = row[0]
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")

        data = json.loads(raw)

        # Newer VS Code/Copilot stores selection as [id, enabled] pairs.
        if is_selected_tools_structured(data):
            toolset_entries = data.get("toolSetEntries")
            tool_entries = data.get("toolEntries")

            before_toolsets_true, before_toolset_ids = count_enabled_true_flags(toolset_entries)
            before_tools_true, before_tool_ids = count_enabled_true_flags(tool_entries)
            before_toolsets_total = len(toolset_entries) if isinstance(toolset_entries, list) else 0
            before_tools_total = len(tool_entries) if isinstance(tool_entries, list) else 0
            before_enabled_flags = before_toolsets_true + before_tools_true
            before_unique_ids = len(set(before_toolset_ids + before_tool_ids))

            # Ensure required allowlisted ids exist (so presets can add missing
            # server config entries rather than only pruning).
            data, ensured_changed = ensure_allow_ids_present(data)

            # Apply allowlist by toggling enabled flags.
            changed = ensured_changed

            if COMPACT:
                data["toolSetEntries"] = compact_pair_entries(data.get("toolSetEntries"))
                data["toolEntries"] = compact_pair_entries(data.get("toolEntries"))
                changed = True
            else:
                changed = apply_allowlist_to_pair_entries(data.get("toolSetEntries")) or changed
                changed = apply_allowlist_to_pair_entries(data.get("toolEntries")) or changed

            after_toolsets_true, after_toolset_ids = count_enabled_true_flags(data.get("toolSetEntries"))
            after_tools_true, after_tool_ids = count_enabled_true_flags(data.get("toolEntries"))
            after_toolsets_total = len(data.get("toolSetEntries")) if isinstance(data.get("toolSetEntries"), list) else 0
            after_tools_total = len(data.get("toolEntries")) if isinstance(data.get("toolEntries"), list) else 0
            after_enabled_flags = after_toolsets_true + after_tools_true
            after_unique_ids = len(set(after_toolset_ids + after_tool_ids))

            # VS Code's UI typically reports the number of enabled tool entries (often excluding toolsets).
            # We report both, because toolsets and tools are stored separately.
            print(
                f"Enabled flags before: {before_enabled_flags} (toolsets={before_toolsets_true}/{before_toolsets_total}, tools={before_tools_true}/{before_tools_total}); unique ids={before_unique_ids}"
            )
            print(
                f"Enabled flags after:  {after_enabled_flags} (toolsets={after_toolsets_true}/{after_toolsets_total}, tools={after_tools_true}/{after_tools_total}); unique ids={after_unique_ids}"
            )
            if after_enabled_flags > MAX_TOOLS:
                print(
                    f"Refusing to proceed: enabled tool flags would be {after_enabled_flags} which exceeds MaxTools={MAX_TOOLS}."
                )
                print("Tighten the preset/allowlist or increase MaxTools.")
                return 2

            removed = sorted(set(before_toolset_ids + before_tool_ids) - set(after_toolset_ids + after_tool_ids))
            kept = sorted(set(after_toolset_ids + after_tool_ids))
            new_data = data

        else:
            before = set()
            collect_toolish(data, before)

            new_data, changed = filter_obj(data)

            # Ensure required allowlisted ids exist (so presets can add missing
            # server config entries rather than only pruning).
            new_data, ensured_changed = ensure_allow_ids_present(new_data)
            changed = changed or ensured_changed

            after = set()
            collect_toolish(new_data, after)

            print(f"Tool-ish ids before: {len(before)}")
            print(f"Tool-ish ids after:  {len(after)}")
            if len(after) > MAX_TOOLS:
                print(
                    f"Refusing to proceed: selected tools would be {len(after)} which exceeds MaxTools={MAX_TOOLS}."
                )
                print("Tighten the preset/allowlist or increase MaxTools.")
                return 2
            removed = sorted(before - after)
            kept = sorted(after)

        # Print a small summary (no secrets expected here).
        print("Kept (first 50):")
        for t in kept[:50]:
            print(f"- {t}")
        if len(kept) > 50:
            print(f"- ... ({len(kept) - 50} more)")

        print("Removed (first 50):")
        for t in removed[:50]:
            print(f"- {t}")
        if len(removed) > 50:
            print(f"- ... ({len(removed) - 50} more)")

        if DUMP_PATH:
            dump = {
                "db": DB,
                "dry_run": DRY_RUN,
                "max_tools": MAX_TOOLS,
                "allow_ids": ALLOW_ID_LIST,
                "allow_prefixes": ALLOW_PREFIXES,
                "kept": kept,
                "removed": removed,
            }
            try:
                with open(DUMP_PATH, "w", encoding="utf-8") as f:
                    json.dump(dump, f, ensure_ascii=False, indent=2)
                print(f"Wrote dump: {DUMP_PATH}")
            except Exception as e:
                print(f"WARNING: Failed to write dump to {DUMP_PATH}: {e}")

        if not changed:
            print("No changes needed (already matches allowlist).")
            return 0

        if DRY_RUN:
            print("Dry-run: not writing DB.")
            return 0

        new_raw = json.dumps(new_data, ensure_ascii=False, separators=(",", ":"))
        cur.execute(
            "UPDATE ItemTable SET value = ? WHERE key = 'chat/selectedTools';",
            (new_raw,),
        )
        conn.commit()
        print("Updated chat/selectedTools.")
        return 0
    finally:
        conn.close()

if __name__ == "__main__":
    sys.exit(main())
'@

try {
    $tmpPyPath = Join-Path $env:TEMP ("vscode_selected_tools_{0}.py" -f ([guid]::NewGuid().ToString('N')))
    $tmpAllowPath = Join-Path $env:TEMP ("vscode_selected_tools_allow_{0}.json" -f ([guid]::NewGuid().ToString('N')))
    Set-Content -LiteralPath $tmpPyPath -Value $py -Encoding UTF8
    Set-Content -LiteralPath $tmpAllowPath -Value $allowJson -Encoding UTF8
    & $python.Source $tmpPyPath $dbPath $tmpAllowPath ([string]$effectiveDryRun)
    Remove-Item -LiteralPath $tmpPyPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tmpAllowPath -Force -ErrorAction SilentlyContinue
}
catch {
    throw "Failed to update state.vscdb. Ensure VS Code is closed for profile '$VSCodeProfile'. Error: $($_.Exception.Message)"
}
