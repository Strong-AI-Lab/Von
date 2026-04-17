from __future__ import annotations

from typing import Any, cast

from src.backend.integrations.internal_mcp.orchestrator import (
    InternalMCPChatOrchestrator,
)

_TEST_BASE_PROMPT = (
    "You have access to internal MCP tools.\n\n"
    "{auth_status}\n"
    "Available tools:\n"
    "{listing}"
)


class _GatewayStub:
    enabled = True

    def describe_methods(self):
        return {}


def test_build_augmented_context_includes_effective_identity_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        lambda concept_id: {
            "#V#test_user": {"name": "Test User"},
            "#V#test_org": {"name": "Test Org"},
        }.get(concept_id),
    )
    monkeypatch.setattr(
        InternalMCPChatOrchestrator,
        "_load_base_system_prompt_from_vontology",
        lambda self, preferred_language=None: (
            _TEST_BASE_PROMPT,
            "#V#test_base_prompt",
        ),
    )

    orchestrator = InternalMCPChatOrchestrator(
        gateway=cast(Any, _GatewayStub()),
        max_tool_invocations=1,
    )

    augmented = orchestrator._build_augmented_context(
        [{"role": "user", "content": "Who am I?"}],
        user_namespace="#V#test_user@test_org",
        auxiliary_system_prompt="Please be terse.",
        user_concept_id="#V#test_user",
        org_concept_id="#V#test_org",
    )

    assert augmented
    system_message = augmented[0]
    assert system_message["role"] == "system"
    content = str(system_message["content"])
    assert "AUTHENTICATION STATUS: Authenticated" in content
    assert "CURRENT USER CONTEXT: Test User (#V#test_user)" in content
    assert "CURRENT ORGANISATION CONTEXT: Test Org (#V#test_org)" in content
    assert "USER-SPECIFIC SYSTEM PROMPT (from Vontology):" in content
    assert "Please be terse." in content
