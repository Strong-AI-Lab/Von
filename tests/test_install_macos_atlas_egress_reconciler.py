from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from scripts import install_macos_atlas_egress_reconciler as installer


def test_launch_agent_is_periodic_private_and_secret_free(tmp_path: Path) -> None:
    arguments = installer.build_program_arguments(
        python_executable=tmp_path / "python",
        script_path=tmp_path / "reconciler.py",
        config_path=tmp_path / "config.json",
    )

    payload = installer.build_launch_agent(
        label=installer.DEFAULT_LABEL,
        program_arguments=arguments,
        interval_seconds=3600,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert payload["RunAtLoad"] is True
    assert payload["StartInterval"] == 3600
    assert payload["Umask"] == 0o077
    assert "KeepAlive" not in payload
    assert arguments[-2:] == ["cycle", "--notify"]
    rendered = json.dumps(payload)
    assert "client_secret" not in rendered
    assert "mdb_sa" not in rendered


def test_install_preflights_then_writes_private_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "python"
    script = tmp_path / "reconciler.py"
    config = tmp_path / "config.json"
    for path in (python, script, config):
        path.write_text("test", encoding="utf-8")
        path.chmod(0o700 if path == python else 0o600)
    plist_path = tmp_path / "LaunchAgents" / "agent.plist"
    plist_path.parent.mkdir(mode=0o700)
    log_directory = tmp_path / "logs"
    events: list[str] = []
    loaded = False

    def fake_preflight(*_args):
        events.append("preflight")

    def fake_launchctl(arguments, *, check):
        nonlocal loaded
        events.append(arguments[0])
        if arguments[0] == "bootstrap":
            loaded = True
        elif arguments[0] == "bootout":
            loaded = False
        return installer.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(installer, "_run_preflight", fake_preflight)
    monkeypatch.setattr(installer, "_run_launchctl", fake_launchctl)
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: loaded)
    monkeypatch.setattr(installer, "_service_pid", lambda _label: None)

    result = installer.run(
        [
            "install",
            "--python-executable",
            str(python),
            "--script-path",
            str(script),
            "--config-path",
            str(config),
            "--plist-path",
            str(plist_path),
            "--log-directory",
            str(log_directory),
        ]
    )

    assert result == 0
    assert events[:2] == ["preflight", "bootstrap"]
    assert plist_path.stat().st_mode & 0o777 == 0o600
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["Label"] == installer.DEFAULT_LABEL
    assert payload["ProgramArguments"][-2:] == ["cycle", "--notify"]


def test_failed_replacement_restores_previous_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    python = tmp_path / "python"
    script = tmp_path / "reconciler.py"
    config = tmp_path / "config.json"
    for path in (python, script, config):
        path.write_text("test", encoding="utf-8")
        path.chmod(0o700 if path == python else 0o600)
    plist_path = tmp_path / "LaunchAgents" / "agent.plist"
    plist_path.parent.mkdir(mode=0o700)
    previous = plistlib.dumps(
        {
            "Label": installer.DEFAULT_LABEL,
            "ProgramArguments": ["/old/python", "/old/script"],
        }
    )
    plist_path.write_bytes(previous)
    plist_path.chmod(0o600)
    log_directory = tmp_path / "logs"
    loaded = True
    bootstrap_count = 0

    monkeypatch.setattr(installer, "_run_preflight", lambda *_args: None)

    def fake_launchctl(arguments, *, check):
        nonlocal loaded, bootstrap_count
        if arguments[0] == "bootout":
            loaded = False
        elif arguments[0] == "bootstrap":
            bootstrap_count += 1
            loaded = bootstrap_count > 1
            if bootstrap_count == 1:
                raise installer.subprocess.CalledProcessError(5, arguments)
        return installer.subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(installer, "_run_launchctl", fake_launchctl)
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: loaded)

    with pytest.raises(RuntimeError, match="prior state was restored"):
        installer.run(
            [
                "install",
                "--python-executable",
                str(python),
                "--script-path",
                str(script),
                "--config-path",
                str(config),
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(log_directory),
            ]
        )

    assert plist_path.read_bytes() == previous
    assert loaded is True


def test_uninstall_refuses_foreign_plist(tmp_path: Path) -> None:
    plist_path = tmp_path / "foreign.plist"
    plist_path.write_bytes(plistlib.dumps({"Label": "org.example.foreign"}))

    with pytest.raises(ValueError, match="another label"):
        installer.run(
            [
                "uninstall",
                "--confirm-uninstall",
                "--plist-path",
                str(plist_path),
            ]
        )


def test_interval_cannot_turn_launchd_into_a_tight_loop(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 300"):
        installer.build_launch_agent(
            label=installer.DEFAULT_LABEL,
            program_arguments=["/usr/bin/python3"],
            interval_seconds=30,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
        )
