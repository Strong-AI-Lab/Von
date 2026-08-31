from __future__ import annotations

from flask import Flask, session


def _patch_prompt_dependencies(monkeypatch):
    from src.backend.services import concept_service

    monkeypatch.setattr(
        concept_service, "get_concept_details_from_db", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        concept_service,
        "gather_descriptive_material",
        lambda *_args, **_kwargs: {"context_block": ""},
    )
    monkeypatch.setattr(
        concept_service,
        "get_concept_notes",
        lambda _concept: "Known notes",
    )


def test_generate_concept_question_treats_person_instances_as_people(
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    _patch_prompt_dependencies(monkeypatch)

    concept = {
        "concept_id": "#V#alex_smith",
        "name": "Alex Smith",
        "relationships": {"is_an_instance_of": ["#V#person"]},
    }

    prompt = concept_service.generate_concept_question(
        concept=concept,
        db=None,  # type: ignore[arg-type]
        is_initial=True,
    )

    assert "Do not refer to Alex Smith as 'you'." in prompt
    assert "Use existing context and available data/search first." in prompt
    assert "one concise, low-effort, high-value question" in prompt


def test_generate_concept_question_prioritises_user_questions_and_salient_predicates(
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    _patch_prompt_dependencies(monkeypatch)
    prompt = concept_service.generate_concept_question(
        concept={"concept_id": "#V#primary_labs", "name": "Primary Labs"},
        db=None,  # type: ignore[arg-type]
        interaction_history="user_question: Who founded Primary Labs?",
        user_answer="Who founded Primary Labs?",
        is_initial=False,
        elicitation_plan=[
            {
                "predicate_concept_id": "#V#has_member_role",
                "predicate_label": "has member role",
                "priority": 1,
                "question": "What role do you have in Primary Labs?",
            }
        ],
    )

    assert "If the user's latest input asks a question, answer it directly" in prompt
    assert "do not ignore it" in prompt
    assert "#V#has_member_role" in prompt
    assert "What role do you have in Primary Labs?" in prompt
    assert "Resolve first-person references such as 'I' to the known current user." in prompt
    assert "prefer a useful refinement such as their role or position" in prompt


def test_generate_concept_question_allows_second_person_for_current_user(
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    _patch_prompt_dependencies(monkeypatch)

    concept = {
        "concept_id": "#V#alex_smith",
        "name": "Alex Smith",
        "relationships": {"is_an_instance_of": ["#V#person"]},
    }

    app = Flask(__name__)
    app.secret_key = "test-secret"
    with app.test_request_context("/"):
        session["user_concept_id"] = "#V#alex_smith"
        prompt = concept_service.generate_concept_question(
            concept=concept,
            db=None,  # type: ignore[arg-type]
            is_initial=True,
        )

    assert "Second-person pronouns are allowed for the current user." in prompt
    assert "Do not refer to Alex Smith as 'you'." not in prompt


def test_generate_concept_question_does_not_apply_person_pronoun_rule_to_types(
    monkeypatch,
) -> None:
    from src.backend.services import concept_service

    _patch_prompt_dependencies(monkeypatch)

    concept = {
        "concept_id": "#V#person",
        "name": "Person",
        "relationships": {"is_a_type_of": ["#V#thing"], "is_an_instance_of": []},
    }

    prompt = concept_service.generate_concept_question(
        concept=concept,
        db=None,  # type: ignore[arg-type]
        is_initial=True,
    )

    assert "Do not refer to Person as 'you'." not in prompt
    assert "Second-person pronouns are allowed for the current user." not in prompt


def test_generate_initial_question_fallback_uses_minimal_imposition_prompt(
    monkeypatch,
) -> None:
    from src.backend.languagemodels import llm_interface
    from src.backend.services import concept_service

    class _LLMClient:
        def generate(self, *, prompt: str, model: str, llm_params=None) -> str:
            return ""

    concept = {
        "_id": "mongo-id-1",
        "concept_id": "#V#alex_smith",
        "name": "Alex Smith",
        "relationships": {"is_an_instance_of": ["#V#person"]},
    }

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda _concept_id: concept,
    )
    monkeypatch.setattr(concept_service, "get_db", lambda: object())
    monkeypatch.setattr(
        concept_service,
        "generate_concept_question",
        lambda **_kwargs: "Prompt text",
    )
    monkeypatch.setattr(
        concept_service,
        "get_concept_display_name_with_names_fallback",
        lambda _concept: "Alex Smith",
    )
    monkeypatch.setattr(concept_service, "get_concept_notes", lambda _concept: "")
    monkeypatch.setattr(llm_interface, "get_llm_client", lambda **_kwargs: _LLMClient())
    monkeypatch.setattr(
        llm_interface, "get_active_model_name", lambda **_kwargs: "test-model"
    )
    monkeypatch.setattr(
        llm_interface, "get_active_model_parameters", lambda **_kwargs: {}
    )

    result = concept_service.generate_initial_question("#V#alex_smith")

    assert result["status"] == "success"
    assert (
        result["question"]
        == "If you can answer from memory, what is one quick, high-value correction or missing fact about Alex Smith?"
    )
    assert "What would you like to tell me about" not in result["question"]
