"""Optional disposable Android sessions behind a fixed operator host binding.

Only the trusted controller may invoke this executable. Requests never select
host paths, SDKs, accounts or shell commands. Same-account Unix access is not a
sandbox; separate untrusted tenants need separate prepared accounts.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:
    from .codex_von_instance import binding
    from .codex_von_capacity import admission, available_memory_bytes
except ImportError:
    from codex_von_instance import binding
    from codex_von_capacity import admission, available_memory_bytes

IDENTITY = (
    "instance_id",
    "agent_id",
    "delegator_id",
    "organisation_id",
    "host",
    "unix_account",
)


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def validate(spec):
    cfg = spec["android"]
    if [platform.system(), platform.machine()] != cfg["platform"]:
        raise ValueError("Unsupported test-host platform")
    if not cfg.get("capacity_enrolment_verified"):
        raise ValueError("Enrol test-host consumers in shared capacity first")
    if cfg["ram_mib"] < 1024 or cfg["cores"] < 1:
        raise ValueError("Invalid emulator resource budget")
    if cfg["cores"] > (os.cpu_count() or 1):
        raise ValueError("Insufficient host CPU capacity")
    if cfg["host_capacity"]["launch_headroom_bytes"] < cfg["ram_mib"] * 1024**2:
        raise ValueError("Headroom must include guest RAM and calibrated host overhead")
    sdk = Path(cfg["sdk_root"]).resolve()
    image = (sdk / cfg["system_image"]).resolve()
    image.relative_to(sdk)
    for p in (
        sdk / "emulator/emulator",
        sdk / "platform-tools/adb",
        image / "system.img",
    ):
        if not p.is_file():
            raise ValueError("Prepared SDK or image unavailable")
    root = Path(cfg["state_root"])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    return cfg, root


def command(cfg, *args, binary=False):
    result = subprocess.run(
        [
            str(Path(cfg["sdk_root"]) / "platform-tools/adb"),
            "-P",
            str(cfg["adb_port"]),
            "-s",
            "emulator-" + str(cfg["emulator_port"]),
            *args,
        ],
        capture_output=True,
        timeout=30,
        check=True,
    )
    return result.stdout if binary else result.stdout.decode().strip()


def ports_free(cfg):
    for port in (cfg["adb_port"], cfg["emulator_port"], cfg["emulator_port"] + 1):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))


def probe(cfg):
    sdk = Path(cfg["sdk_root"])
    acceleration = subprocess.run(
        [str(sdk / "emulator/emulator"), "-accel-check"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "platform": [platform.system(), platform.machine()],
        "acceleration_available": acceleration.returncode == 0,
        "emulator_version": subprocess.check_output(
            [str(sdk / "emulator/emulator"), "-version"], text=True
        ).splitlines()[0],
        "adb_version": subprocess.check_output(
            [str(sdk / "platform-tools/adb"), "version"], text=True
        ).splitlines()[:2],
        "ram_mib": cfg["ram_mib"],
        "cores": cfg["cores"],
        "coverage": "native_android_emulator",
        "limitations": [
            "Not physical-device evidence; browser and keyboard versions must be read per run.",
            "Disposable data; no enrolled Google account.",
        ],
    }


def current(root):
    path = root / "session.json"
    return json.loads(path.read_text()) if path.exists() else None


def own_session(state, request):
    if (
        not state
        or state["task_id"] != request["task_id"]
        or state["run_id"] != request["run_id"]
    ):
        raise PermissionError("Session does not belong to this task and run")


def live_process(state):
    if not state or not state.get("emulator_pid"):
        return False
    p = subprocess.run(
        ["ps", "-p", str(state["emulator_pid"]), "-o", "command="],
        text=True,
        capture_output=True,
    )
    return bool(
        p.returncode == 0 and state["avd_name"] in p.stdout and "qemu" in p.stdout
    )


def stop_owned(cfg, root, state):
    # The dedicated ADB ports are operator-reserved and checked before launch.
    # Do not touch another session if the retained process identity does not match.
    if live_process(state):
        os.kill(state["emulator_pid"], signal.SIGTERM)
        for _ in range(100):
            if not live_process(state):
                break
            time.sleep(0.1)
        if live_process(state):
            raise RuntimeError("Owned emulator did not stop; capacity remains occupied")
    adb_pid = state.get("adb_pid")
    if adb_pid:
        p = subprocess.run(
            ["ps", "-p", str(adb_pid), "-o", "command="], text=True, capture_output=True
        )
        if p.returncode == 0 and "adb" in p.stdout and str(cfg["adb_port"]) in p.stdout:
            os.kill(adb_pid, signal.SIGTERM)
    run_root = root / state["avd_name"]
    if run_root.is_dir():
        shutil.rmtree(run_root)
    (root / "control.sock").unlink(missing_ok=True)
    state.update(status="stopped", stopped_at=time.time())
    write(root / "session.json", state)


def serve(spec, request):
    cfg, root = validate(spec)
    state = current(root)
    own_session(state, request)
    stop_request = root / ("stop-" + request["run_id"] + ".json")
    with admission({"host_capacity": cfg["host_capacity"]}) as fds:
        if fds is None:
            state.update(status="waiting_for_capacity")
            write(root / "session.json", state)
            return
        ports_free(cfg)
        run_root = root / state["avd_name"]
        run_root.mkdir(mode=0o700)
        avds = run_root / "avd"
        avds.mkdir()
        avd = avds / (state["avd_name"] + ".avd")
        avd.mkdir()
        config = {
            "abi.type": cfg["abi"],
            "hw.cpu.arch": cfg["arch"],
            "hw.cpu.ncore": cfg["cores"],
            "hw.ramSize": cfg["ram_mib"],
            "hw.lcd.width": 1080,
            "hw.lcd.height": 1920,
            "hw.lcd.density": 420,
            "hw.keyboard": "no",
            "hw.mainKeys": "no",
            "hw.gpu.enabled": "yes",
            "hw.gpu.mode": "host",
            "hw.camera.back": "none",
            "hw.camera.front": "none",
            "image.sysdir.1": str(Path(cfg["sdk_root"]) / cfg["system_image"]) + "/",
            "disk.dataPartition.size": "6G",
            "showDeviceFrame": "no",
        }
        (avd / "config.ini").write_text(
            "\n".join(f"{k}={v}" for k, v in config.items())
        )
        (avds / (state["avd_name"] + ".ini")).write_text(f"path={avd}\n")
        env = {
            **os.environ,
            "ANDROID_SDK_ROOT": cfg["sdk_root"],
            "ANDROID_AVD_HOME": str(avds),
            "ANDROID_ADB_SERVER_PORT": str(cfg["adb_port"]),
        }
        env.pop("ANDROID_SERIAL", None)
        log = (run_root / "host.log").open("ab")
        adb = emulator = None

        def interrupted(*_):
            raise InterruptedError("Test-host session interrupted")

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            adb = subprocess.Popen(
                [
                    str(Path(cfg["sdk_root"]) / "platform-tools/adb"),
                    "-L",
                    f"tcp:127.0.0.1:{cfg['adb_port']}",
                    "nodaemon",
                    "server",
                ],
                env=env,
                stdout=log,
                stderr=log,
                pass_fds=fds,
            )
            state["adb_pid"] = adb.pid
            write(root / "session.json", state)
            time.sleep(1)
            if adb.poll() is not None:
                raise RuntimeError("Private ADB server failed")
            emulator = subprocess.Popen(
                [
                    str(Path(cfg["sdk_root"]) / "emulator/emulator"),
                    "-avd",
                    state["avd_name"],
                    "-memory",
                    str(cfg["ram_mib"]),
                    "-cores",
                    str(cfg["cores"]),
                    "-port",
                    str(cfg["emulator_port"]),
                    "-no-snapshot",
                    "-no-audio",
                    "-no-boot-anim",
                    "-gpu",
                    "host",
                ],
                env=env,
                stdout=log,
                stderr=log,
                pass_fds=fds,
            )
            state["emulator_pid"] = emulator.pid
            write(root / "session.json", state)
            # Protect indefinite failed boot; this bounds occupancy of the test host.
            deadline = time.monotonic() + cfg.get("boot_timeout_seconds", 600)
            while time.monotonic() < deadline:
                if stop_request.exists():
                    raise InterruptedError("Task requested stop during boot")
                if available_memory_bytes() < cfg["host_capacity"]["reserve_bytes"]:
                    raise RuntimeError("Host memory reserve crossed during boot")
                if emulator.poll() is not None:
                    raise RuntimeError("Emulator exited before boot")
                try:
                    if command(cfg, "shell", "getprop", "sys.boot_completed") == "1":
                        break
                except subprocess.SubprocessError:
                    pass
                time.sleep(2)
            else:
                raise RuntimeError("Emulator boot exceeded host occupancy bound")
            state.update(status="ready", environment=probe(cfg))
            state["environment"].update(
                android=command(cfg, "shell", "getprop", "ro.build.version.release"),
                sdk=command(cfg, "shell", "getprop", "ro.build.version.sdk"),
                build=command(cfg, "shell", "getprop", "ro.build.id"),
            )
            for name, package in [
                ("chrome", "com.android.chrome"),
                ("gboard", "com.google.android.inputmethod.latin"),
            ]:
                versions = command(cfg, "shell", "dumpsys", "package", package)
                state["environment"][name] = re.findall(
                    r"versionName=([^\s]+)", versions
                )
            write(root / "session.json", state)
            endpoint = root / "control.sock"
            endpoint.unlink(missing_ok=True)
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(endpoint))
                endpoint.chmod(0o600)
                listener.listen(2)
                listener.settimeout(1)
                active = time.monotonic()
                while emulator.poll() is None:
                    if stop_request.exists():
                        break
                    if available_memory_bytes() < cfg["host_capacity"]["reserve_bytes"]:
                        state["stop_reason"] = "host_memory_reserve_crossed"
                        break
                    if time.monotonic() - active > cfg.get(
                        "idle_timeout_seconds", 1800
                    ):
                        break
                    try:
                        client, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with client:
                        client.settimeout(5)
                        data = client.recv(65537)
                        try:
                            req = json.loads(data)
                            own_session(state, req)
                            result = operate(cfg, state, req)
                            active = time.monotonic()
                            client.sendall(json.dumps(result).encode())
                            if req["action"] == "stop":
                                break
                        except Exception as exc:
                            try:
                                client.sendall(
                                    json.dumps({"error": type(exc).__name__}).encode()
                                )
                            except OSError:
                                pass
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            for child in (emulator, adb):
                if child and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
            (root / "control.sock").unlink(missing_ok=True)
            state.update(status="stopped", stopped_at=time.time())
            write(root / "session.json", state)
            shutil.rmtree(run_root)
            log.close()


def operate(cfg, state, req):
    action = req["action"]
    if action == "status" or action == "stop":
        return dict(state)
    if action == "report":
        raw = json.dumps(state, indent=2).encode()
        return {
            **state,
            "artifact": {
                "filename": "android-run-report.json",
                "media_type": "application/json",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "data_base64": base64.b64encode(raw).decode(),
            },
        }
    if action == "screenshot":
        raw = command(cfg, "exec-out", "screencap", "-p", binary=True)
        return {
            **state,
            "artifact": {
                "filename": "android-screen.png",
                "media_type": "image/png",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "data_base64": base64.b64encode(raw).decode(),
            },
        }
    if action == "tap":
        x, y = req["x"], req["y"]
        if (
            type(x) is not int
            or type(y) is not int
            or not (0 <= x < 1080 and 0 <= y < 1920)
        ):
            raise ValueError("Tap outside display")
        command(cfg, "shell", "input", "tap", str(x), str(y))
    elif action == "text":
        value = req["text"]
        # adb shell joins arguments; only a literal, safely quoted text value.
        if (
            not isinstance(value, str)
            or len(value) > 2000
            or not re.fullmatch(r"[a-zA-Z0-9 .,!?@_\-]+", value)
        ):
            raise ValueError(
                "Native text input supports plain ASCII letters/digits/punctuation; use fixture tools for richer input"
            )
        command(cfg, "shell", "input", "text", "'" + value.replace(" ", "%s") + "'")
    elif action == "open_fixture":
        url = cfg["fixtures"][req["fixture"]]
        if not re.fullmatch(r"https?://[a-zA-Z0-9.:/_?=&%\-]+", url):
            raise ValueError("Invalid operator fixture URL")
        command(
            cfg,
            "shell",
            "am",
            "start",
            "-a",
            "android.intent.action.VIEW",
            "-d",
            "'" + url + "'",
            "com.android.chrome",
        )
    else:
        raise ValueError("Unsupported Android action")
    state.setdefault("actions", []).append(
        {"action": action, "observed_at": time.time()}
    )
    state["actions"] = state["actions"][-200:]
    return {**state, "performed": action}


def lifecycle(spec, request, binding_path):
    cfg, root = validate(spec)
    identity = {key: spec[key] for key in IDENTITY}
    allowed = {"action", "task_id", "run_id", "x", "y", "text", "fixture"}
    if request.get("action") not in {
        "discover",
        "start",
        "status",
        "stop",
        "screenshot",
        "report",
        "tap",
        "text",
        "open_fixture",
    }:
        raise ValueError("Unsupported Android action")
    if set(request) - allowed:
        raise ValueError("Unrecognised Android request fields")
    if request["action"] == "discover":
        return {
            **identity,
            "status": "available",
            **probe(cfg),
            "fixtures": list(cfg.get("fixtures", {})),
        }
    if not isinstance(request.get("task_id"), str) or not request["task_id"].startswith(
        "#V#"
    ):
        raise ValueError("Canonical task required")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", request.get("run_id", "")):
        raise ValueError("Stable run ID required")
    with (root / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = current(root)
        if request["action"] == "start":
            if state and state["status"] not in {"stopped", "waiting_for_capacity"}:
                own_session(state, request)
                return {
                    **identity,
                    **state,
                }  # lost acknowledgement never launches twice
            if (
                state
                and state["run_id"] == request["run_id"]
                and state["status"] == "stopped"
            ):
                own_session(state, request)
                return {
                    **identity,
                    **state,
                }  # explicit new run needed after termination
            digest = hashlib.sha256(
                (
                    spec["organisation_id"] + request["task_id"] + request["run_id"]
                ).encode()
            ).hexdigest()[:24]
            state = {
                "task_id": request["task_id"],
                "run_id": request["run_id"],
                "avd_name": "von_" + digest,
                "status": "starting",
                "started_at": time.time(),
            }
            write(root / "session.json", state)
            log = (root / "supervisor.log").open("ab")
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--binding",
                    str(binding_path),
                    "--serve",
                ],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            child.stdin.write(json.dumps(request).encode())
            child.stdin.close()
            state["supervisor_pid"] = child.pid
            # Supervisor records emulator state; do not overwrite its concurrent receipt.
            (root / "supervisor.pid").write_text(str(child.pid))
            log.close()
            return {**identity, **state}
        own_session(state, request)
        endpoint = root / "control.sock"
        if request["action"] == "stop":
            write(root / ("stop-" + request["run_id"] + ".json"), request)
        if not endpoint.exists():
            if request["action"] == "stop":
                pid_path = root / "supervisor.pid"
                if pid_path.exists():
                    pid = int(pid_path.read_text())
                    process = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "command="],
                        text=True,
                        capture_output=True,
                    )
                    if (
                        "--serve" in process.stdout
                        and str(binding_path) in process.stdout
                    ):
                        os.kill(pid, signal.SIGTERM)
                        return {**identity, **state, "status": "stopping"}
                stop_owned(cfg, root, state)
            return {**identity, **state, "supervisor_ready": False}
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(40)
            try:
                client.connect(str(endpoint))
            except (ConnectionRefusedError, FileNotFoundError):
                if request["action"] == "stop":
                    stop_owned(cfg, root, state)
                    return {**identity, **state}
                return {
                    **identity,
                    **state,
                    "supervisor_ready": False,
                    "recovery": "Controller disconnected; request stop before a new run",
                }
            client.sendall(json.dumps(request).encode())
            parts = []
            while data := client.recv(65536):
                parts.append(data)
                if sum(map(len, parts)) > 32 * 1024**2:
                    raise ValueError("Host evidence exceeds response bound")
        result = json.loads(b"".join(parts))
        if "error" in result:
            raise RuntimeError(result["error"])
        return {**identity, **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    spec = binding(args.binding)
    request = json.load(sys.stdin)
    if args.serve:
        try:
            serve(spec, request)
        except Exception as exc:
            root = Path(spec["android"]["state_root"])
            state = current(root)
            if state and state["run_id"] == request["run_id"]:
                state.update(status="stopped", error=type(exc).__name__)
                write(root / "session.json", state)
            raise
    else:
        print(json.dumps(lifecycle(spec, request, args.binding)))


if __name__ == "__main__":
    main()
