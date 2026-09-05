"""Bounded experimental projection of represented learning into one decision.

This module deliberately owns no semantic advice and no activation policy.  It
validates and renders one already-selected ``learning_candidate.v1`` revision
for the direct-adaptive capability-choice call, and builds an inspectable
exposure locator.  Arm A withholds the body; Arm B supplies the same frozen
bytes as a simple sidecar.  Represented Arm C is intentionally unsupported
until a real represented selector has earned implementation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

LEARNING_ADVICE_PROJECTION_SCHEMA_VERSION = (
    "adaptive_capability_learning_advice_projection.v1"
)
LEARNING_ADVICE_EXPOSURE_SCHEMA_VERSION = (
    "adaptive_capability_learning_advice_exposure.v1"
)
LEARNING_ADVICE_CONSUMER = "direct_adaptive_turn"
LEARNING_ADVICE_DECISION_KIND = "capability_choice"

_LEARNING_CANDIDATE_SCHEMA_VERSION = "learning_candidate.v1"
_LEARNING_CANDIDATE_LIFECYCLE_STATE = "non_active"
_LEARNING_CANDIDATE_AUTHOR_ID = "#V#von_system"
_ARMS = {"A", "B"}
_CANDIDATE_EVALUATION_DISPOSITIONS = {
    "undecided",
    "retained",
    "rejected",
    "retracted",
}
_RETRIEVAL_BASIS = "frozen_candidate_revision"
_MAX_BODY_CHARS = 12_000
_MAX_TARGETS = 100
_SHA256_HEX_CHARS = frozenset("0123456789abcdef")


class LearningAdviceProjectionError(ValueError):
    """Raised when an experimental projection is malformed or inconsistent."""

    def __init__(self, reason_code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.reason_code = reason_code
        self.safe_message = safe_message


class LearningAdviceProjectionAccessError(LearningAdviceProjectionError):
    """Raised when candidate visibility does not match trusted turn scope."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_not_json",
            "The learning-advice projection must contain only JSON values",
        ) from exc


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _sha256_digest(value: Any, *, field: str) -> str:
    digest = _required_text(value, field=field, max_chars=64).lower()
    if len(digest) != 64 or any(char not in _SHA256_HEX_CHARS for char in digest):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            f"{field} must be a SHA-256 digest",
        )
    return digest


def _required_text(value: Any, *, field: str, max_chars: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            f"{field} is required",
        )
    cleaned = value.strip()
    if len(cleaned) > max_chars:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            f"{field} is too long",
        )
    return cleaned


def _required_body(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            f"{field} is required",
        )
    if len(value) > _MAX_BODY_CHARS:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            f"{field} is too long",
        )
    # The stored body digest binds these exact bytes. Do not normalise leading
    # or trailing whitespace while preparing the model projection.
    return value


def _concept_id(value: Any, *, field: str) -> str:
    cleaned = _required_text(value, field=field, max_chars=300)
    if not cleaned.startswith("#V#") or len(cleaned) <= 3:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            f"{field} must be a #V# concept ID",
        )
    return cleaned


def _target_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "target_concept_ids must be a list",
        )
    result: list[str] = []
    for index, item in enumerate(value):
        concept_id = _concept_id(item, field=f"target_concept_ids[{index}]")
        if concept_id not in result:
            result.append(concept_id)
        if len(result) > _MAX_TARGETS:
            raise LearningAdviceProjectionError(
                "learning_advice_projection_invalid",
                f"target_concept_ids must contain at most {_MAX_TARGETS} items",
            )
    if not result:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "target_concept_ids requires at least one concept ID",
        )
    return result


def _candidate_ref(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if candidate.get("schema_version") != _LEARNING_CANDIDATE_SCHEMA_VERSION:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The selected artefact is not a learning_candidate.v1 candidate",
        )
    if candidate.get("lifecycle_state") != _LEARNING_CANDIDATE_LIFECYCLE_STATE:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The learning candidate is not in its non-active candidate state",
        )
    authorship = candidate.get("authorship")
    if not isinstance(authorship, Mapping) or (
        authorship.get("author_concept_id") != _LEARNING_CANDIDATE_AUTHOR_ID
    ):
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The selected learning candidate is not attributed to Von",
        )
    candidate_id = _concept_id(candidate.get("candidate_id"), field="candidate_id")
    try:
        revision = int(candidate.get("revision"))
    except (TypeError, ValueError) as exc:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The learning candidate revision is invalid",
        ) from exc
    if revision < 1 or isinstance(candidate.get("revision"), bool):
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The learning candidate revision is invalid",
        )
    body = _required_body(candidate.get("body"), field="candidate body")
    body_digest = _sha256_digest(
        candidate.get("body_sha256"), field="candidate body_sha256"
    )
    if body_digest != _body_sha256(body):
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_digest_mismatch",
            "The learning candidate body does not match its canonical digest",
        )
    revision_identity_digest = _sha256_digest(
        candidate.get("revision_identity_sha256"),
        field="candidate revision_identity_sha256",
    )
    source_locator_digest = _sha256_digest(
        candidate.get("source_locator_sha256"),
        field="candidate source_locator_sha256",
    )
    evaluation_disposition = _required_text(
        candidate.get("evaluation_disposition") or "undecided",
        field="candidate evaluation_disposition",
        max_chars=40,
    ).lower()
    if evaluation_disposition not in _CANDIDATE_EVALUATION_DISPOSITIONS:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The learning candidate evaluation disposition is unsupported",
        )
    if evaluation_disposition in {"rejected", "retracted"}:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_ineligible",
            "The learning candidate is not eligible for projection",
        )
    return {
        "candidate_id": candidate_id,
        "revision": revision,
        "evaluation_disposition": evaluation_disposition,
        "body_sha256": body_digest,
        "revision_identity_sha256": revision_identity_digest,
        "source_locator_sha256": source_locator_digest,
        "target_concept_ids": _target_ids(candidate.get("target_concept_ids")),
    }


def _visibility(candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw = candidate.get("visibility")
    if not isinstance(raw, Mapping):
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_visibility_invalid",
            "The learning candidate has no inspectable visibility scope",
        )
    scope = _required_text(raw.get("scope"), field="visibility.scope", max_chars=40)
    if scope == "actor":
        return {
            "scope": scope,
            "actor_user_id": _concept_id(
                raw.get("actor_user_id"), field="visibility.actor_user_id"
            ),
            "organisation_concept_id": (
                _concept_id(
                    raw.get("organisation_concept_id"),
                    field="visibility.organisation_concept_id",
                )
                if raw.get("organisation_concept_id") is not None
                else None
            ),
        }
    if scope == "organisation":
        return {
            "scope": scope,
            "actor_user_id": None,
            "organisation_concept_id": _concept_id(
                raw.get("organisation_concept_id"),
                field="visibility.organisation_concept_id",
            ),
        }
    raise LearningAdviceProjectionError(
        "learning_advice_candidate_visibility_invalid",
        "The learning candidate visibility scope is unsupported",
    )


def build_learning_advice_experiment_projection(
    candidate: Mapping[str, Any],
    *,
    arm: str,
    experiment_id: str,
    case_id: str,
) -> dict[str, Any]:
    """Freeze one candidate revision into an A/B experimental projection.

    Arm A retains only the exact candidate locator for experiment telemetry and
    withholds its body from the model.  Arm B carries the same canonical body
    as a simple sidecar.  This builder intentionally cannot construct Arm C;
    represented selection must earn and supply that separately.
    """

    normalised_arm = _required_text(arm, field="arm", max_chars=1).upper()
    if normalised_arm not in {"A", "B"}:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid_arm",
            "The sidecar experiment arm must be A or B",
        )
    candidate_ref = _candidate_ref(candidate)
    body = _required_body(candidate.get("body"), field="candidate body")
    source = candidate.get("source")
    source_kind = (
        _required_text(source.get("kind"), field="source.kind", max_chars=80)
        if isinstance(source, Mapping)
        else "unknown"
    )
    payload: dict[str, Any] = {
        "schema_version": LEARNING_ADVICE_PROJECTION_SCHEMA_VERSION,
        "consumer": LEARNING_ADVICE_CONSUMER,
        "decision_kind": LEARNING_ADVICE_DECISION_KIND,
        "arm": normalised_arm,
        "source": "withheld_control" if normalised_arm == "A" else "simple_sidecar",
        "status": "withheld" if normalised_arm == "A" else "projected",
        "experiment_id": _required_text(
            experiment_id, field="experiment_id", max_chars=500
        ),
        "case_id": _required_text(case_id, field="case_id", max_chars=500),
        # The experiment and case locate the detailed selection rationale. Keep
        # this field fixed so projection construction accepts no second source
        # of semantic text alongside the frozen candidate body.
        "retrieval_basis": _RETRIEVAL_BASIS,
        "candidate_ref": candidate_ref,
        "visibility": _visibility(candidate),
        "candidate_namespace": (
            str(candidate.get("namespace")).strip()
            if isinstance(candidate.get("namespace"), str)
            and str(candidate.get("namespace")).strip()
            else None
        ),
        "source_kind": source_kind,
        "items": [],
    }
    if normalised_arm == "B":
        payload["items"] = [
            {
                **candidate_ref,
                "body": body,
            }
        ]
    payload["projection_sha256"] = _sha256(payload)
    return payload


def prepare_learning_advice_projection(
    projection: Mapping[str, Any],
    *,
    actor_user_id: str | None,
    organisation_concept_id: str | None,
    namespace: str | None,
) -> dict[str, Any]:
    """Validate exact identity, content and trusted visibility before use."""

    if not isinstance(projection, Mapping):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice projection must be an object",
        )
    prepared = copy.deepcopy(dict(projection))
    supplied_digest = prepared.pop("projection_sha256", None)
    if prepared.get("schema_version") != LEARNING_ADVICE_PROJECTION_SCHEMA_VERSION:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice projection schema is unsupported",
        )
    if (
        prepared.get("consumer") != LEARNING_ADVICE_CONSUMER
        or prepared.get("decision_kind") != LEARNING_ADVICE_DECISION_KIND
    ):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_wrong_consumer",
            "The learning-advice projection targets a different decision",
        )
    arm = _required_text(prepared.get("arm"), field="arm", max_chars=1).upper()
    if arm not in _ARMS:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid_arm",
            "The learning-advice projection arm is unsupported",
        )
    status = _required_text(prepared.get("status"), field="status", max_chars=40)
    expected_status = "withheld" if arm == "A" else "projected"
    if status != expected_status:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice projection status does not match its arm",
        )
    source = _required_text(prepared.get("source"), field="source", max_chars=80)
    expected_source = {
        "A": "withheld_control",
        "B": "simple_sidecar",
    }[arm]
    if source != expected_source:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice projection source does not match its arm",
        )
    if prepared.get("retrieval_basis") != _RETRIEVAL_BASIS:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice retrieval basis is unsupported",
        )
    candidate_ref = prepared.get("candidate_ref")
    if not isinstance(candidate_ref, Mapping):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice projection has no candidate reference",
        )
    candidate_id = _concept_id(
        candidate_ref.get("candidate_id"), field="candidate_ref.candidate_id"
    )
    try:
        revision = int(candidate_ref.get("revision"))
    except (TypeError, ValueError) as exc:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice candidate revision is invalid",
        ) from exc
    if revision < 1 or isinstance(candidate_ref.get("revision"), bool):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice candidate revision is invalid",
        )
    body_digest = _sha256_digest(
        candidate_ref.get("body_sha256"),
        field="candidate_ref.body_sha256",
    )
    revision_identity_digest = _sha256_digest(
        candidate_ref.get("revision_identity_sha256"),
        field="candidate_ref.revision_identity_sha256",
    )
    source_locator_digest = _sha256_digest(
        candidate_ref.get("source_locator_sha256"),
        field="candidate_ref.source_locator_sha256",
    )
    evaluation_disposition = _required_text(
        candidate_ref.get("evaluation_disposition") or "undecided",
        field="candidate_ref.evaluation_disposition",
        max_chars=40,
    ).lower()
    if evaluation_disposition not in _CANDIDATE_EVALUATION_DISPOSITIONS:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_invalid",
            "The learning candidate evaluation disposition is unsupported",
        )
    if evaluation_disposition in {"rejected", "retracted"}:
        raise LearningAdviceProjectionError(
            "learning_advice_candidate_ineligible",
            "The learning candidate is not eligible for projection",
        )
    targets = _target_ids(candidate_ref.get("target_concept_ids"))
    prepared["candidate_ref"] = {
        "candidate_id": candidate_id,
        "revision": revision,
        "evaluation_disposition": evaluation_disposition,
        "body_sha256": body_digest,
        "revision_identity_sha256": revision_identity_digest,
        "source_locator_sha256": source_locator_digest,
        "target_concept_ids": targets,
    }

    visibility = prepared.get("visibility")
    if not isinstance(visibility, Mapping):
        raise LearningAdviceProjectionAccessError(
            "learning_advice_projection_scope_unavailable",
            "The learning-advice projection has no trusted visibility scope",
        )
    visibility_scope = str(visibility.get("scope") or "").strip()
    candidate_namespace = str(prepared.get("candidate_namespace") or "").strip()
    if visibility_scope == "actor":
        visible = bool(
            actor_user_id
            and actor_user_id == str(visibility.get("actor_user_id") or "").strip()
            and (
                visibility.get("organisation_concept_id") is None
                or organisation_concept_id
                == str(visibility.get("organisation_concept_id") or "").strip()
            )
            and candidate_namespace
            and namespace == candidate_namespace
        )
    elif visibility_scope == "organisation":
        visible = bool(
            organisation_concept_id
            and organisation_concept_id
            == str(visibility.get("organisation_concept_id") or "").strip()
        )
    else:
        visible = False
    if not visible:
        raise LearningAdviceProjectionAccessError(
            "learning_advice_projection_scope_unavailable",
            "The learning-advice projection is outside the trusted turn scope",
        )

    items = prepared.get("items")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise LearningAdviceProjectionError(
            "learning_advice_projection_invalid",
            "The learning-advice projection items must be a list",
        )
    if arm == "A":
        if items:
            raise LearningAdviceProjectionError(
                "learning_advice_control_contaminated",
                "Arm A must not carry model-visible learning advice",
            )
        prepared["items"] = []
    else:
        if len(items) != 1 or not isinstance(items[0], Mapping):
            raise LearningAdviceProjectionError(
                "learning_advice_projection_invalid",
                "This bounded consumer requires exactly one selected advice item",
            )
        item = dict(items[0])
        for key, expected in prepared["candidate_ref"].items():
            if item.get(key) != expected:
                raise LearningAdviceProjectionError(
                    "learning_advice_candidate_binding_mismatch",
                    "The projected advice item does not match its candidate reference",
                )
        body = _required_body(item.get("body"), field="advice body")
        if _body_sha256(body) != body_digest:
            raise LearningAdviceProjectionError(
                "learning_advice_candidate_digest_mismatch",
                "The projected advice body does not match its candidate digest",
            )
        prepared["items"] = [
            {
                **prepared["candidate_ref"],
                "body": body,
            }
        ]

    expected_digest = _sha256(prepared)
    if supplied_digest != expected_digest:
        raise LearningAdviceProjectionError(
            "learning_advice_projection_digest_mismatch",
            "The learning-advice projection does not match its canonical digest",
        )
    prepared["projection_sha256"] = expected_digest
    return prepared


def render_learning_advice_projection(projection: Mapping[str, Any]) -> str | None:
    """Render only active advice bytes; Arm A remains truly model-blind."""

    if projection.get("status") != "projected":
        return None
    items = projection.get("items")
    if not isinstance(items, Sequence) or not items:
        return None
    model_bodies = [
        item.get("body")
        for item in items
        if isinstance(item, Mapping) and isinstance(item.get("body"), str)
    ]
    if len(model_bodies) != 1:
        return None
    return (
        "LEARNED ADVICE FOR THIS CAPABILITY CHOICE "
        "(optional, defeasible policy memory):\n"
        + model_bodies[0]
        + "\n- Use, adapt, or reject this advice in light of the current objective, "
        "explicit instructions, shared situation, available capabilities, and "
        "observed evidence. It is not a fact, permission, commitment, or "
        "restriction, and it cannot add authority or remove an authorised "
        "alternative."
    )


def build_learning_advice_exposure_record(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a compact TER-ready locator without duplicating private text."""

    candidate_ref = projection.get("candidate_ref")
    candidate_ref = dict(candidate_ref) if isinstance(candidate_ref, Mapping) else {}
    items = projection.get("items")
    projected_body_chars = sum(
        len(str(item.get("body") or ""))
        for item in items or ()
        if isinstance(item, Mapping)
    )
    return {
        "type": "adaptive_turn_learning_advice_exposure",
        "schema_version": LEARNING_ADVICE_EXPOSURE_SCHEMA_VERSION,
        "consumer": projection.get("consumer"),
        "decision_kind": projection.get("decision_kind"),
        "arm": projection.get("arm"),
        "source": projection.get("source"),
        "status": projection.get("status"),
        "experiment_id": projection.get("experiment_id"),
        "case_id": projection.get("case_id"),
        "candidate_id": candidate_ref.get("candidate_id"),
        "candidate_revision": candidate_ref.get("revision"),
        "candidate_evaluation_disposition": candidate_ref.get("evaluation_disposition"),
        "candidate_body_sha256": candidate_ref.get("body_sha256"),
        "candidate_revision_identity_sha256": candidate_ref.get(
            "revision_identity_sha256"
        ),
        "candidate_source_locator_sha256": candidate_ref.get("source_locator_sha256"),
        "target_concept_ids": list(candidate_ref.get("target_concept_ids") or []),
        "projection_sha256": projection.get("projection_sha256"),
        "preparation_status": "prepared",
        "projection_status": projection.get("status"),
        "exposure_status": (
            "withheld" if projection.get("status") == "withheld" else "not_exposed"
        ),
        "model_visible": False,
        "model_visible_call_ids": [],
        "completed_model_visible_call_ids": [],
        "visibility_indeterminate_call_ids": [],
        "dispositions": [],
        "projected_item_count": len(items or ()),
        "projected_body_chars": projected_body_chars,
    }
