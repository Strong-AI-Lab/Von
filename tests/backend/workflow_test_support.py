from __future__ import annotations

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
    WRITE_TOOL_POLICY_WORKFLOW_ID,
)
from src.backend.workflows.durable.parent_specificity_concept_dossier_workflow import (
    PARENT_SPECIFICITY_DOSSIER_WORKFLOW_ID,
    get_parent_specificity_concept_dossier_workflow_registration,
)
from src.backend.workflows.durable.parent_specificity_rumination_workflow import (
    PARENT_SPECIFICITY_RUMINATION_WORKFLOW_ID,
    get_parent_specificity_rumination_workflow_registration,
)
from src.backend.workflows.durable.planning_workflow import (
    PLANNING_WORKFLOW_ID,
    get_planning_workflow_registration,
)
from src.backend.workflows.durable.rumination_workflow import (
    RUMINATION_WORKFLOW_ID,
    get_rumination_workflow_registration,
)
from src.backend.workflows.durable.rag_sync_workflow import (
    RAG_TEXT_RELATION_SYNC_WORKFLOW_ID,
    get_rag_sync_workflow_registration,
)
from src.backend.workflows.durable.enrichment_workflow import (
    ENRICHMENT_WORKFLOW_ID,
    get_enrichment_workflow_registration,
)
from src.backend.workflows.durable.workflow_introspection_maintenance_workflow import (
    WORKFLOW_INTROSPECTION_MAINTENANCE_WORKFLOW_ID,
    get_workflow_introspection_maintenance_registration,
)
from src.backend.workflows.durable.entity_identity_resolution_workflow import (
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    get_entity_identity_resolution_workflow_registration,
)
from src.backend.workflows.durable.jira_task_incremental_import_workflow import (
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
    get_jira_task_incremental_import_workflow_registration,
)
from src.backend.workflows.durable.workflow_gap_recovery_workflow import (
    WORKFLOW_DISCOVERY_GAP_RECOVERY_WORKFLOW_ID,
    WORKFLOW_GAP_TEST_WORKFLOW_ID,
    get_workflow_discovery_gap_recovery_workflow_registration,
    get_workflow_gap_test_workflow_registration,
)
from src.backend.workflows.durable.file_copy_interpretation_workflow import (
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
    get_file_copy_interpretation_workflow_registration,
)
from src.backend.workflows.durable.file_copy_typing_workflow import (
    FILE_COPY_TYPING_WORKFLOW_ID,
    get_file_copy_typing_workflow_registration,
)
from src.backend.workflows.durable.file_copy_upload_classification_workflow import (
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    get_file_copy_upload_classification_workflow_registration,
)
from src.backend.workflows.durable.file_copy_upload_handler_workflow import (
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    get_file_copy_upload_handler_workflow_registration,
)
from src.backend.workflows.workflow_registry import (
    LazyWorkflowRegistration,
    WorkflowRegistration,
    WorkflowRegistry,
)
from src.backend.workflows import workflow_concept_authority_service as authority_service


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
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID: (
        "Detect duplicate entities, apply confidence-scored resolutions, and "
        "summarise identity-maintenance results."
    ),
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID: (
        "Synchronise Jira tasks into Von via the shared incremental migration "
        "runner."
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
    ENTITY_IDENTITY_RESOLUTION_WORKFLOW_ID,
    JIRA_TASK_INCREMENTAL_IMPORT_WORKFLOW_ID,
)
AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS: tuple[str, ...] = (
    FILE_COPY_TYPING_WORKFLOW_ID,
    FILE_COPY_UPLOAD_CLASSIFICATION_WORKFLOW_ID,
    FILE_COPY_UPLOAD_HANDLER_WORKFLOW_ID,
    FILE_COPY_INTERPRETATION_WORKFLOW_ID,
)


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
    spec = authority_service._CANONICAL_WORKFLOW_PUBLICATION_SPECS[workflow_id]
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
        get_planning_workflow_registration(),
        get_rumination_workflow_registration(),
        get_parent_specificity_concept_dossier_workflow_registration(),
        get_parent_specificity_rumination_workflow_registration(),
        get_workflow_discovery_gap_recovery_workflow_registration(),
        get_workflow_gap_test_workflow_registration(),
    ):
        registry.register(registration)

    return authority_service.bootstrap_workflow_concepts(
        registry=registry,
        target_workflow_ids=AUTHORITATIVE_REASONING_RECOVERY_WORKFLOW_IDS,
    )


def bootstrap_authoritative_support_maintenance_workflows() -> dict[str, Any]:
    registry = WorkflowRegistry()
    for registration in (
        get_rag_sync_workflow_registration(),
        get_enrichment_workflow_registration(),
        get_workflow_introspection_maintenance_registration(),
        get_entity_identity_resolution_workflow_registration(),
        get_jira_task_incremental_import_workflow_registration(),
    ):
        registry.register(registration)

    return authority_service.bootstrap_workflow_concepts(
        registry=registry,
        target_workflow_ids=AUTHORITATIVE_SUPPORT_MAINTENANCE_WORKFLOW_IDS,
    )


def bootstrap_authoritative_file_copy_workflows() -> dict[str, Any]:
    registry = WorkflowRegistry()
    for registration in (
        get_file_copy_typing_workflow_registration(),
        get_file_copy_upload_classification_workflow_registration(),
        get_file_copy_upload_handler_workflow_registration(),
        get_file_copy_interpretation_workflow_registration(),
    ):
        registry.register(registration)

    return authority_service.bootstrap_workflow_concepts(
        registry=cast(Any, registry),
        target_workflow_ids=AUTHORITATIVE_FILE_COPY_WORKFLOW_IDS,
    )
