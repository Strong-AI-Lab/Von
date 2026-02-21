from __future__ import annotations

from src.backend.services import coding_agent_identity_bootstrap_service as bootstrap_service


def test_ensure_coding_agent_identity_concepts_creates_missing_type_and_instance(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bootstrap_service, "_bootstrap_completed", False)

    existing: set[str] = {
        bootstrap_service.THING_PRIMARY_ID,
        bootstrap_service.AGENT_PARENT_TYPE_ID,
        bootstrap_service.VON_SYSTEM_ID,
    }
    create_calls: list[dict] = []
    relationship_calls: list[tuple[str, str, str]] = []

    def _fake_get_concept_by_concept_id(concept_id: str):
        if concept_id in existing:
            return {"concept_id": concept_id}
        raise RuntimeError("not found")

    def _fake_create_concept(**kwargs):
        create_calls.append(dict(kwargs))
        concept_id = str(kwargs.get("concept_id"))
        existing.add(concept_id)
        return {"concept_id": concept_id}

    def _fake_add_relationship(source_id: str, predicate: str, target: str):
        relationship_calls.append((source_id, predicate, target))
        return {"success": True}

    monkeypatch.setattr(
        bootstrap_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )
    monkeypatch.setattr(
        bootstrap_service.concept_service,
        "create_concept",
        _fake_create_concept,
    )
    monkeypatch.setattr(bootstrap_service, "add_relationship", _fake_add_relationship)
    monkeypatch.setattr(
        bootstrap_service,
        "ensure_thing_exists_and_link_orphans",
        lambda: {"thing_created": False},
    )

    result = bootstrap_service.ensure_coding_agent_identity_concepts(force=True)
    assert result.get("cached") is False
    assert result.get("type_created") is True
    assert result.get("instance_created") is True
    assert result.get("parent_concept_id_used") == bootstrap_service.AGENT_PARENT_TYPE_ID
    assert result.get("errors") == []

    assert len(create_calls) == 2
    assert create_calls[0]["concept_id"] == bootstrap_service.CODING_AGENT_TYPE_ID
    assert create_calls[0]["parent_concept_ids"] == [bootstrap_service.AGENT_PARENT_TYPE_ID]
    assert create_calls[0]["create_as_instance"] is False
    assert create_calls[1]["concept_id"] == bootstrap_service.GITHUB_COPILOT_INSTANCE_ID
    assert create_calls[1]["parent_concept_ids"] == [bootstrap_service.CODING_AGENT_TYPE_ID]
    assert create_calls[1]["create_as_instance"] is True

    assert relationship_calls == [
        (
            bootstrap_service.CODING_AGENT_TYPE_ID,
            "related_to",
            bootstrap_service.VON_SYSTEM_ID,
        ),
        (
            bootstrap_service.GITHUB_COPILOT_INSTANCE_ID,
            "related_to",
            bootstrap_service.VON_SYSTEM_ID,
        ),
    ]

    cached_result = bootstrap_service.ensure_coding_agent_identity_concepts(force=False)
    assert cached_result.get("cached") is True
    assert len(create_calls) == 2


def test_ensure_coding_agent_identity_concepts_falls_back_to_thing_parent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(bootstrap_service, "_bootstrap_completed", False)

    existing: set[str] = {bootstrap_service.THING_PRIMARY_ID}
    create_calls: list[dict] = []
    relationship_calls: list[tuple[str, str, str]] = []
    ensure_thing_calls: list[bool] = []

    def _fake_get_concept_by_concept_id(concept_id: str):
        if concept_id in existing:
            return {"concept_id": concept_id}
        raise RuntimeError("not found")

    def _fake_create_concept(**kwargs):
        create_calls.append(dict(kwargs))
        concept_id = str(kwargs.get("concept_id"))
        existing.add(concept_id)
        return {"concept_id": concept_id}

    def _fake_add_relationship(source_id: str, predicate: str, target: str):
        relationship_calls.append((source_id, predicate, target))
        return {"success": True}

    def _fake_ensure_thing():
        ensure_thing_calls.append(True)
        return {"thing_created": False}

    monkeypatch.setattr(
        bootstrap_service.concept_service,
        "get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )
    monkeypatch.setattr(
        bootstrap_service.concept_service,
        "create_concept",
        _fake_create_concept,
    )
    monkeypatch.setattr(bootstrap_service, "add_relationship", _fake_add_relationship)
    monkeypatch.setattr(
        bootstrap_service,
        "ensure_thing_exists_and_link_orphans",
        _fake_ensure_thing,
    )

    result = bootstrap_service.ensure_coding_agent_identity_concepts(force=True)
    assert result.get("cached") is False
    assert result.get("errors") == []
    assert result.get("parent_concept_id_used") == bootstrap_service.THING_PRIMARY_ID
    assert result.get("type_created") is True
    assert result.get("instance_created") is True
    assert result.get("type_related_to_von_system") is False
    assert result.get("instance_related_to_von_system") is False
    assert result.get("bootstrap_completed") is False
    assert ensure_thing_calls == []
    assert relationship_calls == []

    assert len(create_calls) == 2
    assert create_calls[0]["parent_concept_ids"] == [bootstrap_service.THING_PRIMARY_ID]

    follow_up = bootstrap_service.ensure_coding_agent_identity_concepts(force=False)
    assert follow_up.get("cached") is False

