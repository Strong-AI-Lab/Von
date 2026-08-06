import pytest

from bson import ObjectId
from pymongo.errors import DuplicateKeyError
from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from src.backend.security.access_control import bypass_access_control
from src.backend.services.text_value_service import (
    create_text_value,
    upsert_text_for_concept,
)


@pytest.fixture(autouse=True)
def clean_collections():
    db = TextValuesRepository.db()
    if db is None:
        yield
        return

    concepts = ConceptsRepository.collection()
    relations = TextRelationsRepository.collection()
    text_values = TextValuesRepository.collection()

    if concepts is None or relations is None or text_values is None:
        yield
        return

    concepts.delete_many({})
    relations.delete_many({})
    text_values.delete_many({})
    yield
    concepts.delete_many({})
    relations.delete_many({})
    text_values.delete_many({})


def test_upsert_does_not_dedup_away_newlines():
    """Regression: fingerprints must preserve newlines.

    Previously, fingerprinting collapsed all whitespace (including newlines), which
    could cause an update that adds Markdown newlines to reuse an older text_value
    whose stored text had spaces instead.
    """

    if TextValuesRepository.db() is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#fingerprint_newlines_test_concept"
    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts.insert_one({"concept_id": concept_id, "relationships": {}})

    with bypass_access_control():
        first = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="Hello world",
            lang="en-NZ",
        )
        second = upsert_text_for_concept(
            subject_concept_id=concept_id,
            predicate="hasDescription",
            text="Hello\nworld",
            lang="en-NZ",
        )

    assert first["text_value_id"] != second["text_value_id"]

    tv = TextValuesRepository.find_one({"_id": ObjectId(second["text_value_id"])})
    assert tv is not None
    assert tv.get("text") == "Hello\nworld"


def test_create_text_value_reconciles_duplicate_key_race(monkeypatch):
    """A concurrent insert winner is returned by its canonical fingerprint row."""

    existing_id = ObjectId()
    lookups: list[dict[str, str]] = []

    def find_one(query):
        lookups.append(query)
        if len(lookups) == 1:
            return None
        return {"_id": existing_id}

    monkeypatch.setattr(TextValuesRepository, "find_one", find_one)
    monkeypatch.setattr(
        TextValuesRepository,
        "insert_one",
        lambda _doc: (_ for _ in ()).throw(DuplicateKeyError("duplicate")),
    )

    text_value_id = create_text_value("Same title", lang="en-NZ")

    expected = {"fingerprint": "same title||en-nz", "lang": "en-NZ"}
    assert text_value_id == str(existing_id)
    assert lookups == [expected, expected]
