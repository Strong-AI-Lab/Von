import pytest

from src.backend.integrations.internal_mcp.catalogue import (
    _create_concepts,
    build_default_catalogue,
)
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport


@pytest.mark.parametrize(
    "field_name",
    [
        "identity_candidate_concept_ids",
        "identity_rejected_candidate_concept_ids",
    ],
)
def test_create_concepts_rejects_top_level_identity_review_field(
    field_name: str,
) -> None:
    result = _create_concepts(
        parent_id="#V#parent_must_not_be_resolved",
        concepts=[{"name": "Imported record", "kind": "instance"}],
        **{field_name: ["#V#reviewed_candidate"]},
    )

    assert result["success"] is False
    assert result["error_code"] == "complex_create_requires_typed_effects"
    assert field_name in result["error"]
    assert result["error_details"] == {"rejected_fields": [field_name]}
    assert result["effect_status"] == "not_started"
    assert result["changed"] is False
    assert "suggestions" not in result
    assert "recovery_affordances" not in result


def test_create_concepts_reports_every_misplaced_identity_review_field() -> None:
    field_names = [
        "identity_candidate_concept_ids",
        "identity_rejected_candidate_concept_ids",
    ]

    result = _create_concepts(
        parent_id="#V#parent_must_not_be_resolved",
        concepts=[{"name": "Imported record", "kind": "instance"}],
        identity_candidate_concept_ids=["#V#confirmed_candidate"],
        identity_rejected_candidate_concept_ids=["#V#rejected_candidate"],
    )

    assert result["success"] is False
    assert result["error_code"] == "complex_create_requires_typed_effects"
    assert result["error_details"] == {"rejected_fields": field_names}
    assert all(field_name in result["error"] for field_name in field_names)
    assert "suggestions" not in result
    assert "recovery_affordances" not in result


def test_create_concepts_gateway_preserves_misplaced_field_error() -> None:
    field_name = "identity_candidate_concept_ids"
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )

    result = gateway.invoke(
        "create_concepts",
        {
            "parent_id": "#V#parent_must_not_be_resolved",
            "concepts": [{"name": "Imported record", "kind": "instance"}],
            field_name: ["#V#reviewed_candidate"],
        },
    ).payload

    assert result["success"] is False
    assert result["error_code"] == "complex_create_requires_typed_effects"
    assert result["error_details"]["rejected_fields"] == [field_name]
    assert result["effect_status"] == "not_started"
    assert "suggestions" not in result
    assert "recovery_affordances" not in result
