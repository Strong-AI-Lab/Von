import socket
import subprocess
import sys
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("fcntl")
from scripts import codex_von_deploy as deployer


def test_public_health_identifies_client_and_preserves_service_credentials():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            agent = self.headers.get("User-Agent", "")
            seen.append((agent, self.headers.get("CF-Access-Client-Secret")))
            self.send_response(403 if agent.startswith("Python-urllib") else 200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert deployer.runtime._read_health(
            f"http://127.0.0.1:{server.server_port}/health",
            {"CF-Access-Client-Secret": "fixture-only"},
        ) == {"status": "healthy"}
        assert seen == [("Von-Deployment/1", "fixture-only")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_public_credentials_are_not_forwarded_through_redirects():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{self.server.server_port}/login-provider"
            )
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(deployer.runtime.HealthAccessError):
            deployer.runtime._read_health(
                f"http://127.0.0.1:{server.server_port}/health",
                {"CF-Access-Client-Secret": "fixture-only"},
            )
        assert paths == ["/health"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    config = {
        "primary_root": str(tmp_path / "primary"),
        "runtime_root": str(tmp_path / "runtime"),
        "state_root": str(tmp_path),
        "health_url": "http://localhost:5000/health",
        "public_health_url": "https://example.test/health",
    }
    previous = "a" * 40
    target = "b" * 40
    events = []
    monkeypatch.setattr(deployer.runtime, "_validate_runtime", lambda *a: None)
    monkeypatch.setattr(deployer.runtime, "_run", lambda *a, **kw: None)
    monkeypatch.setattr(deployer.runtime, "_git", lambda *a: target)
    monkeypatch.setattr(
        deployer.runtime,
        "_read_health",
        lambda *a: {"version_details": {"git_commit": previous}},
    )
    monkeypatch.setattr(
        deployer.runtime,
        "_health_error",
        lambda h, c: (
            None if h["version_details"]["git_commit"] == c else "wrong revision"
        ),
    )
    monkeypatch.setattr(deployer, "verify", lambda c, sha: {"commit": sha})
    monkeypatch.setattr(
        deployer,
        "recover",
        lambda c, sha: events.append(("recover", sha)) or {"commit": sha},
    )
    return config, previous, target, events, tmp_path / "receipt.json"


def test_startup_failure_recovers_once_and_keeps_failed_outcome(
    deployment, monkeypatch
):
    config, previous, target, events, path = deployment

    def fail(**kwargs):
        events.append(("deploy", kwargs["expected_commit"]))
        raise deployer.runtime.DeploymentError("fixture startup failure")

    monkeypatch.setattr(deployer.runtime, "deploy", fail)
    receipt = deployer.execute(config, target, path)
    assert receipt["status"] == "rolled_back"
    assert receipt["verification"]["commit"] == previous
    assert events == [("deploy", target), ("recover", previous)]
    assert deployer.execute(config, target, path) == receipt
    assert len(events) == 2


def test_interrupted_completed_deployment_is_read_back_without_restart(
    deployment, monkeypatch
):
    config, previous, target, events, path = deployment
    deployer.save(
        path,
        {"status": "started", "requested_commit": target, "previous_commit": previous},
    )
    monkeypatch.setattr(
        deployer.runtime,
        "_read_health",
        lambda *a: {"version_details": {"git_commit": target}},
    )
    monkeypatch.setattr(
        deployer.runtime, "deploy", lambda **kw: pytest.fail("restarted")
    )
    assert deployer.execute(config, target, path)["status"] == "deployed"
    assert not events


def test_recovery_failure_is_preserved_without_claiming_deployment(
    deployment, monkeypatch
):
    config, previous, target, _events, path = deployment
    deployer.save(
        path,
        {"status": "started", "requested_commit": target, "previous_commit": previous},
    )

    def fail(*args):
        raise deployer.runtime.DeploymentError("fixture recovery failure")

    monkeypatch.setattr(deployer, "recover", fail)
    assert deployer.execute(config, target, path)["status"] == "recovery_failed"


def test_stale_revision_has_no_deployment_effect(deployment, monkeypatch):
    config, previous, _target, events, path = deployment
    monkeypatch.setattr(
        deployer.runtime, "deploy", lambda **kw: pytest.fail("stale deployment")
    )
    with pytest.raises(deployer.runtime.DeploymentError, match="no longer origin/main"):
        deployer.execute(config, previous, path)
    assert not path.exists()
    assert not events


def test_recovery_only_cannot_start_a_new_deployment(deployment, monkeypatch):
    config, _previous, target, _events, path = deployment
    monkeypatch.setattr(
        deployer.runtime, "deploy", lambda **kw: pytest.fail("new deployment")
    )
    with pytest.raises(ValueError, match="existing deployment receipt"):
        deployer.execute(config, target, path, recover_only=True)
    assert not path.exists()


def test_web_and_worker_release_activation_share_recovery_receipt(
    deployment, monkeypatch
):
    config, previous, target, events, path = deployment
    root = path.parent / "releases"
    config["worker_release_root"] = str(root)
    monkeypatch.setattr(deployer.releases, "current", lambda _: root / previous)
    monkeypatch.setattr(deployer.releases, "verify", lambda *a: {})
    monkeypatch.setattr(deployer.releases, "prepare", lambda p, r, c: root / c)
    monkeypatch.setattr(deployer.runtime, "deploy", lambda **_: events.append("web"))
    monkeypatch.setattr(
        deployer, "verify", lambda c, sha: events.append("health") or {"commit": sha}
    )
    monkeypatch.setattr(
        deployer.releases,
        "activate",
        lambda r, t: events.append("pointer") or {"commit": target},
    )
    receipt = deployer.execute(config, target, path)
    assert receipt["status"] == "deployed"
    assert events == ["web", "health", "pointer"]
    assert receipt["previous_worker_release"] == str(root / previous)
    assert receipt["worker_activation"]["commit"] == target


def test_interrupted_pointer_switch_reconciles_without_web_restart(
    deployment, monkeypatch
):
    config, previous, target, events, path = deployment
    root = path.parent / "releases"
    config["worker_release_root"] = str(root)
    deployer.save(
        path,
        {
            "status": "started",
            "requested_commit": target,
            "previous_commit": previous,
            "previous_worker_release": str(root / previous),
        },
    )
    monkeypatch.setattr(
        deployer.runtime,
        "_read_health",
        lambda _: {"version_details": {"git_commit": target}},
    )
    monkeypatch.setattr(
        deployer.runtime, "deploy", lambda **_: pytest.fail("restarted web")
    )
    monkeypatch.setattr(deployer.releases, "prepare", lambda p, r, c: root / c)
    monkeypatch.setattr(deployer.releases, "activate", lambda r, t: {"commit": t.name})
    receipt = deployer.execute(config, target, path)
    assert receipt["status"] == "deployed"
    assert receipt["worker_activation"]["commit"] == target
    assert not events


def test_worker_activation_failure_restores_both_selections(deployment, monkeypatch):
    config, previous, target, events, path = deployment
    root = path.parent / "releases"
    config["worker_release_root"] = str(root)
    monkeypatch.setattr(deployer.releases, "current", lambda _: root / previous)
    monkeypatch.setattr(deployer.releases, "verify", lambda *a: {})
    monkeypatch.setattr(deployer.releases, "prepare", lambda p, r, c: root / c)
    monkeypatch.setattr(deployer.runtime, "deploy", lambda **_: None)

    def activate(r, target_path):
        events.append(("pointer", Path(target_path).name))
        if Path(target_path).name == target:
            raise OSError("fixture pointer switch failure")
        return {"commit": previous}

    monkeypatch.setattr(deployer.releases, "activate", activate)
    receipt = deployer.execute(config, target, path)
    assert receipt["status"] == "rolled_back"
    assert receipt["worker_activation"]["commit"] == previous
    assert events[-2:] == [("pointer", previous), ("recover", previous)]


def test_worker_rollback_failure_does_not_skip_web_recovery(deployment, monkeypatch):
    config, previous, target, events, path = deployment
    config["worker_release_root"] = str(path.parent / "releases")
    deployer.save(
        path,
        {
            "status": "started",
            "requested_commit": target,
            "previous_commit": previous,
            "previous_worker_release": "/fixture/old",
        },
    )

    def fail(*a):
        raise OSError("fixture inaccessible release")

    monkeypatch.setattr(deployer.releases, "activate", fail)
    receipt = deployer.execute(config, target, path)
    assert receipt["status"] == "recovery_failed"
    assert receipt["worker_recovery_failure_type"] == "OSError"
    assert receipt["verification"]["commit"] == previous
    assert events == [("recover", previous)]


def test_revoked_request_cannot_select_a_new_worker_release(deployment, monkeypatch):
    config, previous, target, events, path = deployment
    root = path.parent / "releases"
    config["worker_release_root"] = str(root)
    deployer.save(
        path,
        {
            "status": "started",
            "requested_commit": target,
            "previous_commit": previous,
            "previous_worker_release": str(root / previous),
        },
    )
    monkeypatch.setattr(
        deployer.runtime,
        "_read_health",
        lambda _: {"version_details": {"git_commit": target}},
    )
    monkeypatch.setattr(deployer.releases, "current", lambda _: root / previous)
    monkeypatch.setattr(deployer.runtime, "_git", lambda *a: previous)
    monkeypatch.setattr(
        deployer.releases, "prepare", lambda *a: pytest.fail("new activation")
    )
    monkeypatch.setattr(
        deployer.releases, "activate", lambda r, p: {"commit": Path(p).name}
    )
    receipt = deployer.execute(config, target, path, recover_only=True)
    assert receipt["status"] == "rolled_back"
    assert receipt["worker_activation"]["commit"] == previous


def test_failed_real_process_restores_previous_git_revision_and_http_service(
    tmp_path, monkeypatch
):
    from tests.test_deploy_local_main import _git, _setup_repositories

    _, seed, primary, checkout = _setup_repositories(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    (seed / ".gitignore").write_text("*.pid\n*.log\n__pycache__/\n")
    (seed / "pdm.lock").write_text("# unchanged fixture environment\n")
    (seed / "run.sh").write_text(f'#!/bin/sh\nexec {sys.executable} launcher.py "$@"\n')
    (seed / "launcher.py").write_text(
        """import os, pathlib, signal, subprocess, sys, time
p=pathlib.Path('fixture.pid')
action=sys.argv[1]
port=sys.argv[sys.argv.index('-Port')+1]
if action in ('stop','restart') and p.exists():
 try: os.kill(int(p.read_text()),signal.SIGTERM)
 except ProcessLookupError: pass
 p.unlink()
 time.sleep(0.15)
if action in ('start','restart'):
 with open('fixture.log','ab') as log:
  child=subprocess.Popen([sys.executable,'server.py',port],stdout=log,stderr=log,start_new_session=True)
 p.write_text(str(child.pid))
"""
    )
    (seed / "server.py").write_text(
        """import http.server, json, os, pathlib, subprocess, sys
if pathlib.Path('fail-startup').exists():sys.exit(7)
commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
payload={'status':'healthy','pid':os.getpid(),'version_details':{'git_commit':commit,'git_dirty':False},'runtime_authority':{'durable_workflows':{'database_connected':True,'worker_running':True,'scheduler_running':True},'startup_seed_materialisations':{'ready':True,'state':'ready'}}}
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  data=json.dumps(payload).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
class Server(http.server.HTTPServer):allow_reuse_address=True
Server(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
"""
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "healthy fixture")
    _git(seed, "push", "origin", "main")
    previous = deployer.runtime._prepare_primary(
        primary, remote="origin", branch="main"
    )
    deployer.runtime._prepare_runtime(primary, checkout, previous)
    subprocess.run(
        ["bash", "run.sh", "start", "-Port", str(port)], cwd=checkout, check=True
    )
    url = f"http://127.0.0.1:{port}/health"
    deployer.runtime._wait_for_verified_health(url, previous, 3)
    (seed / "fail-startup").write_text("intentional fixture failure\n")
    _git(seed, "add", "fail-startup")
    _git(seed, "commit", "-m", "failing fixture")
    _git(seed, "push", "origin", "main")
    target = _git(seed, "rev-parse", "HEAD")
    config = {
        "primary_root": str(primary),
        "runtime_root": str(checkout),
        "state_root": str(tmp_path),
        "health_url": url,
        "public_health_url": url,
        "health_timeout_seconds": 3,
    }
    monkeypatch.setattr(
        deployer.runtime, "_stop_verified_predecessor", lambda **kw: None
    )
    monkeypatch.setattr(
        deployer.runtime, "_reconcile_startup_seed_materialisations", lambda *a: None
    )
    monkeypatch.setattr(deployer.runtime, "_verify_workers", lambda *a: (1, 2))
    try:
        receipt = deployer.execute(config, target, tmp_path / "real-recovery.json")
        assert receipt["status"] == "rolled_back"
        assert _git(checkout, "rev-parse", "HEAD") == previous
        assert _git(primary, "rev-parse", "HEAD") == target
        assert (
            deployer.runtime._read_health(url)["version_details"]["git_commit"]
            == previous
        )
    finally:
        subprocess.run(
            ["bash", "run.sh", "stop", "-Port", str(port)], cwd=checkout, check=True
        )
