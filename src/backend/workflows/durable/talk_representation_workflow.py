"""Durable actions for talk and presentation representation workflows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ...services.talk_representation_service import (
    GENERIC_TALK_VERIFICATION_PROFILE,
    materialise_talk_representation,
    verify_talk_representation,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)

TALK_NORMALISE_INPUTS_ACTION_ID = "talk.normalise_inputs"
TALK_MATERIALISE_ACTION_ID = "talk.materialise_representation"
TALK_VERIFY_ACTION_ID = "talk.verify_representation"


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _first_non_empty_text(*values: Any) -> str | None:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = [item.strip() for item in value.split(",")]
        return [item for item in parts if item]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    values: list[str] = []
    for item in value:
        cleaned = _clean_text(item)
        if cleaned:
            values.append(cleaned)
    return values


def _resolve_context_list(request: WorkflowActionRequest, *keys: str) -> list[str]:
    for key in keys:
        values = _coerce_string_list(request.inputs.get(key))
        if values:
            return values
        values = _coerce_string_list(request.data.get(key))
        if values:
            return values
    return []


def _build_normalise_inputs_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        title = _first_non_empty_text(
            request.inputs.get("title"),
            request.inputs.get("presentation_title"),
            request.data.get("title"),
            request.data.get("presentation_title"),
        )
        speaker_name = _first_non_empty_text(
            request.inputs.get("speaker_name"),
            request.inputs.get("presenter_name"),
            request.data.get("speaker_name"),
            request.data.get("presenter_name"),
        )
        presentation_concept_id = _first_non_empty_text(
            request.inputs.get("presentation_concept_id"),
            request.data.get("presentation_concept_id"),
        )
        if not title and not presentation_concept_id:
            return WorkflowActionResult(
                status="failed",
                error="talk_title_or_presentation_concept_id_missing",
            )

        outputs = {
            "presentation_concept_id": presentation_concept_id,
            "title": title,
            "speaker_name": speaker_name,
            "summary": _first_non_empty_text(
                request.inputs.get("summary"),
                request.inputs.get("description"),
                request.data.get("summary"),
                request.data.get("description"),
            ),
            "start_time": _first_non_empty_text(
                request.inputs.get("start_time"),
                request.inputs.get("scheduled_start"),
                request.data.get("start_time"),
                request.data.get("scheduled_start"),
            ),
            "meeting_link": _first_non_empty_text(
                request.inputs.get("meeting_link"),
                request.data.get("meeting_link"),
            ),
            "meeting_id": _first_non_empty_text(
                request.inputs.get("meeting_id"),
                request.data.get("meeting_id"),
            ),
            "meeting_passcode": _first_non_empty_text(
                request.inputs.get("meeting_passcode"),
                request.data.get("meeting_passcode"),
            ),
            "presentation_status": _first_non_empty_text(
                request.inputs.get("presentation_status"),
                request.data.get("presentation_status"),
            ),
            "presentation_type_ids": _resolve_context_list(
                request,
                "presentation_type_ids",
                "type_ids",
            ),
            "verification_profile": _first_non_empty_text(
                request.inputs.get("verification_profile"),
                request.data.get("verification_profile"),
                GENERIC_TALK_VERIFICATION_PROFILE,
            ),
            "normalised_talk_inputs": True,
        }
        return WorkflowActionResult(status="success", outputs=outputs)

    return _handle


def _build_materialise_representation_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        try:
            report = materialise_talk_representation(
                title=_first_non_empty_text(
                    request.inputs.get("title"),
                    request.data.get("title"),
                ),
                speaker_name=_first_non_empty_text(
                    request.inputs.get("speaker_name"),
                    request.data.get("speaker_name"),
                ),
                summary=_first_non_empty_text(
                    request.inputs.get("summary"),
                    request.data.get("summary"),
                ),
                start_time=_first_non_empty_text(
                    request.inputs.get("start_time"),
                    request.data.get("start_time"),
                ),
                meeting_link=_first_non_empty_text(
                    request.inputs.get("meeting_link"),
                    request.data.get("meeting_link"),
                ),
                meeting_id=_first_non_empty_text(
                    request.inputs.get("meeting_id"),
                    request.data.get("meeting_id"),
                ),
                meeting_passcode=_first_non_empty_text(
                    request.inputs.get("meeting_passcode"),
                    request.data.get("meeting_passcode"),
                ),
                presentation_status=_first_non_empty_text(
                    request.inputs.get("presentation_status"),
                    request.data.get("presentation_status"),
                ),
                presentation_concept_id=_first_non_empty_text(
                    request.inputs.get("presentation_concept_id"),
                    request.data.get("presentation_concept_id"),
                ),
                presentation_type_ids=(
                    _resolve_context_list(
                        request,
                        "presentation_type_ids",
                        "type_ids",
                    )
                    or None
                ),
                verification_profile=_first_non_empty_text(
                    request.inputs.get("verification_profile"),
                    request.data.get("verification_profile"),
                ),
                user_concept_id=_first_non_empty_text(
                    request.inputs.get("user_concept_id"),
                    request.data.get("user_concept_id"),
                ),
                organisation_concept_id=_first_non_empty_text(
                    request.inputs.get("org_concept_id"),
                    request.inputs.get("organisation_concept_id"),
                    request.data.get("org_concept_id"),
                    request.data.get("organisation_concept_id"),
                ),
                namespace=_clean_text(getattr(request.environment, "user_namespace", None))
                or None,
            )
        except Exception as exc:
            return WorkflowActionResult(
                status="failed",
                error=f"talk_representation_materialisation_failed:{exc}",
            )

        return WorkflowActionResult(
            status="success",
            outputs={
                **report,
                "talk_representation_materialised": True,
                "materialisation_report": dict(report),
            },
        )

    return _handle


def _build_verify_representation_handler():
    def _handle(request: WorkflowActionRequest) -> WorkflowActionResult:
        verification = verify_talk_representation(
            presentation_concept_id=_first_non_empty_text(
                request.inputs.get("presentation_concept_id"),
                request.data.get("presentation_concept_id"),
            ),
            speaker_concept_id=_first_non_empty_text(
                request.inputs.get("speaker_concept_id"),
                request.data.get("speaker_concept_id"),
            ),
            summary=_first_non_empty_text(
                request.inputs.get("summary"),
                request.data.get("summary"),
            ),
            start_time=_first_non_empty_text(
                request.inputs.get("start_time"),
                request.data.get("start_time"),
            ),
            meeting_link=_first_non_empty_text(
                request.inputs.get("meeting_link"),
                request.data.get("meeting_link"),
            ),
            meeting_id=_first_non_empty_text(
                request.inputs.get("meeting_id"),
                request.data.get("meeting_id"),
            ),
            meeting_passcode=_first_non_empty_text(
                request.inputs.get("meeting_passcode"),
                request.data.get("meeting_passcode"),
            ),
            presentation_status=_first_non_empty_text(
                request.inputs.get("presentation_status"),
                request.data.get("presentation_status"),
            ),
            presentation_type_ids=(
                _resolve_context_list(
                    request,
                    "presentation_type_ids",
                    "type_ids",
                )
                or None
            ),
            verification_profile=_first_non_empty_text(
                request.inputs.get("verification_profile"),
                request.data.get("verification_profile"),
            ),
        )
        return WorkflowActionResult(status="success", outputs=verification)

    return _handle


def register_talk_representation_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=TALK_NORMALISE_INPUTS_ACTION_ID,
            handler=_build_normalise_inputs_handler(),
            description="Normalise talk and presentation workflow inputs.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TALK_MATERIALISE_ACTION_ID,
            handler=_build_materialise_representation_handler(),
            description="Materialise a talk or presentation concept in Vontology.",
        )
    )
    registry.register_if_absent(
        ActionSpec(
            action_id=TALK_VERIFY_ACTION_ID,
            handler=_build_verify_representation_handler(),
            description="Verify talk or presentation representation postconditions.",
        )
    )


__all__ = [
    "TALK_MATERIALISE_ACTION_ID",
    "TALK_NORMALISE_INPUTS_ACTION_ID",
    "TALK_VERIFY_ACTION_ID",
    "register_talk_representation_actions",
]
