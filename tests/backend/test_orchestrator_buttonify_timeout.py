from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from orchestrator_test_harness import build_db_independent_orchestrator


class _DummyGateway:
    enabled = True

    def describe_methods(self) -> dict[str, Any]:
        return {}


def test_buttonify_extract_options_honours_timeout_override_and_skips_direct_fallback(
    monkeypatch,
) -> None:
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=cast(Any, _DummyGateway()),
        selector_enabled=True,
    )
    policy_state, _ = orchestrator._load_workflow_model_policy(None)
    captured: dict[str, Any] = {}

    def _capture_timeout_and_fail(**kwargs: Any):
        captured["timeout_override_sec"] = kwargs.get("timeout_override_sec")
        raise TimeoutError("LLM call timed out after 29s (stage=buttonify)")

    monkeypatch.setattr(
        orchestrator,
        "_run_llm_with_fallbacks",
        _capture_timeout_and_fail,
    )

    result = orchestrator._action_buttonify_extract_options(
        SimpleNamespace(
            data={
                "screen_text": "Choose one option.",
                "user_prompt": "Offer a couple of quick replies.",
                "buttonify_prompt_text": "Return JSON array only.",
                "buttonify_prompt_available": True,
                "policy_state": policy_state,
                "record_llm_call": lambda **_kwargs: None,
                "aux_llm_calls": [],
                "registry_snapshot": None,
                "user_concept_id": None,
                "org_concept_id": None,
                "llm_calls": [],
                "emit_progress": lambda _info: None,
                "prefer_default_model": True,
                "conversation_turn_llm_timeout_override_sec": 29,
                "default_model": "test-model",
            },
            environment=SimpleNamespace(
                llm_client=SimpleNamespace(
                    generate=lambda *args, **kwargs: (_ for _ in ()).throw(
                        AssertionError(
                            "buttonify direct fallback should not run after policy "
                            "timeout"
                        )
                    )
                ),
                model="test-model",
            ),
        )
    )

    assert captured["timeout_override_sec"] == 29.0
    assert result.outputs["buttonify_status"] == "no_op"
    assert result.outputs["buttonify_source"] == "none"
    assert result.outputs["buttonify_model_attempted"] is True
    assert result.outputs["buttonify_error_class"] == "TimeoutError"
    assert result.outputs["buttonify_suppression_reason"] == "model_error"
