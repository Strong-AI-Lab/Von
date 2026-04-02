from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.backend.integrations.internal_mcp import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


def _build_gateway() -> InternalMCPGateway:
    return InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )


@dataclass(frozen=True)
class GatewayScenario:
    domain_id: str
    original_filename: str
    content_text: str
    representation_key: str
    verification_predicate: str
    verification_error: str
    unresolved_reason: str


GATEWAY_SCENARIOS: tuple[GatewayScenario, ...] = (
    GatewayScenario(
        domain_id="person",
        original_filename="candidate-cv.txt",
        content_text="Candidate CV details with role history.",
        representation_key="person_representation",
        verification_predicate="#V#person_representation_verification",
        verification_error="person_representation_not_verified",
        unresolved_reason="person_identity_unresolved",
    ),
    GatewayScenario(
        domain_id="company",
        original_filename="company-webpage.txt",
        content_text="Company profile and product page summary.",
        representation_key="company_representation",
        verification_predicate="#V#company_representation_verification",
        verification_error="company_representation_not_verified",
        unresolved_reason="company_identity_unresolved",
    ),
    GatewayScenario(
        domain_id="meeting",
        original_filename="meeting-transcript.txt",
        content_text="Meeting transcript with attendees and decisions.",
        representation_key="meeting_representation",
        verification_predicate="#V#meeting_representation_verification",
        verification_error="meeting_representation_not_verified",
        unresolved_reason="meeting_identity_unresolved",
    ),
)


def _patch_interpret_file_copy_for_domain_failure(
    monkeypatch, scenario: GatewayScenario
) -> None:
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._read_file_copy",
        lambda **_kwargs: {
            "success": True,
            "text": scenario.content_text,
            "content_type": "text/plain",
            "original_filename": scenario.original_filename,
            "size_bytes": 96,
            "byte_length": 96,
            "blob": {
                "backend": "local",
                "key": f"imports/user/hash/{scenario.original_filename}",
            },
        },
    )
    monkeypatch.setattr(
        "src.backend.services.file_copy_interpretation_service.build_document_interpretation",
        lambda **_kwargs: {
            "kind": "document",
            "description": f"Document text extracted: {scenario.content_text}",
            "subject_tags": ["document"],
            "content_text": scenario.content_text,
            "content_length": len(scenario.content_text),
        },
    )
    monkeypatch.setattr(
        "src.backend.services.text_value_service.upsert_singleton_text_relation",
        lambda **_kwargs: {"relation_id": "rel-cross-domain"},
    )
    monkeypatch.setattr(
        "src.backend.services.rag_text_relation_change_hook_service.maybe_sync_concept_text_relations_to_rag",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.person_file_representation_service.materialise_person_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": False,
            "verified": False,
            "reason": "not_applicable",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.company_file_representation_service.materialise_company_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": False,
            "verified": False,
            "reason": "not_applicable",
        },
    )
    monkeypatch.setattr(
        "src.backend.services.meeting_file_representation_service.materialise_meeting_representation_for_file_copy",
        lambda **_kwargs: {
            "success": True,
            "attempted": False,
            "verified": False,
            "reason": "not_applicable",
        },
    )

    unresolved_payload = {
        "success": False,
        "attempted": True,
        "verified": False,
        "reason": scenario.unresolved_reason,
    }
    if scenario.domain_id == "person":
        monkeypatch.setattr(
            "src.backend.services.person_file_representation_service.materialise_person_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    elif scenario.domain_id == "company":
        monkeypatch.setattr(
            "src.backend.services.company_file_representation_service.materialise_company_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    elif scenario.domain_id == "meeting":
        monkeypatch.setattr(
            "src.backend.services.meeting_file_representation_service.materialise_meeting_representation_for_file_copy",
            lambda **_kwargs: dict(unresolved_payload),
        )
    else:  # pragma: no cover - defensive guard for future scenario edits
        raise AssertionError(f"Unknown gateway scenario domain: {scenario.domain_id}")


@pytest.mark.parametrize(
    "scenario",
    GATEWAY_SCENARIOS,
    ids=[scenario.domain_id for scenario in GATEWAY_SCENARIOS],
)
def test_gateway_cross_domain_fail_closed_reason_codes(
    monkeypatch, scenario: GatewayScenario
) -> None:
    gateway = _build_gateway()
    _patch_interpret_file_copy_for_domain_failure(monkeypatch, scenario)

    interpreted = gateway.invoke(
        "interpret_file_copy",
        {
            "concept_id": "#V#imported_file_gateway",
            "namespace": "#V#user@org",
        },
    ).payload

    assert interpreted.get("success") is False
    representation_payload = interpreted.get(scenario.representation_key) or {}
    assert representation_payload.get("attempted") is True
    assert representation_payload.get("verified") is False
    assert representation_payload.get("reason") == scenario.unresolved_reason

    persist_errors = interpreted.get("persist_errors") or []
    assert any(
        row.get("predicate") == scenario.verification_predicate
        and row.get("error") == scenario.verification_error
        for row in persist_errors
        if isinstance(row, dict)
    )


def test_gateway_explicit_scholarly_materialisation_fails_closed_reason_codes(
    monkeypatch,
) -> None:
    gateway = _build_gateway()

    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#user",
    )

    from src.backend.integrations.internal_mcp import catalogue as internal_catalogue

    monkeypatch.setattr(
        internal_catalogue,
        "_materialise_arxiv_file_copy_representation",
        lambda **_kwargs: {
            "success": False,
            "attempted": True,
            "verified": False,
            "reason": "paper_identity_unresolved",
            "file_copy_concept_id": "#V#imported_file_gateway",
        },
    )

    payload = gateway.invoke(
        "materialise_scholarly_representation_for_file_copy",
        {
            "concept_id": "#V#imported_file_gateway",
            "arxiv_id": "2502.14996",
            "namespace": "#V#user@org",
        },
    ).payload

    assert payload.get("success") is False
    scholarly = payload.get("scholarly_representation") or {}
    assert scholarly.get("attempted") is True
    assert scholarly.get("verified") is False
    assert scholarly.get("reason") == "paper_identity_unresolved"
