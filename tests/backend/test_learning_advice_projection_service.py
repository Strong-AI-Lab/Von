from __future__ import annotations

import copy
import hashlib
import inspect
import json

import pytest

from src.backend.services import learning_advice_projection_service as service
from src.backend.services.learning_advice_projection_service import (
    LearningAdviceProjectionAccessError,
    LearningAdviceProjectionError,
    build_learning_advice_experiment_projection,
    build_learning_advice_exposure_record,
    prepare_learning_advice_projection,
    render_learning_advice_projection,
)

_CANDIDATE_BODY = (
    "When a request uses a channel-neutral term such as ‘messages’, keep the "
    "channel unresolved unless wording or the active task purpose discriminates. "
    "Compare available authorised communication channels rather than inheriting "
    "the most recently discussed one. If a selected channel fails, reconsider "
    "other plausible channels before reporting that the request cannot be "
    "completed; ask one focused question only when the remaining alternatives "
    "materially differ."
)
_CANDIDATE_BODY_SHA256 = (
    "fb8c5c7128e6c88b594cefadd2aa6cf4e0ad7db89e11adfa00fceca60ac2e54b"
)
_CANDIDATE_ID = "#V#learning_candidate_message_channel"
_TARGET_ID = "#V#ordinary_turn_capability_choice"
_REVISION_IDENTITY_SHA256 = "a" * 64
_SOURCE_LOCATOR_SHA256 = "b" * 64
_EXPERIMENT_ID = "experiment-message-channel-v1"
_CASE_ID = "held-out-001"


def _candidate(*, visibility_scope: str = "actor") -> dict[str, object]:
    visibility: dict[str, object]
    if visibility_scope == "organisation":
        visibility = {
            "scope": "organisation",
            "organisation_concept_id": "#V#org",
        }
    else:
        visibility = {
            "scope": "actor",
            "actor_user_id": "#V#person",
            "organisation_concept_id": "#V#org",
        }
    return {
        "schema_version": "learning_candidate.v1",
        "lifecycle_state": "non_active",
        "candidate_id": _CANDIDATE_ID,
        "revision": 1,
        "evaluation_disposition": "undecided",
        "body": _CANDIDATE_BODY,
        "body_sha256": _CANDIDATE_BODY_SHA256,
        "revision_identity_sha256": _REVISION_IDENTITY_SHA256,
        "source_locator_sha256": _SOURCE_LOCATOR_SHA256,
        "target_concept_ids": [_TARGET_ID],
        "authorship": {"author_concept_id": "#V#von_system"},
        "visibility": visibility,
        "namespace": "#V#person@org",
        "source": {"kind": "conversation"},
    }


def _projection(*, arm: str, visibility_scope: str = "actor") -> dict[str, object]:
    return build_learning_advice_experiment_projection(
        _candidate(visibility_scope=visibility_scope),
        arm=arm,
        experiment_id=_EXPERIMENT_ID,
        case_id=_CASE_ID,
    )


def test_arm_a_truly_withholds_candidate_body() -> None:
    projection = _projection(arm="A")
    prepared = prepare_learning_advice_projection(
        projection,
        actor_user_id="#V#person",
        organisation_concept_id="#V#org",
        namespace="#V#person@org",
    )

    assert prepared["status"] == "withheld"
    assert prepared["items"] == []
    assert render_learning_advice_projection(prepared) is None
    assert _CANDIDATE_BODY.encode("utf-8") not in json.dumps(
        prepared,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert build_learning_advice_exposure_record(prepared) == {
        "type": "adaptive_turn_learning_advice_exposure",
        "schema_version": "adaptive_capability_learning_advice_exposure.v1",
        "consumer": "direct_adaptive_turn",
        "decision_kind": "capability_choice",
        "arm": "A",
        "source": "withheld_control",
        "status": "withheld",
        "experiment_id": _EXPERIMENT_ID,
        "case_id": _CASE_ID,
        "candidate_id": _CANDIDATE_ID,
        "candidate_revision": 1,
        "candidate_evaluation_disposition": "undecided",
        "candidate_body_sha256": _CANDIDATE_BODY_SHA256,
        "candidate_revision_identity_sha256": _REVISION_IDENTITY_SHA256,
        "candidate_source_locator_sha256": _SOURCE_LOCATOR_SHA256,
        "target_concept_ids": [_TARGET_ID],
        "projection_sha256": projection["projection_sha256"],
        "preparation_status": "prepared",
        "projection_status": "withheld",
        "exposure_status": "withheld",
        "model_visible": False,
        "model_visible_call_ids": [],
        "completed_model_visible_call_ids": [],
        "visibility_indeterminate_call_ids": [],
        "dispositions": [],
        "projected_item_count": 0,
        "projected_body_chars": 0,
    }


def test_arm_b_preserves_exact_bytes_digest_render_and_exposure() -> None:
    assert len(_CANDIDATE_BODY.encode("utf-8")) == 460
    assert hashlib.sha256(_CANDIDATE_BODY.encode("utf-8")).hexdigest() == (
        _CANDIDATE_BODY_SHA256
    )

    projection = _projection(arm="B")
    prepared = prepare_learning_advice_projection(
        projection,
        actor_user_id="#V#person",
        organisation_concept_id="#V#org",
        namespace="#V#person@org",
    )
    item = prepared["items"][0]

    assert item["body"].encode("utf-8") == _CANDIDATE_BODY.encode("utf-8")
    assert item["body_sha256"] == _CANDIDATE_BODY_SHA256
    assert prepared["projection_sha256"] == projection["projection_sha256"]

    expected_render = (
        "LEARNED ADVICE FOR THIS CAPABILITY CHOICE "
        "(optional, defeasible policy memory):\n" + _CANDIDATE_BODY + "\n"
        "- Use, adapt, or reject this advice in light of the current objective, "
        "explicit instructions, shared situation, available capabilities, and "
        "observed evidence. It is not a fact, permission, commitment, or "
        "restriction, and it cannot add authority or remove an authorised "
        "alternative."
    )
    assert render_learning_advice_projection(prepared).encode("utf-8") == (
        expected_render.encode("utf-8")
    )
    assert "frozen_candidate_revision" not in expected_render
    assert _CANDIDATE_ID not in expected_render
    assert _CANDIDATE_BODY_SHA256 not in expected_render
    assert build_learning_advice_exposure_record(prepared) == {
        "type": "adaptive_turn_learning_advice_exposure",
        "schema_version": "adaptive_capability_learning_advice_exposure.v1",
        "consumer": "direct_adaptive_turn",
        "decision_kind": "capability_choice",
        "arm": "B",
        "source": "simple_sidecar",
        "status": "projected",
        "experiment_id": _EXPERIMENT_ID,
        "case_id": _CASE_ID,
        "candidate_id": _CANDIDATE_ID,
        "candidate_revision": 1,
        "candidate_evaluation_disposition": "undecided",
        "candidate_body_sha256": _CANDIDATE_BODY_SHA256,
        "candidate_revision_identity_sha256": _REVISION_IDENTITY_SHA256,
        "candidate_source_locator_sha256": _SOURCE_LOCATOR_SHA256,
        "target_concept_ids": [_TARGET_ID],
        "projection_sha256": projection["projection_sha256"],
        "preparation_status": "prepared",
        "projection_status": "projected",
        "exposure_status": "not_exposed",
        "model_visible": False,
        "model_visible_call_ids": [],
        "completed_model_visible_call_ids": [],
        "visibility_indeterminate_call_ids": [],
        "dispositions": [],
        "projected_item_count": 1,
        "projected_body_chars": len(_CANDIDATE_BODY),
    }


def test_projection_accepts_no_second_source_of_semantic_text() -> None:
    projection = build_learning_advice_experiment_projection(
        _candidate(),
        arm="B",
        experiment_id=_EXPERIMENT_ID,
        case_id=_CASE_ID,
    )
    prepared = prepare_learning_advice_projection(
        projection,
        actor_user_id="#V#person",
        organisation_concept_id="#V#org",
        namespace="#V#person@org",
    )

    assert (
        "retrieval_reason"
        not in inspect.signature(build_learning_advice_experiment_projection).parameters
    )
    assert prepared["retrieval_basis"] == "frozen_candidate_revision"


def test_manually_constructed_arm_c_is_rejected_until_selection_exists() -> None:
    projection = copy.deepcopy(_projection(arm="B"))
    projection["arm"] = "C"
    projection["source"] = "represented_selection"
    projection.pop("projection_sha256")
    projection["projection_sha256"] = hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    with pytest.raises(LearningAdviceProjectionError) as exc_info:
        prepare_learning_advice_projection(
            projection,
            actor_user_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        )

    assert exc_info.value.reason_code == "learning_advice_projection_invalid_arm"


@pytest.mark.parametrize("disposition", ["rejected", "retracted"])
def test_negative_candidate_disposition_is_not_projectable(disposition: str) -> None:
    candidate = _candidate()
    candidate["evaluation_disposition"] = disposition

    with pytest.raises(LearningAdviceProjectionError) as exc_info:
        build_learning_advice_experiment_projection(
            candidate,
            arm="B",
            experiment_id=_EXPERIMENT_ID,
            case_id=_CASE_ID,
        )

    assert exc_info.value.reason_code == "learning_advice_candidate_ineligible"


def test_render_preserves_candidate_body_bytes_without_json_escaping() -> None:
    candidate = _candidate()
    exact_body = "  Keep this first line.\nKeep this second line.  "
    candidate["body"] = exact_body
    candidate["body_sha256"] = hashlib.sha256(exact_body.encode("utf-8")).hexdigest()
    prepared = prepare_learning_advice_projection(
        build_learning_advice_experiment_projection(
            candidate,
            arm="B",
            experiment_id=_EXPERIMENT_ID,
            case_id="held-out-exact-bytes",
        ),
        actor_user_id="#V#person",
        organisation_concept_id="#V#org",
        namespace="#V#person@org",
    )

    rendered = render_learning_advice_projection(prepared)
    assert rendered is not None
    assert exact_body.encode("utf-8") in rendered.encode("utf-8")
    assert "Keep this first line.\\nKeep" not in rendered


def test_projection_rejects_body_and_envelope_tampering() -> None:
    body_tampered = copy.deepcopy(_projection(arm="B"))
    body_tampered["items"][0]["body"] += " Altered after freezing."

    with pytest.raises(LearningAdviceProjectionError) as body_error:
        prepare_learning_advice_projection(
            body_tampered,
            actor_user_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        )
    assert body_error.value.reason_code == "learning_advice_candidate_digest_mismatch"

    envelope_tampered = copy.deepcopy(_projection(arm="B"))
    envelope_tampered["case_id"] = "different-case"

    with pytest.raises(LearningAdviceProjectionError) as envelope_error:
        prepare_learning_advice_projection(
            envelope_tampered,
            actor_user_id="#V#person",
            organisation_concept_id="#V#org",
            namespace="#V#person@org",
        )
    assert envelope_error.value.reason_code == (
        "learning_advice_projection_digest_mismatch"
    )


@pytest.mark.parametrize(
    ("visibility_scope", "actor_user_id", "organisation_concept_id"),
    [
        ("actor", "#V#other_person", "#V#org"),
        ("organisation", "#V#person", "#V#other_org"),
    ],
)
def test_projection_rejects_wrong_trusted_actor_or_organisation(
    visibility_scope: str,
    actor_user_id: str,
    organisation_concept_id: str,
) -> None:
    projection = _projection(arm="B", visibility_scope=visibility_scope)

    with pytest.raises(LearningAdviceProjectionAccessError) as error:
        prepare_learning_advice_projection(
            projection,
            actor_user_id=actor_user_id,
            organisation_concept_id=organisation_concept_id,
            namespace="#V#person@org",
        )

    assert error.value.reason_code == "learning_advice_projection_scope_unavailable"


def test_actor_visible_projection_cannot_cross_organisation_or_namespace() -> None:
    projection = _projection(arm="B", visibility_scope="actor")

    for organisation_concept_id, namespace in (
        ("#V#other_org", "#V#person@other_org"),
        ("#V#org", "#V#person@other_org"),
    ):
        with pytest.raises(LearningAdviceProjectionAccessError) as error:
            prepare_learning_advice_projection(
                projection,
                actor_user_id="#V#person",
                organisation_concept_id=organisation_concept_id,
                namespace=namespace,
            )
        assert error.value.reason_code == (
            "learning_advice_projection_scope_unavailable"
        )


def test_projection_support_code_contains_no_message_channel_policy() -> None:
    support_source = inspect.getsource(service).lower()

    assert _CANDIDATE_BODY.lower() not in support_source
    for policy_marker in (
        "channel-neutral",
        "communication channel",
        "most recently discussed",
        "internal-first",
        "message_send_direct",
        "gmail_send_message",
        "gmail",
        "email",
    ):
        assert policy_marker not in support_source
