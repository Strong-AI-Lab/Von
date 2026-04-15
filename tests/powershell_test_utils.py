from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

import pytest

from scripts.powershell_probe_support import (
    DEFAULT_POWERSHELL_PROBE_TIMEOUT_SECONDS,
    cleanup_powershell_probe_dir,
)


POWERSHELL_EXE = shutil.which("powershell") or shutil.which("pwsh")
SAFE_RESULT_FIELD_RE = re.compile(r"^[A-Za-z0-9_]+$")


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_powershell_result(
    *,
    repo_root: Path,
    script: str,
    result_variable: str = "result",
    timeout_seconds: float = DEFAULT_POWERSHELL_PROBE_TIMEOUT_SECONDS,
) -> tuple[dict[str, Any], subprocess.CompletedProcess[str]]:
    if not POWERSHELL_EXE:
        pytest.skip("PowerShell is required for run.ps1 launcher-path tests")

    temp_root = Path(mkdtemp(prefix="run_ps1_probe_"))
    result_dir = temp_root / "result"
    manifest_path = result_dir / "manifest.tsv"
    stage_path = temp_root / "stage.txt"
    script_path = temp_root / "probe.ps1"
    result_variable_name = result_variable.strip() or "result"
    wrapped_script = f"""
$ErrorActionPreference = 'Stop'
$script:TestResultDir = {ps_quote(str(result_dir))}
$script:TestResultManifestPath = Join-Path $script:TestResultDir 'manifest.tsv'
$script:TestStagePath = {ps_quote(str(stage_path))}
$script:Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force -Path $script:TestResultDir | Out-Null
function global:Write-TestStage {{
    param([Parameter(Mandatory = $true)][string]$Stage)
    [System.IO.File]::AppendAllText($script:TestStagePath, $Stage + [Environment]::NewLine, $script:Utf8NoBom)
}}
function global:Assert-TestResultFieldName {{
    param([Parameter(Mandatory = $true)][string]$Name)
    if ($Name -notmatch '^[A-Za-z0-9_]+$') {{
        throw "Unsupported test-result field name '$Name'. Use only letters, numbers, and underscores."
    }}
}}
function global:Add-TestResultManifestEntry {{
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Kind,
        [string]$RelativePath = ''
    )
    Assert-TestResultFieldName -Name $Name
    [System.IO.File]::AppendAllText(
        $script:TestResultManifestPath,
        "$Name`t$Kind`t$RelativePath" + [Environment]::NewLine,
        $script:Utf8NoBom
    )
}}
function global:Write-TestResultString {{
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][string]$Value
    )
    if ($null -eq $Value) {{
        Write-TestResultNull -Name $Name
        return
    }}
    $relativePath = "$Name.string.txt"
    $fieldPath = Join-Path $script:TestResultDir $relativePath
    [System.IO.File]::WriteAllText($fieldPath, $Value, $script:Utf8NoBom)
    Add-TestResultManifestEntry -Name $Name -Kind 'string' -RelativePath $relativePath
}}
function global:Write-TestResultLines {{
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [string[]]$Values = @()
    )
    $relativePath = "$Name.lines.txt"
    $fieldPath = Join-Path $script:TestResultDir $relativePath
    $text = [System.String]::Join([Environment]::NewLine, @($Values))
    [System.IO.File]::WriteAllText($fieldPath, $text, $script:Utf8NoBom)
    Add-TestResultManifestEntry -Name $Name -Kind 'lines' -RelativePath $relativePath
}}
function global:Write-TestResultBool {{
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Value
    )
    $relativePath = "$Name.bool.txt"
    $fieldPath = Join-Path $script:TestResultDir $relativePath
    [System.IO.File]::WriteAllText($fieldPath, ([string]$Value).ToLowerInvariant(), $script:Utf8NoBom)
    Add-TestResultManifestEntry -Name $Name -Kind 'bool' -RelativePath $relativePath
}}
function global:Write-TestResultNumber {{
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)]$Value
    )
    $relativePath = "$Name.number.txt"
    $fieldPath = Join-Path $script:TestResultDir $relativePath
    if ($Value -is [System.IFormattable]) {{
        $text = $Value.ToString($null, [System.Globalization.CultureInfo]::InvariantCulture)
    }}
    else {{
        $text = [string]$Value
    }}
    [System.IO.File]::WriteAllText($fieldPath, $text, $script:Utf8NoBom)
    Add-TestResultManifestEntry -Name $Name -Kind 'number' -RelativePath $relativePath
}}
function global:Write-TestResultNull {{
    param([Parameter(Mandatory = $true)][string]$Name)
    Add-TestResultManifestEntry -Name $Name -Kind 'null'
}}
function global:Write-TestResultProjection {{
    param([Parameter(Mandatory = $true)]$Value)
    if ($null -eq $Value) {{
        throw 'PowerShell test result projection cannot serialise a null root object.'
    }}

    $fields = @()
    if ($Value -is [System.Collections.IDictionary]) {{
        foreach ($key in $Value.Keys) {{
            $fields += [pscustomobject]@{{ Name = [string]$key; Value = $Value[$key] }}
        }}
    }}
    else {{
        $fields = @($Value.PSObject.Properties | ForEach-Object {{
            [pscustomobject]@{{ Name = [string]$_.Name; Value = $_.Value }}
        }})
    }}

    foreach ($field in $fields) {{
        $name = [string]$field.Name
        $fieldValue = $field.Value
        if ($null -eq $fieldValue) {{
            Write-TestResultNull -Name $name
            continue
        }}
        if ($fieldValue -is [string] -or $fieldValue -is [char]) {{
            Write-TestResultString -Name $name -Value ([string]$fieldValue)
            continue
        }}
        if ($fieldValue -is [bool]) {{
            Write-TestResultBool -Name $name -Value ([bool]$fieldValue)
            continue
        }}
        if ($fieldValue -is [byte] -or $fieldValue -is [sbyte] -or $fieldValue -is [int16] -or $fieldValue -is [uint16] -or $fieldValue -is [int32] -or $fieldValue -is [uint32] -or $fieldValue -is [int64] -or $fieldValue -is [uint64] -or $fieldValue -is [single] -or $fieldValue -is [double] -or $fieldValue -is [decimal]) {{
            Write-TestResultNumber -Name $name -Value $fieldValue
            continue
        }}
        if (
            $fieldValue -is [System.Collections.IEnumerable] -and
            -not ($fieldValue -is [string]) -and
            -not ($fieldValue -is [System.Collections.IDictionary]) -and
            -not ($fieldValue -is [pscustomobject])
        ) {{
            $lines = New-Object System.Collections.Generic.List[string]
            foreach ($item in $fieldValue) {{
                if ($null -eq $item) {{
                    $lines.Add('') | Out-Null
                }}
                elseif (
                    $item -is [System.Collections.IDictionary] -or
                    $item -is [pscustomobject]
                ) {{
                    throw "Unsupported nested structured item in field '$name'. Convert it to raw text or explicit scalar fields before returning it."
                }}
                else {{
                    $lines.Add([string]$item) | Out-Null
                }}
            }}
            Write-TestResultLines -Name $name -Values $lines.ToArray()
            continue
        }}

        throw "Unsupported test-result field '$name' of type '$($fieldValue.GetType().FullName)'. Convert it to plain text, scalar fields, or string arrays before returning it."
    }}
}}
Write-TestStage 'wrapper-start'
try {{
    Write-TestStage 'before-user-script'
{script}
    Write-TestStage 'after-user-script'
}}
catch {{
    Write-TestStage ('probe-error:' + $_.Exception.Message)
    throw
}}
if (-not (Test-Path -LiteralPath $script:TestResultManifestPath)) {{
    if (Get-Variable -Name {result_variable_name} -ErrorAction SilentlyContinue) {{
        Write-TestStage 'before-projection'
        Write-TestResultProjection -Value (Get-Variable -Name {result_variable_name}).Value
        Write-TestStage 'after-projection'
    }}
    else {{
        throw "PowerShell test script did not populate `${result_variable_name}` or write explicit test-result fields."
    }}
}}
Write-TestStage 'probe-complete'
""".strip()
    script_path.write_text(wrapped_script, encoding="utf-8")

    cleaned_probe_dir = False
    try:
        completed = subprocess.run(
            [
                POWERSHELL_EXE,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup = cleanup_powershell_probe_dir(temp_root, timeout_seconds=0.5)
        cleaned_probe_dir = True
        stage_trace = stage_path.read_text(encoding="utf-8") if stage_path.exists() else ""
        raise AssertionError(
            "PowerShell probe timed out.\n"
            f"timeout_seconds={timeout_seconds}\n"
            f"stage_trace:\n{stage_trace}\n"
            f"killed_pids={cleanup['killed_pids']}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        cleanup_powershell_probe_dir(temp_root, timeout_seconds=0.5)
        cleaned_probe_dir = True
        stage_trace = stage_path.read_text(encoding="utf-8") if stage_path.exists() else ""
        raise AssertionError(
            "PowerShell test script failed.\n"
            f"stage_trace:\n{stage_trace}\n"
            f"stdout:\n{exc.stdout}\n\nstderr:\n{exc.stderr}"
        ) from exc

    try:
        if not manifest_path.exists():
            raise AssertionError(
                "PowerShell test script completed without writing a result manifest.\n"
                f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}"
            )

        payload: dict[str, Any] = {}
        for raw_line in manifest_path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                name, kind, relative_path = raw_line.split("\t")
            except ValueError as exc:
                raise AssertionError(
                    f"Malformed PowerShell result manifest line: {raw_line!r}"
                ) from exc
            if not SAFE_RESULT_FIELD_RE.fullmatch(name):
                raise AssertionError(f"Unsupported field name in result manifest: {name!r}")

            field_path = result_dir / relative_path if relative_path else None
            if kind == "null":
                payload[name] = None
            elif kind == "string":
                if not field_path:
                    raise AssertionError(
                        f"String field {name!r} did not provide a payload path"
                    )
                payload[name] = field_path.read_text(encoding="utf-8")
            elif kind == "lines":
                if not field_path:
                    raise AssertionError(
                        f"Lines field {name!r} did not provide a payload path"
                    )
                content = field_path.read_text(encoding="utf-8")
                payload[name] = content.splitlines() if content else []
            elif kind == "bool":
                if not field_path:
                    raise AssertionError(
                        f"Bool field {name!r} did not provide a payload path"
                    )
                payload[name] = field_path.read_text(encoding="utf-8").strip().lower() == "true"
            elif kind == "number":
                if not field_path:
                    raise AssertionError(
                        f"Number field {name!r} did not provide a payload path"
                    )
                text = field_path.read_text(encoding="utf-8").strip()
                payload[name] = float(text) if any(ch in text for ch in ".eE") else int(text)
            else:
                raise AssertionError(f"Unsupported PowerShell result field kind: {kind!r}")

        return payload, completed
    finally:
        if not cleaned_probe_dir:
            cleanup_powershell_probe_dir(temp_root, timeout_seconds=0.5)
