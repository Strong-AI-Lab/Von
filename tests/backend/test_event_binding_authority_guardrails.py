from src.backend.workflows.workflow_purity_report import (
    build_workflow_purity_report,
)


def test_runtime_event_binding_authority_has_no_env_fallback() -> None:
    report = build_workflow_purity_report(registry=None)
    env_event_binding = report["details"]["env_event_binding_authority"]
    matches = env_event_binding["matches"]
    assert matches == []


def test_event_binding_tools_no_longer_advertise_env_fallback() -> None:
    report = build_workflow_purity_report(registry=None)
    env_event_binding = report["details"]["env_event_binding_authority"]
    offending_paths = sorted(
        {
            entry["path"]
            for entry in env_event_binding["matches"]
            if entry["pattern"] == "include_env_fallback"
        }
    )

    assert offending_paths == []
