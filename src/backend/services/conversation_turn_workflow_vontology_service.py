"""Materialise the canonical conversation-turn workflow family."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .text_value_service import get_texts_for_concept, upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
    safe_str,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle
from .synthesiser_context_framing_service import (
    SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
)
from ..workflows.definitions import (
    CHAT_ASSISTANT_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
    GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
)

_MANAGED_BY = "conversation_turn_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2333"
_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "canonical_workflow_publication_seed_bundle.json"
)
_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID = (
    "#V#prompt_turn_execution_expected_outcome_inference"
)
_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID = (
    "#V#turn_prompt_context_adjudication_prompt"
)
_SELECTOR_PROMPT_CONCEPT_ID = "#V#chat_turn_classifier_prompt"
_NARRATION_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_narrate_completion_report"
_RECOVERY_PROMPT_CONCEPT_ID = "#V#prompt_turn_execution_recovery_decision"
_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID = "#V#missing_tool_call_retry_prompt"
_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID = (
    "#V#prompt_turn_execution_postcondition_critic"
)
_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID = "#V#tool_call_repair_prompt"
_EXPECTED_OUTCOME_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_expected_outcome_inference_seed.md"
)
_CONTEXT_ADJUDICATION_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "turn_prompt_context_adjudication_prompt_seed.md"
)
_SELECTOR_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_chat_turn_classifier_seed.md"
)
_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_narrate_completion_report_seed.md"
)
_RECOVERY_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_recovery_decision_seed.md"
)
_MISSING_TOOL_RETRY_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "missing_tool_call_retry_prompt_seed.md"
)
_POSTCONDITION_CRITIC_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_turn_execution_postcondition_critic_seed.md"
)
_TOOL_CALL_REPAIR_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "tool_call_repair_prompt_seed.md"
)
_SYNTHESISER_CONTEXT_FRAMING_PROMPT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "synthesiser_context_framing_prompt_seed.json"
)
_TARGET_WORKFLOW_IDS: tuple[str, ...] = (
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
    GENERAL_MAIL_REVIEW_WORKFLOW_ID,
    GMAIL_MESSAGE_DETAIL_FETCH_WORKFLOW_ID,
    KB_MUTATION_POSTCONDITION_CRITIC_WORKFLOW_ID,
    TURN_COMPLETION_GATE_WORKFLOW_ID,
    WORKFLOW_EXPERIENCE_CONTEXT_PRELUDE_WORKFLOW_ID,
    TURN_PROMPT_CONTEXT_ADJUDICATION_WORKFLOW_ID,
    CONVERSATION_TURN_EXECUTION_WORKFLOW_ID,
)


def _load_narration_prompt_seed_text() -> str:
    prompt_text = _PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("turn_execution_narration_prompt_seed_missing")
    return prompt_text


def _load_expected_outcome_prompt_seed_text() -> str:
    prompt_text = _EXPECTED_OUTCOME_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("turn_execution_expected_outcome_prompt_seed_missing")
    return prompt_text


def _load_context_adjudication_prompt_seed_text() -> str:
    prompt_text = _CONTEXT_ADJUDICATION_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("turn_prompt_context_adjudication_prompt_seed_missing")
    return prompt_text


def _load_selector_prompt_seed_text() -> str:
    prompt_text = _SELECTOR_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("chat_turn_classifier_prompt_seed_missing")
    return prompt_text


def _load_recovery_prompt_seed_text() -> str:
    prompt_text = _RECOVERY_PROMPT_SEED_ASSET_PATH.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise ValueError("turn_execution_recovery_prompt_seed_missing")
    return prompt_text


def _load_missing_tool_retry_prompt_seed_text() -> str:
    prompt_text = _MISSING_TOOL_RETRY_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("missing_tool_call_retry_prompt_seed_missing")
    return prompt_text


def _load_postcondition_critic_prompt_seed_text() -> str:
    prompt_text = _POSTCONDITION_CRITIC_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("turn_execution_postcondition_critic_prompt_seed_missing")
    return prompt_text


def _load_tool_call_repair_prompt_seed_text() -> str:
    prompt_text = _TOOL_CALL_REPAIR_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("tool_call_repair_prompt_seed_missing")
    return prompt_text


def _load_synthesiser_context_framing_prompt_seed_text() -> str:
    prompt_text = _SYNTHESISER_CONTEXT_FRAMING_PROMPT_SEED_ASSET_PATH.read_text(
        encoding="utf-8"
    ).strip()
    if not prompt_text:
        raise ValueError("synthesiser_context_framing_prompt_seed_missing")
    return prompt_text


def _prompt_seed_needs_refresh(
    prompt_concept_id: str,
    *,
    required_markers: tuple[str, ...],
) -> bool:
    try:
        rows = get_texts_for_concept(
            subject_concept_id=prompt_concept_id,
            limit=16,
        )
    except Exception:
        return False

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        predicate = safe_str(row.get("predicate"))
        if predicate not in {"hasContent", "#V#hasContent"}:
            continue
        text = safe_str(row.get("text")) or ""
        if text and any(marker not in text for marker in required_markers):
            return True
    return False


def _ensure_conversation_turn_prompt_support(
    *,
    force_prompt_seed: bool = False,
) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID,
                name="Turn expected-outcome inference prompt",
                description=(
                    "Canonical early-turn inference prompt for deriving the "
                    "grounded success contract that should shape workflow "
                    "selection, omission policy, and direct-answer behaviour."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID,
                name="Turn prompt context adjudication prompt",
                description=(
                    "Canonical conversation-turn prompt for deciding which "
                    "prior conversational context is relevant to the current "
                    "prompt before expected-outcome inference and workflow "
                    "selection consume context."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_SELECTOR_PROMPT_CONCEPT_ID,
                name="Chat turn classifier prompt",
                description=(
                    "Canonical workflow-selector prompt for conversation turns. "
                    "The selector receives the full turn context as LLM context "
                    "messages and chooses the best workflow from the candidate set."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_NARRATION_PROMPT_CONCEPT_ID,
                name="Turn execution completion-report narration prompt",
                description=(
                    "Canonical conversation-turn narration prompt for composing "
                    "the user-facing answer from selected-workflow result "
                    "content, using completion-report data only as supporting "
                    "evidence."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_RECOVERY_PROMPT_CONCEPT_ID,
                name="Turn execution recovery decision prompt",
                description=(
                    "Canonical recovery prompt for choosing the next best "
                    "bounded executable turn-next-action from accumulated turn "
                    "evidence, including a workflow retry, a direct bounded "
                    "tool batch, a direct grounded answer, or an explicit "
                    "follow-up response when no further automated route is "
                    "likely to help."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID,
                name="Missing tool-call retry prompt",
                description=(
                    "Canonical retry prompt used when a response describes an "
                    "action requiring MCP tools but omits the executable "
                    "tool-call JSON. The prompt instructs the model to use "
                    "available generic Vontology mutation tools for explicit "
                    "low-risk additive writes, and exact external write tools "
                    "for explicitly requested side effects, rather than "
                    "inventing domain-specific tool names."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID,
                name="Turn execution postcondition critic prompt",
                description=(
                    "Canonical postcondition-critic prompt for deciding whether "
                    "a turn's answer is safely supported by the evidence actually "
                    "produced by the selected workflow and verification reads."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID,
                name="Tool-call repair prompt",
                description=(
                    "Canonical one-shot repair prompt for converting a malformed "
                    "or schema-invalid tool plan into a corrected JSON-only MCP "
                    "tool-call batch, or an empty batch when no repair is possible."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
            WorkflowPromptConceptSpec(
                concept_id=SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
                name="Synthesiser context framing template",
                description=(
                    "Canonical represented template for the summariser-stage "
                    "system messages that preserve the active request and "
                    "Vontology-authored tool-output hints."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        provenance_source=_MANAGED_BY,
    )

    seeded_prompt_ids: list[str] = []
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _EXPECTED_OUTCOME_PROMPT_CONCEPT_ID,
            required_markers=(
                "gmail_send_message",
                "external-system side effect",
                "turn_context_handoff_decision",
                "`no_prior_context`",
                "represented labels, categories, tags, role markers",
                "#V#represented_artefact_creation_workflow",
                "grounded `parent_id`",
                "stable target handles",
                "read-only Gmail retrieval",
                "#V#general_mail_review_workflow",
                "gmail_list_profiles",
                "gmail_list_messages",
                "gmail_get_message",
                "read-only Jira retrieval",
                "jira_get_issue",
                "jira_search",
                "Jira direct issue-key lookup example",
                "Do not use Jira import/reconciliation workflows",
                "#V#jira_task_full_reconciliation_workflow",
                "task_import_jira_issues",
                "Jira recency/list lookup example",
                "Do not use the mail-review workflow for Gmail auth",
                "Gmail auth, OAuth, scope, auth-config, and token-status checks",
                "read-only authentication diagnostics",
                "gmail_get_auth_config",
                "confirmation doubt about a Gmail/interface access check",
                "no manual token-refresh tool exists",
                "Gmail access-check doubt with refresh-if-available example",
                "predicates, relation schema, usage, incidence",
                "must contain only exact tool IDs",
                "Never invent capability-shaped tool names",
                "workflow_concept_ids",
                "`conditional_required_tools`",
                "put `workflow_execute` in `conditional_required_tools`",
                "Do not substitute Gmail listing, reading, or label-modification tools",
                "#V#arxiv_paper_representation_workflow",
                "#V#scholarly_article_metadata_representation_workflow",
                "Distinguish prior *referents* from prior *obligations*",
                "prior_obligation_carry_forward",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_expected_outcome_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID,
            required_markers=(
                "turn_context_handoff_decision",
                "`no_prior_context`",
                "`raw_recent_turns_required`",
                "Do not answer the user",
                "prior_obligation_carry_forward",
                "suppressed_prior_obligations",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_context_adjudication_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": "JVNAUTOSCI-2544", "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_SELECTOR_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _SELECTOR_PROMPT_CONCEPT_ID,
            required_markers=(
                "turn_context_handoff_decision",
                "`no_prior_context`",
                "adjudicated prior-context handoff",
                "Treat `required_tools` as exact-symbol evidence only",
                "do not select a semantically similar workflow",
                "Treat auth, OAuth, scope, auth-config, credential",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_SELECTOR_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_selector_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_SELECTOR_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_NARRATION_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _NARRATION_PROMPT_CONCEPT_ID,
            required_markers=(
                "Grounding a concrete result to the right target",
                "verified this turn",
                "Do not collapse a multi-target request to a single concept",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_NARRATION_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_narration_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_NARRATION_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_RECOVERY_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _RECOVERY_PROMPT_CONCEPT_ID,
            required_markers=(
                "Gmail/mail tool blockers",
                "gmail_list_profiles",
                "do not answer as if Gmail auth or mailbox state was verified",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_RECOVERY_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_recovery_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_RECOVERY_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID,
            required_markers=(
                "gmail_send_message",
                "external-system side effect",
                "represented labels, categories, tags, workflow markers",
                "Do NOT emit `create_concepts` without `parent_id`",
                "Do NOT use `#V#thing` as the parent",
                "read-only Gmail tools",
                "gmail_list_profiles",
                "gmail_list_messages",
                "gmail_get_message",
                "Gmail auth, token, OAuth-scope, or auth-config checks",
                "Never invent placeholder aliases such as `user_profile_123`",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_missing_tool_retry_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID,
            required_markers=(
                "final-answer synthesis telemetry",
                "final_answer_synthesis.tool_evidence_projection",
                "operational status/ledger summary",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_postcondition_critic_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID)
    if (
        force_prompt_seed
        or not prompt_concept_has_content(_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID)
        or _prompt_seed_needs_refresh(
            _TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID,
            required_markers=(
                "Gmail profile-scoped requests",
                "gmail_get_auth_config",
                "Never emit placeholders such as `user_profile_123`",
            ),
        )
    ):
        upsert_singleton_text_relation(
            subject_concept_id=_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_tool_call_repair_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID)
    if force_prompt_seed or not prompt_concept_has_content(
        SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID
    ):
        upsert_singleton_text_relation(
            subject_concept_id=SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID,
            predicate="hasContent",
            text=_load_synthesiser_context_framing_prompt_seed_text(),
            lang="en-NZ",
            context={"jira": "JVNAUTOSCI-2350", "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded_prompt_ids.append(SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID)

    report = dict(report)
    errors_by_target = dict(report.get("errors_by_target") or {})
    missing_content_prompt_ids = list(report.get("missing_content_prompt_ids") or [])
    if prompt_concept_has_content(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _EXPECTED_OUTCOME_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _EXPECTED_OUTCOME_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_EXPECTED_OUTCOME_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_CONTEXT_ADJUDICATION_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_SELECTOR_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_SELECTOR_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _SELECTOR_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _SELECTOR_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_SELECTOR_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_NARRATION_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_NARRATION_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _NARRATION_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _NARRATION_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_NARRATION_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_RECOVERY_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_RECOVERY_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _RECOVERY_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _RECOVERY_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_RECOVERY_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_MISSING_TOOL_RETRY_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_POSTCONDITION_CRITIC_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID):
        errors_by_target.pop(_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != _TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if _TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(_TOOL_CALL_REPAIR_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids
    if prompt_concept_has_content(SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID):
        errors_by_target.pop(SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID, None)
        missing_content_prompt_ids = [
            prompt_id
            for prompt_id in missing_content_prompt_ids
            if prompt_id != SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID
        ]
        validated_prompt_ids = list(report.get("validated_prompt_ids") or [])
        if SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID not in validated_prompt_ids:
            validated_prompt_ids.append(SYNTHESISER_CONTEXT_FRAMING_PROMPT_CONCEPT_ID)
        report["validated_prompt_ids"] = validated_prompt_ids

    report["errors_by_target"] = errors_by_target
    report["missing_content_prompt_ids"] = missing_content_prompt_ids
    report["counts"] = {
        "created_prompts": len(report.get("created_prompt_ids") or []),
        "validated_prompts": len(report.get("validated_prompt_ids") or []),
        "missing_content_prompts": len(missing_content_prompt_ids),
        "linked_workflows": len(report.get("linked_workflow_ids") or []),
        "errors": len(errors_by_target),
    }
    report["source"] = _SOURCE_TAG
    report["managed_by"] = _MANAGED_BY
    report["seeded_prompt_ids"] = seeded_prompt_ids
    report["seeded_prompt_count"] = len(seeded_prompt_ids)
    report["success"] = not errors_by_target and not missing_content_prompt_ids
    return report


def bootstrap_canonical_conversation_turn_workflows(
    *,
    force_republish: bool = False,
) -> dict[str, Any]:
    """Publish and validate the canonical conversation-turn workflow family."""

    prompt_support = _ensure_conversation_turn_prompt_support(
        force_prompt_seed=bool(force_republish),
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
        target_workflow_ids=_TARGET_WORKFLOW_IDS,
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        "success": bool(prompt_support.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
        "workflow_ids": list(_TARGET_WORKFLOW_IDS),
        "prompt_support": prompt_support,
        "publication": publication.get("publication"),
        "typed_workflow_ids": publication.get("typed_workflow_ids") or [],
        "typed_step_ids": publication.get("typed_step_ids") or [],
        "validation_by_workflow_id": publication.get("validation_by_workflow_id") or {},
    }


__all__ = ["bootstrap_canonical_conversation_turn_workflows"]
