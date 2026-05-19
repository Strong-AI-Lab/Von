from __future__ import annotations

from src.backend.services.file_copy_typing_service import (
    ensure_file_copy_typing_types_exist,
    infer_file_copy_typing,
)


def test_infer_file_copy_typing_derives_dynamic_content_type_subtype() -> None:
    result = infer_file_copy_typing(
        content_type="application/json",
        original_filename="dataset.bin",
    )

    assert result["determinable"] is True
    assert (
        result["format_type_concept_id"]
        == "#V#content_type_application_json_computer_file_copy"
    )
    assert (
        result["primary_type_concept_id"]
        == "#V#content_type_application_json_computer_file_copy"
    )
    assert "mime_type" in result["matched_signals"]
    assert "dynamic_content_type" in result["matched_rule_ids"]
    assert (
        "#V#content_type_application_json_computer_file_copy"
        in result["asserted_type_concept_ids"]
    )


def test_infer_file_copy_typing_derives_dynamic_extension_subtype_when_needed() -> None:
    result = infer_file_copy_typing(
        content_type="application/octet-stream",
        original_filename="embedding.parquet",
    )

    assert result["determinable"] is True
    assert (
        result["format_type_concept_id"]
        == "#V#extension_parquet_computer_file_copy"
    )
    assert "filename_extension" in result["matched_signals"]
    assert "dynamic_extension" in result["matched_rule_ids"]


def test_infer_file_copy_typing_extracts_arxiv_identifier_from_filename() -> None:
    result = infer_file_copy_typing(
        content_type="application/pdf",
        original_filename="2502.14996.pdf",
    )

    assert result["route_hint"] == "arxiv"
    assert result["route_confidence"] == 0.99
    assert result["arxiv_id"] == "2502.14996"
    assert result["arxiv_ids"] == ["2502.14996"]
    assert result["semantic_type_concept_id"] == "#V#scholarly_paper_file_copy"


def test_ensure_file_copy_typing_types_exist_creates_dynamic_blueprints(monkeypatch) -> None:
    ensured_calls: list[dict[str, object]] = []

    def _record_ensure(**kwargs):
        ensured_calls.append(dict(kwargs))

    monkeypatch.setattr(
        "src.backend.services.file_copy_typing_service.ensure_specific_computer_file_copy_type_exists",
        _record_ensure,
    )

    created_or_known = ensure_file_copy_typing_types_exist(
        {
            "content_type_token": "application/json",
            "filename_extension": ".json",
            "asserted_type_concept_ids": [
                "#V#content_type_application_json_computer_file_copy"
            ],
        }
    )

    assert created_or_known == ["#V#content_type_application_json_computer_file_copy"]
    assert len(ensured_calls) == 1
    assert ensured_calls[0]["type_concept_id"] == "#V#content_type_application_json_computer_file_copy"
    assert ensured_calls[0]["name"] == "application/json file copy"
    assert ensured_calls[0]["parent_type_concept_id"] == "#V#computer_file_copy"
    assert (
        ensured_calls[0]["description"]
        == 'A computer file copy whose MIME content type is "application/json".'
    )
    assert ensured_calls[0]["system_tags"] == ["file", "mime", "application", "json"]
    assert ensured_calls[0]["logger"] is not None
