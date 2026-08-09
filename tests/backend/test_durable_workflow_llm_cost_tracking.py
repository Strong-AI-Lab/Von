from __future__ import annotations

from src.backend.workflows.durable.instance_manager import WorkflowInstanceManager
from src.backend.workflows.durable.llm_cost_tracking import (
    LLM_USAGE_COST_SUMMARY_KEY,
    build_durable_llm_usage_cost_summary,
    merge_nested_llm_calls_for_cost,
    project_llm_calls_for_cost,
)
from src.backend.workflows.durable.models import WorkflowInstance


def _registry() -> dict:
    return {
        "models": [
            {
                "provider": "openai",
                "model_id": "gpt-5.6-terra",
                "pricing": {
                    "schema_version": "llm_model_pricing.v1",
                    "version": "test-2026-08-09",
                    "source": "test_fixture",
                    "effective_at_utc": "2026-08-09T00:00:00Z",
                    "model_id": "gpt-5.6-terra",
                    "currency": "USD",
                    "unit_tokens": 1_000_000,
                    "rates": {
                        "input_tokens": 2.0,
                        "output_tokens": 12.0,
                    },
                },
            }
        ]
    }


def _priced_call(call_id: str) -> dict:
    return {
        "call_id": call_id,
        "type": "llm.generate",
        "stage": "paper_relevance",
        "provider": "openai",
        "requested_model": "gpt-5.6-terra",
        "selected_model": "gpt-5.6-terra",
        "effective_model": "gpt-5.6-terra",
        "model_identity_source": "provider_response",
        "provider_request_sent": True,
        "usage": {"prompt_tokens": 1_000, "completion_tokens": 100},
        "prompt": "must not cross the durable accounting boundary",
        "response": "must not cross the durable accounting boundary",
    }


def test_deterministic_durable_run_records_no_model_calls_not_zero_cost() -> None:
    summary = build_durable_llm_usage_cost_summary({})

    assert summary["call_count"] == 0
    assert summary["usage"]["status"] == "not_applicable"
    assert summary["estimated_cost"]["status"] == "not_applicable"
    assert summary["estimated_cost"]["amount"] is None


def test_nested_model_calls_are_content_free_and_registry_priced() -> None:
    parent: dict = {}
    merge_nested_llm_calls_for_cost(parent, {"llm_calls": [_priced_call("child-1")]})

    assert parent["llm_calls"] == project_llm_calls_for_cost(
        [_priced_call("child-1")]
    )
    assert "prompt" not in parent["llm_calls"][0]
    assert "response" not in parent["llm_calls"][0]

    summary = build_durable_llm_usage_cost_summary(
        parent,
        model_registry=_registry(),
    )
    assert summary["call_count"] == 1
    assert summary["usage"]["status"] == "reported"
    assert summary["usage"]["total_tokens"] == 1_100
    assert summary["estimated_cost"]["status"] == "estimated"
    assert summary["estimated_cost"]["amount"] == 0.0032


def test_nested_merge_does_not_recount_calls_inherited_from_parent() -> None:
    inherited = _priced_call("parent-1")
    child_only = _priced_call("child-1")
    parent = {"llm_calls": project_llm_calls_for_cost([inherited])}

    merge_nested_llm_calls_for_cost(
        parent,
        {"llm_calls": [inherited, child_only]},
    )

    assert [call["call_id"] for call in parent["llm_calls"]] == [
        "parent-1",
        "child-1",
    ]


def test_instance_round_trip_and_status_expose_canonical_cost_summary() -> None:
    summary = build_durable_llm_usage_cost_summary({})
    instance = WorkflowInstance.create(
        "#V#scheduled_paper_workflow",
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
        schedule_id="#V#paper_schedule",
    )
    instance.llm_usage_cost_summary = summary

    restored = WorkflowInstance.from_doc(instance.to_doc())

    assert restored.schedule_id == "#V#paper_schedule"
    assert restored.llm_usage_cost_summary == summary
    assert restored.to_status_dict()[LLM_USAGE_COST_SUMMARY_KEY] == summary


def test_checkpoint_promotes_cost_summary_to_first_class_instance_field(
    monkeypatch,
) -> None:
    summary = build_durable_llm_usage_cost_summary({})
    instance = WorkflowInstance.create(
        "#V#scheduled_paper_workflow",
        user_id="#V#user",
        org_id="#V#org",
        namespace="#V#user@org",
        schedule_id="#V#paper_schedule",
    )
    manager = object.__new__(WorkflowInstanceManager)
    captured: dict = {}
    monkeypatch.setattr(
        manager,
        "_compact_instance_payload_field",
        lambda payload, **_kwargs: dict(payload),
    )
    monkeypatch.setattr(
        manager,
        "_claim_fence_query",
        lambda **_kwargs: {"instance_id": instance.instance_id},
    )

    def _update(query, update, **_kwargs):
        captured["query"] = query
        captured["update"] = update
        return instance

    monkeypatch.setattr(manager, "_find_one_and_update_instance", _update)
    monkeypatch.setattr(manager, "_broadcast_instance", lambda _instance: None)

    saved = manager.checkpoint(
        instance.instance_id,
        current_state="done",
        workflow_data={LLM_USAGE_COST_SUMMARY_KEY: summary},
    )

    assert saved is True
    assert captured["update"]["$set"][LLM_USAGE_COST_SUMMARY_KEY] == summary
