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
    expected_path = f"concepts[i].{field_name}"

    result = _create_concepts(
        parent_id="#V#parent_must_not_be_resolved",
        concepts=[{"name": "Imported record", "kind": "instance"}],
        **{field_name: ["#V#reviewed_candidate"]},
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_parameter"
    assert expected_path in result["error"]
    assert result["error_details"] == {
        "misplaced_top_level_fields": [field_name],
        "expected_concept_item_paths": [expected_path],
    }
    assert expected_path in result["suggestions"][0]


def test_create_concepts_reports_every_misplaced_identity_review_field() -> None:
    field_names = [
        "identity_candidate_concept_ids",
        "identity_rejected_candidate_concept_ids",
    ]
    expected_paths = [f"concepts[i].{field_name}" for field_name in field_names]

    result = _create_concepts(
        parent_id="#V#parent_must_not_be_resolved",
        concepts=[{"name": "Imported record", "kind": "instance"}],
        identity_candidate_concept_ids=["#V#confirmed_candidate"],
        identity_rejected_candidate_concept_ids=["#V#rejected_candidate"],
    )

    assert result["success"] is False
    assert result["error_code"] == "invalid_parameter"
    assert result["error_details"] == {
        "misplaced_top_level_fields": field_names,
        "expected_concept_item_paths": expected_paths,
    }
    assert all(expected_path in result["error"] for expected_path in expected_paths)
    assert all(
        expected_path in suggestion
        for expected_path, suggestion in zip(expected_paths, result["suggestions"])
    )


def test_create_concepts_gateway_preserves_misplaced_field_error() -> None:
    field_name = "identity_candidate_concept_ids"
    expected_path = f"concepts[i].{field_name}"
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
    assert result["error_code"] == "invalid_parameter"
    assert result["error_details"]["expected_concept_item_paths"] == [expected_path]
