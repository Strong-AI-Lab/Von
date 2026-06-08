import json
from pathlib import Path

from src.backend.services.workflow_capability_service import (
    BUILTIN_WORKFLOW_CAPABILITIES,
)
from src.backend.workflows.workflow_registry import (
    LazyWorkflowRegistration,
    WorkflowRegistry,
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
        (
            "def _build_sample_workflow_spec():\n"
            "    return {'workflow_id': '#V#paper'}\n\n"
            "def build_registration():\n"
            "    return WorkflowRegistration(workflow_id='#V#paper')\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/workflow_concept_authority_service.py",
        "_CANONICAL_WORKFLOW_PUBLICATION_SPECS = {}\n",
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
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 2,
                    "remaining_python_workflow_family_count": 3,
                    "python_authored_canonical_workflow_source_count": 2,
                    "python_authored_workflow_prompt_source_count": 2,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 3,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 1,
                    "builtin_capability_override_count": len(
                        BUILTIN_WORKFLOW_CAPABILITIES
                    ),
                    "non_vontology_discoverable_workflow_count": 3,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
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
        "python_authored_canonical_workflow_source_count": 2,
        "python_authored_workflow_prompt_source_count": 0,
        "python_authored_support_prompt_source_count": 0,
        "direct_instance_create_callsite_count": 0,
        "env_event_binding_count": 0,
        "legacy_selector_mode_count": 1,
        "builtin_capability_override_count": len(BUILTIN_WORKFLOW_CAPABILITIES),
        "non_vontology_discoverable_workflow_count": 3,
        "repo_seed_authority_drift_path_count": 0,
        "vontology_first_seed_fallback_violation_count": 0,
        "workflow_id_special_case_count": 0,
        "supervised_fail_open_fallback_count": 0,
        "support_surface_policy_contract_violation_count": 0,
        "synthesized_launch_contract_count": 0,
    }
    assert report["details"]["non_vontology_discoverable_workflow_ids"] == [
        "#V#chat_assistant_workflow",
        "#V#custom_runtime_workflow",
        "#V#tool_calling_workflow",
    ]
    family_files = report["details"]["remaining_python_workflow_family_files"]
    assert [item["path"] for item in family_files] == [
        "src/backend/services/paper_representation_workflow_vontology_service.py",
        "src/backend/workflows/definitions.py",
        "src/backend/workflows/durable/sample_workflow.py",
    ]
    canonical_sources = report["details"]["python_authored_canonical_workflow_sources"]
    assert canonical_sources == [
        {
            "path": "src/backend/services/paper_representation_workflow_vontology_service.py",
            "matched_symbols": ["_build_sample_workflow_spec"],
        },
        {
            "path": "src/backend/workflows/workflow_concept_authority_service.py",
            "matched_symbols": ["canonical_publication_spec_literal"],
        },
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
    assert report["details"]["repo_seed_authority_drift"]["offending_paths"] == []
    assert (
        report["details"]["vontology_first_seed_fallback_contracts"]["violations"] == []
    )
    assert report["baseline"]["comparison"]["regression_detected"] is False


def test_build_workflow_purity_report_flags_baseline_regressions(
    tmp_path: Path,
) -> None:
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

    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
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


def test_build_workflow_purity_report_does_not_resolve_lazy_registrations(
    tmp_path: Path,
) -> None:
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": len(
                        BUILTIN_WORKFLOW_CAPABILITIES
                    ),
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    loader_calls: list[str] = []

    def _unexpected_loader(workflow_id: str):
        loader_calls.append(workflow_id)
        raise AssertionError(
            "purity reporting must not resolve lazy workflow definitions"
        )

    registry = WorkflowRegistry(definition_loader=_unexpected_loader)
    registry.register_lazy(
        LazyWorkflowRegistration(
            workflow_id="#V#lazy_vontology_workflow",
            source="vontology",
        )
    )

    report = build_workflow_purity_report(
        registry=registry,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert loader_calls == []
    assert report["details"]["registry_source_counts"] == {"vontology": 1}
    assert report["counters"]["built_in_registration_count"] == 0
    assert report["counters"]["non_vontology_discoverable_workflow_count"] == 0


def test_build_workflow_purity_report_flags_repo_seed_authority_drift(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/rogue_workflow_service.py",
        (
            "from src.backend.workflows.workflow_concept_authority_service import "
            "load_repo_seed_workflow_bundle\n"
            "from src.backend.workflows.workflow_template_profile_service import "
            "ensure_repo_seeded_workflow_template_bundle\n\n"
            "def bootstrap_runtime_authority():\n"
            "    load_repo_seed_workflow_bundle('bundle.json')\n"
            "    ensure_repo_seeded_workflow_template_bundle()\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    repo_seed_drift = report["details"]["repo_seed_authority_drift"]
    assert report["counters"]["repo_seed_authority_drift_path_count"] == 1
    assert repo_seed_drift["offending_paths"] == [
        "src/backend/services/rogue_workflow_service.py"
    ]
    assert sorted(item["pattern"] for item in repo_seed_drift["offending_matches"]) == [
        "repo_seed_bundle_loader",
        "repo_seed_template_hydration",
    ]


def test_build_workflow_purity_report_allows_episode_seed_bootstrap_service(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/episode_evaluation_workflow_vontology_service.py",
        (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_canonical_episode_evaluation_workflow():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    repo_seed_drift = report["details"]["repo_seed_authority_drift"]
    assert report["counters"]["repo_seed_authority_drift_path_count"] == 0
    assert repo_seed_drift["offending_paths"] == []


def test_build_workflow_purity_report_allows_conversation_turn_seed_bootstrap_service(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/conversation_turn_workflow_vontology_service.py",
        (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_canonical_conversation_turn_workflows():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    repo_seed_drift = report["details"]["repo_seed_authority_drift"]
    assert report["counters"]["repo_seed_authority_drift_path_count"] == 0
    assert repo_seed_drift["offending_paths"] == []


def test_build_workflow_purity_report_allows_retrieval_monitoring_seed_bootstrap_services(
    tmp_path: Path,
) -> None:
    for relative_path, function_name in {
        (
            "src/backend/services/"
            "concept_search_instance_retrieval_workflow_vontology_service.py"
        ): "bootstrap_canonical_concept_search_instance_retrieval_workflow",
        (
            "src/backend/services/"
            "entity_information_retrieval_workflow_vontology_service.py"
        ): "bootstrap_canonical_entity_information_retrieval_workflow",
        (
            "src/backend/services/"
            "multilingual_concept_enrichment_vontology_service.py"
        ): "bootstrap_canonical_multilingual_concept_enrichment_workflow",
        (
            "src/backend/services/"
            "turn_pipeline_monitoring_workflow_vontology_service.py"
        ): "bootstrap_canonical_turn_pipeline_monitoring_workflows",
    }.items():
        _write(
            relative_path,
            (
                "from src.backend.services.workflow_repo_seed_bootstrap import "
                "bootstrap_repo_seed_workflow_bundle\n\n"
                f"def {function_name}():\n"
                "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
            ),
            root=tmp_path,
        )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    repo_seed_drift = report["details"]["repo_seed_authority_drift"]
    assert report["counters"]["repo_seed_authority_drift_path_count"] == 0
    assert repo_seed_drift["offending_paths"] == []
    assert all(item["allowed"] for item in repo_seed_drift["matches"])


def test_build_workflow_purity_report_allows_vontology_materialisation_seed_services(
    tmp_path: Path,
) -> None:
    for relative_path, body in {
        (
            "src/backend/services/"
            "email_source_representation_convergence_workflow_vontology_service.py"
        ): (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_canonical_email_source_workflows():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
        ),
        (
            "src/backend/services/"
            "jira_task_incremental_import_workflow_vontology_service.py"
        ): (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_canonical_jira_task_incremental_import_workflow():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
        ),
        "src/backend/services/kr_materialisation_workflow_vontology_service.py": (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_canonical_kr_materialisation_workflows():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n\n"
            "def validate_kr_materialisation_seed_bundle(authority_service):\n"
            "    return authority_service.load_repo_seed_workflow_bundle('bundle.json')\n"
        ),
        (
            "src/backend/services/"
            "representation_workflow_routing_coverage_audit_vontology_service.py"
        ): (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_canonical_representation_workflow_routing_coverage_audit():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
        ),
    }.items():
        _write(relative_path, body, root=tmp_path)
    _write(
        "src/backend/services/unrelated_runtime_workflow_service.py",
        (
            "from src.backend.services.workflow_repo_seed_bootstrap import "
            "bootstrap_repo_seed_workflow_bundle\n\n"
            "def bootstrap_runtime_authority():\n"
            "    return bootstrap_repo_seed_workflow_bundle(asset_path='bundle.json')\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    repo_seed_drift = report["details"]["repo_seed_authority_drift"]
    assert report["counters"]["repo_seed_authority_drift_path_count"] == 1
    assert repo_seed_drift["offending_paths"] == [
        "src/backend/services/unrelated_runtime_workflow_service.py"
    ]
    assert all(
        item["allowed"]
        for item in repo_seed_drift["matches"]
        if item["path"] != "src/backend/services/unrelated_runtime_workflow_service.py"
    )


def test_build_workflow_purity_report_flags_workflow_id_special_case_branches(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/workflows/durable/turn_execution_actions.py",
        (
            "def _handle(selected_workflow_id):\n"
            "    if selected_workflow_id == '#V#arxiv_paper_representation_workflow':\n"
            "        return {'is_arxiv': True}\n"
            "    return {'is_arxiv': False}\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    special_cases = report["details"]["workflow_id_special_cases"]
    assert report["counters"]["workflow_id_special_case_count"] == 1
    assert special_cases["matches"] == [
        {
            "path": "src/backend/workflows/durable/turn_execution_actions.py",
            "line": 2,
            "workflow_ids": ["#V#arxiv_paper_representation_workflow"],
            "operators": ["Eq"],
        }
    ]


def test_build_workflow_purity_report_flags_supervised_fail_open_fallbacks(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/integrations/internal_mcp/orchestrator.py",
        (
            "class Orchestrator:\n"
            "    def execute_conversation_turn_supervised(self):\n"
            "        return self.run(prompt='retry the old path')\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    fail_open = report["details"]["supervised_fail_open_fallbacks"]
    assert report["counters"]["supervised_fail_open_fallback_count"] == 1
    assert fail_open["status"] == "violation"
    assert fail_open["offending_matches"] == [
        {
            "path": "src/backend/integrations/internal_mcp/orchestrator.py",
            "function": "execute_conversation_turn_supervised",
            "pattern": "fallback_to_legacy_run",
            "line": 3,
        }
    ]


def test_build_workflow_purity_report_flags_vontology_first_seed_contract_violations(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/workflows/workflow_template_profile_service.py",
        (
            "def load_workflow_template_bundle():\n"
            "    ensure_repo_seeded_workflow_template_bundle()\n"
            "    return _load_vontology_workflow_template_bundle_cached()\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/workflow_concept_authority_service.py",
        (
            "def publish_canonical_chat_workflow_graphs():\n"
            "    seed_canonical_workflow_publication_specs()\n"
            "    seed_canonical_workflow_text_relations()\n"
            "    _resolve_authoritative_publication_specs()\n"
            "    _resolve_authoritative_workflow_text_relations()\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    contracts = report["details"]["vontology_first_seed_fallback_contracts"]
    assert report["counters"]["vontology_first_seed_fallback_violation_count"] == 2
    assert sorted(item["name"] for item in contracts["violations"]) == [
        "canonical_workflow_publication_vontology_first",
        "workflow_template_bundle_vontology_first",
    ]


def test_build_workflow_purity_report_flags_python_authored_workflow_prompt_sources(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/workflow_gap_vontology_service.py",
        (
            'WORKFLOW_GAP_ANALYSIS_PROMPT = "Return JSON that analyses the gap and '
            'proposes reusable workflow behaviour for the user request."\n\n'
            "def _build_candidate_prompt_template():\n"
            '    return "Create a candidate workflow prompt body with acceptance checks '
            'and recent-turn context."\n'
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert report["counters"]["python_authored_workflow_prompt_source_count"] == 2
    assert report["details"]["python_authored_workflow_prompt_sources"] == [
        {
            "path": "src/backend/services/workflow_gap_vontology_service.py",
            "kind": "assignment",
            "symbol": "WORKFLOW_GAP_ANALYSIS_PROMPT",
            "line": 1,
        },
        {
            "path": "src/backend/services/workflow_gap_vontology_service.py",
            "kind": "function",
            "symbol": "_build_candidate_prompt_template",
            "line": 3,
        },
    ]


def test_build_workflow_purity_report_ignores_prompt_metadata_specs(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/episode_evaluation_workflow_vontology_service.py",
        (
            "_PROMPT_CONCEPT_SPECS = (\n"
            "    {\n"
            "        'concept_id': '#V#episode_evaluation_prompt',\n"
            "        'asset_path': 'episode_evaluation_prompt_seed.md',\n"
            "        'name': 'Episode critic evaluation prompt',\n"
            "        'description': 'Canonical prompt metadata for a Vontology "
            "prompt concept; the body remains in text relations.',\n"
            "    },\n"
            ")\n\n"
            "WORKFLOW_GAP_ANALYSIS_PROMPT = 'Return JSON that analyses the gap "
            "and proposes reusable workflow behaviour for the user request.'\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert report["counters"]["python_authored_workflow_prompt_source_count"] == 1
    assert report["details"]["python_authored_workflow_prompt_sources"] == [
        {
            "path": "src/backend/services/episode_evaluation_workflow_vontology_service.py",
            "kind": "assignment",
            "symbol": "WORKFLOW_GAP_ANALYSIS_PROMPT",
            "line": 10,
        },
    ]


def test_build_workflow_purity_report_flags_post_cleanup_support_surface_drift(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/integrations/internal_mcp/orchestrator.py",
        (
            "import re\n\n"
            "class Orchestrator:\n"
            "    _PROMPT_MCP_TOOL_FAMILY_PATTERN = re.compile(r'web|jira')\n\n"
            "    def build_prompt(self):\n"
            '        return """Current turn request to route:\\n'
            "Use the surrounding turn context to resolve references and continuity.\\n"
            "Keep this request as the immediate routing objective.\\n"
            '"""\n\n'
            "    def _extract_topic_keywords_from_context(self):\n"
            "        return ()\n\n"
            "    def mark_source(self):\n"
            "        return {'source': 'code_fallback'}\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/services/workflow_capability_service.py",
        (
            '"""Retired BM25/stopword core should only be mentioned in prose."""\n'
            "from rank_bm25 import BM25Okapi\n\n"
            "_STOP_WORDS = {'the', 'a'}\n\n"
            "def _tokenise_query(value):\n"
            "    return [value]\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/write_tool_policy.py",
        (
            "import re\n\n"
            "_CONFIRMATION_PATTERN = re.compile(r'yes')\n"
            "EXTRA_PATTERN = re.compile(r'create|update')\n"
            "INLINE_ALLOW = re.search(r'create', 'create task')\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert report["counters"]["python_authored_support_prompt_source_count"] == 1
    support_prompt_sources = report["details"]["python_authored_support_prompt_sources"]
    assert support_prompt_sources[0]["path"] == (
        "src/backend/integrations/internal_mcp/orchestrator.py"
    )
    assert support_prompt_sources[0]["line"] == 7
    assert str(support_prompt_sources[0]["preview"]).startswith(
        "Current turn request to route:\n"
        "Use the surrounding turn context to resolve references and continuity.\n"
        "Keep this request"
    )
    support_contracts = report["details"]["support_surface_policy_contracts"]
    assert report["counters"]["support_surface_policy_contract_violation_count"] == 10
    patterns = [item["pattern"] for item in support_contracts["violations"]]
    assert patterns.count("unexpected_write_tool_regex_backstop") == 3
    assert sorted(set(patterns)) == [
        "code_fallback_source_marker",
        "retired_semantic_regex_symbol",
        "retired_topic_keyword_helper",
        "retired_workflow_capability_bm25_import",
        "retired_workflow_capability_bm25_import_name",
        "retired_workflow_capability_stopword_symbol",
        "retired_workflow_capability_tokeniser_symbol",
        "unexpected_write_tool_regex_backstop",
    ]
    assert "domain_specific_arxiv_literal" not in patterns


def test_build_workflow_purity_report_ignores_explanatory_docstrings_in_guarded_files(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/workflow_capability_service.py",
        (
            '"""This file replaced the old BM25 and stopword implementation."""\n'
            "# BM25 / stopword references in comments should stay non-authoritative.\n"
            "def describe():\n"
            "    return 'workflow retrieval support'\n"
        ),
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/write_tool_policy.py",
        (
            '"""Bounded regex backstops remain allowed here."""\n'
            "import re\n\n"
            "_CONFIRMATION_PATTERN = re.compile(r'yes')\n"
            "_DESTRUCTIVE_MUTATION_PATTERN = re.compile(r'delete')\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert report["counters"]["support_surface_policy_contract_violation_count"] == 2
    patterns = [
        item["pattern"]
        for item in report["details"]["support_surface_policy_contracts"]["violations"]
    ]
    assert patterns == [
        "unexpected_write_tool_regex_backstop",
        "unexpected_write_tool_regex_backstop",
    ]


def test_build_workflow_purity_report_detects_presenter_nested_evidence_drift(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/server/routes/von_routes.py",
        (
            "def fallback():\n"
            "    return 'NESTED WORKFLOW EVIDENCE (authoritative):\\n"
            "Representation/read-back verified for #V#paper_nested'\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    assert report["counters"]["support_surface_policy_contract_violation_count"] == 2
    patterns = {
        item["pattern"]
        for item in report["details"]["support_surface_policy_contracts"]["violations"]
    }
    assert patterns == {
        "python_authored_presenter_nested_evidence_heading",
        "python_authored_presenter_readback_wording",
    }


def test_build_workflow_purity_report_detects_annotation_and_workflow_creation_drift(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/annotation_extraction_service.py",
        "def _infer_type_label(text):\n    return 'person'\n",
        root=tmp_path,
    )
    _write(
        "src/backend/workflows/durable/workflow_creation_workflow.py",
        (
            "_AUTO_WORKFLOW_ID_STOPWORDS = {'the'}\n"
            "def _extract_labeled_value(text):\n    return text\n"
        ),
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    patterns = {
        item["pattern"]
        for item in report["details"]["support_surface_policy_contracts"]["violations"]
    }
    assert "retired_annotation_type_inference_symbol" in patterns
    assert "retired_workflow_authoring_stopword_symbol" in patterns
    assert "retired_workflow_authoring_label_parser_symbol" in patterns


def test_build_workflow_purity_report_detects_file_copy_representation_drift(
    tmp_path: Path,
) -> None:
    _write(
        "src/backend/services/person_file_representation_service.py",
        "_EMAIL_PATTERN = None\n"
        "def _extract_candidate_names(text):\n    return []\n",
        root=tmp_path,
    )
    _write(
        "src/backend/services/company_file_representation_service.py",
        "_COMPANY_LABEL_PATTERN = None\n"
        "def _extract_candidate_company_names(text):\n    return []\n",
        root=tmp_path,
    )
    _write(
        "src/backend/services/meeting_file_representation_service.py",
        "_MEETING_TEXT_HINT_PATTERN = None\n"
        "def _extract_participants(text):\n    return []\n",
        root=tmp_path,
    )
    baseline_path = (
        tmp_path / "tests" / "backend" / "fixtures" / "workflow_purity_baseline.json"
    )
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow_purity_baseline.v1",
                "counters": {
                    "built_in_registration_count": 0,
                    "remaining_python_workflow_family_count": 0,
                    "python_authored_canonical_workflow_source_count": 0,
                    "python_authored_workflow_prompt_source_count": 0,
                    "python_authored_support_prompt_source_count": 0,
                    "direct_instance_create_callsite_count": 0,
                    "env_event_binding_count": 0,
                    "legacy_selector_mode_count": 0,
                    "builtin_capability_override_count": 0,
                    "non_vontology_discoverable_workflow_count": 0,
                    "repo_seed_authority_drift_path_count": 0,
                    "vontology_first_seed_fallback_violation_count": 0,
                    "workflow_id_special_case_count": 0,
                    "supervised_fail_open_fallback_count": 0,
                    "support_surface_policy_contract_violation_count": 0,
                    "synthesized_launch_contract_count": 0,
                },
            }
        ),
        encoding="utf-8",
    )

    report = build_workflow_purity_report(
        registry=None,
        project_root=tmp_path,
        baseline_path=baseline_path,
    )

    patterns = {
        item["pattern"]
        for item in report["details"]["support_surface_policy_contracts"]["violations"]
    }
    assert "retired_person_file_copy_semantic_regex_symbol" in patterns
    assert "retired_person_file_copy_semantic_helper_symbol" in patterns
    assert "retired_company_file_copy_semantic_regex_symbol" in patterns
    assert "retired_company_file_copy_semantic_helper_symbol" in patterns
    assert "retired_meeting_file_copy_semantic_regex_symbol" in patterns
    assert "retired_meeting_file_copy_semantic_helper_symbol" in patterns
