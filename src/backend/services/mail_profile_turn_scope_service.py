"""Actor-scoped Gmail profile choices for ordinary conversation turns.

This module intersects represented authority with runtime availability and
projects only non-secret resource choices.  It does not choose semantically on
the model's behalf: an explicit request, a represented default, or a sole
available resource supplies a default; otherwise the bounded model may select
from the trusted set or ask one focused question.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.backend.integrations.google.gmail_service import (
    list_profile_summaries,
    load_profiles_from_env,
)

from .mail_profile_resource_vontology_service import (
    list_authorised_gmail_profiles_for_user,
)

TRUSTED_ARGUMENT_CHOICE_SCHEMA_VERSION = "trusted_argument_choice.v1"


def build_gmail_profile_turn_scope(
    *,
    user_concept_id: str,
    requested_profile_id: str | None = None,
    remembered_resource_scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the actor's runtime-available represented Gmail choices.

    A remembered resource is only a conversational default.  It is matched
    against the fresh represented-authority/runtime intersection on every
    turn, so a shared, stale, or no-longer-authorised profile cannot grant
    access.  An explicit API request remains authoritative for the turn and
    fails closed when it is foreign or unavailable.
    """

    authority = list_authorised_gmail_profiles_for_user(
        user_concept_id=user_concept_id,
    )
    if not authority.get("success"):
        return {
            "success": False,
            "reason_code": authority.get("reason_code")
            or "mail_profile_actor_not_represented",
            "profile_id": None,
            "choices": [],
        }

    try:
        configured_profiles = load_profiles_from_env()
        configured_summaries = {
            str(item.get("profile_id") or ""): dict(item)
            for item in list_profile_summaries(configured_profiles)
            if isinstance(item, Mapping) and str(item.get("profile_id") or "")
        }
    except Exception as exc:  # noqa: BLE001 - typed fail-closed projection
        return {
            "success": False,
            "reason_code": "gmail_profile_runtime_configuration_unavailable",
            "profile_id": None,
            "choices": [],
            "error_type": type(exc).__name__,
        }

    represented_profiles = [
        dict(item)
        for item in authority.get("profiles") or []
        if isinstance(item, Mapping)
    ]
    choices: list[dict[str, Any]] = []
    for profile in represented_profiles:
        runtime_alias = str(profile.get("profile_id") or "").strip()
        resource_id = str(
            profile.get("profile_resource_concept_id") or ""
        ).strip()
        if not runtime_alias or not resource_id or runtime_alias not in configured_profiles:
            continue
        summary = configured_summaries.get(runtime_alias) or {}
        authorised_email = str(summary.get("authorised_email") or "").strip()
        choice = {
            "selector": resource_id,
            "value": runtime_alias,
            "source_family": "gmail",
            "resource_id": resource_id,
            "runtime_alias": runtime_alias,
            "is_default": profile.get("is_default") is True,
            "represented_identity_concept_ids": [
                str(value)
                for value in profile.get("represented_identity_concept_ids") or []
                if isinstance(value, str) and value.strip()
            ],
        }
        # Runtime aliases are operational selectors, not user-facing account
        # labels.  Omit presentation metadata when the connector cannot supply
        # an explicitly human-readable identity.
        if authorised_email:
            choice["display_label"] = authorised_email
        choices.append(choice)

    requested = str(requested_profile_id or "").strip() or None
    represented_request_match = next(
        (
            profile
            for profile in represented_profiles
            if requested
            in {
                str(profile.get("profile_id") or "").strip(),
                str(profile.get("profile_resource_concept_id") or "").strip(),
            }
        ),
        None,
    )
    requested_choice = next(
        (
            choice
            for choice in choices
            if requested in {choice["selector"], choice["value"]}
        ),
        None,
    )
    if requested and represented_request_match is None:
        return {
            "success": False,
            "reason_code": "mail_profile_not_authorised_for_actor",
            "profile_id": None,
            "choices": choices,
        }
    if requested and requested_choice is None:
        return {
            "success": False,
            "reason_code": "authorised_mail_profile_unavailable",
            "profile_id": None,
            "choices": choices,
        }
    if not choices:
        return {
            "success": False,
            "reason_code": "authorised_mail_profiles_unavailable",
            "profile_id": None,
            "choices": [],
        }

    remembered_choice: dict[str, Any] | None = None
    if requested_choice is None and isinstance(remembered_resource_scope, Mapping):
        remembered_source_family = str(
            remembered_resource_scope.get("source_family") or ""
        ).strip()
        remembered_resource_id = str(
            remembered_resource_scope.get("resource_id") or ""
        ).strip()
        remembered_runtime_alias = str(
            remembered_resource_scope.get("runtime_alias") or ""
        ).strip()
        if remembered_source_family == "gmail" and (
            remembered_resource_id or remembered_runtime_alias
        ):
            # The represented resource is the stable identity.  Its runtime
            # alias may legitimately change between turns, so use the alias
            # only as a compatibility fallback when no resource ID survived.
            remembered_matches = (
                [
                    choice
                    for choice in choices
                    if choice["resource_id"] == remembered_resource_id
                ]
                if remembered_resource_id
                else [
                    choice
                    for choice in choices
                    if choice["runtime_alias"] == remembered_runtime_alias
                ]
            )
            if len(remembered_matches) == 1:
                remembered_choice = remembered_matches[0]

    default_choice: dict[str, Any] | None = requested_choice or remembered_choice
    selection_source = "request" if requested_choice is not None else None
    if remembered_choice is not None:
        selection_source = "conversation_situation"
    if default_choice is None:
        represented_defaults = [
            choice for choice in choices if choice.get("is_default") is True
        ]
        if len(represented_defaults) == 1:
            default_choice = represented_defaults[0]
            selection_source = "represented_default"
        elif not represented_defaults and len(choices) == 1:
            default_choice = choices[0]
            selection_source = "sole_authorised_profile"
        elif len(represented_defaults) > 1:
            selection_source = "multiple_represented_defaults"
        else:
            selection_source = "semantic_choice_required"

    default_selector = (
        str(default_choice.get("selector")) if default_choice is not None else None
    )
    dispatch_choices = [requested_choice] if requested_choice is not None else choices
    selected_scope = None
    if default_choice is not None:
        selected_scope = {
            key: value
            for key, value in default_choice.items()
            if key
            in {
                "source_family",
                "resource_id",
                "runtime_alias",
                "display_label",
            }
        }
        selected_scope["selection_source"] = str(selection_source)

    trusted_argument_choice: dict[str, Any] = {
        "schema_version": TRUSTED_ARGUMENT_CHOICE_SCHEMA_VERSION,
        "default_selector": default_selector,
        "choices": dispatch_choices,
    }
    if default_selector is not None and selection_source is not None:
        trusted_argument_choice["default_selection_source"] = str(
            selection_source
        )

    return {
        "success": True,
        "reason_code": "authorised_mail_profile_choices_resolved",
        "profile_id": (
            str(default_choice.get("value")) if default_choice is not None else None
        ),
        "profile_resource_concept_id": (
            str(default_choice.get("resource_id"))
            if default_choice is not None
            else None
        ),
        "selection_source": selection_source,
        "selected_resource_scope": selected_scope,
        "choices": choices,
        "trusted_argument_choice": trusted_argument_choice,
    }


__all__ = [
    "TRUSTED_ARGUMENT_CHOICE_SCHEMA_VERSION",
    "build_gmail_profile_turn_scope",
]
