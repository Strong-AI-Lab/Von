from __future__ import annotations

import builtins
from typing import Any

import pytest

from src.backend.db.mongo_client import close_connection, get_db
from src.backend.services import concept_service
from src.backend.services.workflow_vontology_materialisation_helpers import (
    suspend_event_workflow_integration,
)


@pytest.fixture
def _reset_mock_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VON_USE_MOCK_DB", "1")
    close_connection()

    db = get_db()
    assert db is not None
    for collection_name in ("concepts", "text_relations", "text_values"):
        try:
            db.drop_collection(collection_name)
        except Exception:
            pass

    yield db

    close_connection()


def test_bulk_materialisation_skips_event_and_discovery_imports(
    _reset_mock_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked_attempts: list[str] = []
    original_import = builtins.__import__

    blocked_modules = {
        "src.backend.services.workflow_event_integration_service",
        "src.backend.services.workflow_discovery_service",
        "src.backend.workflows.durable.workflow_instance_submission_service",
    }

    def _guarded_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ):
        package = (
            globals.get("__package__")
            if isinstance(globals, dict)
            else None
        )
        resolved_name = name
        if level == 1 and isinstance(package, str) and package:
            resolved_name = f"{package}.{name}"
        if resolved_name in blocked_modules:
            blocked_attempts.append(resolved_name)
            raise ModuleNotFoundError(f"blocked import during bulk materialisation: {resolved_name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)

    with suspend_event_workflow_integration():
        created = concept_service.create_concept(
            name="Bulk Materialisation Guard Concept",
            concept_id="#V#bulk_materialisation_guard_concept",
            description="Guard regression concept",
            parent_concept_ids=[],
            create_as_instance=True,
        )
        updated = concept_service.update_concept(
            "#V#bulk_materialisation_guard_concept",
            {"attributes.guard_checked": True},
        )

    assert created["concept_id"] == "#V#bulk_materialisation_guard_concept"
    assert (updated.get("attributes") or {}).get("guard_checked") is True
    assert blocked_attempts == []
