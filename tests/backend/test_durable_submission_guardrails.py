from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "src" / "backend"
DIRECT_CREATE_PATTERN = re.compile(r"\.create_instance(?:_for_event)?\(")

# JVNAUTOSCI-1518: user-facing durable launches must route through the verified
# submission service. These internals remain the only allowed direct callers.
ALLOWED_DIRECT_CREATE_CALLSITES = {
    "src/backend/workflows/durable/durable_executor.py",
    "src/backend/workflows/durable/instance_manager.py",
    "src/backend/workflows/durable/workflow_instance_submission_service.py",
}


def test_direct_durable_instance_creation_is_restricted_to_authoritative_paths() -> None:
    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        relative_path = path.relative_to(PROJECT_ROOT).as_posix()
        if not DIRECT_CREATE_PATTERN.search(path.read_text(encoding="utf-8")):
            continue
        if relative_path not in ALLOWED_DIRECT_CREATE_CALLSITES:
            offenders.append(relative_path)

    assert offenders == [], (
        "Direct durable instance creation bypasses the verified submission pathway. "
        "Route user-facing launches through "
        "workflow_instance_submission_service.submit_verified_workflow_instance(). "
        f"Offending files: {offenders}"
    )
