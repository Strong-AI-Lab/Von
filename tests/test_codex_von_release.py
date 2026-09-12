"""Local Git/process fixtures; never touch installed services or start Codex."""

import fcntl
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import codex_von_deploy as deployer
from scripts import codex_von_release as releases


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "user.name", "Fixture")
    repo = Path(__file__).resolve().parents[1]
    for name in (
        "codex_von_worker.py",
        "codex_von_inbox.py",
        "codex_von_deploy.py",
        "codex_von_release.py",
        "deploy_local_main.py",
        "codex_von_worker_prompt.md",
        "codex_von_inbox_prompt.md",
    ):
        target = source / "scripts" / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((repo / "scripts" / name).read_bytes())
    service = source / "src/backend/services/task_management_service.py"
    service.parent.mkdir(parents=True)
    service.write_text("RELEASE = 'old'\n")
    git(source, "add", ".")
    git(source, "commit", "-qm", "old fixture")
    old = git(source, "rev-parse", "HEAD")
    service.write_text("RELEASE = 'new'\n")
    git(source, "commit", "-qam", "new fixture")
    return source, old, git(source, "rev-parse", "HEAD")


def test_complete_release_switch_preserves_loaded_files_and_rollback(source, tmp_path):
    source, old, new = source
    root = tmp_path / "releases"
    old_path = releases.prepare(source, root, old)
    releases.activate(root, old_path)
    # Untracked operator material is never carried into a prepared release.
    (source / "private-fixture.txt").write_text("not a release input")
    new_path = releases.prepare(source, root, new)
    assert not (new_path / "private-fixture.txt").exists()
    assert releases.current(root) == old_path
    old_contents = (
        old_path / "src/backend/services/task_management_service.py"
    ).read_text()
    receipt = releases.activate(root, new_path)
    assert receipt["status"] == "selected_for_next_invocation"
    assert receipt["commit"] == new
    assert releases.current(root) == new_path
    assert (
        old_path / "src/backend/services/task_management_service.py"
    ).read_text() == old_contents
    # Fresh process resolves both the actual worker and canonical service from
    # the selected release, without a model call or a database connection.
    code = """import json, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / 'scripts'))
import codex_von_worker as worker
worker.bind_backend_root(root)
from src.backend.services import task_management_service as tasks
print(json.dumps([worker.__file__, tasks.__file__, tasks.RELEASE]))
"""
    result = json.loads(
        subprocess.check_output(
            [sys.executable, "-c", code, str(root / "current")], cwd=tmp_path, text=True
        )
    )
    assert result == [
        str(new_path / "scripts/codex_von_worker.py"),
        str(new_path / "src/backend/services/task_management_service.py"),
        "new",
    ]
    assert releases.prepare(source, root, new) == new_path
    releases.activate(root, old_path)
    assert releases.current(root) == old_path


def test_modified_release_is_not_reused_or_activated(source, tmp_path):
    source, old, new = source
    root = tmp_path / "releases"
    predecessor = releases.prepare(source, root, old)
    releases.activate(root, predecessor)
    target = releases.prepare(source, root, new)
    (target / "scripts/codex_von_worker.py").write_text("# damaged fixture\n")
    for operation in (
        lambda: releases.prepare(source, root, new),
        lambda: releases.activate(root, target),
    ):
        with pytest.raises(deployer.runtime.DeploymentError, match="modified"):
            operation()
    assert releases.current(root) == predecessor


def test_activation_cannot_redirect_to_an_unrelated_checkout(source, tmp_path):
    source, _, _ = source
    root = tmp_path / "releases"
    root.mkdir()
    with pytest.raises(deployer.runtime.DeploymentError, match="outside"):
        releases.activate(root, source)


def test_shared_controller_lock_is_inherited_without_unlocking(tmp_path):
    config = {"state_root": str(tmp_path)}
    with (tmp_path / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            with deployer.worker_lock(config):
                pytest.fail("overlapped active worker")
        code = """import json, sys
from scripts.codex_von_deploy import worker_lock
with worker_lock(json.loads(sys.argv[1]), int(sys.argv[2])):
 print('controller lock retained')
"""
        result = subprocess.check_output(
            [sys.executable, "-c", code, json.dumps(config), str(lock.fileno())],
            pass_fds=(lock.fileno(),),
            text=True,
        )
        assert result.strip() == "controller lock retained"
        with pytest.raises(BlockingIOError):
            with deployer.worker_lock(config):
                pytest.fail("child released parent's lock")
    with deployer.worker_lock(config):
        pass
    with (tmp_path / "other.lock").open("a") as other:
        with pytest.raises(ValueError, match="not the configured"):
            with deployer.worker_lock(config, other.fileno()):
                pytest.fail("accepted unrelated lock")


def test_operator_bootstrap_cli_selects_only_merged_source_and_keeps_receipt(
    source, tmp_path
):
    source, old, new = source
    git(source, "branch", "-M", "main")
    git(source, "remote", "add", "origin", str(source))
    config = tmp_path / "fixture-config.json"
    root = tmp_path / "releases"
    config.write_text(
        json.dumps(
            {
                "primary_root": str(source),
                "state_root": str(tmp_path),
                "worker_release_root": str(root),
            }
        )
    )
    receipt = tmp_path / "installation.json"
    command = [
        sys.executable,
        "scripts/codex_von_release.py",
        "--config",
        str(config),
        "--receipt",
        str(receipt),
        "--commit",
    ]
    stale = subprocess.run([*command, old], capture_output=True, text=True)
    assert stale.returncode != 0
    assert "current origin/main" in stale.stderr
    assert not receipt.exists()
    for _ in range(2):
        output = subprocess.check_output([*command, new], text=True)
        observed = json.loads(output)
        assert observed["status"] == "selected_for_next_invocation"
        assert observed["previous_worker_release"] is None
        assert observed == json.loads(receipt.read_text())
        assert releases.current(root) == root / new
