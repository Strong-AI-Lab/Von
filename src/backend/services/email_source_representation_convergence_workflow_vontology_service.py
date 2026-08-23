"""Materialise email-source representation convergence workflows.

The repo bundle is a reviewable bootstrap fixture for Vontology authority.  At
runtime, workflow policy is loaded from Vontology; this module only ensures the
represented workflow and tool-follow-up artefacts exist and can be repaired from
the canonical seed when startup detects drift.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..workflows.workflow_repo_seed_export_service import (
    diff_repo_seed_workflow_bundle_from_authority,
    write_repo_seed_workflow_bundle_from_authority,
)
from .paper_representation_workflow_vontology_service import (
    bootstrap_canonical_paper_representation_workflows,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle

ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID = "#V#zhan_gmail_arxiv_ingestion_workflow"
EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID = (
    "#V#email_arxiv_ingestion_from_message_workflow"
)
EMAIL_RESOURCE_LINK_EXTRACTION_WORKFLOW_ID = "#V#email_resource_link_extraction"
ARXIV_RESOURCE_INGESTION_FROM_EMAIL_REFERENCE_WORKFLOW_ID = (
    "#V#arxiv_resource_ingestion_from_email_reference_workflow"
)
GMAIL_GET_MESSAGE_TOOL_ID = "#V#gmail_get_message_tool"
GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE = "#V#output_followup_hint"
GMAIL_PAPER_SIGNAL_EXTRACTION_HINT_PREDICATE = (
    "#V#output_item_signal_extraction_hint"
)

_REPO_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "email_source_representation_convergence_workflow_seed_bundle.json"
)
_GMAIL_COMPLETION_HINT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "gmail_get_message_terminal_completion_hint_seed.json"
)
_GMAIL_PAPER_SIGNAL_EXTRACTION_HINT_SEED_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "workflows"
    / "repo_seed_bundles"
    / "gmail_get_message_paper_signal_extraction_hint_seed.json"
)
_MANAGED_BY = "email_source_representation_convergence_workflow_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2244"


def _load_gmail_completion_hint_seed() -> dict[str, Any]:
    payload = json.loads(
        _GMAIL_COMPLETION_HINT_SEED_ASSET_PATH.read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "tool_output_followup_hint.v1"
    ):
        raise ValueError("gmail_completion_hint_seed_invalid")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("gmail_completion_hint_seed_entries_missing")
    return payload


def ensure_email_source_gmail_completion_hint() -> dict[str, Any]:
    """Materialise the represented Gmail terminal-completion hint."""

    payload = _load_gmail_completion_hint_seed()
    write_report = upsert_singleton_text_relation(
        subject_concept_id=GMAIL_GET_MESSAGE_TOOL_ID,
        predicate=GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE,
        text=json.dumps(payload, ensure_ascii=True, sort_keys=True),
        lang="en-NZ",
        context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
        garbage_collect=True,
    )
    return {
        "success": True,
        "tool_concept_id": GMAIL_GET_MESSAGE_TOOL_ID,
        "predicate": GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE,
        "entry_count": len(payload.get("entries") or []),
        "write_report": write_report,
    }


def ensure_email_source_gmail_paper_signal_extraction_hint() -> dict[str, Any]:
    """Materialise generous paper-identification policy as Vontology text."""

    payload = json.loads(
        _GMAIL_PAPER_SIGNAL_EXTRACTION_HINT_SEED_ASSET_PATH.read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(payload, dict) or payload.get("schema_version") != (
        "tool_output_item_signal_extraction_hint.v1"
    ):
        raise ValueError("gmail_paper_signal_extraction_hint_seed_invalid")
    hint = str(payload.get("hint") or "").strip()
    if not hint:
        raise ValueError("gmail_paper_signal_extraction_hint_seed_missing")
    write_report = upsert_singleton_text_relation(
        subject_concept_id=GMAIL_GET_MESSAGE_TOOL_ID,
        predicate=GMAIL_PAPER_SIGNAL_EXTRACTION_HINT_PREDICATE,
        text=hint,
        lang="en-NZ",
        context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
        garbage_collect=True,
    )
    return {
        "success": True,
        "tool_concept_id": GMAIL_GET_MESSAGE_TOOL_ID,
        "predicate": GMAIL_PAPER_SIGNAL_EXTRACTION_HINT_PREDICATE,
        "write_report": write_report,
    }


def bootstrap_canonical_email_source_representation_convergence_workflows(
    *,
    force_republish: bool = False,
    paper_workflow_dependency_report: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish or repair the canonical email-source convergence workflow family."""

    paper_workflow_dependency = (
        dict(paper_workflow_dependency_report)
        if paper_workflow_dependency_report is not None
        else bootstrap_canonical_paper_representation_workflows()
    )
    completion_hint = ensure_email_source_gmail_completion_hint()
    paper_signal_extraction_hint = (
        ensure_email_source_gmail_paper_signal_extraction_hint()
    )
    publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_REPO_SEED_ASSET_PATH,
        force_republish=force_republish,
    )
    publication_counts = dict(
        (publication.get("publication") or {}).get("counts") or {}
    )
    return {
        **dict(publication),
        "paper_workflow_dependency": paper_workflow_dependency,
        "gmail_completion_hint": completion_hint,
        "gmail_paper_signal_extraction_hint": paper_signal_extraction_hint,
        "success": bool(paper_workflow_dependency.get("success"))
        and bool(completion_hint.get("success"))
        and bool(paper_signal_extraction_hint.get("success"))
        and int(publication_counts.get("errors") or 0) == 0,
    }


def export_email_source_representation_convergence_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Refresh the email-source convergence seed bundle from Vontology authority."""

    return write_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


def diff_email_source_representation_convergence_workflow_repo_seed_bundle(
    *,
    asset_path: str | Path | None = None,
) -> dict[str, Any]:
    """Diff the seed bundle against authoritative Vontology workflow state."""

    return diff_repo_seed_workflow_bundle_from_authority(
        asset_path=asset_path or _REPO_SEED_ASSET_PATH
    )


__all__ = [
    "ARXIV_RESOURCE_INGESTION_FROM_EMAIL_REFERENCE_WORKFLOW_ID",
    "EMAIL_ARXIV_INGESTION_FROM_MESSAGE_WORKFLOW_ID",
    "EMAIL_RESOURCE_LINK_EXTRACTION_WORKFLOW_ID",
    "GMAIL_GET_MESSAGE_TOOL_ID",
    "GMAIL_OUTPUT_FOLLOWUP_HINT_PREDICATE",
    "GMAIL_PAPER_SIGNAL_EXTRACTION_HINT_PREDICATE",
    "ZHAN_GMAIL_ARXIV_INGESTION_WORKFLOW_ID",
    "bootstrap_canonical_email_source_representation_convergence_workflows",
    "diff_email_source_representation_convergence_workflow_repo_seed_bundle",
    "ensure_email_source_gmail_completion_hint",
    "ensure_email_source_gmail_paper_signal_extraction_hint",
    "export_email_source_representation_convergence_workflow_repo_seed_bundle",
]
