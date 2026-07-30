from __future__ import annotations

import json
import plistlib
from pathlib import Path

import pytest

from scripts import install_macos_mongo_ssh_tunnel as installer


def _forwards() -> list[installer.Forward]:
    return [
        installer.Forward(
            27018,
            "ac-example-shard-00-00.mongodb.net",
            27017,
        ),
        installer.Forward(
            27019,
            "ac-example-shard-00-01.mongodb.net",
            27017,
        ),
        installer.Forward(
            27020,
            "ac-example-shard-00-02.mongodb.net",
            27017,
        ),
    ]


def test_program_arguments_are_direct_fixed_and_secret_free(tmp_path: Path) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"

    arguments = installer.build_program_arguments(
        ssh_destination="operator@relay.example",
        identity_file=identity,
        known_hosts_file=known_hosts,
        forwards=_forwards(),
    )

    assert arguments[0] == "/usr/bin/ssh"
    assert arguments[-1] == "operator@relay.example"
    assert arguments[1:3] == ["-F", "/dev/null"]
    assert "BatchMode=yes" in arguments
    assert "StrictHostKeyChecking=yes" in arguments
    assert "ExitOnForwardFailure=yes" in arguments
    assert "ControlMaster=no" in arguments
    assert arguments.count("-L") == 3
    assert all(
        "127.0.0.1:" in arguments[index + 1]
        for index, value in enumerate(arguments)
        if value == "-L"
    )
    rendered = "\n".join(arguments)
    assert "mongodb://" not in rendered
    assert "password" not in rendered.lower()


def test_launch_agent_is_supervised_and_owner_only_by_design(tmp_path: Path) -> None:
    payload = installer.build_launch_agent(
        label=installer.DEFAULT_LABEL,
        program_arguments=["/usr/bin/ssh", "-N", "operator@relay.example"],
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
    )

    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 10
    assert payload["Umask"] == 0o077
    assert payload["ProgramArguments"][0] == "/usr/bin/ssh"


@pytest.mark.parametrize(
    "value",
    (
        "0:remote.example:27017",
        "27018:bad host:27017",
        "27018:remote.example:70000",
        "27018:remote.example",
    ),
)
def test_invalid_forwards_are_rejected(value: str) -> None:
    with pytest.raises(installer.argparse.ArgumentTypeError):
        installer._forward(value)


def test_install_writes_private_plist_and_bootstraps_launchd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test key material\n", encoding="utf-8")
    known_hosts.write_text("relay.example test-host-key\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    plist_path = tmp_path / "LaunchAgents" / "tunnel.plist"
    log_directory = tmp_path / "logs"
    launchctl_calls: list[tuple[list[str], bool]] = []

    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)
    monkeypatch.setattr(installer, "_ports_are_available", lambda _items: True)
    monkeypatch.setattr(
        installer, "_wait_for_listeners", lambda _label, _items: True
    )

    def _fake_launchctl(arguments, *, check):
        launchctl_calls.append((list(arguments), check))

    monkeypatch.setattr(installer, "_run_launchctl", _fake_launchctl)
    monkeypatch.setattr(
        installer,
        "_status_payload",
        lambda **_kwargs: {"installed": True, "loaded": True},
    )

    result = installer.run(
        [
            "install",
            "--ssh-destination",
            "operator@relay.example",
            "--identity-file",
            str(identity),
            "--known-hosts-file",
            str(known_hosts),
            "--plist-path",
            str(plist_path),
            "--log-directory",
            str(log_directory),
            *[
                item
                for forward in _forwards()
                for item in (
                    "--forward",
                    f"{forward.local_port}:{forward.remote_host}:{forward.remote_port}",
                )
            ],
        ]
    )

    assert result == 0
    assert plist_path.stat().st_mode & 0o777 == 0o600
    with plist_path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["ProgramArguments"].count("-L") == 3
    assert launchctl_calls == [
        (
            [
                "bootstrap",
                f"gui/{installer.os.getuid()}",
                str(plist_path),
            ],
            True,
        )
    ]


def test_known_hosts_may_be_world_readable_but_not_world_writable(
    tmp_path: Path,
) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("relay.example test-host-key\n", encoding="utf-8")
    known_hosts.chmod(0o644)

    assert (
        installer._absolute_existing_file(
            str(known_hosts),
            description="known-hosts file",
            allow_public_read=True,
        )
        == known_hosts
    )

    known_hosts.chmod(0o646)
    with pytest.raises(ValueError, match="unsafe group or other permissions"):
        installer._absolute_existing_file(
            str(known_hosts),
            description="known-hosts file",
            allow_public_read=True,
        )


def test_install_does_not_replace_a_foreign_port_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test\n", encoding="utf-8")
    known_hosts.write_text("test\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    plist_path = tmp_path / "tunnel.plist"

    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)
    monkeypatch.setattr(installer, "_ports_are_available", lambda _items: False)

    with pytest.raises(RuntimeError, match="no process was killed"):
        installer.run(
            [
                "install",
                "--ssh-destination",
                "operator@relay.example",
                "--identity-file",
                str(identity),
                "--known-hosts-file",
                str(known_hosts),
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
                "--forward",
                "27018:remote.example:27017",
            ]
        )

    assert not plist_path.exists()


def test_install_refuses_unsafe_existing_log_directory_without_changing_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test\n", encoding="utf-8")
    known_hosts.write_text("test\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    log_directory = tmp_path / "existing-logs"
    log_directory.mkdir(mode=0o755)
    log_directory.chmod(0o755)
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)

    with pytest.raises(ValueError, match="owner-only rwx"):
        installer.run(
            [
                "install",
                "--ssh-destination",
                "operator@relay.example",
                "--identity-file",
                str(identity),
                "--known-hosts-file",
                str(known_hosts),
                "--plist-path",
                str(tmp_path / "tunnel.plist"),
                "--log-directory",
                str(log_directory),
                "--forward",
                "27018:remote.example:27017",
            ]
        )

    assert log_directory.stat().st_mode & 0o777 == 0o755


def test_install_refuses_symlinked_log_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test\n", encoding="utf-8")
    known_hosts.write_text("test\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    real_logs = tmp_path / "real-logs"
    real_logs.mkdir(mode=0o700)
    log_directory = tmp_path / "linked-logs"
    log_directory.symlink_to(real_logs, target_is_directory=True)
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)

    with pytest.raises(ValueError, match="symbolic link"):
        installer.run(
            [
                "install",
                "--ssh-destination",
                "operator@relay.example",
                "--identity-file",
                str(identity),
                "--known-hosts-file",
                str(known_hosts),
                "--plist-path",
                str(tmp_path / "tunnel.plist"),
                "--log-directory",
                str(log_directory),
                "--forward",
                "27018:remote.example:27017",
            ]
        )


def test_status_infers_forwards_and_checks_launchd_pid_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plist_path = tmp_path / "tunnel.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": installer.DEFAULT_LABEL,
                "ProgramArguments": [
                    "/usr/bin/ssh",
                    "-L",
                    "127.0.0.1:27018:remote.example:27017",
                    "operator@relay.example",
                ],
            }
        )
    )
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: True)
    monkeypatch.setattr(installer, "_service_pid", lambda _label: 1234)
    monkeypatch.setattr(installer, "_listener_pids", lambda _port: {1234})

    assert (
        installer.run(
            [
                "status",
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["configuration_status"] == "configured"
    assert result["plist_valid"] is True
    assert result["label_matches"] is True
    assert result["listeners_checked"] is True
    assert result["listener_coverage_complete"] is True
    assert result["listeners"] == {"27018": True}
    assert result["mongo_route_probed"] is False


@pytest.mark.parametrize(
    ("contents", "expected_status", "expected_valid", "expected_label_matches"),
    (
        (b"not a plist", "invalid", False, None),
        (
            plistlib.dumps(
                {
                    "Label": "example.unrelated.service",
                    "ProgramArguments": ["/usr/bin/true"],
                }
            ),
            "foreign_label",
            True,
            False,
        ),
    ),
)
def test_status_types_invalid_and_foreign_plists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    contents: bytes,
    expected_status: str,
    expected_valid: bool,
    expected_label_matches: bool | None,
) -> None:
    plist_path = tmp_path / "tunnel.plist"
    plist_path.write_bytes(contents)
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)

    assert (
        installer.run(
            [
                "status",
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
            ]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["installed"] is True
    assert result["configuration_status"] == expected_status
    assert result["plist_valid"] is expected_valid
    assert result["label_matches"] is expected_label_matches
    assert result["listeners_checked"] is False
    assert result["listener_coverage_complete"] is False


def test_status_and_uninstall_do_not_require_forward_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plist_path = tmp_path / "tunnel.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": installer.DEFAULT_LABEL,
                "ProgramArguments": ["/usr/bin/ssh", "-N", "operator@relay.example"],
            }
        )
    )
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)

    assert (
        installer.run(
            [
                "status",
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
            ]
        )
        == 0
    )
    assert (
        installer.run(
            [
                "uninstall",
                "--confirm-uninstall",
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
            ]
        )
        == 0
    )
    assert not plist_path.exists()


def test_uninstall_refuses_loaded_service_without_matching_plist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    launchctl_calls: list[list[str]] = []
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: True)
    monkeypatch.setattr(
        installer,
        "_run_launchctl",
        lambda arguments, *, check: launchctl_calls.append(list(arguments)),
    )

    with pytest.raises(RuntimeError, match="without its matching plist"):
        installer.run(
            [
                "uninstall",
                "--confirm-uninstall",
                "--plist-path",
                str(tmp_path / "missing.plist"),
                "--log-directory",
                str(tmp_path / "logs"),
            ]
        )

    assert launchctl_calls == []


@pytest.mark.parametrize("action", ("install", "uninstall"))
def test_mutating_actions_refuse_plist_owned_by_another_label(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    action: str,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test\n", encoding="utf-8")
    known_hosts.write_text("test\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    plist_path = tmp_path / "other.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": "example.unrelated.service",
                "ProgramArguments": ["/usr/bin/true"],
            }
        )
    )
    original = plist_path.read_bytes()
    monkeypatch.setattr(installer, "_service_is_loaded", lambda _label: False)

    arguments = [
        action,
        "--plist-path",
        str(plist_path),
        "--log-directory",
        str(tmp_path / "logs"),
    ]
    if action == "install":
        arguments.extend(
            [
                "--ssh-destination",
                "operator@relay.example",
                "--identity-file",
                str(identity),
                "--known-hosts-file",
                str(known_hosts),
                "--forward",
                "27018:remote.example:27017",
            ]
        )
    else:
        arguments.append("--confirm-uninstall")

    with pytest.raises(ValueError, match="different label"):
        installer.run(arguments)

    assert plist_path.read_bytes() == original


def test_failed_reinstall_restores_and_reloads_previous_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test\n", encoding="utf-8")
    known_hosts.write_text("test\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    plist_path = tmp_path / "tunnel.plist"
    previous_plist = plistlib.dumps(
        {
            "Label": installer.DEFAULT_LABEL,
            "ProgramArguments": ["/usr/bin/ssh", "-N", "old@relay.example"],
        }
    )
    plist_path.write_bytes(previous_plist)
    service_loaded_calls = 0
    bootstrap_calls = 0

    def _fake_service_is_loaded(_label: str) -> bool:
        nonlocal service_loaded_calls
        service_loaded_calls += 1
        return service_loaded_calls == 1

    def _fake_launchctl(arguments, *, check):
        nonlocal bootstrap_calls
        assert check is True
        if arguments[0] == "bootstrap":
            bootstrap_calls += 1
            if bootstrap_calls == 1:
                raise RuntimeError("simulated replacement bootstrap failure")

    monkeypatch.setattr(installer, "_service_is_loaded", _fake_service_is_loaded)
    monkeypatch.setattr(installer, "_ports_are_available", lambda _items: True)
    monkeypatch.setattr(installer, "_run_launchctl", _fake_launchctl)

    with pytest.raises(RuntimeError, match="simulated replacement"):
        installer.run(
            [
                "install",
                "--ssh-destination",
                "operator@relay.example",
                "--identity-file",
                str(identity),
                "--known-hosts-file",
                str(known_hosts),
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
                "--forward",
                "27018:remote.example:27017",
            ]
        )

    assert plist_path.read_bytes() == previous_plist
    assert plist_path.stat().st_mode & 0o777 == 0o600
    assert bootstrap_calls == 2


def test_listener_timeout_rolls_back_loaded_previous_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test\n", encoding="utf-8")
    known_hosts.write_text("test\n", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts.chmod(0o600)
    plist_path = tmp_path / "tunnel.plist"
    previous_plist = plistlib.dumps(
        {
            "Label": installer.DEFAULT_LABEL,
            "ProgramArguments": ["/usr/bin/ssh", "-N", "old@relay.example"],
        }
    )
    plist_path.write_bytes(previous_plist)
    service_loaded_calls = 0
    launchctl_calls: list[str] = []

    def _fake_service_is_loaded(_label: str) -> bool:
        nonlocal service_loaded_calls
        service_loaded_calls += 1
        return True

    def _fake_launchctl(arguments, *, check):
        assert check is True
        launchctl_calls.append(arguments[0])

    monkeypatch.setattr(installer, "_service_is_loaded", _fake_service_is_loaded)
    monkeypatch.setattr(installer, "_ports_are_available", lambda _items: True)
    monkeypatch.setattr(
        installer, "_wait_for_listeners", lambda _label, _items: False
    )
    monkeypatch.setattr(installer, "_run_launchctl", _fake_launchctl)

    with pytest.raises(RuntimeError, match="listeners became ready"):
        installer.run(
            [
                "install",
                "--ssh-destination",
                "operator@relay.example",
                "--identity-file",
                str(identity),
                "--known-hosts-file",
                str(known_hosts),
                "--plist-path",
                str(plist_path),
                "--log-directory",
                str(tmp_path / "logs"),
                "--forward",
                "27018:remote.example:27017",
            ]
        )

    assert plist_path.read_bytes() == previous_plist
    assert plist_path.stat().st_mode & 0o777 == 0o600
    assert launchctl_calls == ["bootout", "bootstrap", "bootout", "bootstrap"]
