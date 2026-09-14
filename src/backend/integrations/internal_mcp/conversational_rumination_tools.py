"""Model-selected background inquiry submission with server-bound provenance."""

from .gateway import MethodDefinition
from .schemas import Schema


def definitions():
    from ...services.conversational_rumination_service import start_inquiry

    return [
        MethodDefinition(
            name="task_start_background_inquiry",
            handler=start_inquiry,
            input_schema=Schema(
                required={"mention": str, "question": str, "relevant_context": str},
                optional={
                    key: (str, type(None))
                    for key in (
                        "originating_session_id",
                        "request_id",
                        "source_prompt",
                        "namespace",
                        "acting_user_concept_id",
                        "organisation_concept_id",
                    )
                },
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, optional={}, allow_unknown=True),
            category="write",
            ordinary_turn_effect=True,
            ordinary_turn_trusted_argument_bindings={
                "originating_session_id": "conversation_id",
                "request_id": "turn_id",
                "source_prompt": "turn_prompt",
                "namespace": "turn_namespace",
                "acting_user_concept_id": "actor_user_concept_id",
                "organisation_concept_id": "actor_organisation_concept_id",
            },
            description=(
                "Start one bounded independent background inquiry into a worthwhile "
                "conversational unknown. Provide an exact mention from this user turn, "
                "the question and minimal relevant context. Uses an idempotent Von task "
                "and existing dispatcher; returns without waiting for research. "
                "Later turns receive its sourced current work product. Optional; "
                "do not use for novelty alone, declined topics or an existing inquiry."
            ),
        )
    ]
