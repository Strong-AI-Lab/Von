from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any, cast

import pytest

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.security.access_control import override_current_actor
from src.backend.services import learning_candidate_vontology_service as service
from src.backend.services.tool_metadata_service import (
    get_tool_operation_category,
    get_tool_surface_exposure_metadata,
)

USER_ID = "#V#trusted_user"
ORG_ID = "#V#trusted_org"
NAMESPACE = "#V#trusted_user@trusted_org"


def _gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


def _apply_learning_candidate_payload_defaults(
    payload: dict[str, Any],
    *,
    tool_name: str = "learning_candidate_capture",
    conversation_session_id: str | None = "current-session",
) -> list[dict[str, Any]]:
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    orchestrator = object.__new__(InternalMCPChatOrchestrator)
    return orchestrator._apply_payload_defaults(
        tool_name,
        payload,
        schema=None,
        user_namespace=None,
        selected_gmail_profile=None,
        conversation_session_id=conversation_session_id,
    )


def test_capture_defaults_locator_empty_conversation_source() -> None:
    original_source = {
        "kind": "conversation",
        "namespace": "#V#trusted_user@trusted_org",
    }
    payload = {"source": original_source}

    bindings = _apply_learning_candidate_payload_defaults(payload)

    assert payload["source"] == {
        **original_source,
        "session_id": "current-session",
    }
    assert payload["source"] is not original_source
    assert original_source == {
        "kind": "conversation",
        "namespace": "#V#trusted_user@trusted_org",
    }
    assert bindings == [
        {
            "field": "source.session_id",
            "source": "conversation_session_id",
            "value_present": True,
        }
    ]


@pytest.mark.parametrize(
    ("tool_name", "payload", "conversation_session_id"),
    [
        ("learning_candidate_capture", {}, "current-session"),
        (
            "learning_candidate_capture",
            {"source": {"kind": "episode_critique_memory"}},
            "current-session",
        ),
        (
            "learning_candidate_capture",
            {"source": {"kind": "conversation", "session_id": "supplied-session"}},
            "current-session",
        ),
        (
            "learning_candidate_capture",
            {
                "source": {
                    "kind": "conversation",
                    "conversation_concept_id": "#V#conversation_one",
                }
            },
            "current-session",
        ),
        (
            "learning_candidate_capture",
            {"source": {"kind": "conversation", "concept_id": "#V#conversation_one"}},
            "current-session",
        ),
        (
            "learning_candidate_capture",
            {"source": {"kind": "conversation"}},
            None,
        ),
        (
            "learning_candidate_revise",
            {"source": {"kind": "conversation"}},
            "current-session",
        ),
    ],
)
def test_learning_candidate_conversation_default_does_not_overwrite_or_infer_source(
    tool_name: str,
    payload: dict[str, Any],
    conversation_session_id: str | None,
) -> None:
    expected = deepcopy(payload)

    bindings = _apply_learning_candidate_payload_defaults(
        payload,
        tool_name=tool_name,
        conversation_session_id=conversation_session_id,
    )

    assert payload == expected
    assert bindings == []


def test_learning_candidate_contracts_are_exposed_with_non_active_boundaries() -> None:
    snapshot = build_default_catalogue().snapshot()
    expected_categories = {
        "learning_candidate_capture": "write",
        "learning_candidate_get": "read",
        "learning_candidate_list": "read",
        "learning_candidate_revise": "write",
    }

    for method_name, category in expected_categories.items():
        definition = snapshot[method_name]
        assert definition["category"] == category
        description = definition["description"].lower()
        assert "source-linked" in description
        assert "non-active" in description
        assert "not a fact" in description or "not facts" in description
        assert "authority grant" in description or "authority grants" in description
        assert "contributor" in description
        assert "beneficiary" in description
        assert "do not confer visibility or authority" in description
        exposure = get_tool_surface_exposure_metadata(method_name)
        assert exposure.expose_in_vontology_stdio is True
        assert exposure.expose_in_manifest is True
        assert get_tool_operation_category(method_name) == category

    assert (
        "zero candidates is normal"
        in snapshot["learning_candidate_capture"]["description"].lower()
    )
    assert (
        "zero candidates" in snapshot["learning_candidate_list"]["description"].lower()
    )


def test_learning_candidate_tools_forward_only_the_trusted_actor_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, dict[str, Any]] = {}

    def capture(**kwargs: Any) -> dict[str, Any]:
        calls["capture"] = dict(kwargs)
        return {
            "candidate_id": "#V#learning_candidate_one",
            "lifecycle_state": "non_active",
            "created": True,
        }

    def get(candidate_id: str, **kwargs: Any) -> dict[str, Any]:
        calls["get"] = {"candidate_id": candidate_id, **kwargs}
        return {
            "candidate_id": candidate_id,
            "lifecycle_state": "non_active",
        }

    def list_candidates(**kwargs: Any) -> list[dict[str, Any]]:
        calls["list"] = dict(kwargs)
        return [
            {
                "candidate_id": "#V#learning_candidate_one",
                "lifecycle_state": "non_active",
            }
        ]

    def revise(candidate_id: str, **kwargs: Any) -> dict[str, Any]:
        calls["revise"] = {"candidate_id": candidate_id, **kwargs}
        return {
            "candidate_id": candidate_id,
            "lifecycle_state": "non_active",
            "revised": True,
        }

    monkeypatch.setattr(service, "capture_learning_candidate", capture)
    monkeypatch.setattr(service, "get_learning_candidate", get)
    monkeypatch.setattr(service, "list_learning_candidates", list_candidates)
    monkeypatch.setattr(service, "revise_learning_candidate", revise)

    gateway = _gateway()
    with override_current_actor(USER_ID, ORG_ID):
        captured = gateway.invoke(
            "learning_candidate_capture",
            {
                "body": "Retain this material lesson for later evaluation.",
                "source": {
                    "kind": "conversation",
                    "conversation_concept_id": "#V#conversation_one",
                },
                "contributor_concept_ids": ["#V#von_system"],
                "target_concept_ids": ["#V#workflow_one"],
                "beneficiary_concept_ids": ["#V#ecosystem_one"],
                "request_id": "capture-one",
                "namespace": NAMESPACE,
            },
        ).payload
        fetched = gateway.invoke(
            "learning_candidate_get",
            {"candidate_id": "#V#learning_candidate_one", "namespace": NAMESPACE},
        ).payload
        listed = gateway.invoke(
            "learning_candidate_list",
            {
                "target_concept_id": "#V#workflow_one",
                "source_kind": "conversation",
                "namespace": NAMESPACE,
            },
        ).payload
        revised = gateway.invoke(
            "learning_candidate_revise",
            {
                "candidate_id": "#V#learning_candidate_one",
                "body": "Refined lesson.",
                "revision_request_id": "revision-two",
                "purpose_concept_ids": ["#V#purpose_one"],
                "namespace": NAMESPACE,
            },
        ).payload

    assert captured == {
        "success": True,
        "candidate_id": "#V#learning_candidate_one",
        "lifecycle_state": "non_active",
        "created": True,
    }
    assert fetched["success"] is True
    assert listed == {
        "success": True,
        "candidates": [
            {
                "candidate_id": "#V#learning_candidate_one",
                "lifecycle_state": "non_active",
            }
        ],
        "count": 1,
    }
    assert revised["success"] is True

    for forwarded in calls.values():
        assert forwarded["actor_user_id"] == USER_ID
        assert forwarded["organisation_concept_id"] == ORG_ID
        assert forwarded["namespace"] == NAMESPACE
        assert "user_id" not in forwarded
        assert "org_id" not in forwarded
        assert "user_concept_id" not in forwarded

    assert calls["capture"]["visibility_scope"] == "actor"
    assert calls["list"]["limit"] == 50
    assert calls["revise"]["purpose_concept_ids"] == ["#V#purpose_one"]


def test_learning_candidate_capture_preserves_org_only_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forwarded: dict[str, Any] = {}

    def capture(**kwargs: Any) -> dict[str, Any]:
        forwarded.update(kwargs)
        return {
            "candidate_id": "#V#learning_candidate_org",
            "lifecycle_state": "non_active",
        }

    monkeypatch.setattr(service, "capture_learning_candidate", capture)

    with override_current_actor(None, ORG_ID):
        result = (
            _gateway()
            .invoke(
                "learning_candidate_capture",
                {
                    "body": "An organisation-level operational lesson.",
                    "source": {
                        "kind": "episode_critique_memory",
                        "memory_id": "#V#episode_memory_one",
                    },
                    "contributor_concept_ids": ["#V#von_system"],
                    "target_concept_ids": ["#V#organisation_role_one"],
                    "beneficiary_concept_ids": ["#V#ecosystem_one"],
                    "request_id": "org-capture-one",
                },
            )
            .payload
        )

    assert result["success"] is True
    assert forwarded["actor_user_id"] is None
    assert forwarded["organisation_concept_id"] == ORG_ID
    assert forwarded["namespace"] is None
    assert forwarded["visibility_scope"] == "organisation"


@pytest.mark.parametrize(
    ("method_name", "arguments"),
    [
        (
            "learning_candidate_capture",
            {
                "body": "Untrusted capture",
                "source": {
                    "kind": "conversation",
                    "conversation_concept_id": "#V#conversation_one",
                },
                "contributor_concept_ids": ["#V#von_system"],
                "target_concept_ids": ["#V#workflow_one"],
                "request_id": "untrusted-capture",
            },
        ),
        (
            "learning_candidate_get",
            {"candidate_id": "#V#learning_candidate_one"},
        ),
        ("learning_candidate_list", {}),
        (
            "learning_candidate_revise",
            {
                "candidate_id": "#V#learning_candidate_one",
                "body": "Untrusted revision",
                "revision_request_id": "untrusted-revision",
            },
        ),
    ],
)
def test_learning_candidate_tools_reject_payload_only_actor_identity(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    arguments: dict[str, Any],
) -> None:
    def unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("service must not run without trusted actor provenance")

    monkeypatch.setattr(service, "capture_learning_candidate", unexpected)
    monkeypatch.setattr(service, "get_learning_candidate", unexpected)
    monkeypatch.setattr(service, "list_learning_candidates", unexpected)
    monkeypatch.setattr(service, "revise_learning_candidate", unexpected)

    result = (
        _gateway()
        .invoke(
            method_name,
            {
                **arguments,
                "user_concept_id": "#V#payload_user",
                "org_id": "#V#payload_org",
                "namespace": "#V#payload_user@payload_org",
            },
        )
        .payload
    )

    assert result["success"] is False
    assert result["error_code"] == "authenticated_actor_context_required"


def test_learning_candidate_payload_identity_cannot_replace_trusted_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "get_learning_candidate",
        lambda *_args, **_kwargs: pytest.fail("mismatched call reached the service"),
    )

    with override_current_actor(USER_ID, ORG_ID):
        result = (
            _gateway()
            .invoke(
                "learning_candidate_get",
                {
                    "candidate_id": "#V#learning_candidate_one",
                    "user_concept_id": "#V#different_user",
                    "org_id": ORG_ID,
                    "namespace": "#V#different_user@trusted_org",
                },
            )
            .payload
        )

    assert result["success"] is False
    assert result["error_code"] == "workflow_actor_scope_mismatch"
    assert "user_id" in result["error_details"]["mismatch_fields"]


def test_raw_stdio_learning_candidate_reads_do_not_trust_payload_identity() -> None:
    from src.backend.mcp_server import mcp_stdio_server

    async def invoke(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await mcp_stdio_server.call_tool(tool_name, arguments)
        first = cast(list[Any], result)[0]
        return cast(dict[str, Any], json.loads(cast(str, first.text)))

    claims = {
        "user_concept_id": "#V#payload_user",
        "org_id": "#V#payload_org",
        "namespace": "#V#payload_user@payload_org",
    }

    async def run_calls() -> tuple[dict[str, Any], dict[str, Any]]:
        fetched, listed = await asyncio.gather(
            invoke(
                "learning_candidate_get",
                {**claims, "candidate_id": "#V#learning_candidate_one"},
            ),
            invoke("learning_candidate_list", claims),
        )
        return fetched, listed

    fetched, listed = asyncio.run(run_calls())

    for payload in (fetched, listed):
        assert payload["success"] is False
        assert payload["error_code"] == "authenticated_actor_context_required"
