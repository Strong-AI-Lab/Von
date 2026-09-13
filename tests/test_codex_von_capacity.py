"""Actual lock contenders; no model calls or coding workers are launched."""

import subprocess
import sys

import pytest

pytest.importorskip("fcntl")
from scripts import codex_von_capacity as capacity


def test_two_contenders_and_inherited_lock_survive_controller_close(
    tmp_path, monkeypatch
):
    lock = tmp_path / "host.lock"
    lock.touch(mode=0o644)
    config = {
        "host_capacity": {
            "lock_path": str(lock),
            "reserve_bytes": 10,
            "launch_headroom_bytes": 20,
        }
    }
    monkeypatch.setattr(capacity, "available_memory_bytes", lambda: 100)
    with capacity.admission(config) as fds:
        assert fds is not None
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; print('holding', flush=True); sys.stdin.read(1)",
            ],
            pass_fds=fds,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout.readline().strip() == "holding"
        with capacity.admission(config) as second:
            assert second is None
    try:
        with capacity.admission(config) as second:
            assert second is None  # child retains the original open description
    finally:
        child.communicate("x", timeout=5)
    with capacity.admission(config) as second:
        assert second is not None
    monkeypatch.setattr(capacity, "available_memory_bytes", lambda: 29)
    with capacity.admission(config) as second:
        assert second is None


def test_unsafe_or_missing_lock_denies(tmp_path):
    path = tmp_path / "lock"
    config = {"host_capacity": {"lock_path": str(path)}}
    with pytest.raises(FileNotFoundError), capacity.admission(config):
        pass
    path.touch()
    path.chmod(0o666)
    with pytest.raises(PermissionError), capacity.admission(config):
        pass


def test_legacy_unenrolled_worker_is_not_silently_migrated():
    with capacity.admission({}) as fds:
        assert fds == ()
