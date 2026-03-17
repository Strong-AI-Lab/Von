import json
from pathlib import Path

from src.backend.services.workflow_capability_service import (
    BUILTIN_WORKFLOW_CAPABILITIES,
)
from src.backend.workflows.workflow_purity_report import (
    build_workflow_purity_report,
)


class _DummyRegistry:
    def __init__(self, source_by_workflow_id: dict[str, str]) -> None:
        self._source_by_workflow_id = dict(source_by_workflow_id)

    def all_workflow_ids(self):
        return list(self._source_by_workflow_id)

    def get_registration(self, workflow_id: str):
        source = self._source_by_workflow_id.get(workflow_id)
        if source is None:
            return None
        return type("Registration", (), {"source": source})()


def _write(relative_path: str, text: str, *, root: Path) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_build_workflow_purity_report_counts_runtime_and_code_impurity(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/workflows/definitions.py",
        (
            "def build_chat_assistant_workflow():\n"
            "    return None\n\n"
            "def register_default_workflows(registry):\n"
            "    return registry\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/durable/sample_workflow.py",
        (
            "def build_sample_workflow():\n"
            "    return None\n\n"
            "def get_sample_workflow_registration():\n"
            "    return None\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/services/paper_representation_workflow_vontology_service.py",
        "def build_registration():\n    return WorkflowRegistration(workflow_id='#V#paper')\n",
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/workflow_selector.py",
        (
            "_LEGACY_CLASSIFIER_PROMPT = 'legacy'\n"
            "_LEGACY_DISCOVERY_SUFFIX = 'legacy'\n\n"
            "class WorkflowSelector:\n"
            "    def __init__(self, verdict_mapping=None):\n"
            "        self.verdict_mapping = verdict_mapping\n\n"
            "def _prepare_legacy_prompt():\n"
            "    return None\n\n"
            "def _parse_legacy_selection():\n"
            "    return None\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/durable/durable_executor.py",
        "manager.create_instance()\n",
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/durable/instance_manager.py",
        "self.create_instance()\n",
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/durable/workflow_instance_submission_service.py",
        "manager.create_instance_for_event()\n",
        root=tmp_path,
    )
    _write(
        "src/backend/services/workflow_event_integration_service.py",
        "# bootstrap-only path\n",
        root=tmp_path,
    )
    _write(
        "src/backend/mcp_server/vontology_mcp.json",
        "{}\n",
        root=tmp_path,
    )

    registry = _DummyRegistry(
        {
            "#V#chat_assistant_workflow": "built_in",
            "#V#tool_calling_workflow": "built_in",
            "#V#planning_workflow": "vontology",
            "#V#custom_runtime_workflow": "custom",
        }
    )
    baseline_path = tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 2,
                    "remaining_python_workflow_family_count": 3,
                    "direct_instance_create_callsite_count": 3,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 1,
                    "builtin_capability_override_count": len(
                        BUILTIN_WORKFLOW_CAPABILITIES
                    ),
                    "non_vontology_discoverable_workflow_count": 3,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=registry,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert report["counters"] == {
        "built_in_registration_count": 2,
        "remaining_python_workflow_family_count": 3,
        "direct_instance_create_callsite_count": 3,
        "env_event_binding_count": 0,
        "legacy_selector_mode_count": 1,
        "builtin_capability_override_count": len(BUILTIN_WORKFLOW_CAPABILITIES),
        "non_vontology_discoverable_workflow_count": 3,
    }
    assert (
        report["details"]["non_vontology_discoverable_workflow_ids"]
        == [
            "#V#chat_assistant_workflow",
            "#V#custom_runtime_workflow",
            "#V#tool_calling_workflow",
        ]
    )
    family_files = report["details"]["remaining_python_workflow_family_files"]
    assert [item["path"] for item in family_files] == [
        "src/backend/services/paper_representation_workflow_vontology_service.py",
        "src/backend/workflows/definitions.py",
        "src/backend/workflows/durable/sample_workflow.py",
    ]
    direct_calls = report["details"]["direct_instance_create"]["callsites"]
    assert len(direct_calls) == 3
    assert report["details"]["legacy_selector_support"]["matched_constructs"] == [
        "legacy_classifier_prompt",
        "legacy_discovery_suffix",
        "verdict_mapping",
        "prepare_legacy_prompt",
        "parse_legacy_selection",
    ]
    assert report["baseline"]["comparison"]["regression_detected"] is False


def test_build_workflow_purity_report_flags_baseline_regressions(tmp_path: Path) -> None:
    _write(
        "src/backend/workflows/definitions.py",
        "def register_default_workflows(registry):\n    return registry\n",
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/workflow_selector.py",
        "_LEGACY_CLASSIFIER_PROMPT = 'legacy'\nverdict_mapping = None\n",
        root=tmp_path,
    )
    _write(
        "src/backend/mcp_server/vontology_mcp.json",
        "{}\n",
        root=tmp_path,
    )

    baseline_path = tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    registry = _DummyRegistry({"#V#chat_assistant_workflow": "built_in"})
    report = build_workflow_purity_report(
        registry=registry,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    comparison = report["baseline"]["comparison"]
    assert comparison["regression_detected"] is True
    assert comparison["increased_counters"]["built_in_registration_count"] == {
        "baseline": 0,
        "current": 1,
        "delta": 1,
    }
    assert "builtin_capability_override_count" not in comparison["increased_counters"]
