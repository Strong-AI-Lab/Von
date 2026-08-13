from __future__ import annotations

import pytest

from src.backend.services import text_effect_readback_service as service
from src.backend.workflows.action_registry import ActionRegistry, WorkflowEnvironment
from src.backend.workflows.durable import control_flow_actions
from src.backend.workflows.durable.control_flow_actions import (
    register_control_flow_actions,
)
from src.backend.workflows.execution_contracts import (
    WORKFLOW_CONTROL_ACTION_TEXT_EFFECT_READBACK_ID,
)


def _receipt(
    predicate: str,
    text: str,
    *,
    concept_id: str = "#V#meeting",
    success: bool = True,
) -> dict:
    return {
        "tool": "upsert_singleton_text_relation",
        "status": "ok",
        "effective_arguments": {
            "concept_id": concept_id,
            "predicate": predicate,
            "text": text,
        },
        "effective_payload": {
            "success": success,
            "effect_status": "succeeded" if success else "failed",
            "concept_id": concept_id,
            "predicate": predicate,
        },
    }


@pytest.fixture
def canonical_rows(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict]]:
    rows = {
        "#V#date_of_event": [
            {"relation_id": "date-rel", "text": "2026-08-13"}
        ],
        "#V#has_start_time": [
            {"relation_id": "start-rel", "text": "2026-08-13T13:05:00+01:00"}
        ],
        "#V#has_end_time": [
            {"relation_id": "end-rel", "text": "2026-08-13T13:30:00+01:00"}
        ],
    }

    def _read(*, subject_concept_id, predicate, limit, context_view):
        assert subject_concept_id == "#V#meeting"
        assert limit == 20
        assert context_view == "actor_effective"
        return rows.get(predicate, [])

    monkeypatch.setattr(service, "get_texts_for_concept", _read)
    return rows


def test_text_effect_readback_verifies_required_and_extra_receipted_facts(
    canonical_rows: dict[str, list[dict]],
) -> None:
    receipts = [
        _receipt("#V#date_of_event", "2026-08-13"),
        _receipt("#V#has_start_time", "2026-08-13T13:05:00+01:00"),
        _receipt("#V#has_end_time", "2026-08-13T13:30:00+01:00"),
    ]

    result = service.verify_text_effect_readback(
        required_predicates=["#V#date_of_event", "#V#has_start_time"],
        optional_predicates=["#V#has_end_time"],
        text_effect_receipts=receipts,
        expected_concept_id="#V#meeting",
    )

    assert result["text_effect_readback_verified"] is True
    assert result["text_effect_readback_failure_code"] is None
    assert result["represented_concept_id"] == "#V#meeting"
    assert [row["predicate"] for row in result["verified_text_effects"]] == [
        "#V#date_of_event",
        "#V#has_start_time",
        "#V#has_end_time",
    ]


def test_text_effect_readback_derives_concept_from_llm_tool_invocations(
    canonical_rows: dict[str, list[dict]],
) -> None:
    result = service.verify_text_effect_readback(
        required_predicates=["#V#date_of_event", "#V#has_start_time"],
        tool_invocations=[
            _receipt("#V#date_of_event", "2026-08-13"),
            _receipt("#V#has_start_time", "2026-08-13T13:05:00+01:00"),
        ],
    )

    assert result["text_effect_readback_verified"] is True
    assert result["represented_concept_id"] == "#V#meeting"


def test_text_effect_readback_rejects_note_only_schedule(
    canonical_rows: dict[str, list[dict]],
) -> None:
    result = service.verify_text_effect_readback(
        required_predicates=["#V#date_of_event", "#V#has_start_time"],
        tool_invocations=[
            _receipt("#V#hasNote", "Thursday 13 August 2026, 1:05 pm")
        ],
    )

    assert result["text_effect_readback_verified"] is False
    assert (
        result["text_effect_readback_failure_code"]
        == "text_effect_concept_missing"
    )


def test_text_effect_readback_rejects_wrong_canonical_value(
    canonical_rows: dict[str, list[dict]],
) -> None:
    result = service.verify_text_effect_readback(
        required_predicates=["#V#date_of_event", "#V#has_start_time"],
        tool_invocations=[
            _receipt("#V#date_of_event", "2026-08-13"),
            _receipt("#V#has_start_time", "2026-08-13T14:05:00+01:00"),
        ],
    )

    assert result["text_effect_readback_verified"] is False
    assert (
        result["text_effect_readback_failure_code"]
        == "text_effect_exact_readback_missing"
    )


def test_text_effect_readback_rejects_ambiguous_concept_receipts(
    canonical_rows: dict[str, list[dict]],
) -> None:
    result = service.verify_text_effect_readback(
        required_predicates=["#V#date_of_event", "#V#has_start_time"],
        tool_invocations=[
            _receipt("#V#date_of_event", "2026-08-13", concept_id="#V#meeting"),
            _receipt(
                "#V#has_start_time",
                "2026-08-13T13:05:00+01:00",
                concept_id="#V#other_meeting",
            ),
        ],
    )

    assert result["text_effect_readback_verified"] is False
    assert result["text_effect_readback_failure_code"] == (
        "text_effect_concept_ambiguous"
    )


def test_control_action_registers_and_forwards_text_effect_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(
        control_flow_actions,
        "verify_text_effect_readback",
        lambda **kwargs: calls.append(dict(kwargs))
        or {
            "text_effect_readback_verified": True,
            "text_effect_readback_failure_code": None,
            "represented_concept_id": "#V#meeting",
            "verified_text_effects": [],
        },
    )
    registry = ActionRegistry()
    register_control_flow_actions(registry, definition_loader=lambda _workflow_id: None)
    spec = registry.get(WORKFLOW_CONTROL_ACTION_TEXT_EFFECT_READBACK_ID)
    assert spec is not None

    result = registry.execute(
        WORKFLOW_CONTROL_ACTION_TEXT_EFFECT_READBACK_ID,
        inputs={
            "required_predicates": ["#V#date_of_event", "#V#has_start_time"],
            "tool_invocations": [{"tool": "upsert_singleton_text_relation"}],
            "text_effect_receipts": [{"tool": "upsert_singleton_text_relation"}],
            "expected_concept_id": "#V#meeting",
        },
        context={},
        env=WorkflowEnvironment(llm_client=None),
    )

    assert result.status == "success"
    assert result.outputs["text_effect_readback_verified"] is True
    assert calls == [
        {
            "required_predicates": [
                "#V#date_of_event",
                "#V#has_start_time",
            ],
            "optional_predicates": None,
            "tool_invocations": [
                {"tool": "upsert_singleton_text_relation"}
            ],
            "text_effect_receipts": [
                {"tool": "upsert_singleton_text_relation"}
            ],
            "expected_concept_id": "#V#meeting",
        }
    ]
