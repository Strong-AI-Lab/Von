"""Resolve governed visible-disclosure policy for outbound Gmail messages.

Visible text in a recipient's message is semantic policy, not an unavoidable
transport property.  The policy therefore lives as a versioned attribute on a
trusted actor, organisation, or represented mailbox concept.  This module owns
only the small deterministic boundary needed to validate, resolve, render, and
report that policy.

The attribute name is :data:`GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE`.  Its
value has this shape::

    {
        "schema_version": "gmail_visible_ai_agent_disclosure_policy.v1",
        "policy_id": "organisation-mail-disclosure",
        "policy_version": "2026-08-26",
        "mode": "required",  # or "off"
        "binding": true,
        "default_language": "en",
        "templates": {
            "en": {
                "von_sent": "Sent by Von, an AI agent.",
                "von_drafted_and_sent": "Drafted and sent by Von, an AI agent.",
                "user_authored_von_sent": "Sent by Von, an AI agent, using text supplied by the user."
            }
        }
    }

``templates`` are required only for ``required`` policies.  The transport has
no built-in English disclosure or language-specific semantic matcher.  An
explicit message language must have an exact or primary-language template; it
never silently falls back to another language.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from . import concept_service

GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE = "gmail_visible_ai_agent_disclosure_policy"
GMAIL_VISIBLE_DISCLOSURE_POLICY_SCHEMA_VERSION = (
    "gmail_visible_ai_agent_disclosure_policy.v1"
)
GMAIL_VISIBLE_DISCLOSURE_RECEIPT_SCHEMA_VERSION = "gmail_visible_ai_agent_disclosure.v2"

DISCLOSURE_MODE_OFF = "off"
DISCLOSURE_MODE_REQUIRED = "required"
DISCLOSURE_MODES = frozenset({DISCLOSURE_MODE_OFF, DISCLOSURE_MODE_REQUIRED})

BODY_AUTHORSHIP_VON_DRAFTED = "von_drafted"
BODY_AUTHORSHIP_USER_SUPPLIED = "user_supplied"
BODY_AUTHORSHIP_UNSPECIFIED = "unspecified"
BODY_AUTHORSHIP_VALUES = frozenset(
    {
        BODY_AUTHORSHIP_VON_DRAFTED,
        BODY_AUTHORSHIP_USER_SUPPLIED,
        BODY_AUTHORSHIP_UNSPECIFIED,
    }
)
_AUTHORSHIP_TEMPLATE_ROLE = {
    BODY_AUTHORSHIP_VON_DRAFTED: "von_drafted_and_sent",
    BODY_AUTHORSHIP_USER_SUPPLIED: "user_authored_von_sent",
    BODY_AUTHORSHIP_UNSPECIFIED: "von_sent",
}
_REQUIRED_TEMPLATE_ROLES = frozenset(_AUTHORSHIP_TEMPLATE_ROLE.values())

_LANGUAGE_TAG = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_MAX_POLICY_IDENTIFIER_CHARS = 256
_MAX_TEMPLATE_CHARS = 4_000


class GmailVisibleDisclosurePolicyError(ValueError):
    """A represented visible-disclosure policy cannot be applied safely."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


def load_concepts(concept_ids: list[str]) -> Mapping[str, Mapping[str, Any]]:
    """Batch-read policy authorities without conflating absence and failure."""

    try:
        return concept_service.get_concepts_by_concept_ids_exact(concept_ids)
    except Exception as exc:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_resolution_unavailable",
            (
                "Outbound email was not sent because governed visible-disclosure "
                "policy could not be resolved."
            ),
        ) from exc


@dataclass(frozen=True)
class _PolicyCandidate:
    mode: str
    policy_id: str
    policy_version: str
    authority_scope: str
    authority_concept_id: str
    binding: bool
    default_language: str | None
    templates: Mapping[str, Mapping[str, str]]

    def projection(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "authority_scope": self.authority_scope,
            "authority_concept_id": self.authority_concept_id,
            "binding": self.binding,
        }


@dataclass(frozen=True)
class GmailVisibleDisclosureResolution:
    """Body-free resolution used by MIME construction, audit, and receipts."""

    mode: str
    policy_id: str
    policy_version: str
    authority_scope: str
    authority_concept_id: str | None
    binding: bool
    selection_reason: str
    requested_language: str | None
    resolved_language: str | None
    body_authorship: str
    template_role: str | None
    visible_text: str | None
    considered_policies: tuple[Mapping[str, Any], ...]

    def as_receipt(self) -> dict[str, Any]:
        """Return inspectable policy provenance without the message body."""

        return {
            "schema_version": GMAIL_VISIBLE_DISCLOSURE_RECEIPT_SCHEMA_VERSION,
            "requested_policies": [dict(item) for item in self.considered_policies],
            "resolved_mode": self.mode,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "authority_scope": self.authority_scope,
            "authority_concept_id": self.authority_concept_id,
            "binding": self.binding,
            "selection_reason": self.selection_reason,
            "requested_language": self.requested_language,
            "resolved_language": self.resolved_language,
            "body_authorship": self.body_authorship,
            "template_role": self.template_role,
            "visible_text": self.visible_text,
            "plain_text_part": self.mode == DISCLOSURE_MODE_REQUIRED,
            "html_part": False,
            "machine_header": "X-Von-AI-Agent",
        }


def _clean_identifier(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            f"{field_name} must be a non-empty string",
        )
    cleaned = value.strip()
    if len(cleaned) > _MAX_POLICY_IDENTIFIER_CHARS:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            f"{field_name} is too long",
        )
    return cleaned


def _normalise_language_tag(value: Any, *, field_name: str) -> str:
    cleaned = _clean_identifier(value, field_name=field_name)
    if not _LANGUAGE_TAG.fullmatch(cleaned):
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            f"{field_name} must be a BCP-47-style language tag",
        )
    return cleaned.lower()


def _parse_templates(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping) or not value:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            "required disclosure policy templates must be a non-empty object",
        )
    templates: dict[str, dict[str, str]] = {}
    for raw_language, raw_roles in value.items():
        language = _normalise_language_tag(
            raw_language,
            field_name="templates language",
        )
        if language in templates:
            raise GmailVisibleDisclosurePolicyError(
                "gmail_visible_disclosure_policy_invalid",
                f"duplicate disclosure template language {language!r}",
            )
        if not isinstance(raw_roles, Mapping):
            raise GmailVisibleDisclosurePolicyError(
                "gmail_visible_disclosure_policy_invalid",
                f"templates.{language} must be an object",
            )
        unknown_roles = sorted(set(raw_roles) - _REQUIRED_TEMPLATE_ROLES)
        missing_roles = sorted(_REQUIRED_TEMPLATE_ROLES - set(raw_roles))
        if unknown_roles or missing_roles:
            details: list[str] = []
            if missing_roles:
                details.append("missing " + ", ".join(missing_roles))
            if unknown_roles:
                details.append("unknown " + ", ".join(unknown_roles))
            raise GmailVisibleDisclosurePolicyError(
                "gmail_visible_disclosure_policy_invalid",
                f"templates.{language} has " + "; ".join(details),
            )
        roles: dict[str, str] = {}
        for role in _REQUIRED_TEMPLATE_ROLES:
            text = raw_roles.get(role)
            if not isinstance(text, str) or not text.strip():
                raise GmailVisibleDisclosurePolicyError(
                    "gmail_visible_disclosure_policy_invalid",
                    f"templates.{language}.{role} must be non-empty text",
                )
            if len(text) > _MAX_TEMPLATE_CHARS:
                raise GmailVisibleDisclosurePolicyError(
                    "gmail_visible_disclosure_policy_invalid",
                    f"templates.{language}.{role} is too long",
                )
            roles[role] = text
        templates[language] = roles
    return templates


def _parse_candidate(
    raw_policy: Any,
    *,
    authority_scope: str,
    authority_concept_id: str,
) -> _PolicyCandidate:
    if not isinstance(raw_policy, Mapping):
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            (
                f"{GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE} on "
                f"{authority_concept_id} must be an object"
            ),
        )
    supplied_schema = raw_policy.get("schema_version")
    if supplied_schema != GMAIL_VISIBLE_DISCLOSURE_POLICY_SCHEMA_VERSION:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            (
                "visible disclosure policy schema_version must be "
                f"{GMAIL_VISIBLE_DISCLOSURE_POLICY_SCHEMA_VERSION!r}"
            ),
        )
    mode = str(raw_policy.get("mode") or "").strip().lower()
    if mode not in DISCLOSURE_MODES:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            "visible disclosure policy mode must be 'off' or 'required'",
        )
    policy_id = _clean_identifier(raw_policy.get("policy_id"), field_name="policy_id")
    policy_version = _clean_identifier(
        raw_policy.get("policy_version"),
        field_name="policy_version",
    )
    binding = raw_policy.get("binding", False)
    if not isinstance(binding, bool):
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            "visible disclosure policy binding must be a boolean",
        )
    if binding and authority_scope == "actor":
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            "an actor preference cannot declare itself binding",
        )
    if binding and mode != DISCLOSURE_MODE_REQUIRED:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            "only a required disclosure policy may be binding",
        )

    default_language: str | None = None
    templates: dict[str, dict[str, str]] = {}
    if mode == DISCLOSURE_MODE_REQUIRED:
        default_language = _normalise_language_tag(
            raw_policy.get("default_language"),
            field_name="default_language",
        )
        templates = _parse_templates(raw_policy.get("templates"))
        if default_language not in templates:
            raise GmailVisibleDisclosurePolicyError(
                "gmail_visible_disclosure_policy_invalid",
                "default_language must name an available disclosure template",
            )

    return _PolicyCandidate(
        mode=mode,
        policy_id=policy_id,
        policy_version=policy_version,
        authority_scope=authority_scope,
        authority_concept_id=authority_concept_id,
        binding=binding,
        default_language=default_language,
        templates=templates,
    )


def _load_candidate(
    concept_id: str | None,
    *,
    authority_scope: str,
    concept_loader: Callable[[str], Mapping[str, Any] | None],
) -> _PolicyCandidate | None:
    cleaned_id = str(concept_id or "").strip()
    if not cleaned_id:
        return None
    concept = concept_loader(cleaned_id)
    if not isinstance(concept, Mapping):
        return None
    attributes = concept.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    raw_policy = attributes.get(GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE)
    if raw_policy is None:
        return None
    return _parse_candidate(
        raw_policy,
        authority_scope=authority_scope,
        authority_concept_id=cleaned_id,
    )


def _select_candidate(
    candidates: Mapping[str, _PolicyCandidate],
) -> tuple[_PolicyCandidate | None, str]:
    # A binding organisation or mailbox requirement is a trusted ceiling on
    # actor preference.  Organisation wins if both bind because it is the
    # broader governing authority; both necessarily resolve to ``required``.
    for scope in ("organisation", "mailbox"):
        candidate = candidates.get(scope)
        if candidate is not None and candidate.binding:
            return candidate, "binding_required"

    # In the absence of a binding requirement, the actor's explicit preference
    # is most specific, followed by the mailbox and then organisation default.
    for scope, reason in (
        ("actor", "actor_preference"),
        ("mailbox", "mailbox_policy"),
        ("organisation", "organisation_policy"),
    ):
        candidate = candidates.get(scope)
        if candidate is not None:
            return candidate, reason
    return None, "default_absence"


def _resolve_template_language(
    *,
    candidate: _PolicyCandidate,
    requested_language: str | None,
) -> str:
    if requested_language is None:
        assert candidate.default_language is not None
        return candidate.default_language
    normalised = _normalise_language_tag(
        requested_language,
        field_name="body_language",
    )
    if normalised in candidate.templates:
        return normalised
    primary = normalised.split("-", 1)[0]
    if primary in candidate.templates:
        return primary
    raise GmailVisibleDisclosurePolicyError(
        "gmail_visible_disclosure_language_unavailable",
        (
            f"required visible disclosure policy {candidate.policy_id!r} has no "
            f"template for message language {normalised!r}"
        ),
    )


def resolve_gmail_visible_disclosure_policy(
    *,
    profile_resource_concept_id: str,
    acting_user_concept_id: str | None,
    organisation_concept_id: str | None,
    body_language: str | None = None,
    body_authorship: str | None = None,
    concept_loader: Callable[[str], Mapping[str, Any] | None] | None = None,
) -> GmailVisibleDisclosureResolution:
    """Resolve one trusted policy and render its role/language-specific text."""

    scoped_concept_ids = (
        ("organisation", organisation_concept_id),
        ("mailbox", profile_resource_concept_id),
        ("actor", acting_user_concept_id),
    )
    if concept_loader is None:
        policy_concept_ids = [
            str(concept_id).strip()
            for _scope, concept_id in scoped_concept_ids
            if isinstance(concept_id, str) and concept_id.strip()
        ]
        loaded_concepts = load_concepts(policy_concept_ids)
        loader = loaded_concepts.get
    else:
        loader = concept_loader
    candidates: dict[str, _PolicyCandidate] = {}
    for scope, concept_id in scoped_concept_ids:
        candidate = _load_candidate(
            concept_id,
            authority_scope=scope,
            concept_loader=loader,
        )
        if candidate is not None:
            candidates[scope] = candidate

    selected, selection_reason = _select_candidate(candidates)
    considered = tuple(
        candidates[scope].projection()
        for scope in ("organisation", "mailbox", "actor")
        if scope in candidates
    )
    requested_language = (
        str(body_language).strip()
        if isinstance(body_language, str) and body_language.strip()
        else None
    )
    authorship = str(body_authorship or BODY_AUTHORSHIP_UNSPECIFIED).strip().lower()
    if authorship not in BODY_AUTHORSHIP_VALUES:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_authorship_invalid",
            (
                "body_authorship must be 'von_drafted', 'user_supplied', or "
                "'unspecified'"
            ),
        )

    if selected is None:
        return GmailVisibleDisclosureResolution(
            mode=DISCLOSURE_MODE_OFF,
            policy_id="von-default-no-visible-disclosure",
            policy_version="1",
            authority_scope="default",
            authority_concept_id=None,
            binding=False,
            selection_reason=selection_reason,
            requested_language=requested_language,
            resolved_language=None,
            body_authorship=authorship,
            template_role=None,
            visible_text=None,
            considered_policies=considered,
        )

    if selected.mode == DISCLOSURE_MODE_OFF:
        return GmailVisibleDisclosureResolution(
            mode=selected.mode,
            policy_id=selected.policy_id,
            policy_version=selected.policy_version,
            authority_scope=selected.authority_scope,
            authority_concept_id=selected.authority_concept_id,
            binding=selected.binding,
            selection_reason=selection_reason,
            requested_language=requested_language,
            resolved_language=None,
            body_authorship=authorship,
            template_role=None,
            visible_text=None,
            considered_policies=considered,
        )

    resolved_language = _resolve_template_language(
        candidate=selected,
        requested_language=requested_language,
    )
    template_role = _AUTHORSHIP_TEMPLATE_ROLE[authorship]
    visible_text = selected.templates[resolved_language][template_role]
    return GmailVisibleDisclosureResolution(
        mode=selected.mode,
        policy_id=selected.policy_id,
        policy_version=selected.policy_version,
        authority_scope=selected.authority_scope,
        authority_concept_id=selected.authority_concept_id,
        binding=selected.binding,
        selection_reason=selection_reason,
        requested_language=requested_language,
        resolved_language=resolved_language,
        body_authorship=authorship,
        template_role=template_role,
        visible_text=visible_text,
        considered_policies=considered,
    )


def apply_gmail_visible_disclosure(
    body_text: str,
    resolution: GmailVisibleDisclosureResolution,
) -> str:
    """Return the exact outbound body, adding a required block exactly once.

    ``off`` is a byte-preserving pass-through.  Under ``required``, an existing
    canonical block is retained rather than duplicated.  Multiple exact copies
    are collapsed to the first; no language-specific heuristic or regex decides
    what counts as the policy-owned block.
    """

    if resolution.mode == DISCLOSURE_MODE_OFF:
        return body_text
    visible_text = resolution.visible_text
    if not isinstance(visible_text, str) or not visible_text:
        raise GmailVisibleDisclosurePolicyError(
            "gmail_visible_disclosure_policy_invalid",
            "required visible disclosure resolution has no rendered text",
        )
    occurrences = body_text.count(visible_text)
    if occurrences == 0:
        return f"{body_text}\n\n{visible_text}"
    if occurrences == 1:
        return body_text

    first, *remaining = body_text.split(visible_text)
    return visible_text.join((first, "".join(remaining)))


__all__ = [
    "BODY_AUTHORSHIP_UNSPECIFIED",
    "BODY_AUTHORSHIP_USER_SUPPLIED",
    "BODY_AUTHORSHIP_VALUES",
    "BODY_AUTHORSHIP_VON_DRAFTED",
    "DISCLOSURE_MODE_OFF",
    "DISCLOSURE_MODE_REQUIRED",
    "GMAIL_VISIBLE_DISCLOSURE_POLICY_ATTRIBUTE",
    "GMAIL_VISIBLE_DISCLOSURE_POLICY_SCHEMA_VERSION",
    "GmailVisibleDisclosurePolicyError",
    "GmailVisibleDisclosureResolution",
    "apply_gmail_visible_disclosure",
    "resolve_gmail_visible_disclosure_policy",
]
