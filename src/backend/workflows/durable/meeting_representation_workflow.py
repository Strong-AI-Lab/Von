"""Deterministic source preparation for the represented meeting workflow."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ...services.computer_file_copy_service import fetch_file_copy_bytes
from ...services.ics_meeting_parser_service import (
    DEFAULT_ICS_MAX_CHARS,
    IcsCalendarAddress,
    IcsMeeting,
    IcsMeetingParseOutcome,
    parse_ics_meeting,
)
from ...services.meeting_file_representation_service import (
    IcsMeetingMaterialisationError,
    materialise_ics_meeting_representation_for_file_copy,
)
from ...services.workflow_event_integration_service import (
    suppress_event_workflow_launches,
)
from ..action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
)

logger = logging.getLogger(__name__)

MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID = (
    "meeting_representation.materialise_ics_file_copy"
)
ICS_FAST_PATH_MATERIALISED = "materialised"
ICS_FAST_PATH_NOT_APPLICABLE = "not_applicable"
ICS_FAST_PATH_FAILED = "failed"
ICS_PARTICIPANT_OUTPUT_LIMIT = 80


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _not_applicable_outputs(
    *, reason: str, parse_result: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    outputs: dict[str, Any] = {
        "ics_materialisation_outcome": ICS_FAST_PATH_NOT_APPLICABLE,
        "ics_fast_path_status": ICS_FAST_PATH_NOT_APPLICABLE,
        "ics_materialisation_succeeded": False,
        "ics_materialisation_reason": reason,
    }
    if isinstance(parse_result, Mapping):
        outputs["ics_parse_result"] = dict(parse_result)
    return outputs


def _failure_result(
    *, code: str, details: Mapping[str, Any] | None = None
) -> WorkflowActionResult:
    outputs: dict[str, Any] = {
        "ics_materialisation_outcome": ICS_FAST_PATH_FAILED,
        "ics_fast_path_status": ICS_FAST_PATH_FAILED,
        "ics_materialisation_succeeded": False,
        "ics_materialisation_reason": code,
    }
    if isinstance(details, Mapping):
        outputs["ics_materialisation_error_details"] = dict(details)
    return WorkflowActionResult(status="failed", error=code, outputs=outputs)


def _bounded_max_bytes(value: Any) -> int:
    if value is None:
        return DEFAULT_ICS_MAX_CHARS
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("ics_max_bytes_invalid") from exc
    if parsed <= 0 or parsed > DEFAULT_ICS_MAX_CHARS:
        raise ValueError("ics_max_bytes_out_of_bounds")
    return parsed


def _participant_identity_key(
    address: IcsCalendarAddress,
) -> tuple[str, str] | None:
    email = _clean_text(address.email)
    if email:
        return ("email", email.casefold())
    uri = _clean_text(address.uri)
    if uri:
        return ("uri", uri)
    common_name = _clean_text(address.common_name)
    if common_name:
        return ("common_name", common_name.casefold())
    return None


def _participant_projection(meeting: IcsMeeting) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    indexes_by_identity: dict[tuple[str, str], int] = {}
    role_addresses: list[tuple[str, IcsCalendarAddress]] = []
    if meeting.organizer is not None:
        role_addresses.append(("organizer", meeting.organizer))
    role_addresses.extend(("attendee", attendee) for attendee in meeting.attendees)

    for role, address in role_addresses:
        identity_key = _participant_identity_key(address)
        if identity_key is None:
            continue
        existing_index = indexes_by_identity.get(identity_key)
        if existing_index is not None:
            existing = participants[existing_index]
            roles = existing["roles"]
            if isinstance(roles, list) and role not in roles:
                roles.append(role)
            for field_name in ("uri", "common_name", "email"):
                if not existing.get(field_name):
                    existing[field_name] = getattr(address, field_name)
            continue
        if len(participants) >= ICS_PARTICIPANT_OUTPUT_LIMIT:
            continue

        participant = {
            **address.to_payload(),
            "roles": [role],
        }
        indexes_by_identity[identity_key] = len(participants)
        participants.append(participant)

    return participants


def _handle_materialise_ics_file_copy(
    request: WorkflowActionRequest,
) -> WorkflowActionResult:
    file_copy_concept_id = _clean_text(
        request.inputs.get("file_copy_concept_id")
        or request.data.get("file_copy_concept_id")
    )
    if not file_copy_concept_id:
        return WorkflowActionResult(
            status="success",
            outputs=_not_applicable_outputs(reason="file_copy_concept_id_missing"),
        )

    user_concept_id = _clean_text(request.environment.user_concept_id)
    organisation_concept_id = _clean_text(request.environment.org_concept_id) or None
    namespace = _clean_text(request.environment.user_namespace) or None
    if not user_concept_id:
        return _failure_result(code="ics_materialisation_actor_missing")

    try:
        max_bytes = _bounded_max_bytes(request.inputs.get("max_bytes"))
    except ValueError as exc:
        return _failure_result(code=str(exc))

    try:
        read_result = fetch_file_copy_bytes(
            file_copy_concept_id=file_copy_concept_id,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            namespace=namespace,
            max_bytes=max_bytes,
            allow_large=False,
            logger=logger,
        )
    except Exception as exc:
        logger.warning(
            "[meeting_representation] ICS file-copy read failed for %s: %s",
            file_copy_concept_id,
            exc,
            exc_info=True,
        )
        return _failure_result(
            code="ics_file_copy_read_failed:unexpected_failure",
            details={
                "file_copy_concept_id": file_copy_concept_id,
                "exception_type": type(exc).__name__,
            },
        )
    if not isinstance(read_result, Mapping) or read_result.get("success") is not True:
        error = (
            _clean_text(read_result.get("error"))
            if isinstance(read_result, Mapping)
            else "unexpected_file_copy_response"
        )
        return _failure_result(
            code=f"ics_file_copy_read_failed:{error or 'unknown'}",
            details={
                "file_copy_concept_id": file_copy_concept_id,
                "read_error": error or "unknown",
            },
        )

    raw_bytes = read_result.get("data")
    if not isinstance(raw_bytes, (bytes, bytearray)):
        return _failure_result(code="ics_file_copy_bytes_missing")
    try:
        source_text = bytes(raw_bytes).decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        return WorkflowActionResult(
            status="success",
            outputs=_not_applicable_outputs(reason="ics_source_not_utf8_text"),
        )

    parse_result = parse_ics_meeting(source_text, max_chars=max_bytes)
    parse_payload = parse_result.to_payload()
    if parse_result.outcome is not IcsMeetingParseOutcome.PARSED:
        return WorkflowActionResult(
            status="success",
            outputs=_not_applicable_outputs(
                reason=parse_result.error_code or parse_result.outcome.value,
                parse_result=parse_payload,
            ),
        )
    if parse_result.meeting is None:
        return _failure_result(
            code="ics_parser_contract_violation",
            details={"parse_result": parse_payload},
        )

    try:
        # This is one compound action.  Its own exact receipt and workflow
        # read-back provide observability; nested mutation-triggered workflows
        # would duplicate work and dominated the latency of the model path.
        with suppress_event_workflow_launches(
            MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID
        ):
            materialisation = materialise_ics_meeting_representation_for_file_copy(
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
                namespace=namespace,
                file_copy_concept_id=file_copy_concept_id,
                meeting=parse_result.meeting,
            )
    except IcsMeetingMaterialisationError as exc:
        return _failure_result(code=exc.code, details=exc.details)
    except Exception as exc:
        logger.warning(
            "[meeting_representation] ICS materialisation failed for %s: %s",
            file_copy_concept_id,
            exc,
            exc_info=True,
        )
        return _failure_result(
            code="ics_materialisation_unexpected_failure",
            details={"exception_type": type(exc).__name__},
        )

    if (
        not isinstance(materialisation, Mapping)
        or materialisation.get("success") is not True
    ):
        return _failure_result(code="ics_materialisation_response_invalid")
    meeting_concept_id = _clean_text(materialisation.get("meeting_concept_id"))
    receipt = materialisation.get("relationship_effect_receipt")
    response_text = _clean_text(materialisation.get("response_text"))
    if not meeting_concept_id or not isinstance(receipt, Mapping) or not response_text:
        return _failure_result(code="ics_materialisation_response_incomplete")
    participants = _participant_projection(parse_result.meeting)

    return WorkflowActionResult(
        status="success",
        outputs={
            "ics_materialisation_outcome": ICS_FAST_PATH_MATERIALISED,
            "ics_fast_path_status": ICS_FAST_PATH_MATERIALISED,
            "ics_materialisation_succeeded": True,
            "ics_materialisation_reason": materialisation.get("reason"),
            "ics_parse_result": parse_payload,
            "ics_materialisation_result": dict(materialisation),
            "ics_participant_count": len(participants),
            "ics_participants": participants,
            "meeting_concept_id": meeting_concept_id,
            "relationship_effect_receipt": dict(receipt),
            "response_text": response_text,
        },
    )


def register_meeting_representation_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID,
            handler=_handle_materialise_ics_file_copy,
            description=(
                "Read one actor-visible file copy and, when it is exactly one "
                "valid iCalendar event, materialise its core meeting/source "
                "facts by UID and expose structured participant evidence for "
                "the workflow's mandatory semantic representation step."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_copy_concept_id": {"type": "string"},
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": DEFAULT_ICS_MAX_CHARS,
                    },
                },
            },
            output_schema={
                "type": "object",
                "properties": {
                    "ics_materialisation_outcome": {
                        "type": "string",
                        "enum": [
                            ICS_FAST_PATH_MATERIALISED,
                            ICS_FAST_PATH_NOT_APPLICABLE,
                            ICS_FAST_PATH_FAILED,
                        ],
                    },
                    "ics_fast_path_status": {
                        "type": "string",
                        "enum": [
                            ICS_FAST_PATH_MATERIALISED,
                            ICS_FAST_PATH_NOT_APPLICABLE,
                            ICS_FAST_PATH_FAILED,
                        ],
                    },
                    "ics_materialisation_succeeded": {"type": "boolean"},
                    "ics_materialisation_reason": {"type": "string"},
                    "ics_participant_count": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": ICS_PARTICIPANT_OUTPUT_LIMIT,
                    },
                    "ics_participants": {
                        "type": "array",
                        "maxItems": ICS_PARTICIPANT_OUTPUT_LIMIT,
                        "items": {
                            "type": "object",
                            "properties": {
                                "uri": {"type": "string"},
                                "common_name": {"type": ["string", "null"]},
                                "email": {"type": ["string", "null"]},
                                "roles": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": ["organizer", "attendee"],
                                    },
                                    "maxItems": 2,
                                    "uniqueItems": True,
                                },
                            },
                            "required": ["uri", "common_name", "email", "roles"],
                        },
                    },
                    "meeting_concept_id": {"type": "string"},
                    "relationship_effect_receipt": {"type": "object"},
                    "response_text": {"type": "string"},
                },
            },
            side_effects="write",
        )
    )


__all__ = [
    "ICS_FAST_PATH_FAILED",
    "ICS_FAST_PATH_MATERIALISED",
    "ICS_FAST_PATH_NOT_APPLICABLE",
    "MEETING_REPRESENTATION_MATERIALISE_ICS_FILE_COPY_ACTION_ID",
    "register_meeting_representation_actions",
]
