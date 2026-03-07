from src.backend.services.file_copy_reference_service import (
    extract_file_copy_concept_ids_from_text,
    is_file_copy_concept_id,
)


def test_extract_file_copy_concept_ids_strips_trailing_sentence_punctuation() -> None:
    text = (
        "Represent this artefact #V#uploaded_file_copy_person_1. "
        "Then inspect #V#uploaded_file_copy_company_2)."
    )

    assert extract_file_copy_concept_ids_from_text(text) == [
        "#V#uploaded_file_copy_person_1",
        "#V#uploaded_file_copy_company_2",
    ]
    assert is_file_copy_concept_id("#V#uploaded_file_copy_person_1.")
