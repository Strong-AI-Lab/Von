"""Materialise represented operational-certification evaluator authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from . import concept_service
from .concept_service import ConceptNotFoundError
from .benchmark_suite_vontology_service import (
    OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
    ensure_canonical_benchmark_suites_from_seed_fixtures,
)
from .text_value_service import upsert_singleton_text_relation
from .workflow_prompt_authority_service import (
    DEFAULT_PROMPT_TYPE_ID,
    WorkflowPromptConceptSpec,
    WorkflowPromptLinkSpec,
    ensure_prompt_concept_support,
    prompt_concept_has_content,
)
from .workflow_repo_seed_bootstrap import bootstrap_repo_seed_workflow_bundle
from .workflow_vontology_materialisation_helpers import (
    suspend_event_workflow_integration,
)
from .operational_certification_contract_service import stable_payload_digest


OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID = (
    "#V#operational_state_evidence_evaluator"
)
OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID = (
    "#V#operational_marker_absence_probe_workflow"
)
OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID = (
    "#V#prompt_operational_certification_state_evaluator"
)
OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_LINK_PREDICATE = (
    "#V#has_operational_certification_evaluator_prompt"
)
OPERATIONAL_CERTIFICATION_CAMPAIGN_EVIDENCE_CONCEPT_ID = (
    "#V#first_sail_operational_campaign_evidence"
)
REPRESENTED_OPERATIONAL_CAMPAIGN_EVIDENCE_SCHEMA_VERSION = (
    "represented_operational_campaign_evidence.v1"
)

_MANAGED_BY = "operational_certification_vontology_service"
_SOURCE_TAG = "JVNAUTOSCI-2577"
_SEED_DIR = Path(__file__).resolve().parents[1] / "workflows" / "repo_seed_bundles"
_WORKFLOW_BUNDLE_PATH = (
    _SEED_DIR / "operational_certification_evaluator_workflow_seed_bundle.json"
)
_PROMPT_SEED_PATH = _SEED_DIR / "operational_certification_evaluator_prompt_seed.md"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _exact_candidate_bindings(evidence: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_bindings = evidence.get("evaluated_learning_release_candidate_bindings")
    legacy_ids = evidence.get("evaluated_learning_release_candidate_ids")
    if raw_bindings is None:
        if legacy_ids:
            raise ValueError("operational_campaign_candidate_bindings_required")
        return []
    if not isinstance(raw_bindings, list):
        raise ValueError("operational_campaign_candidate_bindings_invalid")
    bindings: dict[tuple[str, str], dict[str, str]] = {}
    for raw_binding in raw_bindings:
        if not isinstance(raw_binding, Mapping):
            raise ValueError("operational_campaign_candidate_bindings_invalid")
        candidate_id = _clean(raw_binding.get("candidate_id"))
        release_sha256 = _clean(raw_binding.get("candidate_release_sha256")).lower()
        if not candidate_id or not _is_sha256(release_sha256):
            raise ValueError("operational_campaign_candidate_bindings_invalid")
        bindings[(candidate_id, release_sha256)] = {
            "candidate_id": candidate_id,
            "candidate_release_sha256": release_sha256,
        }
    return [bindings[key] for key in sorted(bindings)]


def _load_learning_release_campaign_projection(
    *,
    namespace: str,
    user_id: str,
    org_id: str,
) -> dict[str, Any]:
    from .operational_learning_release_vontology_service import (
        load_operational_learning_release_state,
    )

    state_record = load_operational_learning_release_state(
        namespace=namespace,
        user_id=user_id,
        org_id=org_id,
    )
    projection = state_record.get("campaign_evidence_projection")
    if not isinstance(projection, Mapping):
        raise ValueError("operational_learning_release_campaign_projection_missing")
    return dict(projection)


def load_represented_operational_campaign_evidence(
    *,
    concept_id: str = OPERATIONAL_CERTIFICATION_CAMPAIGN_EVIDENCE_CONCEPT_ID,
    expected_namespace: str | None = None,
    expected_user_id: str | None = None,
    expected_org_id: str | None = None,
) -> dict[str, Any] | None:
    """Load user/pilot and learning-loop evidence from live Vontology authority.

    The concept is deliberately not auto-created: pilot agreement, envelopes,
    release receipts, and the safe operating envelope are authored facts.  An
    absent concept therefore remains an honest missing gate rather than a repo
    default silently becoming live authority.
    """

    resolved_concept_id = _clean(concept_id)
    if not resolved_concept_id.startswith("#V#"):
        raise ValueError("operational_campaign_evidence_concept_id_invalid")
    try:
        concept = concept_service.get_concept_by_concept_id(resolved_concept_id)
    except ConceptNotFoundError:
        return None
    if not isinstance(concept, Mapping):
        return None
    attributes = concept.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    raw = attributes.get("operational_certification_campaign_evidence")
    if not isinstance(raw, Mapping):
        return None
    evidence = dict(raw)
    scope_expectations = {
        "effective_namespace": _clean(expected_namespace),
        "effective_user_id": _clean(expected_user_id),
        "effective_org_id": _clean(expected_org_id),
    }
    mismatches: list[str] = []
    for field_name, expected in scope_expectations.items():
        if expected and _clean(evidence.get(field_name)) != expected:
            mismatches.append(field_name)
    if mismatches:
        raise ValueError(
            "operational_campaign_evidence_scope_mismatch:" + ",".join(mismatches)
        )

    manually_authored_learning_fields = [
        field_name
        for field_name in (
            "completed_learning_loop_count",
            "learning_release_receipt_ids",
            "evaluated_learning_release_candidate_ids",
            "evaluated_learning_release_candidate_bindings",
        )
        if evidence.get(field_name) not in (None, [], 0)
    ]
    if manually_authored_learning_fields:
        raise ValueError(
            "operational_campaign_learning_release_evidence_must_be_state_derived:"
            + ",".join(manually_authored_learning_fields)
        )

    release_projection = _load_learning_release_campaign_projection(
        namespace=scope_expectations["effective_namespace"],
        user_id=scope_expectations["effective_user_id"],
        org_id=scope_expectations["effective_org_id"],
    )
    receipt_ids = sorted(
        {
            _clean(item)
            for item in (release_projection.get("learning_release_receipt_ids") or [])
            if _clean(item)
        }
    )
    completed_learning_loop_count = release_projection.get(
        "completed_learning_loop_count"
    )
    if (
        not isinstance(completed_learning_loop_count, int)
        or isinstance(completed_learning_loop_count, bool)
        or completed_learning_loop_count < 0
    ):
        completed_learning_loop_count = 0
    completed_learning_loop_count = min(
        completed_learning_loop_count,
        len(receipt_ids),
    )
    evaluated_candidate_bindings = _exact_candidate_bindings(release_projection)
    evaluated_candidate_ids = sorted(
        {binding["candidate_id"] for binding in evaluated_candidate_bindings}
    )
    safe_envelope = evidence.get("safe_operating_envelope")
    safe_envelope = dict(safe_envelope) if isinstance(safe_envelope, Mapping) else None
    authority_revision = stable_payload_digest(
        {"concept_id": resolved_concept_id, "evidence": evidence}
    )
    projection: dict[str, Any] = {
        "schema_version": REPRESENTED_OPERATIONAL_CAMPAIGN_EVIDENCE_SCHEMA_VERSION,
        "source": "vontology",
        "authority": {
            "concept_id": resolved_concept_id,
            "revision_sha256": authority_revision,
        },
        "learning_release_authority": release_projection.get("authority"),
        "effective_namespace": scope_expectations["effective_namespace"] or None,
        "effective_user_id": scope_expectations["effective_user_id"] or None,
        "effective_org_id": scope_expectations["effective_org_id"] or None,
        "pilot_corpus_agreed": evidence.get("pilot_corpus_agreed") is True,
        "pilot_envelopes_agreed": evidence.get("pilot_envelopes_agreed") is True,
        "completed_learning_loop_count": completed_learning_loop_count,
        "learning_release_receipt_ids": receipt_ids,
        "evaluated_learning_release_candidate_bindings": (evaluated_candidate_bindings),
        "evaluated_learning_release_candidate_ids": evaluated_candidate_ids,
    }
    if safe_envelope:
        projection["safe_operating_envelope"] = safe_envelope
    projection["evidence_sha256"] = stable_payload_digest(projection)
    return projection


def _ensure_evaluator_prompt(*, force_prompt_seed: bool = False) -> dict[str, Any]:
    report = ensure_prompt_concept_support(
        prompt_specs=(
            WorkflowPromptConceptSpec(
                concept_id=OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID,
                name="Operational certification state evaluator prompt",
                description=(
                    "Represented strict evaluator policy for operational trial "
                    "world-state, path, receipt, safety and recovery evidence."
                ),
                parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
            ),
        ),
        workflow_links=(
            WorkflowPromptLinkSpec(
                workflow_id=OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID,
                prompt_concept_id=OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID,
                predicate=(OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_LINK_PREDICATE),
                context={"jira": _SOURCE_TAG},
                reason="operational_certification_evaluator_prompt_bootstrap",
            ),
        ),
        provenance_source=_MANAGED_BY,
    )
    seeded = False
    if force_prompt_seed or not prompt_concept_has_content(
        OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID
    ):
        prompt_text = _PROMPT_SEED_PATH.read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise ValueError("operational_certification_evaluator_prompt_seed_missing")
        upsert_singleton_text_relation(
            subject_concept_id=OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID,
            predicate="hasContent",
            text=prompt_text,
            lang="en-NZ",
            context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
            garbage_collect=True,
        )
        seeded = True
        # The first support pass may correctly report missing content before a
        # new prompt is seeded.  Read the live authority again so bootstrap
        # success reflects the post-write state rather than that transient
        # precondition.
        report = ensure_prompt_concept_support(
            prompt_specs=(
                WorkflowPromptConceptSpec(
                    concept_id=OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID,
                    name="Operational certification state evaluator prompt",
                    description=(
                        "Represented strict evaluator policy for operational trial "
                        "world-state, path, receipt, safety and recovery evidence."
                    ),
                    parent_concept_ids=(DEFAULT_PROMPT_TYPE_ID,),
                ),
            ),
            workflow_links=(
                WorkflowPromptLinkSpec(
                    workflow_id=OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID,
                    prompt_concept_id=OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID,
                    predicate=(
                        OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_LINK_PREDICATE
                    ),
                    context={"jira": _SOURCE_TAG},
                    reason="operational_certification_evaluator_prompt_bootstrap",
                ),
            ),
            provenance_source=_MANAGED_BY,
        )
    projection = dict(report)
    projection["seeded"] = seeded
    projection["content_ready"] = prompt_concept_has_content(
        OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID
    )
    projection["success"] = bool(
        projection["content_ready"] and not projection.get("errors_by_target")
    )
    return projection


def bootstrap_operational_certification_authority(
    *,
    force_republish: bool = False,
    overwrite_suite_definition: bool = False,
) -> dict[str, Any]:
    """Publish evaluator prompt/workflow and import the suite if required."""

    prompt_support = _ensure_evaluator_prompt(
        force_prompt_seed=force_republish,
    )
    workflow_publication = bootstrap_repo_seed_workflow_bundle(
        asset_path=_WORKFLOW_BUNDLE_PATH,
        publish_context_manager_factory=suspend_event_workflow_integration,
        force_republish=force_republish,
        target_workflow_ids=(
            OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID,
            OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID,
        ),
    )
    suite_support = ensure_canonical_benchmark_suites_from_seed_fixtures(
        suite_concept_ids=[
            OPERATIONAL_CERTIFICATION_BENCHMARK_SUITE_CONCEPT_ID,
        ],
        overwrite_existing=overwrite_suite_definition,
        provenance={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
        context={"jira": _SOURCE_TAG, "source": _MANAGED_BY},
    )
    errors: list[str] = []
    if prompt_support.get("success") is not True:
        errors.append("evaluator_prompt_support_failed")
    publication_error_count = int(
        ((workflow_publication.get("publication") or {}).get("counts") or {}).get(
            "errors"
        )
        or 0
    )
    if publication_error_count:
        errors.append("evaluator_workflow_publication_failed")
    if suite_support.get("success") is not True:
        errors.append("operational_suite_support_failed")
    return {
        "success": not errors,
        "schema_version": "operational_certification_authority_bootstrap.v1",
        "managed_by": _MANAGED_BY,
        "source_tag": _SOURCE_TAG,
        "evaluator_workflow_id": OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID,
        "marker_absence_probe_workflow_id": (
            OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID
        ),
        "evaluator_prompt_id": OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID,
        "prompt_support": prompt_support,
        "workflow_publication": workflow_publication,
        "suite_support": suite_support,
        "errors": errors,
    }


__all__ = [
    "OPERATIONAL_CERTIFICATION_CAMPAIGN_EVIDENCE_CONCEPT_ID",
    "OPERATIONAL_CERTIFICATION_EVALUATOR_PROMPT_ID",
    "OPERATIONAL_CERTIFICATION_EVALUATOR_WORKFLOW_ID",
    "OPERATIONAL_MARKER_ABSENCE_PROBE_WORKFLOW_ID",
    "REPRESENTED_OPERATIONAL_CAMPAIGN_EVIDENCE_SCHEMA_VERSION",
    "bootstrap_operational_certification_authority",
    "load_represented_operational_campaign_evidence",
]
