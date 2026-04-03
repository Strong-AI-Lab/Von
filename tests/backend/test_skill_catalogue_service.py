from __future__ import annotations

import json
from pathlib import Path

from src.backend.services import skill_catalogue_service as service


def _write_skill(
    root: Path,
    *,
    skill_name: str = "triage-issue",
    description: str = "Triage an issue deterministically.",
    argument_hint: str | None = "issue key and expected outcome",
    disable_model_invocation: bool = False,
    body: str = "Follow the checklist in [guide](guide.md).",
) -> Path:
    skill_dir = root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter_lines = [
        "---",
        f"name: {skill_name}",
        f"description: {description}",
        f"user-invokable: {'true'}",
        f"disable-model-invocation: {'true' if disable_model_invocation else 'false'}",
    ]
    if argument_hint is not None:
        frontmatter_lines.append(f"argument-hint: {argument_hint}")
    frontmatter_lines.extend(["---", body, ""])
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("\n".join(frontmatter_lines), encoding="utf-8")
    (skill_dir / "guide.md").write_text(
        "Use the repository issue template before proposing changes.",
        encoding="utf-8",
    )
    return skill_file


def test_list_skill_catalogue_builds_entries_from_real_skill_files(tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    _write_skill(project_root)

    payload = service.list_skill_catalogue(
        include_default_roots=False,
        project_roots=[str(project_root)],
        include_body=True,
    )

    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["roots_by_scope"]["project"] == [str(project_root.resolve())]
    skill = payload["skills"][0]
    assert skill["name"] == "triage-issue"
    assert skill["workflow_id"] == "#V#skill_triage_issue_workflow"
    assert skill["skill_concept_id"].startswith("#V#agent_skill_triage_issue_")
    assert skill["interop_metadata"]["workflow_id"] == "#V#skill_triage_issue_workflow"
    assert skill["body"].startswith("Follow the checklist")
    assert "guide.md" in skill["referenced_resources"]


def test_sync_skill_catalogue_materialises_projection(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    _write_skill(project_root)

    create_calls: list[dict[str, object]] = []
    upsert_calls: list[dict[str, object]] = []

    monkeypatch.setattr(service, "load_concept", lambda concept_id: None)
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: create_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(service, "ensure_instance_typing", lambda **kwargs: False)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: upsert_calls.append(dict(kwargs))
        or {"success": True, "kept_relation_id": "rel-1"},
    )
    monkeypatch.setattr(
        service,
        "get_text_relations_summary",
        lambda *args, **kwargs: {"groups": []},
    )
    monkeypatch.setattr(
        service,
        "delete_text_relation",
        lambda *args, **kwargs: None,
    )

    payload = service.sync_skill_catalogue_to_vontology(
        include_default_roots=False,
        project_roots=[str(project_root)],
    )

    assert payload["success"] is True
    assert payload["count"] == 1
    assert service.AGENT_SKILL_TYPE_ID in payload["ensured_concept_ids"]
    created_concept_ids = {call["concept_id"] for call in create_calls}
    assert service.AGENT_SKILL_TYPE_ID in created_concept_ids
    synced_skill = payload["synced_skills"][0]
    assert synced_skill["skill_concept_id"] in created_concept_ids

    by_predicate = {
        call["predicate"]: call["text"]
        for call in upsert_calls
        if call["subject_concept_id"] == synced_skill["skill_concept_id"]
    }
    assert by_predicate["hasDescription"] == "Triage an issue deterministically."
    assert by_predicate["hasContent"] == "Follow the checklist in [guide](guide.md)."
    assert (
        by_predicate[service.SKILL_WORKFLOW_ID_PREDICATE_ID]
        == "#V#skill_triage_issue_workflow"
    )
    metadata = json.loads(
        str(by_predicate[service.SKILL_INTEROP_METADATA_JSON_PREDICATE_ID])
    )
    assert metadata["workflow_id"] == "#V#skill_triage_issue_workflow"
    assert metadata["skill_concept_id"] == synced_skill["skill_concept_id"]
    assert by_predicate["#V#has_skill_name"] == "triage-issue"
    assert (
        by_predicate["#V#has_skill_argument_hint"] == "issue key and expected outcome"
    )


def test_sync_skill_catalogue_dry_run_does_not_mutate(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    _write_skill(project_root)

    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected create")),
    )
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("unexpected upsert")),
    )

    payload = service.sync_skill_catalogue_to_vontology(
        include_default_roots=False,
        project_roots=[str(project_root)],
        dry_run=True,
    )

    assert payload["success"] is True
    assert payload["dry_run"] is True
    assert payload["count"] == 1


def test_sync_skill_catalogue_clears_optional_argument_hint(monkeypatch, tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    _write_skill(project_root, argument_hint=None)

    deleted_relation_ids: list[str] = []

    monkeypatch.setattr(service, "load_concept", lambda concept_id: None)
    monkeypatch.setattr(
        service.concept_service,
        "create_concept",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(service, "ensure_instance_typing", lambda **kwargs: False)
    monkeypatch.setattr(
        service,
        "upsert_singleton_text_relation",
        lambda **kwargs: {"success": True, "kept_relation_id": "rel-1"},
    )
    monkeypatch.setattr(
        service,
        "get_text_relations_summary",
        lambda *args, **kwargs: {
            "groups": [
                {
                    "predicate": "#V#has_skill_argument_hint",
                    "language": "en-NZ",
                    "relation_ids": ["rel-7"],
                }
            ]
        },
    )
    monkeypatch.setattr(
        service,
        "delete_text_relation",
        lambda concept_id, relation_id, garbage_collect=True: deleted_relation_ids.append(
            relation_id
        ),
    )

    payload = service.sync_skill_catalogue_to_vontology(
        include_default_roots=False,
        project_roots=[str(project_root)],
    )

    assert payload["success"] is True
    assert deleted_relation_ids == ["rel-7"]
