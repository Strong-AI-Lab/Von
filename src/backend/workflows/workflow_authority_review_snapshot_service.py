"""Generated review snapshots for Vontology-authored workflow authority.

These snapshots are derived artefacts for human/agent inspection only. They
must never become an authored source of workflow truth.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Any

from .vontology_loader import (
    build_workflow_process_graph,
    discover_workflow_ids,
    resolve_workflow_background_launch_policy,
    resolve_workflow_description,
    resolve_workflow_discovery_exemplars,
    resolve_workflow_launch_input_contract,
    resolve_workflow_publication_lifecycle,
    resolve_workflow_routing_profile,
)
from .workflow_definition_identity_service import build_workflow_definition_identity_from_graph
from .workflow_template_profile_service import load_workflow_template_bundle

WORKFLOW_AUTHORITY_REVIEW_SNAPSHOT_SCHEMA_VERSION = (
    "workflow_authority_review_snapshot.v1"
)
WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "generated"
    / "workflow_authority_review_snapshots"
)
WORKFLOW_AUTHORITY_REVIEW_NOTICE = (
    "Generated review snapshot derived from authoritative Vontology workflow "
    "state. Non-authoritative: do not edit or treat these files as a source of truth."
)
WORKFLOW_AUTHORITY_REVIEW_COMMANDS = {
    "export": "pdm run python scripts/workflow_authority_review_snapshot.py export",
    "diff": "pdm run python scripts/workflow_authority_review_snapshot.py diff",
}
WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME = (
    "workflow_authority_manifest.generated.json"
)
WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME = (
    "workflow_authority_workflows.generated.json"
)
WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME = (
    "workflow_authority_templates.generated.json"
)
WORKFLOW_AUTHORITY_REVIEW_FILE_ORDER = (
    WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME,
    WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME,
    WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME,
)


def _stable_json_like(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return _stable_json_like(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _stable_json_like(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_json_like(item) for item in value]
    if isinstance(value, set):
        return sorted(_stable_json_like(item) for item in value)
    return str(value)


def _compact_mapping(
    payload: Mapping[str, Any],
    *,
    keep_empty_keys: Sequence[str] = (),
) -> dict[str, Any]:
    keep = {str(item) for item in keep_empty_keys}
    compacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key in keep:
            compacted[str(key)] = value
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, Mapping) and not value:
            continue
        if isinstance(value, Sequence) and not isinstance(value, str) and not value:
            continue
        compacted[str(key)] = value
    return compacted


def _build_workflow_review_entry(workflow_id: str) -> dict[str, Any]:
    workflow_id_text = str(workflow_id or "").strip()
    graph: dict[str, Any] | None = None
    graph_warnings: list[str] = []
    graph_error: str | None = None
    try:
        raw_graph, raw_warnings = build_workflow_process_graph(workflow_id_text)
        if isinstance(raw_graph, Mapping):
            graph = _stable_json_like(dict(raw_graph))
        if isinstance(raw_warnings, list):
            graph_warnings = [
                str(item).strip()
                for item in raw_warnings
                if isinstance(item, str) and str(item).strip()
            ]
    except Exception as exc:
        graph_error = str(exc)

    description, description_source = resolve_workflow_description(
        workflow_id_text,
        workflow_source="vontology",
    )
    publication_lifecycle, publication_lifecycle_source = (
        resolve_workflow_publication_lifecycle(workflow_id_text)
    )
    routing_profile, routing_profile_source = resolve_workflow_routing_profile(
        workflow_id_text
    )
    discovery_exemplars, discovery_exemplars_source = (
        resolve_workflow_discovery_exemplars(workflow_id_text)
    )
    background_launch_policy, background_launch_policy_source = (
        resolve_workflow_background_launch_policy(workflow_id_text)
    )
    launch_input_contract, launch_input_contract_source = (
        resolve_workflow_launch_input_contract(workflow_id_text)
    )

    return _compact_mapping(
        {
            "workflow_id": workflow_id_text,
            "authoritative_source": "vontology",
            "description": str(description or "").strip() or None,
            "description_source": str(description_source or "").strip() or None,
            "publication_lifecycle": _stable_json_like(publication_lifecycle),
            "publication_lifecycle_source": (
                str(publication_lifecycle_source or "").strip() or None
            ),
            "routing_profile": _stable_json_like(routing_profile),
            "routing_profile_source": str(routing_profile_source or "").strip() or None,
            "discovery_exemplars": _stable_json_like(discovery_exemplars),
            "discovery_exemplars_source": (
                str(discovery_exemplars_source or "").strip() or None
            ),
            "background_launch_policy": _stable_json_like(background_launch_policy),
            "background_launch_policy_source": (
                str(background_launch_policy_source or "").strip() or None
            ),
            "launch_input_contract": _stable_json_like(launch_input_contract),
            "launch_input_contract_source": (
                str(launch_input_contract_source or "").strip() or None
            ),
            "graph_identity": build_workflow_definition_identity_from_graph(
                workflow_id=workflow_id_text,
                graph=graph,
            ),
            "workflow_graph": graph,
            "workflow_graph_warnings": graph_warnings,
            "workflow_graph_error": graph_error,
        },
        keep_empty_keys=("workflow_id", "workflow_graph_warnings"),
    )


def _build_workflow_templates_snapshot() -> dict[str, Any]:
    bundle_error: str | None = None
    bundle: dict[str, Any] = {}
    try:
        loaded_bundle = load_workflow_template_bundle(auto_seed=False)
        if isinstance(loaded_bundle, Mapping):
            bundle = dict(loaded_bundle)
    except Exception as exc:
        bundle_error = str(exc)

    templates = dict(bundle.get("templates") or {})
    profiles = dict(bundle.get("profiles") or {})
    template_ids = sorted(
        {
            str(item).strip()
            for item in list(templates) + list(profiles)
            if isinstance(item, str) and str(item).strip()
        }
    )
    return {
        "schema_version": WORKFLOW_AUTHORITY_REVIEW_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_kind": "workflow_authority_review_templates",
        "authoritative_source": "vontology",
        "notice": WORKFLOW_AUTHORITY_REVIEW_NOTICE,
        "source": str(bundle.get("source") or "vontology"),
        "template_count": len(template_ids),
        "template_ids": template_ids,
        "templates": _stable_json_like(templates),
        "profiles": _stable_json_like(profiles),
        "errors_by_concept_id": _stable_json_like(
            dict(bundle.get("errors_by_concept_id") or {})
        ),
        "bundle_error": bundle_error,
    }


def build_workflow_authority_review_snapshot_documents() -> dict[str, dict[str, Any]]:
    workflow_ids = sorted(
        {
            str(item).strip()
            for item in discover_workflow_ids()
            if isinstance(item, str) and str(item).strip()
        }
    )
    workflow_entries = [
        _build_workflow_review_entry(workflow_id)
        for workflow_id in workflow_ids
    ]
    templates_payload = _build_workflow_templates_snapshot()
    manifest_payload = {
        "schema_version": WORKFLOW_AUTHORITY_REVIEW_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_kind": "workflow_authority_review_manifest",
        "authoritative_source": "vontology",
        "notice": WORKFLOW_AUTHORITY_REVIEW_NOTICE,
        "generated_filenames": list(WORKFLOW_AUTHORITY_REVIEW_FILE_ORDER),
        "workflow_count": len(workflow_entries),
        "template_count": int(templates_payload.get("template_count", 0)),
        "commands": dict(WORKFLOW_AUTHORITY_REVIEW_COMMANDS),
        "output_directory_contract": (
            "generated review snapshots only; never canonical authoring source"
        ),
    }
    workflows_payload = {
        "schema_version": WORKFLOW_AUTHORITY_REVIEW_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_kind": "workflow_authority_review_workflows",
        "authoritative_source": "vontology",
        "notice": WORKFLOW_AUTHORITY_REVIEW_NOTICE,
        "workflow_count": len(workflow_entries),
        "workflow_ids": workflow_ids,
        "workflows": workflow_entries,
    }
    return {
        WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME: manifest_payload,
        WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME: workflows_payload,
        WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME: templates_payload,
    }


def _render_snapshot_documents(
    documents: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    return {
        filename: json.dumps(payload, indent=2, sort_keys=True) + "\n"
        for filename, payload in documents.items()
    }


def render_workflow_authority_review_snapshot_documents() -> dict[str, str]:
    return _render_snapshot_documents(build_workflow_authority_review_snapshot_documents())


def write_workflow_authority_review_snapshot(
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir = (output_dir or WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    documents = build_workflow_authority_review_snapshot_documents()
    rendered = _render_snapshot_documents(documents)
    expected_names = set(rendered)
    removed_files: list[str] = []
    for stale_path in sorted(target_dir.glob("*.generated.json")):
        if stale_path.name in expected_names:
            continue
        stale_path.unlink()
        removed_files.append(stale_path.name)

    written_files: list[str] = []
    for filename in WORKFLOW_AUTHORITY_REVIEW_FILE_ORDER:
        text = rendered.get(filename)
        if text is None:
            continue
        output_path = target_dir / filename
        output_path.write_text(text, encoding="utf-8")
        written_files.append(filename)

    workflows_payload = documents[WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME]
    templates_payload = documents[WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME]
    return {
        "output_dir": str(target_dir),
        "written_files": written_files,
        "removed_stale_generated_files": removed_files,
        "workflow_count": int(workflows_payload.get("workflow_count", 0)),
        "template_count": int(templates_payload.get("template_count", 0)),
    }


def diff_workflow_authority_review_snapshot(
    *,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    target_dir = (output_dir or WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR).resolve()
    rendered = render_workflow_authority_review_snapshot_documents()
    differences: list[dict[str, Any]] = []

    for filename in WORKFLOW_AUTHORITY_REVIEW_FILE_ORDER:
        expected_text = rendered.get(filename)
        if expected_text is None:
            continue
        snapshot_path = target_dir / filename
        if snapshot_path.exists():
            existing_text = snapshot_path.read_text(encoding="utf-8")
            status = "different" if existing_text != expected_text else "unchanged"
        else:
            existing_text = ""
            status = "missing"
        if status == "unchanged":
            continue
        differences.append(
            {
                "file": filename,
                "status": status,
                "unified_diff": "\n".join(
                    unified_diff(
                        existing_text.splitlines(),
                        expected_text.splitlines(),
                        fromfile=f"{filename} (existing)",
                        tofile=f"{filename} (authoritative_vontology)",
                        lineterm="",
                    )
                ),
            }
        )

    extra_files = sorted(
        path.name
        for path in target_dir.glob("*.generated.json")
        if path.name not in rendered
    )
    for filename in extra_files:
        differences.append(
            {
                "file": filename,
                "status": "unexpected_extra_file",
                "unified_diff": "",
            }
        )

    return {
        "output_dir": str(target_dir),
        "has_differences": bool(differences),
        "difference_count": len(differences),
        "differences": differences,
    }


__all__ = [
    "WORKFLOW_AUTHORITY_REVIEW_COMMANDS",
    "WORKFLOW_AUTHORITY_REVIEW_MANIFEST_FILENAME",
    "WORKFLOW_AUTHORITY_REVIEW_NOTICE",
    "WORKFLOW_AUTHORITY_REVIEW_OUTPUT_DIR",
    "WORKFLOW_AUTHORITY_REVIEW_SNAPSHOT_SCHEMA_VERSION",
    "WORKFLOW_AUTHORITY_REVIEW_TEMPLATES_FILENAME",
    "WORKFLOW_AUTHORITY_REVIEW_WORKFLOWS_FILENAME",
    "build_workflow_authority_review_snapshot_documents",
    "diff_workflow_authority_review_snapshot",
    "render_workflow_authority_review_snapshot_documents",
    "write_workflow_authority_review_snapshot",
]
