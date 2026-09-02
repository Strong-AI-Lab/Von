from __future__ import annotations

import mongomock
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
                "requirement_id": "organisation_has_member_role",
                "predicate_concept_id": "#V#has_member_role",
                "predicate_label": "has member role",
                "priority": 1,
                "requirement_kind": "constitutive_relation",
                "declaration_source": "builtin_compatibility",
                "profile_predicate": None,
                "question": "What role do you have in Primary Labs?",
            }
        ],
    )

    assert "If the user's latest input asks a question, answer it directly" in prompt
    assert "do not ignore it" in prompt
    assert "#V#has_member_role" in prompt
    assert "What role do you have in Primary Labs?" in prompt
    assert (
        "Resolve first-person references such as 'I' to the known current user."
        in prompt
    )
    assert "prefer a useful refinement such as their role or position" in prompt
    assert '"declaration_source": "builtin_compatibility"' in prompt
    assert "compatibility metadata, not represented knowledge" in prompt


def test_q_and_a_suppression_uses_exact_requirement_identity_not_predicate():
    from src.backend.services import concept_service

    first = {
        "requirement_id": "organisation_has_scientist",
        "predicate_concept_id": "#V#hasLead",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#scientist",
        "declared_on_type_concept_id": "#V#organisation",
    }
    second = {
        "requirement_id": "organisation_has_editor",
        "predicate_concept_id": "#V#hasLead",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#editor",
        "declared_on_type_concept_id": "#V#organisation",
    }

    result = concept_service._filter_suppressed_elicitation_plan(
        [first, second],
        {
            "suppressed_requirement_keys": [
                concept_service._elicitation_requirement_identity(first)
            ]
        },
    )

    assert result == [second]


def test_emitted_question_binds_only_the_matching_plan_item():
    from src.backend.services import concept_service

    first = {
        "requirement_id": "organisation_has_member",
        "predicate_concept_id": "#V#memberOfVonOrg",
        "focal_argument": "object",
        "other_argument_type_concept_id": "#V#von_user",
        "declared_on_type_concept_id": "#V#von_user_organisation",
        "question": "Which Von user is a member of Primary Labs?",
    }
    second = {
        "requirement_id": "organisation_has_scientist",
        "predicate_concept_id": "#V#hasChiefScientist",
        "focal_argument": "subject",
        "other_argument_type_concept_id": "#V#person",
        "declared_on_type_concept_id": "#V#organisation",
        "question": "Who is the chief scientist of Primary Labs?",
    }

    matched = concept_service._match_emitted_elicitation_requirement(
        "Thanks. Who is the chief scientist of Primary Labs?",
        [first, second],
    )

    assert matched is not None
    assert matched["requirement_id"] == "organisation_has_scientist"
    assert matched["predicate_concept_id"] == "#V#hasChiefScientist"


def test_paraphrased_or_ambiguous_question_is_not_bound_to_formalisation():
    from src.backend.services import concept_service

    plan = [
        {
            "requirement_id": "organisation_has_member",
            "predicate_concept_id": "#V#memberOfVonOrg",
            "focal_argument": "object",
            "other_argument_type_concept_id": "#V#von_user",
            "declared_on_type_concept_id": "#V#von_user_organisation",
            "question": "Which Von user is a member of Primary Labs?",
        }
    ]

    assert (
        concept_service._match_emitted_elicitation_requirement(
            "Who belongs to Primary Labs?",
            plan,
        )
        is None
    )
    assert (
        concept_service._match_emitted_elicitation_requirement(
            "Which Von user is a member of Primary Labs?",
            [plan[0], {**plan[0], "requirement_id": "different_direction"}],
        )
        is None
    )


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
        provider_name = "ollama"

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
    assert result["llm_debug_data"]["selected"] == {
        "provider": "ollama",
        "model": "test-model",
        "model_parameters": {},
    }
    assert result["llm_debug_data"]["provider"] == "ollama"
    assert result["llm_debug_data"]["model"] == "test-model"


def test_follow_up_debug_reports_actual_selected_provider_after_fallback(monkeypatch):
    from src.backend.languagemodels import llm_interface
    from src.backend.services import concept_service, settings_service

    class _FallbackClient:
        provider_name = "ollama"

        def generate(self, *, prompt: str, model: str, llm_params=None) -> str:
            return "What should we discuss next?"

    database = mongomock.MongoClient()["concept_question_provider"]
    concept = {
        "_id": "507f1f77bcf86cd799439011",
        "concept_id": "#V#primary_labs",
        "name": "Primary Labs",
    }
    monkeypatch.setattr(concept_service, "get_db", lambda: database)
    monkeypatch.setattr(concept_service, "get_concept_by_id", lambda _value: concept)
    monkeypatch.setattr(concept_service, "get_concept_notes", lambda _value: "")
    monkeypatch.setattr(
        concept_service,
        "get_concept_display_name_with_names_fallback",
        lambda _value: "Primary Labs",
    )
    monkeypatch.setattr(
        concept_service,
        "generate_concept_question",
        lambda **_kwargs: "Continue the concept Q&A.",
    )
    monkeypatch.setattr(
        concept_service,
        "_get_concept_elicitation_plan",
        lambda _concept_id: [],
    )
    monkeypatch.setattr(
        settings_service,
        "resolve_llm_setting",
        lambda **_kwargs: {"provider": "openai", "model": "configured-model"},
    )
    monkeypatch.setattr(
        llm_interface,
        "get_active_model_name",
        lambda **_kwargs: "configured-model",
    )
    monkeypatch.setattr(
        llm_interface,
        "get_active_model_parameters",
        lambda **_kwargs: {"temperature": 0.1},
    )
    monkeypatch.setattr(
        llm_interface,
        "get_llm_client",
        lambda **_kwargs: _FallbackClient(),
    )

    result = concept_service.submit_concept_answer(
        interaction_id="concept-qa-provider-test",
        user_answer="Did you preserve the exact input?",
        session={
            "concept_id": concept["_id"],
            "user_id": "#V#actor",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#actor@org",
            "history": [
                {
                    "interaction_type": "llm_question",
                    "details": {"question": "What should be recorded?"},
                }
            ],
        },
        persist_interaction_session=False,
    )

    assert result["status"] == "success"
    assert result["llm_debug_data"]["configured"]["provider"] == "openai"
    assert result["llm_debug_data"]["selected"]["provider"] == "ollama"
    assert result["llm_debug_data"]["provider"] == "ollama"
    assert result["llm_debug_data"]["model"] == "configured-model"


def test_canonical_concept_notes_update_can_be_disabled_while_exact_claim_survives(
    monkeypatch,
):
    from src.backend.services import concept_service

    class _Client:
        provider_name = "test"

        def generate(self, *, prompt: str, model: str, llm_params=None) -> str:
            return "What else should be recorded?"

    database = mongomock.MongoClient()["concept_question_no_canonical_notes"]
    concept = {
        "_id": "507f1f77bcf86cd799439011",
        "concept_id": "#V#primary_labs",
        "name": "Primary Labs",
    }
    monkeypatch.setattr(concept_service, "get_db", lambda: database)
    monkeypatch.setattr(concept_service, "get_concept_by_id", lambda _value: concept)
    monkeypatch.setattr(concept_service, "get_concept_notes", lambda _value: "")
    monkeypatch.setattr(
        concept_service,
        "get_concept_display_name_with_names_fallback",
        lambda _value: "Primary Labs",
    )
    monkeypatch.setattr(
        concept_service,
        "generate_concept_question",
        lambda **_kwargs: "Continue the concept Q&A.",
    )
    monkeypatch.setattr(
        concept_service,
        "_get_concept_elicitation_plan",
        lambda _concept_id: [],
    )
    monkeypatch.setattr(
        concept_service,
        "_resolve_interaction_llm_runtime",
        lambda _session: (
            _Client(),
            "non-sol-test",
            {},
            {"configured_provider": "test", "selected_provider": "test"},
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "synthesize_and_update_concept_notes",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("canonical hasNote synthesis must not run")
        ),
    )

    result = concept_service.submit_concept_answer(
        interaction_id="concept-qa-no-canonical-notes",
        user_answer="The CEO is Aron D'Souza.",
        session={
            "concept_id": concept["_id"],
            "user_id": "#V#actor",
            "organisation_concept_id": "#V#org",
            "namespace": "#V#actor@org",
            "history": [
                {
                    "interaction_type": "llm_question",
                    "details": {"question": "Who is the CEO?"},
                }
            ],
        },
        representation_overrides={
            "exact_answer": {
                "status": "stored",
                "effect_status": "succeeded",
                "assertion_id": "ska_exact_claim",
                "canonical_publication": False,
            }
        },
        persist_interaction_session=False,
        allow_canonical_notes_update=False,
    )

    notes = result["representation"]["concept_notes"]
    assert result["status"] == "success"
    assert result["representation"]["exact_answer"]["assertion_id"] == (
        "ska_exact_claim"
    )
    assert notes["status"] == "not_attempted"
    assert notes["notes_updated"] is False
    assert notes["reason"] == "canonical_concept_edit_authority_not_delegated"
