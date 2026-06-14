from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from typing import Any

from src.backend.workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CHAT_BUTTONIFY_WORKFLOW_ID,
    CHAT_NARRATION_WORKFLOW_ID,
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    MISSING_TOOL_CALL_WORKFLOW_ID,
    TODO_REFRESH_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from src.backend.workflows.durable.parent_specificity_concept_dossier_workflow import (
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
    build_parent_specificity_concept_dossier_workflow_test_registration,
)
from src.backend.workflows.durable.parent_specificity_rumination_workflow import (
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    build_parent_specificity_rumination_workflow_test_registration,
)
from src.backend.workflows.durable.planning_workflow import (
    PLANNING_WORKFLOW_ID,
    build_planning_workflow_test_registration,
)
from src.backend.workflows.durable.rumination_workflow import (
    RUMINATION_WORKFLOW_ID,
    build_rumination_workflow_test_registration,
)
from src.backend.workflows.durable.rag_sync_workflow import (
    RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
    build_rag_text_relation_sync_workflow_test_registration,
)
from src.backend.workflows.durable.enrichment_workflow import (
    ENRICHMENT_WORKFLOW_ID,
    build_enrichment_workflow_test_registration,
)
from src.backend.workflows.durable.workflow_introspection_maintenance_workflow import (
    WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
    build_workflow_introspection_maintenance_workflow_test_registration,
)
from src.backend.workflows.durable.mongo_query_diagnostics_maintenance_workflow import (
    MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,
    build_mongo_query_diagnostics_maintenance_workflow_test_registration,
)
from src.backend.workflows.durable.entity_identity_resolution_workflow import (
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    build_entity_identity_resolution_workflow_test_registration,
)
from src.backend.workflows.durable.jira_task_incremental_import_workflow import (
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
)
from src.backend.workflows.durable.multilingual_concept_enrichment_workflow import (
    MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
    build_multilingual_concept_enrichment_workflow_test_registration,
)
from src.backend.services.representation_workflow_routing_coverage_audit_contracts import (
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
)
from src.backend.workflows.durable.representation_workflow_routing_coverage_audit_workflow import (
    build_representation_workflow_routing_coverage_audit_test_registration,
)
from src.backend.workflows.durable.workflow_gap_recovery_workflow import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    build_workflow_discovery_gap_recovery_workflow_test_registration,
    build_workflow_gap_test_registration,
)
from src.backend.workflows.durable.file_copy_interpretation_workflow import (
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    build_file_copy_interpretation_workflow_test_registration,
)
from src.backend.workflows.durable.file_copy_typing_workflow import (
    FILE_COPY_TYPING_WORKFLOW_ID,
    build_file_copy_typing_workflow_test_registration,
)
from src.backend.workflows.durable.file_copy_upload_classification_workflow import (
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    build_file_copy_upload_classification_workflow_test_registration,
)
from src.backend.workflows.durable.file_copy_upload_handler_workflow import (
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    build_file_copy_upload_handler_workflow_test_registration,
)
from src.backend.workflows.workflow_registry import (
    LazyWorkflowRegistration,
    WorkflowRegistration,
    WorkflowRegistry,
)
from src.backend.workflows import (
    workflow_concept_authority_service as authority_service,
)

TEST_WORKFLOW_PURPOSES: dict[str, str] = {
    MISSING_TOOL_CALL_WORKFLOW_ID: (
        "Recovery workflow for conversation turns where expected tool-call JSON "
        "was missing and a targeted retry may repair the response."
    ),
    CHAT_NARRATION_WORKFLOW_ID: (
        "Narrative generation workflow for conversation turns that need "
        "storytelling, framing, or persona-consistent narration."
    ),
    CHAT_BUTTONIFY_WORKFLOW_ID: (
        "Output-transformation workflow that converts an assistant response into "
        "safe quick-reply button options."
    ),
    CHAT_ASSISTANT_WORKFLOW_ID: (
        "Direct conversational response workflow for greetings, hello messages, "
        "simple questions, clarifications, thank-you replies, and general chat "
        "turns that do not require tools."
    ),
    TODO_REFRESH_WORKFLOW_ID: (
        "Refresh the user's to-do list from Gmail and knowledge-base task data."
    ),
    WRITE_TOOL_POLICY_WORKFLOW_ID: (
        "Policy decision workflow that allows, denies, or escalates proposed "
        "write-category tool invocations."
    ),
    CONCEPT_SUGGESTION_PREFLIGHT_WORKFLOW_ID: (
        "Specialised ontology preflight workflow that emits guarded fallback "
        "type and predicate suggestions when baseline discovery is sparse."
    ),
    TOOL_CALLING_WORKFLOW_ID: (
        "General-purpose tool-calling pipeline for turns that require MCP tools, "
        "data retrieval, file operations, external APIs, web search, arXiv paper "
        "search, transformer-paper lookup, or knowledge-base writes."
    ),
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID: (
        "Critic workflow that evaluates whether required knowledge-base mutation "
        "effects were executed and verified."
    ),
    TURN_COMPLETION_GATE_WORKFLOW_ID: (
        "Completion-gate workflow that prevents false completion claims when "
        "required effects remain unresolved or unverified."
    ),
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID: (
        "Prompt-backed workflow that adjudicates which prior conversation "
        "context may shape expected-outcome inference, routing, and answering."
    ),
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID: (
        "Canonical conversation-turn execution workflow that derives required "
        "effects, runs postcondition checks, and gates completion claims."
    ),
    FILE_COPY_TYPING_WORKFLOW_ID: (
        "Infer and persist authoritative file-copy typing so downstream "
        "workflows can route from Vontology-backed artefact taxonomy."
    ),
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID: (
        "Classify uploaded file copies and persist route decisions before "
        "dispatching into specialised or baseline workflows."
    ),
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID: (
        "General workflow-first upload handler that routes file copies into "
        "specialised or baseline interpretation workflows."
    ),
    FILE_COPY_INTERPRETATION_WORKFLOW_ID: (
        "Interpret uploaded file copies with image/document extraction and "
        "persisted concept enrichment."
    ),
    RAG_TEXT_RELATION_SYNC_WORKFLOW_ID: (
        "Synchronise Vontology text relations to the RAG store with checkpointed "
        "batch upserts."
    ),
    ENRICHMENT_WORKFLOW_ID: (
        "Generate missing text relations for concepts using a parameterised "
        "enrichment workflow."
    ),
    WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID: (
        "Diagnose workflow and prompt incidents, plan bounded repairs, and verify "
        "maintenance outcomes."
    ),
    MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID: (
        "Collect redacted Mongo query-targeting diagnostics for conversation or "
        "recurring maintenance review."
    ),
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID: (
        "Detect duplicate entities, apply confidence-scored resolutions, and "
        "summarise identity-maintenance results."
    ),
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID: (
        "Synchronise Jira tasks into Von via the shared incremental migration "
        "runner."
    ),
    MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID: (
        "Add missing Chinese, Spanish, and French concept names and "
        "descriptions for well-described, non-trivially used concepts."
    ),
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID: (
        "Audit broad representation-workflow routing coverage and persist "
        "workflow-authored improvement suggestions for the self-improvement path."
    ),
    PLANNING_WORKFLOW_ID: (
        "Forward inference workflow that proposes concrete, validated next "
        "actions including tool calls and workflow invocations."
    ),
    RUMINATION_WORKFLOW_ID: (
        "Proactive knowledge quality orchestrator that assesses ontology gaps "
        "and dispatches enrichment work."
    ),
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID: (
        "Gather multilingual dossier evidence for parent-specificity taxonomy "
        "analysis."
    ),
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID: (
        "Review parent candidates using dossier evidence and apply or defer "
        "taxonomy refinements."
    ),
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID: (
        "Recover from workflow discovery misses by analysing the gap, creating "
        "candidate workflows, and optionally testing them."
    ),
    WORKFLOW_GAP_TEST_WORKFLOW_ID: (
        "Run and assess a candidate workflow against explicit workflow-gap "
        "acceptance requirements."
    ),
}

AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS: tuple[str, ...] = (
    PLANNING_WORKFLOW_ID,
    RUMINATION_WORKFLOW_ID,
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
)
AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS: tuple[str, ...] = (
    RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
    ENRICHMENT_WORKFLOW_ID,
    WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
    MONGO_QUERY_DIAGNOSTICS_MAINTENANCE_WORKFLOW_ID,
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
    MULTILINGUAL_CONCEPT_ENRICHMENT_WORKFLOW_ID,
    REPRESENTATION_WORKFLOW_ROUTING_COVERAGE_AUDIT_WORKFLOW_ID,
)
AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS: tuple[str, ...] = (
    FILE_COPY_TYPING_WORKFLOW_ID,
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
)

TEST_WORKFLOW_SELECTOR_PROMPT_ID = "#V#chat_turn_classifier_prompt"
TEST_WORKFLOW_SELECTOR_PROMPT_TEMPLATE = (
    "You are a workflow router. You will receive the full turn context as "
    "LLM context messages, the user's current request, and the candidate "
    "workflows below. Select the single best workflow.\n\n"
    "Return JSON only with fields workflow_id, confidence, and reasoning.\n"
    "Return exactly one JSON object. Do not wrap it in Markdown fences. "
    "Do not include surrounding prose.\n\n"
    "Rules:\n"
    "- Prefer the most specific routing-eligible executable workflow.\n"
    "- workflow_id must be exactly one of the candidate workflow IDs listed below.\n"
    "- Treat the full turn context messages as authoritative context for "
    "resolving user-relative references and continuity.\n"
    "- Use the current user request as the immediate routing objective unless "
    "explicit workflow continuation context shows the turn is mainly a "
    "continuation, repair, verification, or follow-up.\n"
    "- When workflow continuation context is present, treat it as "
    "authoritative routing context for continuation, repair, verification, "
    "or failure-explanation turns unless the user explicitly diverges.\n"
    "- When the current request is a represented-knowledge lookup about an "
    "already-resolved entity and its related facts, artefacts, or "
    "relationships, prefer KB/concept/relation retrieval workflows over "
    "creation, ingestion, or representation workflows unless the user "
    "explicitly asks to create or ingest new artefacts.\n"
    "- Treat maintenance or testing workflows as requiring explicit workflow, "
    "test, or experiment intent when the candidate evidence says workflow "
    "context is required.\n"
    "- Do not ask the user a clarification question from this selector stage.\n"
    "- Do not answer the user directly from this selector stage.\n"
    "- If a specialised candidate is disqualified, name that evidence in the "
    "reasoning.\n\n"
    "Canonical valid output examples:\n"
    '- {"workflow_id":"#V#concept_search_instance_retrieval_workflow",'
    '"confidence":0.96,"reasoning":"The request asks for represented '
    "information about a specific concept, so the specialised retrieval "
    'workflow is the best eligible candidate."}\n'
    '- {"workflow_id":"#V#tool_calling_workflow","confidence":0.88,'
    '"reasoning":"The request requires tools or external actions, so the '
    'general tool-calling workflow is the best eligible route."}\n'
    '- {"workflow_id":"#V#chat_assistant_workflow","confidence":0.84,'
    '"reasoning":"The turn is a plain conversational exchange that does '
    'not require tools or a more specific specialised workflow."}\n\n'
    "Invalid outputs. Never do any of these:\n"
    '- "I\'m not sure which workflow you want. Please clarify."\n'
    '- "Here is the answer to your question."\n'
    '- {"tool_name":"vontology_concept_search","arguments":'
    '{"query":"current user"}}\n\n'
    "Workflow continuation context:\n{continuation_routing_context}\n\n"
    "User request:\n{turn_text}\n\n"
    "Candidate workflows:\n{candidate_list}\n"
)


def render_test_prompt_template(
    template: str,
    variables: dict[str, Any] | None = None,
) -> str:
    rendered = str(template)
    for key, value in dict(variables or {}).items():
        rendered = rendered.replace("{" + str(key) + "}", str(value))
    return rendered


def build_test_prompt_service(
    *,
    prompt_text: str | None = TEST_WORKFLOW_SELECTOR_PROMPT_TEMPLATE,
    prompt_id: str = TEST_WORKFLOW_SELECTOR_PROMPT_ID,
    error: Exception | None = None,
) -> Any:
    class _TestPromptService:
        def render_prompt(
            self,
            concept_ids: Any,
            *,
            variables: Any = None,
            fallback: Any = None,
            max_chars: Any = None,
        ) -> Any:
            _ = (concept_ids, fallback, max_chars)
            if error is not None:
                raise error
            if prompt_text is None:
                return None
            rendered = render_test_prompt_template(
                prompt_text,
                dict(variables or {}),
            )
            return SimpleNamespace(
                prompt_id=prompt_id,
                text=rendered,
                variables=dict(variables or {}),
                truncated=False,
            )

    return _TestPromptService()


def build_test_conversation_turn_registry() -> WorkflowRegistry:
    registry = WorkflowRegistry()
    register_test_conversation_turn_workflows(registry)
    return registry


def register_test_conversation_turn_workflows(
    registry: WorkflowRegistry,
) -> WorkflowRegistry:
    for workflow_id in authority_service.CANONICAL_CHAT_WORKFLOW_IDS:
        registry.register_lazy(
            LazyWorkflowRegistration(
                workflow_id=workflow_id,
                purpose=TEST_WORKFLOW_PURPOSES.get(workflow_id, workflow_id),
                source="vontology",
            )
        )
    return registry


def build_authoritative_test_workflow_definition(workflow_id: str) -> Any:
    spec = authority_service.seed_canonical_workflow_publication_specs()[workflow_id]
    return authority_service._build_definition_from_publication_spec(
        workflow_id=workflow_id,
        spec=spec,
    )


def register_authoritative_test_workflows(
    registry: WorkflowRegistry,
    workflow_ids: tuple[str, ...] | None = None,
) -> WorkflowRegistry:
    target_ids = workflow_ids or authority_service.CANONICAL_CHAT_WORKFLOW_IDS
    for workflow_id in target_ids:
        registry.register(
            WorkflowRegistration(
                workflow_id=workflow_id,
                definition=build_authoritative_test_workflow_definition(workflow_id),
                purpose=TEST_WORKFLOW_PURPOSES.get(workflow_id, workflow_id),
                source="vontology",
            )
        )
    return registry


def bootstrap_authoritative_test_workflows(
    workflow_ids: tuple[str, ...],
) -> dict[str, Any]:
    registry = WorkflowRegistry()
    register_authoritative_test_workflows(registry, workflow_ids=workflow_ids)
    return authority_service.bootstrap_workflow_concepts(registry=cast(Any, registry))


def bootstrap_authoritative_conversation_turn_workflows() -> dict[str, Any]:
    return bootstrap_authoritative_test_workflows(
        authority_service.CANONICAL_CHAT_WORKFLOW_IDS
    )


def authoritative_step_id(*, workflow_id: str, state_id: str) -> str:
    return authority_service._step_concept_id(
        workflow_id=workflow_id,
        state_id=state_id,
    )


def bootstrap_authoritative_reasoning_recovery_workflows() -> dict[str, Any]:
    registry = WorkflowRegistry()
    for registration in (
        build_planning_workflow_test_registration(),
        build_rumination_workflow_test_registration(),
        build_parent_specificity_concept_dossier_workflow_test_registration(),
        build_parent_specificity_rumination_workflow_test_registration(),
        build_workflow_discovery_gap_recovery_workflow_test_registration(),
        build_workflow_gap_test_registration(),
    ):
        registry.register(registration)

    return authority_service.bootstrap_workflow_concepts(
        registry=registry,
        target_workflow_ids=AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS,
    )


def bootstrap_authoritative_support_maintenance_workflows() -> dict[str, Any]:
    from src.backend.services.entity_identity_resolution_workflow_vontology_service import (
        bootstrap_canonical_entity_identity_resolution_workflow,
    )
    from src.backend.services.jira_task_incremental_import_workflow_vontology_service import (
        bootstrap_canonical_jira_task_incremental_import_workflow,
    )
    from src.backend.services.representation_workflow_routing_coverage_audit_vontology_service import (
        bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow,
    )

    bootstrap_canonical_entity_identity_resolution_workflow()
    jira_bootstrap_report = bootstrap_canonical_jira_task_incremental_import_workflow()
    representation_audit_bootstrap_report = (
        bootstrap_canonical_representation_workflow_routing_coverage_audit_workflow()
    )

    registry = WorkflowRegistry()
    for registration in (
        build_rag_text_relation_sync_workflow_test_registration(),
        build_enrichment_workflow_test_registration(),
        build_workflow_introspection_maintenance_workflow_test_registration(),
        build_mongo_query_diagnostics_maintenance_workflow_test_registration(),
        build_entity_identity_resolution_workflow_test_registration(),
        build_multilingual_concept_enrichment_workflow_test_registration(),
        build_representation_workflow_routing_coverage_audit_test_registration(),
    ):
        registry.register(registration)

    report = authority_service.bootstrap_workflow_concepts(
        registry=registry,
        target_workflow_ids=AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS,
    )
    report["jira_task_incremental_import_workflow_bootstrap"] = jira_bootstrap_report
    report["representation_workflow_routing_coverage_audit_bootstrap"] = (
        representation_audit_bootstrap_report
    )
    return report


def bootstrap_authoritative_file_copy_workflows() -> dict[str, Any]:
    registry = WorkflowRegistry()
    for registration in (
        build_file_copy_typing_workflow_test_registration(),
        build_file_copy_upload_classification_workflow_test_registration(),
        build_file_copy_upload_handler_workflow_test_registration(),
        build_file_copy_interpretation_workflow_test_registration(),
    ):
        registry.register(registration)

    return authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry),
        target_workflow_ids=AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS,
    )
