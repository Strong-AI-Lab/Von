from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIX_NEXT_COMMAND = "./setup_all.sh --install-system-deps"
WINDOWS_NEXT_COMMAND = r".\setup_all.ps1 -InstallSystemDeps"


def _read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8-sig")


def _bash_function(script: str, name: str, next_name: str) -> str:
    start = script.index(f"{name}()")
    end = script.index(f"{next_name}()", start)
    return script[start:end]


def _powershell_function(script: str, name: str, next_marker: str) -> str:
    start = script.index(f"function {name}")
    end = script.index(next_marker, start)
    return script[start:end]


@pytest.mark.parametrize("script_name", ["setup_all.sh", "setup_py.sh"])
def test_bash_help_exposes_ocr_contract_without_starting_setup(
    script_name: str,
) -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for setup-script contract tests")

    result = subprocess.run(
        [bash, str(REPO_ROOT / script_name), "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )

    assert "--with-ocr" in result.stdout
    assert "--install-system-deps" in result.stdout
    assert UNIX_NEXT_COMMAND in result.stdout
    assert "Python setup starting" not in result.stdout
    assert "Installing Tesseract" not in result.stdout


def test_every_unavailable_surface_gives_humans_and_agents_the_exact_command() -> None:
    readme = _read("README.md")
    setup_all_sh = _read("setup_all.sh")
    setup_py_sh = _read("setup_py.sh")
    setup_all_ps1 = _read("setup_all.ps1")
    setup_py_ps1 = _read("setup_py.ps1")

    assert "If setup reports `OCR STATUS: UNAVAILABLE`" in readme
    assert f"```bash\n{UNIX_NEXT_COMMAND}\n```" in readme
    assert f"```powershell\n{WINDOWS_NEXT_COMMAND}\n```" in readme

    for script in (setup_all_sh, setup_py_sh):
        assert f"NEXT ACTION: {UNIX_NEXT_COMMAND}" in script
        assert f'VON_SETUP_NEXT_ACTION command="{UNIX_NEXT_COMMAND}"' in script
        assert "as your ordinary user" in script

    for script in (setup_all_ps1, setup_py_ps1):
        assert f"NEXT ACTION: {WINDOWS_NEXT_COMMAND}" in script
        assert f'VON_SETUP_NEXT_ACTION command="{WINDOWS_NEXT_COMMAND}"' in script


def test_bash_orchestrator_forwards_both_ocr_options_and_rejects_skip_py() -> None:
    script = _read("setup_all.sh")

    assert 'PY_ARGS="$PY_ARGS --with-ocr"' in script
    assert 'PY_ARGS="$PY_ARGS --install-system-deps"' in script
    assert re.search(
        r"--install-system-deps\)\s+INSTALL_SYSTEM_DEPS=1\s+WITH_OCR=1",
        script,
    )

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for setup-script contract tests")
    result = subprocess.run(
        [
            bash,
            str(REPO_ROOT / "setup_all.sh"),
            "--skip-py",
            "--install-system-deps",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert UNIX_NEXT_COMMAND in output
    assert "Python setup starting" not in output


def test_bash_package_mutation_is_behind_the_explicit_install_gate() -> None:
    script = _read("setup_py.sh")
    prepare = _bash_function(script, "prepare_tesseract", "verify_tesseract_runtime")
    installer_call = prepare.index("install_tesseract_system_dependency")
    no_authority_guard = prepare.index("[[ $INSTALL_SYSTEM_DEPS -eq 0 ]]")

    assert no_authority_guard < installer_call
    assert "--install-system-deps was provided" in prepare
    assert "brew install tesseract" in script
    assert "apt-get install -y tesseract-ocr tesseract-ocr-eng" in script


def test_bash_has_an_explicit_ordinary_user_apt_fallback_and_launcher_path() -> None:
    setup = _read("setup_py.sh")
    launcher = _read("run.sh")
    local_install = _bash_function(
        setup,
        "install_tesseract_debian_user_local",
        "install_tesseract_system_dependency",
    )
    system_install = _bash_function(
        setup,
        "install_tesseract_system_dependency",
        "prepare_tesseract",
    )

    assert "sudo -n true" in system_install
    assert "install_tesseract_debian_user_local" in system_install
    assert "apt-get download" in local_install
    assert "dpkg-deb -x" in local_install
    assert "${HOME}/.local/share" in local_install
    assert "${HOME}/.local/bin" in local_install
    assert "TESSDATA_PREFIX" in local_install
    assert 'if [ -x "${HOME}/.local/bin/tesseract" ]' in launcher
    assert 'export PATH="${HOME}/.local/bin:${PATH}"' in launcher


def test_unix_final_receipt_qualifies_core_only_and_required_failure() -> None:
    script = _read("setup_all.sh")

    assert "=== FULL SETUP: COMPLETE" in script
    assert "=== CORE SETUP: COMPLETE" in script
    assert "OCR STATUS: UNAVAILABLE" in script
    assert "Image and scanned-PDF OCR will not work." in script
    assert re.search(
        r"if \[\[ \$WITH_OCR -eq 1 \]\] && \[\[ \$OCR_OK -eq 0 \]\]; then\s+"
        r"OVERALL_OK=0",
        script,
    )
    assert "VON_SETUP_CAPABILITY name=ocr state=available" in script
    assert "VON_SETUP_CAPABILITY name=ocr state=unavailable" in script


def test_powershell_forwards_flags_and_never_runs_winget_without_authority() -> None:
    orchestrator = _read("setup_all.ps1")
    python_setup = _read("setup_py.ps1")

    assert "$pyParams['WithOCR'] = $true" in orchestrator
    assert "$pyParams['InstallSystemDeps'] = $true" in orchestrator
    assert re.search(
        r"if \(\$InstallSystemDeps\)\s*{\s*\$WithOCR = \$true\s*}",
        orchestrator,
    )
    assert re.search(
        r"if \(\$InstallSystemDeps\)\s*{\s*\$WithOCR = \$true\s*}",
        python_setup,
    )

    install = _powershell_function(
        python_setup,
        "Install-Tesseract",
        "## Validate parameters",
    )
    winget_call = re.search(r"&\s+\$winget(?:\.Source)?\s+install\b", install)
    authority_guard = re.search(
        r"if\s*\(\s*-not\s+\$InstallSystemDeps\s*\)",
        install,
        flags=re.IGNORECASE,
    )
    assert winget_call, "Install-Tesseract must retain its explicit winget call"
    assert authority_guard, "winget must be guarded by -InstallSystemDeps"
    assert authority_guard.start() < winget_call.start()


def test_powershell_receipt_cannot_claim_success_when_required_ocr_is_missing() -> None:
    orchestrator = _read("setup_all.ps1")
    python_setup = _read("setup_py.ps1")

    assert "OCR UNAVAILABLE: core setup can run" in orchestrator
    assert "VON_SETUP_CAPABILITY name=ocr state=" in orchestrator
    assert "real OCR smoke test passed" in orchestrator
    assert re.search(
        r"if\s*\(\s*\$WithOCR\s*-and\s*\$ocrState\s*-ne\s*'available'\s*\)\s*"
        r"{\s*\$overallOk\s*=\s*\$false",
        orchestrator,
        flags=re.IGNORECASE,
    )
    assert "$env:VON_SETUP_OCR_STATE" in python_setup
    assert re.search(
        r"\$script:OcrState\s*=\s*'available'",
        python_setup,
        flags=re.IGNORECASE,
    )


def test_shell_syntax_and_powershell_contract_syntax() -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is required for setup-script syntax tests")

    for name in ("setup_all.sh", "setup_py.sh", "run.sh"):
        subprocess.run(
            [bash, "-n", str(REPO_ROOT / name)],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )

    pwsh = shutil.which("pwsh")
    if not pwsh:
        # The PowerShell behavioural contract is still covered statically by the
        # preceding tests on hosts where PowerShell itself is unavailable.
        assert "param(" in _read("setup_all.ps1")
        assert "param (" in _read("setup_py.ps1")
        return

    for name in ("setup_all.ps1", "setup_py.ps1"):
        path = str(REPO_ROOT / name).replace("'", "''")
        command = (
            "$tokens = $null; $errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{path}', "
            "[ref]$tokens, [ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
        )
        subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=20,
            check=True,
        )
