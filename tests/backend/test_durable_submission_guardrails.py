from src.backend.workflows.workflow_purity_report import (
    ALLOWED_DIRECT_INSTANCE_CREATE_CALLSITES,
    build_workflow_purity_report,
)


def test_direct_durable_instance_creation_is_restricted_to_authoritative_paths() -> None:
    report = build_workflow_purity_report(registry=None)
    direct_create = report["details"]["direct_instance_create"]
    offenders = sorted(
        {
            f"{entry['path']}:{entry['line']}"
            for entry in direct_create["offending_callsites"]
        }
    )

    assert offenders == [], (
        "Direct durable instance creation bypasses the verified submission pathway. "
        "Route user-facing launches through "
        "workflow_instance_submission_service.submit_verified_workflow_instance(). "
        f"Allowed direct paths: {sorted(ALLOWED_DIRECT_INSTANCE_CREATE_CALLSITES)}. "
        f"Offending callsites: {offenders}"
    )
