# Inspect VS Code (Windows) user settings + global storage for MCP/Copilot/tool configuration.
# Read-only: does not modify any files.
# NZ English spelling.

[CmdletBinding()]
param(
    [switch]$IncludeStable,
    [switch]$ShowNonMatchingGlobalStorageDirs,
    [switch]$ShowSelectedToolsSummary,
    [int]$MaxGlobalStorageFilesToScan = 200,
    [int]$MaxFileBytesToScan = 5MB
)

$ErrorActionPreference = 'Stop'

function Write-Section([string]$title) {
    Write-Output ""
    Write-Output "=== $title ==="
}

function Format-RedactedLine([string]$line) {
    # If line looks like:  "some.key": "value",
    # then redact the string value only.
    if ($line -match '^\s*"[^"]+"\s*:\s*"') {
        return ($line -replace '(^\s*"[^"]+"\s*:\s*)".*?"', '$1"<redacted>"')
    }
    return $line
}

function Format-RedactedText([string]$text) {
    if (-not $text) { return $text }

    # Redact likely tokens/keys: long-ish base64/url-safe strings or hex blobs.
    $t = $text
    $t = [regex]::Replace($t, '([A-Za-z0-9_\-]{24,})', '<redacted>')
    $t = [regex]::Replace($t, '([A-Fa-f0-9]{32,})', '<redacted>')
    return $t
}

function Get-VSCodeProfiles() {
    $profiles = @(
        @{ Name = 'Insiders'; Root = Join-Path $env:APPDATA 'Code - Insiders\User' },
        @{ Name = 'Stable'; Root = Join-Path $env:APPDATA 'Code\User' }
    )

    if (-not $IncludeStable) {
        return $profiles | Where-Object { $_.Name -eq 'Insiders' }
    }

    return $profiles
}

function Write-SettingsMatches([string]$settingsPath) {
    if (-not (Test-Path $settingsPath)) {
        Write-Output "settings.json not found: $settingsPath"
        return
    }

    $item = Get-Item $settingsPath
    Write-Output "settings.json: $settingsPath"
    Write-Output ("LastWriteTime: {0:o}" -f $item.LastWriteTime)

    $keyRegex = '^\s*"(chat\.mcp|chat\.tools|github\.copilot|copilot\.|mcp\.)'
    $lines = Get-Content -LiteralPath $settingsPath

    $settingMatches = for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $keyRegex) {
            [PSCustomObject]@{ LineNumber = $i + 1; Line = (Format-RedactedLine $lines[$i]) }
        }
    }

    if (-not $settingMatches) {
        Write-Output "No matching MCP/Copilot/tool settings found in settings.json."
        return
    }

    Write-Output "Matching lines (string values redacted):"
    $settingMatches | ForEach-Object {
        Write-Output ("{0,5}: {1}" -f $_.LineNumber, $_.Line)
    }
}

function Find-InterestingGlobalStorageDirs([string]$globalStoragePath) {
    if (-not (Test-Path $globalStoragePath)) {
        Write-Output "globalStorage not found: $globalStoragePath"
        return @()
    }

    $dirs = Get-ChildItem -LiteralPath $globalStoragePath -Directory -ErrorAction SilentlyContinue

    $interesting = $dirs | Where-Object {
        $_.Name -match '(?i)(mcp|copilot|github|atlassian|chat|agent|tool)'
    }

    if ($ShowNonMatchingGlobalStorageDirs) {
        Write-Output "All globalStorage directories:"
        $dirs | Sort-Object Name | ForEach-Object { Write-Output ("- {0}" -f $_.Name) }
    }

    Write-Output "Potentially relevant globalStorage directories:"
    if (-not $interesting) {
        Write-Output "(none matched heuristic)"
    }
    else {
        $interesting | Sort-Object Name | ForEach-Object { Write-Output ("- {0}" -f $_.Name) }
    }

    return $interesting
}

function Invoke-StateDbQuery([string]$dbPath) {
    if (-not (Test-Path $dbPath)) {
        Write-Output "state DB not found: $dbPath"
        return
    }

    Write-Output "state DB: $dbPath"
    $dbItem = Get-Item $dbPath
    Write-Output ("LastWriteTime: {0:o}" -f $dbItem.LastWriteTime)

    $sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
    if (-not $sqlite) {
        Write-Output "sqlite3 not found on PATH; cannot query state DB automatically."
        $python = Get-Command python -ErrorAction SilentlyContinue
        if ($python) {
            Write-Output "python detected: $($python.Source)"
            Write-Output "Querying state DB via Python's built-in sqlite3 (keys only; no values dumped)..."

            $py = @'
import sqlite3
import sys
import json
import re

db_path = sys.argv[1]
show_selected = (len(sys.argv) >= 3 and sys.argv[2].lower() == "true")

TOOLISH_RE = re.compile(r"[\w]+[\w\-]*([\.:/\-])[\w\-]+")

def is_toolish_string(s: str) -> bool:
    if not isinstance(s, str):
        return False
    if len(s) == 0 or len(s) > 200:
        return False
    if any(c.isspace() for c in s):
        return False
    return TOOLISH_RE.search(s) is not None

def extract_tool_names_from_cache(obj):
    """Best-effort extraction of tool ids/names from cached tool registries.

    We deliberately avoid printing raw cache values. This returns a set of
    tool-like strings.
    """
    out = set()

    def walk(v):
        if isinstance(v, dict):
            # Common tool shapes: {"name": "...", "description": "...", "inputSchema": {...}}
            name = v.get("name")
            if is_toolish_string(name) and ("inputSchema" in v or "description" in v or "parameters" in v):
                out.add(name)

            for vv in v.values():
                walk(vv)
        elif isinstance(v, list):
            for vv in v:
                walk(vv)
        elif isinstance(v, str):
            # As a last resort, treat tool-ish strings as candidates.
            if is_toolish_string(v):
                out.add(v)

    walk(obj)
    return out

def tool_prefix(tool_id: str) -> str:
    if not tool_id:
        return "<unknown>"
    for sep in ("_", ":"):
        if sep in tool_id:
            return tool_id.split(sep, 1)[0]
    if "." in tool_id:
        return tool_id.split(".", 1)[0]
    return "<other>"

def get_tables(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    return [r[0] for r in cur.fetchall()]

def main():
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = get_tables(conn)
        if not tables:
            print("Could not read tables from state DB (possibly locked).")
            return 0

        print("Tables:")
        for t in tables:
            print(f"- {t}")

        if "ItemTable" not in tables:
            print("No ItemTable table found; schema differs from expected.")
            return 0

        q = """
SELECT key, length(value)
FROM ItemTable
WHERE key LIKE '%mcp%'
   OR key LIKE '%copilot%'
   OR key LIKE '%chat.mcp%'
   OR key LIKE '%tool%'
ORDER BY key
LIMIT 200;
"""
        cur = conn.cursor()
        cur.execute(q)
        rows = cur.fetchall()
        if not rows:
            print("No matching keys found in ItemTable (or DB is locked).")
            return 0

        print("Matching keys in ItemTable (key | value_length):")
        for k, ln in rows:
            print(f"- {k} | {ln}")

        # This is commonly what the UI is showing; print all keys containing 'selectedTools'.
        cur.execute("SELECT key, length(value) FROM ItemTable WHERE key LIKE '%selectedTools%' ORDER BY key;")
        st_rows = cur.fetchall()
        if st_rows:
            print("\nKeys containing 'selectedTools' (key | value_length):")
            for k, ln in st_rows:
                print(f"- {k} | {ln}")

        if show_selected:
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = 'chat/selectedTools' LIMIT 1;")
            row = cur.fetchone()
            if not row or row[0] is None:
                print("\nSelected tools summary: chat/selectedTools not found.")
                return 0

            raw = row[0]
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8", errors="replace")
                except Exception:
                    raw = str(raw)

            print("\nSelected tools summary (from chat/selectedTools):")
            try:
                data = json.loads(raw)
            except Exception:
                print(f"- Could not parse JSON (length={len(raw)})")
                return 0

            def redact_text(s: str) -> str:
                if not s:
                    return s
                # Avoid over-redacting tool ids (which usually contain separators).
                if s.startswith("secret://"):
                    return "secret://<redacted>"

                has_separators = any(ch in s for ch in ("/", ".", ":", "-"))
                if not has_separators:
                    if re.fullmatch(r"[A-Za-z0-9_\-]{32,}", s) or re.fullmatch(r"[A-Fa-f0-9]{32,}", s):
                        return "<redacted>"
                return s

            def describe_value(v):
                if isinstance(v, dict):
                    return f"dict({len(v)})"
                if isinstance(v, list):
                    return f"list({len(v)})"
                if isinstance(v, str):
                    return f"str({len(v)})"
                if v is None:
                    return "null"
                return type(v).__name__

            def structural_summary(obj):
                if isinstance(obj, dict):
                    keys = list(obj.keys())
                    print(f"- Top-level: dict with {len(keys)} keys")
                    for k in keys[:30]:
                        try:
                            print(f"  - {k}: {describe_value(obj.get(k))}")
                        except Exception:
                            print(f"  - {k}: <unavailable>")
                    if len(keys) > 30:
                        print(f"  - ... ({len(keys) - 30} more keys)")
                elif isinstance(obj, list):
                    print(f"- Top-level: list with {len(obj)} items")
                    for i, item in enumerate(obj[:10]):
                        print(f"  - [{i}]: {describe_value(item)}")
                    if len(obj) > 10:
                        print(f"  - ... ({len(obj) - 10} more items)")
                else:
                    print(f"- Top-level: {describe_value(obj)}")

            # Always show a small structural summary to understand the shape
            # of chat/selectedTools across VS Code/Copilot versions.
            print("- Structure summary:")
            structural_summary(data)

            # If this is the newer structured format, show the entry shapes
            # (keys/types only; no values) and any obvious selection flags.
            if isinstance(data, dict):
                tse = data.get("toolSetEntries")
                te = data.get("toolEntries")

                def count_enabled_pair_entries(entries):
                    if not isinstance(entries, list):
                        return 0
                    count = 0
                    for e in entries:
                        if isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str):
                            if e[1] is True:
                                count += 1
                    return count

                def list_enabled_pair_ids(entries, limit=30):
                    if not isinstance(entries, list):
                        return []
                    out = []
                    for e in entries:
                        if isinstance(e, list) and len(e) >= 2 and isinstance(e[0], str):
                            if e[1] is True:
                                out.append(e[0])
                                if len(out) >= limit:
                                    break
                    return out

                def entry_shape(label, entries):
                    if not isinstance(entries, list) or not entries:
                        return
                    first = entries[0]
                    if isinstance(first, dict):
                        print(f"- {label} entry keys (first item):")
                        for k in sorted(first.keys()):
                            v = first.get(k)
                            t = type(v).__name__
                            print(f"  - {k}: {t}")

                        # Flag detection: how many entries appear enabled/selected.
                        flag_keys = ["enabled", "selected", "checked", "isEnabled", "isSelected"]
                        for fk in flag_keys:
                            if fk in first:
                                try:
                                    enabled_count = sum(1 for e in entries if isinstance(e, dict) and e.get(fk) is True)
                                    print(f"  - {fk}=true count: {enabled_count}")
                                except Exception:
                                    pass
                        return

                    if isinstance(first, list):
                        print(f"- {label} entry type: list (len={len(first)})")
                        # Print a few positions to identify which slot is tool id / enabled flag.
                        for i, v in enumerate(first[:12]):
                            t = type(v).__name__
                            extra = ""
                            if isinstance(v, str):
                                vv = redact_text(v)
                                if len(vv) > 160:
                                    vv = vv[:160] + "..."
                                extra = f" value={vv}"
                            print(f"  - [{i}]: {t}{extra}")
                        return

                    print(f"- {label} entry type: {type(first).__name__}")
                    return

                entry_shape("toolSetEntries", tse)
                entry_shape("toolEntries", te)

                if isinstance(tse, list):
                    enabled_tse = count_enabled_pair_entries(tse)
                    print(f"- toolSetEntries: total={len(tse)}, enabled={enabled_tse}")
                    sample = list_enabled_pair_ids(tse, limit=20)
                    if sample:
                        print("  - enabled toolSetEntries (sample):")
                        for s in sample:
                            print(f"    - {redact_text(s)}")

                if isinstance(te, list):
                    enabled_te = count_enabled_pair_entries(te)
                    print(f"- toolEntries: total={len(te)}, enabled={enabled_te}")
                    sample = list_enabled_pair_ids(te, limit=30)
                    if sample:
                        print("  - enabled toolEntries (sample):")
                        for s in sample:
                            print(f"    - {redact_text(s)}")

            # Extract tool-like ids.
            tool_ids = []

            def maybe_add_tool(s):
                if not isinstance(s, str):
                    return
                if len(s) == 0 or len(s) > 200:
                    return
                if any(c.isspace() for c in s):
                    return
                # Heuristic: IDs often contain separators.
                if not any(ch in s for ch in ("/", ".", ":", "-")):
                    return
                tool_ids.append(s)

            def walk(obj):
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if k in ("id", "toolId", "tool", "name", "toolName", "command"):
                            maybe_add_tool(v)
                        walk(v)
                elif isinstance(obj, list):
                    for v in obj:
                        walk(v)
                elif isinstance(obj, str):
                    # As a last resort, consider raw strings.
                    maybe_add_tool(obj)

            walk(data)

            # De-dupe while preserving order.
            seen = set()
            uniq = []
            for t in tool_ids:
                if t not in seen:
                    seen.add(t)
                    uniq.append(t)

            if uniq:
                print(f"- Tool-ish id count (heuristic): {len(uniq)}")
                for t in uniq[:200]:
                    print(f"- {redact_text(t)}")
                if len(uniq) > 200:
                    print(f"- ... ({len(uniq) - 200} more)")
            else:
                print("- Could not extract tool ids with current heuristics.")
                structural_summary(data)
                snippet = redact_text(raw[:800])
                print(f"- Redacted JSON snippet (first 800 chars): {snippet}")

            # Summarise cached tool registries (helps explain UI counts that
            # expand toolsets/MCP servers into many tools).
            print("\nTool cache summary:")
            cur = conn.cursor()
            cur.execute("SELECT value FROM ItemTable WHERE key = 'mcpToolCache' LIMIT 1;")
            cache_row = cur.fetchone()
            if not cache_row or cache_row[0] is None:
                print("- mcpToolCache not found")
            else:
                cache_raw = cache_row[0]
                if isinstance(cache_raw, bytes):
                    cache_raw = cache_raw.decode("utf-8", errors="replace")

                try:
                    cache_data = json.loads(cache_raw)
                    tool_names = extract_tool_names_from_cache(cache_data)
                    print(f"- mcpToolCache JSON parsed (length={len(cache_raw)})")
                    print(f"- Unique tool-ish names found: {len(tool_names)}")
                    # Group by prefix for quick insight (mcp, github-pull-request, azure, etc.).
                    groups = {}
                    for tn in tool_names:
                        p = tool_prefix(tn)
                        groups[p] = groups.get(p, 0) + 1
                    top = sorted(groups.items(), key=lambda kv: kv[1], reverse=True)[:15]
                    print("- Top prefixes:")
                    for p, cnt in top:
                        print(f"  - {p}: {cnt}")
                except Exception as e:
                    print(f"- Could not parse mcpToolCache as JSON (length={len(cache_raw)}): {type(e).__name__}")

            # Detect other blobs that may contain a selectedTools structure.
            # We only report the key name and a boolean indicator.
            for candidate_key in ("GitHub.copilot-chat", "GitHub.copilot", "mcpInputs"):
                try:
                    cur.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1;", (candidate_key,))
                    r = cur.fetchone()
                    if not r or r[0] is None:
                        continue
                    blob = r[0]
                    if isinstance(blob, bytes):
                        blob = blob.decode("utf-8", errors="replace")
                    contains = ("selectedTools" in blob)
                    print(f"- {candidate_key} contains 'selectedTools': {str(contains).lower()}")
                except Exception:
                    # Ignore any unexpected encoding/database errors.
                    pass

        print("\nNote: tool enablement is often stored here, but the key names vary by VS Code/Copilot versions.")
        print("This script only lists likely keys; it does not edit anything.")
        return 0
    finally:
        conn.close()

if __name__ == "__main__":
    sys.exit(main())
'@

            try {
                $tmpPyPath = Join-Path $env:TEMP ("inspect_vscode_state_{0}.py" -f ([guid]::NewGuid().ToString('N')))
                Set-Content -LiteralPath $tmpPyPath -Value $py -Encoding UTF8
                & $python.Source $tmpPyPath $dbPath ([string]$ShowSelectedToolsSummary)
                Remove-Item -LiteralPath $tmpPyPath -Force -ErrorAction SilentlyContinue
            }
            catch {
                Write-Output "Python sqlite3 query failed (DB may be locked or Python not usable): $($_.Exception.Message)"
            }
            return
        }

        Write-Output "If you install sqlite3, you can run queries like:"
        Write-Output ('  sqlite3 "{0}" ".tables"' -f $dbPath)
        Write-Output ('  sqlite3 "{0}" "SELECT key, length(value) FROM ItemTable WHERE key LIKE ''%mcp%'' OR key LIKE ''%copilot%'' LIMIT 50;"' -f $dbPath)
        return
    }

    Write-Output "sqlite3 detected: $($sqlite.Source)"

    # Discover tables first.
    $tables = & sqlite3 $dbPath "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;" 2>$null
    if (-not $tables) {
        Write-Output "Could not read tables from state DB (possibly locked)."
        return
    }

    Write-Output "Tables:"
    ($tables -split "`n") | Where-Object { $_.Trim() } | ForEach-Object { Write-Output "- $_" }

    if ($tables -notmatch 'ItemTable') {
        Write-Output "No ItemTable table found; schema differs from expected."
        return
    }

    # Query keys only, do not dump values.
    $q = @'
SELECT key, length(value)
FROM ItemTable
WHERE key LIKE '%mcp%'
   OR key LIKE '%copilot%'
   OR key LIKE '%chat.mcp%'
   OR key LIKE '%tool%'
ORDER BY key
LIMIT 200;
'@

    $raw = & sqlite3 $dbPath $q 2>$null
    if (-not $raw) {
        Write-Output "No matching keys found in ItemTable (or DB is locked)."
        return
    }

    Write-Output "Matching keys in ItemTable (key | value_length):"
    ($raw -split "`n") | Where-Object { $_.Trim() } | ForEach-Object {
        $line = $_.Trim()
        # sqlite3 default separator is '|'
        $parts = $line -split '\|', 2
        if ($parts.Count -eq 2) {
            $k = $parts[0]
            $len = $parts[1]
            Write-Output ("- {0} | {1}" -f $k, $len)
        }
        else {
            Write-Output ("- $line")
        }
    }

    Write-Output ""
    Write-Output "Note: tool enablement is often stored here, but the key names vary by VS Code/Copilot versions."
    Write-Output "This script only lists likely keys; it does not edit anything."
}

function Invoke-GlobalStorageTextScan([string]$globalStoragePath) {
    if (-not (Test-Path $globalStoragePath)) {
        return
    }

    Write-Output "Scanning a limited number of small text files for MCP/Copilot-related mentions (read-only)..."

    $files = Get-ChildItem -LiteralPath $globalStoragePath -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Length -le $MaxFileBytesToScan -and
        $_.Extension -match '^(\.json|\.txt|\.log|\.md)$'
    } |
    Select-Object -First $MaxGlobalStorageFilesToScan

    if (-not $files) {
        Write-Output "No suitable small text files found to scan."
        return
    }

    # Keep patterns specific; avoid extremely broad matches like 'tool' which produce a lot of noise.
    $patterns = @('mcp', 'copilot', 'chat.mcp', 'selectedTools', 'enabledTools', 'toolSelection')

    foreach ($f in $files) {
        try {
            $content = Get-Content -LiteralPath $f.FullName -Raw -ErrorAction Stop
        }
        catch {
            continue
        }

        $hit = $false
        foreach ($p in $patterns) {
            if ($content -match [regex]::Escape($p)) {
                $hit = $true
                break
            }
        }

        if ($hit) {
            $rel = $f.FullName.Replace($globalStoragePath, '<globalStorage>')
            Write-Output ("- hit: $rel")

            # Print up to 3 redacted sample lines.
            $sample = $content -split "`n" | Where-Object { $_ -match '(?i)(mcp|copilot|selectedtools|toolselection|enabledtools)' } | Select-Object -First 3
            foreach ($s in $sample) {
                $line = (Format-RedactedText $s.Trim())
                if ($line.Length -gt 240) {
                    $line = $line.Substring(0, 240) + ' ...'
                }
                Write-Output ("    " + $line)
            }
        }
    }
}

Write-Section 'VS Code MCP/Copilot settings + storage inspection'
Write-Output "Computer: $env:COMPUTERNAME"
Write-Output "User: $env:USERNAME"
Write-Output "Date: $(Get-Date -Format o)"

foreach ($vscodeProfile in (Get-VSCodeProfiles)) {
    Write-Section "Profile: $($vscodeProfile.Name)"

    $settingsPath = Join-Path $vscodeProfile.Root 'settings.json'
    Write-SettingsMatches $settingsPath

    $globalStoragePath = Join-Path $vscodeProfile.Root 'globalStorage'
    Write-Output ""
    Write-Output "globalStorage: $globalStoragePath"
    Find-InterestingGlobalStorageDirs $globalStoragePath | Out-Null

    Write-Output ""
    Invoke-StateDbQuery (Join-Path $globalStoragePath 'state.vscdb')

    Write-Output ""
    Invoke-GlobalStorageTextScan $globalStoragePath
}
