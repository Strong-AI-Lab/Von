from __future__ import annotations

import json
from pathlib import Path

from src.backend.workflows import workflow_authority_review_snapshot_service as service


def test_build_workflow_authority_review_snapshot_documents_is_deterministic(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        service,
        "discover_workflow_ids",
        lambda: ["#V#b_workflow", "#V#a_workflow", "#V#a_workflow"],
    )
    monkeypatch.setattr(
        service,
        "build_workflow_process_graph",
        lambda workflow_id: (
            {
                "workflow_id": workflow_id,
                "initial_step": "start",
                "steps": [{"step_id": "start"}, {"step_id": "done"}],
            },
            [],
        ),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_description",
        lambda workflow_id, **_kwargs: (f"Description for {workflow_id}", "vontology"),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_publication_lifecycle",
        lambda workflow_id: ({"published": True}, "concept_data"),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_routing_profile",
        lambda workflow_id: ({"role": "authoring"}, "text_relation:#V#x"),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_typed_subworkflow_route_map",
        lambda workflow_id: (
            {
                "schema_version": "workflow_typed_subworkflow_route_map.v1",
                "default_route_key": "interpret",
                "routes": [{"route_key": "interpret", "selected_route_mode": "interpret"}],
            },
            "text_relation:#V#route_map",
        ),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_discovery_exemplars",
        lambda workflow_id: (
            {"keywords": [workflow_id], "examples": [f"Example {workflow_id}"]},
            "text_relation:#V#y",
        ),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_background_launch_policy",
        lambda workflow_id: ({"enabled": True}, "text_relation:#V#z"),
    )
    monkeypatch.setattr(
        service,
        "resolve_workflow_launch_input_contract",
        lambda workflow_id: (
            {"schema_version": "workflow_launch_input_contract.v1"},
            "text_relation:#V#c",
        ),
    )
    monkeypatch.setattr(
        service,
        "build_workflow_definition_identity_from_graph",
        lambda **kwargs: {"workflow_id": kwargs["workflow_id"], "definition_hash": "graph"},
    )
    monkeypatch.setattr(
        service,
        "load_workflow_template_bundle",
        lambda **_kwargs: {
            "source": "vontology",
            "templates": {
                "workflow_creation.default_marker": {"concept_id": "#V#template_a"},
            },
            "profiles": {
                "workflow_creation.default_marker": {"priority": 1},
            },
            "errors_by_concept_id": {},
        },
    )

    documents = service.build_workflow_authority_review_snapshot_documents()

    assert list(documents) == list(service.WORKFLOW_AUTHORITY_REVIEW_FILE_ORDER)
    manifest = documents[service.WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME]
    workflows = documents[service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME]
    templates = documents[service.WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME]

    assert manifest["workflow_count"] == 2
    assert workflows["workflow_ids"] == ["#V#a_workflow", "#V#b_workflow"]
    assert [item["workflow_id"] for item in workflows["workflows"]] == [
        "#V#a_workflow",
        "#V#b_workflow",
    ]
    assert workflows["workflows"][0]["typed_subworkflow_route_map"]["default_route_key"] == "interpret"
    assert templates["template_ids"] == ["workflow_creation.default_marker"]
    rendered = service.render_workflow_authority_review_snapshot_documents()
    assert rendered[service.WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME].endswith("\n")
    parsed_manifest = json.loads(
        rendered[service.WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME]
    )
    assert parsed_manifest["notice"] == service.WORKFLOW_AUTHORITY_REVIEW_NOTICE


def test_write_and_diff_workflow_authority_review_snapshot_manage_generated_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    documents = {
        service.WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME: {"workflow_count": 1},
        service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME: {"b": 2, "workflow_count": 1},
        service.WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME: {"c": 3, "template_count": 2},
    }
    monkeypatch.setattr(
        service,
        "build_workflow_authority_review_snapshot_documents",
        lambda: documents,
    )

    stale_file = tmp_path / "obsolete.generated.json"
    stale_file.write_text("{\"old\": true}\n", encoding="utf-8")

    write_report = service.write_workflow_authority_review_snapshot(output_dir=tmp_path)

    assert write_report["removed_stale_generated_files"] == ["obsolete.generated.json"]
    assert (
        tmp_path / service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME
    ).read_text(encoding="utf-8") == json.dumps(
        documents[service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME],
        indent=2,
        sort_keys=True,
    ) + "\n"

    diff_report = service.diff_workflow_authority_review_snapshot(output_dir=tmp_path)
    assert diff_report["has_differences"] is False

    (
        tmp_path / service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME
    ).write_text("{\n  \"b\": 999\n}\n", encoding="utf-8")
    diff_report = service.diff_workflow_authority_review_snapshot(output_dir=tmp_path)

    assert diff_report["has_differences"] is True
    difference = next(
        item
        for item in diff_report["differences"]
        if item["file"] == service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME
    )
    assert difference["status"] == "different"
    assert service.WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME in difference["unified_diff"]
