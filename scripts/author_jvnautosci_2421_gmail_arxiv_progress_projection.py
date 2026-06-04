"""Author JVNAUTOSCI-2421 Gmail/arXiv progress projection metadata.

This is Vontology-authoring maintenance code. The durable authority is the
singleton text relation written to each workflow or step concept; request-path
Python must remain domain-neutral.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.backend.services.text_value_service import (  # noqa: E402
    upsert_singleton_text_relation,
)
from src.backend.workflows.progress_projection import (  # noqa: E402
    WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION,
)
from src.backend.workflows.vontology_loader import (  # noqa: E402
    load_workflow_definition_from_vontology,
)


PROGRESS_PROJECTION_PREDICATE = "#V#hasWorkflowProgressProjectionJson"

ZHAN_GMAIL_ARXIV_WORKFLOW_ID = "#V#zhan_gmail_arxiv_ingestion_workflow"
PARENT_PROCESS_MESSAGES_STEP_ID = (
    "#V#workflow_step_zhan_gmail_arxiv_ingestion_workflow_process_messages"
)
MESSAGE_NORMALISE_STEP_ID = (
    "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_normalise_message"
)
MESSAGE_INGEST_ARXIV_STEP_ID = (
    "#V#workflow_step_email_arxiv_ingestion_from_message_workflow_ingest_arxiv_resources"
)
RESOURCE_NORMALISE_STEP_ID = (
    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_workflow_"
    "normalise_reference"
)
RESOURCE_REPRESENT_PAPER_STEP_ID = (
    "#V#workflow_step_arxiv_resource_ingestion_from_email_reference_workflow_"
    "represent_arxiv_paper"
)
PAPER_FETCH_METADATA_STEP_ID = (
    "#V#workflow_step_arxiv_paper_representation_workflow_fetch_arxiv_metadata"
)
PAPER_DELEGATE_GENERAL_STEP_ID = (
    "#V#workflow_step_arxiv_paper_representation_workflow_"
    "delegate_to_general_paper_workflow"
)


def _projection(facts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": WORKFLOW_PROGRESS_PROJECTION_SCHEMA_VERSION,
        "facts": facts,
    }


PROJECTIONS: dict[str, dict[str, Any]] = {
    ZHAN_GMAIL_ARXIV_WORKFLOW_ID: _projection(
        [
            {
                "fact_id": "gmail_arxiv_message_count",
                "label": "Messages scanned",
                "source_path": "context.message_item_count",
                "value_kind": "number",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_messages_scanned_progress_fact",
            },
            {
                "fact_id": "gmail_arxiv_success_count",
                "label": "Messages completed",
                "source_path": "context.message_success_count",
                "value_kind": "number",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_messages_completed_progress_fact",
            },
            {
                "fact_id": "gmail_arxiv_error_count",
                "label": "Messages failed",
                "source_path": "context.message_error_count",
                "value_kind": "number",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_messages_failed_progress_fact",
            },
        ]
    ),
    PARENT_PROCESS_MESSAGES_STEP_ID: _projection(
        [
            {
                "fact_id": "gmail_arxiv_batch_result",
                "label": "Result",
                "source_path": "context.last_action_outcome",
                "value_kind": "status",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_batch_result_progress_fact",
            },
            {
                "fact_id": "gmail_arxiv_batch_success_count",
                "label": "Messages completed",
                "source_path": "context.message_success_count",
                "value_kind": "number",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_batch_success_count_progress_fact",
            },
            {
                "fact_id": "gmail_arxiv_batch_error_count",
                "label": "Messages failed",
                "source_path": "context.message_error_count",
                "value_kind": "number",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_batch_error_count_progress_fact",
            },
        ]
    ),
    MESSAGE_NORMALISE_STEP_ID: _projection(
        [
            {
                "fact_id": "gmail_message_subject",
                "label": "Email message",
                "source_path": "context.current_message.subject",
                "value_kind": "title",
                "visibility": "default",
                "sensitive": True,
                "redaction_policy": "private_redacted",
                "contract_id": "#V#gmail_arxiv_email_message_subject_progress_fact",
            },
            {
                "fact_id": "gmail_message_id",
                "label": "Email message id",
                "source_path": "context.message_id",
                "value_kind": "identifier",
                "visibility": "debug",
                "contract_id": "#V#gmail_arxiv_email_message_id_progress_fact",
            },
        ]
    ),
    MESSAGE_INGEST_ARXIV_STEP_ID: _projection(
        [
            {
                "fact_id": "message_arxiv_resource_count",
                "label": "arXiv links",
                "source_path": "context.arxiv_item_count",
                "value_kind": "number",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_resource_count_progress_fact",
            },
            {
                "fact_id": "message_arxiv_result",
                "label": "Result",
                "source_path": "context.last_action_outcome",
                "value_kind": "status",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_message_result_progress_fact",
            },
        ]
    ),
    RESOURCE_NORMALISE_STEP_ID: _projection(
        [
            {
                "fact_id": "arxiv_paper_id",
                "label": "arXiv paper",
                "source_path": "context.arxiv_id",
                "value_kind": "identifier",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_paper_id_progress_fact",
            }
        ]
    ),
    RESOURCE_REPRESENT_PAPER_STEP_ID: _projection(
        [
            {
                "fact_id": "arxiv_paper_concept",
                "label": "Paper concept",
                "source_path": "context.paper_concept_id",
                "value_kind": "concept_id",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_paper_concept_progress_fact",
            },
            {
                "fact_id": "arxiv_resource_result",
                "label": "Result",
                "source_path": "context.last_action_outcome",
                "value_kind": "status",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_resource_result_progress_fact",
            },
        ]
    ),
    PAPER_FETCH_METADATA_STEP_ID: _projection(
        [
            {
                "fact_id": "arxiv_paper_title",
                "label": "arXiv paper",
                "source_path": "context.title",
                "value_kind": "title",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_paper_title_progress_fact",
            },
            {
                "fact_id": "arxiv_paper_id",
                "label": "arXiv id",
                "source_path": "context.arxiv_id",
                "value_kind": "identifier",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_metadata_arxiv_id_progress_fact",
            },
        ]
    ),
    PAPER_DELEGATE_GENERAL_STEP_ID: _projection(
        [
            {
                "fact_id": "paper_concept",
                "label": "Paper concept",
                "source_path": "context.paper_concept_id",
                "value_kind": "concept_id",
                "visibility": "expert",
                "contract_id": "#V#gmail_arxiv_general_paper_concept_progress_fact",
            },
            {
                "fact_id": "paper_representation_result",
                "label": "Result",
                "source_path": "context.last_action_outcome",
                "value_kind": "status",
                "visibility": "default",
                "contract_id": "#V#gmail_arxiv_paper_representation_result_progress_fact",
            },
        ]
    ),
}


def _read_back_loaded_metadata() -> dict[str, Any]:
    workflow_ids = (
        ZHAN_GMAIL_ARXIV_WORKFLOW_ID,
        "#V#email_arxiv_ingestion_from_message_workflow",
        "#V#arxiv_resource_ingestion_from_email_reference_workflow",
        "#V#arxiv_paper_representation_workflow",
    )
    result: dict[str, Any] = {}
    for workflow_id in workflow_ids:
        definition = load_workflow_definition_from_vontology(workflow_id)
        if definition is None:
            result[workflow_id] = {"loaded": False}
            continue
        workflow_metadata = getattr(definition, "metadata", {}) or {}
        states = getattr(definition, "states", {}) or {}
        state_projection_ids = {}
        for state_id, state in states.items():
            metadata = getattr(state, "metadata", {}) or {}
            projection = metadata.get("progress_projection")
            if isinstance(projection, Mapping):
                state_projection_ids[state_id] = [
                    fact.get("fact_id")
                    for fact in projection.get("facts", [])
                    if isinstance(fact, Mapping)
                ]
        result[workflow_id] = {
            "loaded": True,
            "workflow_projection_fact_ids": [
                fact.get("fact_id")
                for fact in (
                    workflow_metadata.get("progress_projection", {}).get("facts", [])
                    if isinstance(
                        workflow_metadata.get("progress_projection"), Mapping
                    )
                    else []
                )
                if isinstance(fact, Mapping)
            ],
            "state_projection_fact_ids": state_projection_ids,
        }
    return result


def author_progress_projection_metadata(*, apply: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "predicate": PROGRESS_PROJECTION_PREDICATE,
        "targets": list(PROJECTIONS.keys()),
        "applied": bool(apply),
    }
    if not apply:
        result["payloads"] = PROJECTIONS
        return result

    upserts: dict[str, Any] = {}
    for concept_id, payload in PROJECTIONS.items():
        upserts[concept_id] = upsert_singleton_text_relation(
            subject_concept_id=concept_id,
            predicate=PROGRESS_PROJECTION_PREDICATE,
            text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
            lang="en-NZ",
            context={
                "source": "jvnautosci_2421_gmail_arxiv_progress_projection",
                "authority_role": "workflow_progress_projection_metadata",
            },
            garbage_collect=True,
        )
    result["upserts"] = upserts
    result["read_back"] = _read_back_loaded_metadata()
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write singleton Vontology text relations. Omit for dry run.",
    )
    args = parser.parse_args()
    result = author_progress_projection_metadata(apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
