from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import atlas_egress_reconciler as reconciler


class FakeAtlas:
    def __init__(self, entries: list[dict[str, str]] | None = None) -> None:
        self.entries = [dict(entry) for entry in (entries or [])]
        self.calls: list[tuple[str, str | None]] = []

    def list_entries(self) -> list[dict[str, str]]:
        self.calls.append(("list", None))
        return [dict(entry) for entry in self.entries]

    def create_entry(self, ip_address: str, comment: str) -> None:
        self.calls.append(("create", ip_address))
        self.entries.append({"ipAddress": ip_address, "comment": comment})

    def get_entry(self, entry_value: str) -> dict[str, str]:
        self.calls.append(("get", entry_value))
        return next(
            dict(entry)
            for entry in self.entries
            if reconciler._entry_ipv4(entry) == entry_value
        )

    def wait_until_active(self, entry_value: str) -> None:
        self.calls.append(("active", entry_value))

    def delete_entry(self, entry_value: str) -> None:
        self.calls.append(("delete", entry_value))
        self.entries = [
            entry
            for entry in self.entries
            if reconciler._entry_value(entry) != entry_value
        ]


def _config(tmp_path: Path, *, local_node: str = "mobile") -> reconciler.Config:
    return reconciler.Config(
        path=tmp_path / "config.json",
        project_id="0123456789abcdef01234567",
        client_id="mdb_sa_id_0123456789abcdef01234567",
        secret=reconciler.SecretSource(source="file", path=tmp_path / "secret"),
        local_node=local_node,
        managed_entries={
            "mobile": "von-mobile-client-egress",
            "dgx": "von-dgx-starlink-egress",
        },
        allowed_peer_nodes=frozenset({"mobile"}),
        ip_observers=("https://one.example/ip", "https://two.example/ip"),
        min_observer_agreement=2,
        database_probe=reconciler.DatabaseProbe("cluster.example.mongodb.net"),
        activation_timeout_seconds=10,
        state_path=tmp_path / "state" / "state.json",
        lock_path=tmp_path / "state" / "state.lock",
        notification_interval_seconds=86400,
        peer=None,
    )


def test_config_loads_private_file_and_never_contains_the_secret(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "client-secret"
    secret.write_text("super-secret-value\n", encoding="utf-8")
    secret.chmod(0o600)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "atlas": {
                    "project_id": "0123456789abcdef01234567",
                    "client_id": "mdb_sa_id_0123456789abcdef01234567",
                    "client_secret": {"source": "file", "path": str(secret)},
                },
                "local_node": "mobile",
                "managed_entries": {
                    "mobile": "von-mobile-client-egress",
                    "dgx": "von-dgx-starlink-egress",
                },
                "database_probe": {"host": "cluster.example.mongodb.net"},
                "state_path": str(tmp_path / "state.json"),
                "lock_path": str(tmp_path / "state.lock"),
            }
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)

    config = reconciler.Config.load(config_path)

    assert config.local_node == "mobile"
    assert config.secret.path == secret
    assert "super-secret-value" not in repr(config)


def test_config_rejects_public_permissions(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(reconciler.ConfigurationError, match="unsafe permissions"):
        reconciler.Config.load(path)


def test_public_ip_requires_two_source_consensus(tmp_path: Path) -> None:
    config = _config(tmp_path)
    values = {
        "https://one.example/ip": "8.8.8.8\n",
        "https://two.example/ip": "8.8.8.8",
    }

    observed = reconciler.observe_public_ip(
        config, fetch=lambda url, _timeout: values[url]
    )

    assert observed == {
        "ip_address": "8.8.8.8",
        "agreement": 2,
        "observer_count": 2,
        "observer_failures": 0,
    }


def test_public_ip_disagreement_is_not_guessed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    values = iter(("8.8.8.8", "1.1.1.1"))

    with pytest.raises(reconciler.ObservationError, match="consensus"):
        reconciler.observe_public_ip(config, fetch=lambda _url, _timeout: next(values))


def test_reconcile_adds_reads_probes_then_deletes_old_entry(tmp_path: Path) -> None:
    config = _config(tmp_path)
    atlas = FakeAtlas([{"ipAddress": "1.1.1.1", "comment": "von-mobile-client-egress"}])
    events: list[str] = []

    result = reconciler.Reconciler(config, atlas).full(
        "mobile", "8.8.8.8", probe=lambda: events.append("probe")
    )

    assert result["probe_verified"] is True
    assert atlas.entries == [
        {"ipAddress": "8.8.8.8", "comment": "von-mobile-client-egress"}
    ]
    assert events == ["probe"]
    assert atlas.calls.index(("create", "8.8.8.8")) < atlas.calls.index(
        ("active", "8.8.8.8")
    )
    assert atlas.calls.index(("get", "8.8.8.8")) < atlas.calls.index(
        ("delete", "1.1.1.1")
    )


def test_probe_failure_preserves_old_and_new_entries(tmp_path: Path) -> None:
    config = _config(tmp_path)
    atlas = FakeAtlas([{"ipAddress": "1.1.1.1", "comment": "von-mobile-client-egress"}])

    def fail_probe() -> None:
        raise reconciler.ProbeError("blocked")

    with pytest.raises(reconciler.ProbeError, match="blocked"):
        reconciler.Reconciler(config, atlas).full("mobile", "8.8.8.8", probe=fail_probe)

    assert {entry["ipAddress"] for entry in atlas.entries} == {
        "1.1.1.1",
        "8.8.8.8",
    }
    assert not any(call[0] == "delete" for call in atlas.calls)


def test_existing_ip_with_foreign_comment_is_never_claimed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    atlas = FakeAtlas([{"ipAddress": "8.8.8.8", "comment": "human-managed"}])

    with pytest.raises(reconciler.AtlasAPIError, match="different comment"):
        reconciler.Reconciler(config, atlas).ensure("mobile", "8.8.8.8")

    assert not any(call[0] in {"create", "delete"} for call in atlas.calls)


def test_dry_run_plans_without_mutation_or_probe(tmp_path: Path) -> None:
    config = _config(tmp_path)
    atlas = FakeAtlas([{"ipAddress": "1.1.1.1", "comment": "von-mobile-client-egress"}])
    probes: list[str] = []

    result = reconciler.Reconciler(config, atlas).full(
        "mobile",
        "8.8.8.8",
        probe=lambda: probes.append("probe"),
        dry_run=True,
    )

    assert result["ensure"]["created"] is True
    assert result["finalise"]["deleted_old_entries"] == 1
    assert probes == []
    assert not any(call[0] in {"create", "delete"} for call in atlas.calls)


def test_peer_request_uses_fixed_ssh_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("key", encoding="utf-8")
    known_hosts.write_text("host key", encoding="utf-8")
    peer = reconciler.PeerConfig(
        node="dgx",
        ssh_destination="mjw@spark-449a",
        identity_file=identity,
        known_hosts_file=known_hosts,
        remote_python="/usr/bin/python3",
        remote_script="/opt/von-ops/atlas_egress_reconciler.py",
        remote_config="/home/mjw/.config/von/atlas-egress-reconciler.json",
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return reconciler.subprocess.CompletedProcess(
            command, 0, stdout='{"success": true}', stderr=""
        )

    monkeypatch.setattr(reconciler.subprocess, "run", fake_run)

    reconciler.PeerClient(peer).request(reconciler._peer_payload("observe"))

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:3] == ["/usr/bin/ssh", "-F", "/dev/null"]
    assert "StrictHostKeyChecking=yes" in command
    assert "BatchMode=yes" in command
    assert command[-5:] == [
        "/usr/bin/python3",
        "/opt/von-ops/atlas_egress_reconciler.py",
        "--config",
        "/home/mjw/.config/von/atlas-egress-reconciler.json",
        "peer-server",
    ]
    assert json.loads(str(captured["input"]))["operation"] == "observe"


def test_peer_cannot_admin_unknown_or_unapproved_node(tmp_path: Path) -> None:
    config = _config(tmp_path, local_node="dgx")

    with pytest.raises(reconciler.PeerError, match="not authorised"):
        reconciler._handle_peer_request(
            config,
            reconciler._peer_payload("admin-ensure", node="dgx", ip_address="8.8.8.8"),
        )


def test_dry_run_never_asks_peer_to_self_reconcile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    base = _config(tmp_path)
    peer_config = reconciler.PeerConfig(
        node="dgx",
        ssh_destination="mjw@spark-449a",
        identity_file=tmp_path / "identity",
        known_hosts_file=tmp_path / "known_hosts",
        remote_python="/usr/bin/python3",
        remote_script="/opt/von-ops/atlas_egress_reconciler.py",
        remote_config="/home/mjw/.config/von/atlas-egress-reconciler.json",
    )
    config = reconciler.Config(**{**base.__dict__, "peer": peer_config})
    operations: list[str] = []

    class FakePeerClient:
        def __init__(self, _peer):
            pass

        def request(self, payload):
            operations.append(payload["operation"])
            return {"success": True, "ip_address": "1.1.1.1"}

    monkeypatch.setattr(
        reconciler,
        "observe_public_ip",
        lambda _config: {"ip_address": "8.8.8.8", "agreement": 2},
    )
    monkeypatch.setattr(reconciler, "AtlasClient", lambda _config: object())
    monkeypatch.setattr(reconciler, "PeerClient", FakePeerClient)
    monkeypatch.setattr(
        reconciler,
        "_local_full_reconcile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            reconciler.AtlasAPIError("list", "blocked", 403)
        ),
    )

    result = reconciler.run_cycle(config, dry_run=True)

    assert result["success"] is True
    assert operations == ["observe"]
    assert result["nodes"]["dgx"]["peer_mutation_skipped"] is True


def test_manual_recovery_names_exact_entry_and_preserves_old(tmp_path: Path) -> None:
    config = _config(tmp_path)

    message = reconciler._manual_recovery(
        config,
        {"local_observation": {"ip_address": "8.8.8.8"}},
    )

    assert "8.8.8.8/32" in message
    assert "von-mobile-client-egress" in message
    assert "Do not remove" in message
