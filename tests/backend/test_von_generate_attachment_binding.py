from __future__ import annotations

import pytest

from src.backend.server.routes import von_routes


def test_authorises_owned_uploaded_file_copy_as_structured_launch_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    concept_id = "#V#uploaded_file_copy_attachment_test"
    user_id = "#V#attachment_test_user"
    lookup_calls: list[dict[str, str]] = []

    def fake_load(**kwargs):
        lookup_calls.append(dict(kwargs))
        return {
            "concept_id": concept_id,
            "relationships": {
                "is_an_instance_of": ["#V#computer_file_copy"],
                "#V#specific_to_user": [user_id],
            },
        }

    monkeypatch.setattr(
        von_routes,
        "_load_authorised_file_copy_concept_doc",
        fake_load,
    )

    result = von_routes._authorise_generate_file_copy_launch_input(
        {
            "file_copy_concept_id": f"  {concept_id}  ",
            "representation_mode": "programme_records",
        },
        user_concept_id=user_id,
    )

    assert result == {
        "file_copy_concept_id": concept_id,
        "representation_mode": "programme_records",
    }
    assert lookup_calls == [
        {
            "concept_id": concept_id,
            "user_concept_id": user_id,
            "log_prefix": "generate/file-copy-input",
        }
    ]


@pytest.mark.parametrize(
    "concept_doc",
    [
        {
            "concept_id": "#V#uploaded_file_copy_attachment_test",
            "relationships": {
                "is_an_instance_of": ["#V#computer_file_copy"],
                "#V#specific_to_user": ["#V#another_user"],
            },
        },
        {
            "concept_id": "#V#uploaded_file_copy_attachment_test",
            "relationships": {
                "is_an_instance_of": ["#V#spreadsheet"],
                "#V#specific_to_user": ["#V#attachment_test_user"],
            },
        },
        {
            "concept_id": "#V#uploaded_file_copy_attachment_test",
            "relationships": {
                "is_an_instance_of": ["#V#computer_file_copy"],
            },
        },
    ],
    ids=["different-owner", "not-a-file-copy", "missing-owner-scope"],
)
def test_rejects_file_copy_launch_input_without_owned_file_copy_authority(
    monkeypatch: pytest.MonkeyPatch,
    concept_doc: dict,
) -> None:
    monkeypatch.setattr(
        von_routes,
        "_load_authorised_file_copy_concept_doc",
        lambda **_kwargs: concept_doc,
    )

    with pytest.raises(PermissionError, match="attachment access is not authorised"):
        von_routes._authorise_generate_file_copy_launch_input(
            {
                "file_copy_concept_id": (
                    "#V#uploaded_file_copy_attachment_test"
                )
            },
            user_concept_id="#V#attachment_test_user",
        )


def test_rejects_malformed_file_copy_launch_input_before_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = pytest.fail
    monkeypatch.setattr(
        von_routes,
        "_load_authorised_file_copy_concept_doc",
        lookup,
    )

    with pytest.raises(ValueError, match="valid Vontology concept id"):
        von_routes._authorise_generate_file_copy_launch_input(
            {"file_copy_concept_id": "#V#bad id"},
            user_concept_id="#V#attachment_test_user",
        )
