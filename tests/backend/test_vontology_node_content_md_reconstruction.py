import pytest

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.vontology import utils_vontology


@pytest.fixture(autouse=True)
def clean_vontology_nodes_collection():
    db = utils_vontology.get_db()
    if db is not None:
        db["vontology_nodes"].delete_many({})
    yield
    if db is not None:
        db["vontology_nodes"].delete_many({})


def test_reconstructed_md_content_omits_boilerplate_metadata_fields():
    db = utils_vontology.get_db()
    if db is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#verified_entity_representation_agentic_workflow_design"

    # Idempotence: this test DB can be persistent between runs.
    concepts.delete_one({"concept_id": concept_id})

    concepts.insert_one(
        {
            "concept_id": concept_id,
            "names": [
                {
                    "name": "Verified Entity Representation Agentic Workflow Design",
                    "type": "NL",
                    "language": "en-NZ",
                }
            ],
            "relationships": {
                "is_an_instance_of": ["#V#von_agentic_component_design_document"],
                "is_a_type_of": [],
            },
            # Intentionally no md_content.
            # Intentionally no source_concept.
            # Description/notes are stored in text relations in newer schema; here we just ensure fallback doesn't invent boilerplate.
        }
    )

    payload = utils_vontology.get_vontology_node_content(concept_id)
    assert "error" not in payload

    md = payload.get("md_content")
    assert isinstance(md, str)

    assert md.startswith("# ")
    assert "Source Concept" not in md
    assert "SubConcept Of" not in md
    assert "Instance Of" not in md


def test_reconstruct_md_false_preserves_missing_md_content():
    db = utils_vontology.get_db()
    if db is None:
        pytest.skip("MongoDB not configured for this test run")

    concepts = ConceptsRepository.collection()
    if concepts is None:
        pytest.skip("MongoDB not configured for this test run")

    concept_id = "#V#md_content_missing_reconstruct_md_false"

    # Idempotence: this test DB can be persistent between runs.
    concepts.delete_one({"concept_id": concept_id})

    concepts.insert_one(
        {
            "concept_id": concept_id,
            "names": [
                {
                    "name": "md_content missing reconstruct_md false",
                    "type": "NL",
                    "language": "en-NZ",
                }
            ],
            "relationships": {
                "is_an_instance_of": [],
                "is_a_type_of": [],
            },
            # Intentionally no md_content.
        }
    )

    payload = utils_vontology.get_vontology_node_content(
        concept_id, reconstruct_md=False
    )
    assert "error" not in payload
    assert payload.get("md_content") is None
    assert payload.get("content_html") == ""


def test_node_content_uses_canonical_concept_service_when_repository_lookup_misses(
    monkeypatch,
):
    concept_id = "#V#service_visible_concept"

    monkeypatch.setattr(
        utils_vontology.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: None,
    )

    def _fake_get_concept_by_concept_id(requested_id: str):
        assert requested_id == concept_id
        return {
            "concept_id": concept_id,
            "names": [
                {
                    "name": "Service Visible Concept",
                    "type": "NL",
                    "language": "en-NZ",
                }
            ],
            "relationships": {
                "is_an_instance_of": ["#V#document"],
                "is_a_type_of": [],
            },
        }

    monkeypatch.setattr(
        "src.backend.services.concept_service.get_concept_by_concept_id",
        _fake_get_concept_by_concept_id,
    )

    payload = utils_vontology.get_vontology_node_content(concept_id)

    assert "error" not in payload
    assert payload["concept_id"] == concept_id
    assert payload["display_name"] == "Service Visible Concept"
    assert payload["kind"] == "individual"


@pytest.mark.parametrize(
    "canonical_name",
    [
        "UoA CS PhD Student",
        "Študentka doktorskega študija",
        "E\u0301tudiante en IA",
        "طالبة دكتوراه",
    ],
)
def test_reconstructed_heading_uses_exact_canonical_relation_backed_name(
    monkeypatch,
    canonical_name,
):
    concept_id = "#V#uo_acs_ph_d_student"
    concept_doc = {
        "_id": "696c975991de325e9d1cf3ce",
        "concept_id": concept_id,
        "relationships": {
            "is_an_instance_of": [],
            "is_a_type_of": ["#V#student"],
        },
    }
    projection_calls = []

    monkeypatch.setattr(
        utils_vontology.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: dict(concept_doc),
    )
    monkeypatch.setattr(utils_vontology, "get_concept_description", lambda _doc: None)
    monkeypatch.setattr(utils_vontology, "get_concept_notes", lambda _doc: None)

    monkeypatch.setattr(
        utils_vontology, "_get_cached_preferred_language", lambda: "en-NZ"
    )

    def _resolve_display_names(docs, *, preferred_language=None):
        projection_calls.append((docs, preferred_language))
        assert docs[0]["concept_id"] == concept_id
        return {concept_id: canonical_name}

    monkeypatch.setattr(
        "src.backend.services.concept_service.resolve_concept_display_names",
        _resolve_display_names,
    )

    payload = utils_vontology.get_vontology_node_content(concept_id)

    assert len(projection_calls) == 1
    assert projection_calls[0][1] == "en-NZ"
    assert payload["display_name"] == canonical_name
    assert payload["md_content"] == f"# {canonical_name}\n\n"
    assert payload["content_html"] == f"<h1>{canonical_name}</h1>"
    assert [ord(char) for char in payload["display_name"]] == [
        ord(char) for char in canonical_name
    ]


def test_node_content_display_name_projection_fails_soft_to_legacy_name(monkeypatch):
    concept_id = "#V#legacy_named_concept"
    concept_doc = {
        "_id": "696c975991de325e9d1cf3cd",
        "concept_id": concept_id,
        "names": [
            {
                "name": "Legacy Živjo",
                "type": "NL",
                "language": "en-NZ",
            }
        ],
        "relationships": {"is_an_instance_of": [], "is_a_type_of": []},
    }

    monkeypatch.setattr(
        utils_vontology.ConceptsRepository,
        "find_one",
        lambda *_args, **_kwargs: dict(concept_doc),
    )
    monkeypatch.setattr(utils_vontology, "get_concept_description", lambda _doc: None)
    monkeypatch.setattr(utils_vontology, "get_concept_notes", lambda _doc: None)
    monkeypatch.setattr(
        "src.backend.services.concept_service.resolve_concept_display_names",
        lambda _docs, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("relation read unavailable")
        ),
    )

    payload = utils_vontology.get_vontology_node_content(concept_id)

    assert payload["display_name"] == "Legacy Živjo"
    assert payload["md_content"] == "# Legacy Živjo\n\n"
