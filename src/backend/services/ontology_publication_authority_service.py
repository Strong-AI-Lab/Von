"""Central authority and receipt contract for canonical ontology publication.

Visibility answers who may read a represented artefact.  It is deliberately not
used as a proxy for who may publish, retract, consolidate, or change its scope.
This service resolves semantic authority from live represented role relations,
binds a server-issued delegation to one exact agent effect, and records a
durable decision/read-back receipt.

The role vocabulary is represented in Vontology.  Short-lived delegations and
effect receipts are transactional operational records linked back to those
represented roles; they are not domain assertions and are therefore kept in
fit-for-purpose collections.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar

from flask import has_request_context
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from ..db.mongo_client import (
    get_ontology_authority_delegations_collection,
    get_ontology_mutation_receipts_collection,
)
from ..db.repositories.concepts_repository import ConceptsRepository
from ..db.repositories.text_value_repository import (
    TextRelationsRepository,
    TextValuesRepository,
)
from ..security.access_control import (
    bypass_access_control,
    can_access_concept,
    get_effective_organisation_concept_id,
    get_effective_user_concept_id,
    override_current_actor,
)
from ..security.visibility_predicates import (
    SPECIFIC_TO_ORG_PREDICATES_READ,
    SPECIFIC_TO_USER_PREDICATES,
    get_specific_to_org_values,
    get_specific_to_user_values,
)

logger = logging.getLogger(__name__)

AUTHORITY_SCHEMA_VERSION = "ontology_publication_authority.v1"
DELEGATION_SCHEMA_VERSION = "ontology_authority_delegation.v1"
RECEIPT_SCHEMA_VERSION = "ontology_mutation_receipt.v1"

AUTHORITY_ROLE_PREDICATE = "#V#has_ontology_authority_role"
ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE = "organisation_ontology_administrator"
GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE = "global_ontology_administrator"
VON_ADMINISTRATOR_CONCEPT_ID = "#V#von_administrator"
_ORGANISATION_ROLE_STORAGE_SEPARATOR = "::"

DEFAULT_AGENT_DELEGATION_TTL_SECONDS = 300
MAX_INTERACTIVE_DELEGATION_TTL_SECONDS = 900
DEFAULT_MUTATION_RESOURCE_LOCK_SECONDS = 300

RESERVED_AUTHORITY_PREDICATES = frozenset(
    {
        AUTHORITY_ROLE_PREDICATE,
        "#V#has_von_operational_administrator",
        "#V#hasRole",
        "#V#memberOf",
        "memberOf",
        "#V#member_of_organisation",
    }
)
RESERVED_SCOPE_PREDICATES = frozenset(
    {
        *SPECIFIC_TO_USER_PREDICATES,
        *SPECIFIC_TO_ORG_PREDICATES_READ,
    }
)
RESERVED_GENERIC_MUTATION_PREDICATES = (
    RESERVED_AUTHORITY_PREDICATES | RESERVED_SCOPE_PREDICATES
)


class PublicationContextKind(StrEnum):
    USER = "user"
    ORGANISATION = "organisation"
    GLOBAL = "global"
    HISTORICAL = "historical"
    MIXED = "mixed"


class OntologyMutationResourceBusy(RuntimeError):
    """Another governed effect currently owns an exact mutation resource."""


@dataclass(frozen=True)
class PublicationContext:
    kind: PublicationContextKind
    concept_id: str | None = None
    source: str = "resolved"
    historical_predicates: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "concept_id": self.concept_id,
            "source": self.source,
        }
        if self.historical_predicates:
            payload["historical_predicates"] = list(self.historical_predicates)
        return payload

    @classmethod
    def global_context(cls, *, source: str = "explicit") -> PublicationContext:
        return cls(PublicationContextKind.GLOBAL, None, source)

    @classmethod
    def organisation(
        cls,
        organisation_concept_id: str,
        *,
        source: str = "explicit",
    ) -> PublicationContext:
        return cls(
            PublicationContextKind.ORGANISATION,
            _normalise_concept_id(organisation_concept_id),
            source,
        )

    @classmethod
    def user(
        cls,
        user_concept_id: str,
        *,
        source: str = "explicit",
    ) -> PublicationContext:
        return cls(
            PublicationContextKind.USER,
            _normalise_concept_id(user_concept_id),
            source,
        )


@dataclass(frozen=True)
class OntologyMutationIntent:
    operation: str
    publication_context: PublicationContext
    target_concept_ids: tuple[str, ...]
    tool_name: str | None = None
    predicate: str | None = None
    source_contexts: tuple[PublicationContext, ...] = ()
    delta: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "operation": _clean_text(self.operation),
            "publication_context": self.publication_context.to_mapping(),
            "source_contexts": [item.to_mapping() for item in self.source_contexts],
            "target_concept_ids": sorted(
                {
                    value
                    for value in (
                        _normalise_concept_id(item) for item in self.target_concept_ids
                    )
                    if value
                }
            ),
            "tool_name": _clean_text(self.tool_name) or None,
            "predicate": _normalise_predicate(self.predicate),
            "delta": _json_safe(dict(self.delta)),
        }

    @property
    def fingerprint(self) -> str:
        return _sha256_json(self.canonical_payload())


@dataclass(frozen=True)
class OntologyInvocationContext:
    surface: str
    executing_agent_concept_id: str | None = None
    audience: str | None = None
    delegation_id: str | None = None
    effect_id: str | None = None
    turn_id: str | None = None
    workflow_id: str | None = None


@dataclass(frozen=True)
class AuthorityRoleEvidence:
    role: str
    actor_concept_id: str
    organisation_concept_id: str | None
    relation_id: str
    revision: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "actor_concept_id": self.actor_concept_id,
            "organisation_concept_id": self.organisation_concept_id,
            "relation_id": self.relation_id,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class OntologyAuthorityDecision:
    allowed: bool
    decision_id: str
    reason_code: str
    message: str
    actor_concept_id: str | None
    organisation_concept_id: str | None
    intent_fingerprint: str
    role_evidence: tuple[AuthorityRoleEvidence, ...] = ()
    delegation_id: str | None = None
    executing_agent_concept_id: str | None = None
    audience: str | None = None
    trust_source: str | None = None

    def public_projection(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "allowed": self.allowed,
            "reason_code": self.reason_code,
            "actor_concept_id": self.actor_concept_id,
            "organisation_concept_id": self.organisation_concept_id,
            "executing_agent_concept_id": self.executing_agent_concept_id,
            "delegation_id": self.delegation_id,
            "audience": self.audience,
            "intent_fingerprint": self.intent_fingerprint,
            "authority_evidence": [
                {
                    "role": item.role,
                    "organisation_concept_id": item.organisation_concept_id,
                    "relation_id": item.relation_id,
                    "revision": item.revision,
                }
                for item in self.role_evidence
            ],
        }
        return payload


@dataclass(frozen=True)
class MutationReceiptHandle:
    receipt_id: str
    replay: bool = False
    stored_response: Mapping[str, Any] | None = None
    stored_receipt: Mapping[str, Any] | None = None
    existing_status: str | None = None
    intent_conflict: bool = False


_ONTOLOGY_INVOCATION: ContextVar[OntologyInvocationContext | None] = ContextVar(
    "ontology_publication_invocation",
    default=None,
)
_AUTHORISED_MUTATION_DECISION: ContextVar[OntologyAuthorityDecision | None] = (
    ContextVar(
        "ontology_authorised_mutation_decision",
        default=None,
    )
)
_AUTHORISED_MUTATION_INTENT: ContextVar[OntologyMutationIntent | None] = ContextVar(
    "ontology_authorised_mutation_intent",
    default=None,
)

T = TypeVar("T")


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """Normalise PyMongo's commonly naive UTC datetimes for comparisons."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalise_concept_id(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return cleaned if cleaned.startswith("#") else f"#V#{cleaned}"


def _normalise_predicate(value: Any) -> str | None:
    cleaned = _clean_text(value)
    if not cleaned:
        return None
    return cleaned


def authority_role_storage_text(
    role: str,
    organisation_concept_id: str | None = None,
) -> str:
    """Return a scope-distinct text token for one represented authority role.

    Text-relation uniqueness is subject/predicate/object rather than context.
    Including the exact organisation in the object token therefore permits one
    person to administer more than one organisation without overwriting a
    prior role context.  Legacy plain organisation-role text remains readable.
    """

    role_value = _clean_text(role).lower()
    organisation_id = _normalise_concept_id(organisation_concept_id)
    if role_value == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        if organisation_id:
            raise ValueError("Global ontology authority cannot have an organisation")
        return role_value
    if role_value == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE:
        if not organisation_id:
            raise ValueError("Organisation ontology authority requires an organisation")
        return f"{role_value}{_ORGANISATION_ROLE_STORAGE_SEPARATOR}{organisation_id}"
    raise ValueError("Unsupported ontology authority role")


def parse_authority_role_storage_text(
    stored_text: object,
    relation_context: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """Decode current scope-distinct and legacy single-organisation role text."""

    raw_text = _clean_text(stored_text)
    text = raw_text.lower()
    context = relation_context if isinstance(relation_context, Mapping) else {}
    context_org = _normalise_concept_id(
        context.get("organisation_concept_id") or context.get("organisation_id")
    )
    if text == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE:
        # A global role has no organisation context.  Treating an org-scoped
        # row as global would turn malformed or forged context into broader
        # authority.
        if context_org:
            return None, None
        return GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE, None
    prefix = (
        ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE + _ORGANISATION_ROLE_STORAGE_SEPARATOR
    )
    if text.startswith(prefix):
        stored_org = _normalise_concept_id(raw_text[len(prefix) :])
        if not stored_org or (context_org and context_org != stored_org):
            return None, None
        return ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE, stored_org
    if text == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE and context_org:
        return ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE, context_org
    return None, None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_json_safe(item) for item in value]
    return str(value)


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        _json_safe(value),
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _new_identifier(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def _release_identity() -> dict[str, Any]:
    return {
        "runtime_code_version": _clean_text(os.getenv("VON_RUNTIME_CODE_VERSION"))
        or None,
        "release_id": _clean_text(os.getenv("VON_RELEASE_ID")) or None,
        "deployment": _clean_text(os.getenv("VON_DEPLOYMENT_NAME")) or None,
    }


@contextmanager
def bind_ontology_invocation(
    *,
    surface: str,
    executing_agent_concept_id: str | None = None,
    audience: str | None = None,
    delegation_id: str | None = None,
    effect_id: str | None = None,
    turn_id: str | None = None,
    workflow_id: str | None = None,
) -> Iterator[OntologyInvocationContext]:
    """Bind server-established execution provenance around one exact effect."""

    invocation = OntologyInvocationContext(
        surface=_clean_text(surface) or "unknown",
        executing_agent_concept_id=_normalise_concept_id(executing_agent_concept_id),
        audience=_clean_text(audience) or None,
        delegation_id=_clean_text(delegation_id) or None,
        effect_id=_clean_text(effect_id) or None,
        turn_id=_clean_text(turn_id) or None,
        workflow_id=_normalise_concept_id(workflow_id)
        or _clean_text(workflow_id)
        or None,
    )
    token = _ONTOLOGY_INVOCATION.set(invocation)
    try:
        yield invocation
    finally:
        _ONTOLOGY_INVOCATION.reset(token)


def current_ontology_invocation() -> OntologyInvocationContext | None:
    return _ONTOLOGY_INVOCATION.get()


@contextmanager
def bind_authorised_ontology_mutation(
    decision: OntologyAuthorityDecision,
    intent: OntologyMutationIntent | None = None,
) -> Iterator[None]:
    """Mark nested canonical primitives as covered by one exact decision."""

    if not decision.allowed:
        raise PermissionError(decision.reason_code)
    decision_token = _AUTHORISED_MUTATION_DECISION.set(decision)
    intent_token = _AUTHORISED_MUTATION_INTENT.set(intent)
    try:
        yield
    finally:
        _AUTHORISED_MUTATION_INTENT.reset(intent_token)
        _AUTHORISED_MUTATION_DECISION.reset(decision_token)


def current_authorised_ontology_mutation() -> OntologyAuthorityDecision | None:
    return _AUTHORISED_MUTATION_DECISION.get()


def current_authorised_ontology_intent() -> OntologyMutationIntent | None:
    """Return the exact intent covering the current canonical primitive."""

    return _AUTHORISED_MUTATION_INTENT.get()


def concept_publication_context(concept_id: str) -> PublicationContext:
    """Resolve publication context without treating malformed history as global."""

    normalised = _normalise_concept_id(concept_id)
    if normalised is None:
        raise ValueError("concept_id must be a non-empty concept identifier")
    with bypass_access_control():
        concept = ConceptsRepository.find_one({"concept_id": normalised})
    if not isinstance(concept, Mapping):
        raise LookupError(f"Concept '{normalised}' not found")  # noqa: TRY004

    relationships = concept.get("relationships")
    relationship_map = relationships if isinstance(relationships, Mapping) else {}
    user_ids = tuple(get_specific_to_user_values(dict(relationship_map)))
    organisation_ids = tuple(get_specific_to_org_values(dict(relationship_map)))
    historical_predicates: list[str] = []
    for predicate in (*SPECIFIC_TO_USER_PREDICATES, *SPECIFIC_TO_ORG_PREDICATES_READ):
        if predicate not in relationship_map:
            continue
        raw = relationship_map.get(predicate)
        raw_values = raw if isinstance(raw, list) else [raw]
        if any(
            isinstance(item, str)
            and item.strip()
            and not item.strip().startswith("#V#")
            for item in raw_values
        ):
            historical_predicates.append(predicate)

    if historical_predicates:
        return PublicationContext(
            PublicationContextKind.HISTORICAL,
            None,
            "historical_visibility",
            tuple(sorted(set(historical_predicates))),
        )
    if user_ids and organisation_ids:
        # Visibility shared by a user and organisation is not evidence that the
        # organisation owns canonical publication. Historical dual-scope rows
        # need explicit administrator adoption instead of silent authority
        # inference.
        return PublicationContext(
            PublicationContextKind.MIXED,
            None,
            "multi_principal_visibility",
        )
    if len(user_ids) == 1:
        return PublicationContext.user(user_ids[0], source="concept_visibility")
    if len(organisation_ids) == 1:
        return PublicationContext.organisation(
            organisation_ids[0],
            source="concept_visibility",
        )
    if len(user_ids) > 1 or len(organisation_ids) > 1:
        return PublicationContext(
            PublicationContextKind.MIXED,
            None,
            "multi_principal_visibility",
        )
    return PublicationContext.global_context(source="concept_visibility")


def publication_context_for_creation(
    *,
    scope_mode: str | None,
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
) -> PublicationContext:
    mode = (_clean_text(scope_mode) or "user_org_default").lower().replace("-", "_")
    if mode in {"global", "global_general", "public"}:
        return PublicationContext.global_context(source="creation_scope")
    if mode in {
        "organisation_general",
        "organization_general",
        "org_general",
    }:
        organisation_id = _normalise_concept_id(organisation_concept_id)
        if organisation_id is None:
            raise ValueError("organisation_general requires an organisation")
        return PublicationContext.organisation(
            organisation_id,
            source="creation_scope",
        )
    actor_id = _normalise_concept_id(actor_concept_id)
    if actor_id is None:
        raise ValueError("Authenticated actor required for default concept scope")
    if mode in {"private", "user_only", "user_only_default"}:
        return PublicationContext.user(actor_id, source="creation_scope_user_only")
    if mode in {"user_org_default", "user_org", "default"}:
        organisation_id = _normalise_concept_id(organisation_concept_id)
        if organisation_id is not None:
            return PublicationContext.organisation(
                organisation_id,
                source="creation_scope_user_org_default",
            )
    return PublicationContext.user(actor_id, source="creation_scope")


def _relation_revision(relation: Mapping[str, Any], role_text: str) -> str:
    return _sha256_json(
        {
            "relation_id": relation.get("_id"),
            "subject_concept_id": relation.get("subject_concept_id"),
            "predicate": relation.get("predicate"),
            "object_text_id": relation.get("object_text_id"),
            "context": relation.get("context"),
            "role": role_text,
            "updated_at": relation.get("updated_at"),
        }
    )


def resolve_live_semantic_roles(
    actor_concept_id: str,
) -> tuple[AuthorityRoleEvidence, ...]:
    """Read represented semantic roles afresh for every authority decision."""

    actor_id = _normalise_concept_id(actor_concept_id)
    if actor_id is None:
        return ()
    relations = TextRelationsRepository.find(
        {
            "subject_concept_id": actor_id,
            "predicate": AUTHORITY_ROLE_PREDICATE,
        }
    )
    evidence: list[AuthorityRoleEvidence] = []
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        object_text_id = relation.get("object_text_id")
        if object_text_id is None:
            continue
        text_value = TextValuesRepository.find_one({"_id": object_text_id})
        if not isinstance(text_value, Mapping):
            continue
        context = relation.get("context")
        context_map = context if isinstance(context, Mapping) else {}
        role, organisation_id = parse_authority_role_storage_text(
            text_value.get("text"),
            context_map,
        )
        if not role:
            continue
        evidence.append(
            AuthorityRoleEvidence(
                role=role,
                actor_concept_id=actor_id,
                organisation_concept_id=organisation_id,
                relation_id=str(relation.get("_id") or ""),
                revision=_relation_revision(
                    relation,
                    _clean_text(text_value.get("text")),
                ),
            )
        )
    evidence.sort(
        key=lambda item: (
            item.role,
            item.organisation_concept_id or "",
            item.relation_id,
        )
    )
    return tuple(evidence)


def _role_evidence_for_context(
    actor_concept_id: str,
    publication_context: PublicationContext,
) -> tuple[AuthorityRoleEvidence, ...]:
    roles = resolve_live_semantic_roles(actor_concept_id)
    if publication_context.kind == PublicationContextKind.GLOBAL:
        return tuple(
            role for role in roles if role.role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
        )
    if publication_context.kind == PublicationContextKind.ORGANISATION:
        return tuple(
            role
            for role in roles
            if role.role == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE
            and role.organisation_concept_id == publication_context.concept_id
        )
    return ()


def _organisation_authority_root_exists(organisation_concept_id: str) -> bool:
    organisation_id = _normalise_concept_id(organisation_concept_id)
    if not organisation_id:
        return False
    relations = TextRelationsRepository.find({"predicate": AUTHORITY_ROLE_PREDICATE})
    for relation in relations:
        if not isinstance(relation, Mapping):
            continue
        text_value = TextValuesRepository.find_one(
            {"_id": relation.get("object_text_id")}
        )
        if not isinstance(text_value, Mapping):
            continue
        role, role_org = parse_authority_role_storage_text(
            text_value.get("text"),
            relation.get("context")
            if isinstance(relation.get("context"), Mapping)
            else {},
        )
        if (
            role == ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE
            and role_org == organisation_id
        ):
            return True
    return False


def _gateway_actor_trust_source() -> str | None:
    try:
        from ..integrations.internal_mcp.gateway import (
            get_internal_mcp_actor_context_source,
        )

        return get_internal_mcp_actor_context_source()
    except Exception:  # noqa: BLE001 - optional integration provenance probe
        return None


def _decision(
    *,
    allowed: bool,
    reason_code: str,
    message: str,
    intent: OntologyMutationIntent,
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
    role_evidence: Sequence[AuthorityRoleEvidence] = (),
    delegation_id: str | None = None,
    invocation: OntologyInvocationContext | None = None,
    trust_source: str | None = None,
) -> OntologyAuthorityDecision:
    return OntologyAuthorityDecision(
        allowed=allowed,
        decision_id=_new_identifier("oad"),
        reason_code=reason_code,
        message=message,
        actor_concept_id=_normalise_concept_id(actor_concept_id),
        organisation_concept_id=_normalise_concept_id(organisation_concept_id),
        intent_fingerprint=intent.fingerprint,
        role_evidence=tuple(role_evidence),
        delegation_id=_clean_text(delegation_id) or None,
        executing_agent_concept_id=(
            invocation.executing_agent_concept_id if invocation else None
        ),
        audience=invocation.audience if invocation else None,
        trust_source=trust_source,
    )


def _direct_authority_decision(
    *,
    intent: OntologyMutationIntent,
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
    invocation: OntologyInvocationContext | None,
    trust_source: str | None,
) -> OntologyAuthorityDecision:
    actor_id = _normalise_concept_id(actor_concept_id)
    active_org_id = _normalise_concept_id(organisation_concept_id)
    if actor_id is None:
        return _decision(
            allowed=False,
            reason_code="authenticated_actor_context_required",
            message="Canonical ontology publication requires a trusted actor.",
            intent=intent,
            actor_concept_id=None,
            organisation_concept_id=active_org_id,
            invocation=invocation,
            trust_source=trust_source,
        )

    predicate = _normalise_predicate(intent.predicate)
    dedicated_authority_operation = intent.operation in {
        "authority.role.grant",
        "authority.role.revoke",
    }
    if predicate in RESERVED_GENERIC_MUTATION_PREDICATES and not (
        dedicated_authority_operation and predicate == AUTHORITY_ROLE_PREDICATE
    ):
        return _decision(
            allowed=False,
            reason_code="dedicated_ontology_governance_operation_required",
            message=(
                "Authority, membership, and visibility predicates may only be "
                "changed through their dedicated governed operations."
            ),
            intent=intent,
            actor_concept_id=actor_id,
            organisation_concept_id=active_org_id,
            invocation=invocation,
            trust_source=trust_source,
        )

    contexts = (*intent.source_contexts, intent.publication_context)
    unresolved_contexts = tuple(
        item
        for item in contexts
        if item.kind
        in {PublicationContextKind.HISTORICAL, PublicationContextKind.MIXED}
    )
    if unresolved_contexts and intent.operation != "scope.change":
        return _decision(
            allowed=False,
            reason_code="explicit_scope_adoption_required",
            message=(
                "Historical or mixed scope must be previewed and adopted through "
                "the dedicated scope-change operation before canonical mutation."
            ),
            intent=intent,
            actor_concept_id=actor_id,
            organisation_concept_id=active_org_id,
            invocation=invocation,
            trust_source=trust_source,
        )

    # A dedicated, optimistic scope adoption is the one operation that may
    # start from legacy/mixed visibility.  It requires global semantic
    # authority over that unresolved source as well as authority over the
    # explicit destination.  This avoids silently treating malformed history
    # as global publication while still leaving a controlled repair path.
    historical_adoption_evidence: tuple[AuthorityRoleEvidence, ...] = ()
    if unresolved_contexts:
        historical_adoption_evidence = tuple(
            role
            for role in resolve_live_semantic_roles(actor_id)
            if role.role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
        )
        if not historical_adoption_evidence:
            return _decision(
                allowed=False,
                reason_code="global_ontology_admin_authority_required",
                message=(
                    "Global ontology authority is required to adopt historical "
                    "or mixed publication scope."
                ),
                intent=intent,
                actor_concept_id=actor_id,
                organisation_concept_id=active_org_id,
                invocation=invocation,
                trust_source=trust_source,
            )

    gathered: list[AuthorityRoleEvidence] = list(historical_adoption_evidence)
    seen_contexts: set[tuple[str, str | None]] = set()
    for context in contexts:
        if context.kind in {
            PublicationContextKind.HISTORICAL,
            PublicationContextKind.MIXED,
        }:
            continue
        key = (context.kind.value, context.concept_id)
        if key in seen_contexts:
            continue
        seen_contexts.add(key)
        if (
            context.kind == PublicationContextKind.USER
            and context.concept_id == actor_id
        ):
            # Preserve the pre-existing private-owner capability. It authorises
            # only the actor's exact user context; any inverse target or
            # destination context is still checked independently below.
            continue
        context_evidence = _role_evidence_for_context(actor_id, context)
        if (
            not context_evidence
            and intent.operation == "authority.role.grant"
            and context.kind == PublicationContextKind.ORGANISATION
            and context.concept_id
            and not _organisation_authority_root_exists(context.concept_id)
        ):
            # After the single global bootstrap, a represented global semantic
            # administrator may establish the first administrator for an
            # actor-visible organisation. This is a root-creation ceremony,
            # not standing global authority over the organisation: once the
            # root exists, exact organisation authority is required.
            context_evidence = tuple(
                role
                for role in resolve_live_semantic_roles(actor_id)
                if role.role == GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE
            )
        if not context_evidence:
            if context.kind == PublicationContextKind.GLOBAL:
                reason_code = "global_ontology_admin_authority_required"
                message = "Global ontology publication authority is required."
            elif context.kind == PublicationContextKind.ORGANISATION:
                reason_code = "organisation_ontology_admin_authority_required"
                message = (
                    "Ontology publication authority for the exact organisation "
                    "context is required."
                )
            elif context.kind == PublicationContextKind.USER:
                reason_code = "private_scope_canonical_publication_not_delegated"
                message = (
                    "Canonical mutation requires ownership of the exact private "
                    "publication context."
                )
            else:
                reason_code = "ontology_publication_authority_required"
                message = "Ontology publication authority is required."
            return _decision(
                allowed=False,
                reason_code=reason_code,
                message=message,
                intent=intent,
                actor_concept_id=actor_id,
                organisation_concept_id=active_org_id,
                invocation=invocation,
                trust_source=trust_source,
            )
        gathered.extend(context_evidence)

    return _decision(
        allowed=True,
        reason_code="semantic_ontology_authority_verified",
        message="Live semantic ontology authority verified.",
        intent=intent,
        actor_concept_id=actor_id,
        organisation_concept_id=active_org_id,
        role_evidence=gathered,
        invocation=invocation,
        trust_source=trust_source,
    )


def _delegation_collection():
    collection = get_ontology_authority_delegations_collection()
    if collection is None:
        raise RuntimeError("ontology authority delegation store unavailable")
    return collection


def _receipt_collection():
    collection = get_ontology_mutation_receipts_collection()
    if collection is None:
        raise RuntimeError("ontology mutation receipt store unavailable")
    return collection


@contextmanager
def ontology_mutation_resource_lock(
    resource_key: str,
    *,
    lease_seconds: int = DEFAULT_MUTATION_RESOURCE_LOCK_SECONDS,
) -> Iterator[None]:
    """Hold a short cross-process lease for one authority-sensitive resource.

    Receipt persistence is already a prerequisite for every governed mutation,
    so its collection also owns these small, explicitly non-receipt lease
    records.  The lease prevents two distinct request IDs from passing the same
    read/check/write boundary concurrently.  It never grants authority and an
    abandoned lease expires without operator intervention.
    """

    cleaned_key = _clean_text(resource_key)
    if not cleaned_key:
        raise ValueError("ontology mutation resource key is required")
    try:
        lease_length = max(1, min(int(lease_seconds), 900))
    except (TypeError, ValueError) as exc:
        raise ValueError("ontology mutation lock lease must be an integer") from exc

    collection = _receipt_collection()
    lock_id = "oml_" + hashlib.sha256(cleaned_key.encode("utf-8")).hexdigest()
    owner_token = secrets.token_urlsafe(24)
    now = _now()
    expires_at = now + timedelta(seconds=lease_length)
    try:
        claimed = collection.find_one_and_update(
            {
                "_id": lock_id,
                "$or": [
                    {"lock_owner_token": {"$exists": False}},
                    {"lock_expires_at": {"$lte": now}},
                ],
            },
            {
                "$setOnInsert": {
                    "schema_version": "ontology_mutation_resource_lock.v1",
                    "receipt_id": lock_id,
                    "record_kind": "resource_lock",
                    "created_at": now,
                },
                "$set": {
                    "resource_key_sha256": hashlib.sha256(
                        cleaned_key.encode("utf-8")
                    ).hexdigest(),
                    "lock_owner_token": owner_token,
                    "lock_expires_at": expires_at,
                    "updated_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise OntologyMutationResourceBusy("ontology_mutation_resource_busy") from exc
    if (
        not isinstance(claimed, Mapping)
        or claimed.get("lock_owner_token") != owner_token
    ):
        raise OntologyMutationResourceBusy("ontology_mutation_resource_busy")

    try:
        yield
    finally:
        released = collection.delete_one(
            {"_id": lock_id, "lock_owner_token": owner_token}
        )
        if int(getattr(released, "deleted_count", 0) or 0) != 1:
            raise RuntimeError("ontology mutation resource lock release failed")


def issue_agent_delegation(
    *,
    intent: OntologyMutationIntent,
    grantor_actor_concept_id: str,
    grantor_organisation_concept_id: str | None,
    delegate_concept_id: str,
    audience: str,
    tool_name: str,
    effect_id: str,
    ttl_seconds: int = DEFAULT_AGENT_DELEGATION_TTL_SECONDS,
    turn_id: str | None = None,
    workflow_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Issue one non-amplifying, exact, interactive agent delegation."""

    issued_at = now or _now()
    if issued_at.tzinfo is None:
        issued_at = issued_at.replace(tzinfo=UTC)
    ttl = int(ttl_seconds)
    if ttl <= 0 or ttl > MAX_INTERACTIVE_DELEGATION_TTL_SECONDS:
        raise ValueError(
            "Interactive ontology delegation ttl_seconds must be between 1 and "
            f"{MAX_INTERACTIVE_DELEGATION_TTL_SECONDS}."
        )
    delegate_id = _normalise_concept_id(delegate_concept_id)
    grantor_id = _normalise_concept_id(grantor_actor_concept_id)
    if not delegate_id or not grantor_id:
        raise ValueError("grantor and delegate concept identifiers are required")
    audience_value = _clean_text(audience)
    tool_value = _clean_text(tool_name)
    effect_value = _clean_text(effect_id)
    if not audience_value or not tool_value or not effect_value:
        raise ValueError("audience, tool_name, and effect_id are required")

    direct = _direct_authority_decision(
        intent=intent,
        actor_concept_id=grantor_id,
        organisation_concept_id=grantor_organisation_concept_id,
        invocation=None,
        trust_source="server_issued_delegation",
    )
    if not direct.allowed:
        raise PermissionError(direct.reason_code)

    collection = _delegation_collection()
    exact_grant_key = _sha256_json(
        {
            "grantor_actor_concept_id": grantor_id,
            "delegate_concept_id": delegate_id,
            "audience": audience_value,
            "effect_id": effect_value,
            "turn_id": _clean_text(turn_id) or None,
            "workflow_id": _normalise_concept_id(workflow_id)
            or _clean_text(workflow_id)
            or None,
        }
    )
    document = {
        "schema_version": DELEGATION_SCHEMA_VERSION,
        "delegation_id": f"oag_{exact_grant_key}",
        "exact_grant_key": exact_grant_key,
        "grantor_actor_concept_id": grantor_id,
        "grantor_organisation_concept_id": _normalise_concept_id(
            grantor_organisation_concept_id
        ),
        "delegate_concept_id": delegate_id,
        "audience": audience_value,
        "tool_name": tool_value,
        "effect_id": effect_value,
        "turn_id": _clean_text(turn_id) or None,
        "workflow_id": _normalise_concept_id(workflow_id)
        or _clean_text(workflow_id)
        or None,
        "intent_fingerprint": intent.fingerprint,
        "intent": intent.canonical_payload(),
        "authority_evidence": [item.to_mapping() for item in direct.role_evidence],
        "authority_decision_id": direct.decision_id,
        "issued_at": issued_at,
        "expires_at": issued_at + timedelta(seconds=ttl),
        "status": "active",
        "revoked_at": None,
        "revoked_by_actor_concept_id": None,
        "revocation_reason": None,
        "non_recursive": True,
        "standing_delegation": False,
    }
    try:
        collection.insert_one(document)
    except DuplicateKeyError:
        existing = collection.find_one({"delegation_id": document["delegation_id"]})
        if not isinstance(existing, Mapping):
            raise
        if (
            existing.get("status") == "active"
            and isinstance(existing.get("expires_at"), datetime)
            and _as_utc(existing["expires_at"]) > issued_at
        ):
            if (
                _clean_text(existing.get("tool_name")) == tool_value
                and _clean_text(existing.get("intent_fingerprint"))
                == intent.fingerprint
            ):
                return _public_delegation_projection(existing)
            raise PermissionError("ontology_delegation_effect_intent_conflict")
        # A revoked/expired exact effect cannot be silently re-issued under the
        # same identity; issue a new effect ID instead.
        raise PermissionError("ontology_delegation_effect_already_finalised")
    stored = collection.find_one({"delegation_id": document["delegation_id"]})
    if not isinstance(stored, Mapping):
        raise LookupError("Delegation canonical read-back failed")  # noqa: TRY004
    return _public_delegation_projection(stored)


def _public_delegation_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": str(document.get("schema_version") or ""),
        "delegation_id": str(document.get("delegation_id") or ""),
        "grantor_actor_concept_id": document.get("grantor_actor_concept_id"),
        "grantor_organisation_concept_id": document.get(
            "grantor_organisation_concept_id"
        ),
        "delegate_concept_id": document.get("delegate_concept_id"),
        "audience": document.get("audience"),
        "tool_name": document.get("tool_name"),
        "effect_id": document.get("effect_id"),
        "turn_id": document.get("turn_id"),
        "workflow_id": document.get("workflow_id"),
        "intent_fingerprint": document.get("intent_fingerprint"),
        "publication_context": (
            (document.get("intent") or {}).get("publication_context")
            if isinstance(document.get("intent"), Mapping)
            else None
        ),
        "target_concept_ids": (
            list((document.get("intent") or {}).get("target_concept_ids") or [])
            if isinstance(document.get("intent"), Mapping)
            else []
        ),
        "operation": (
            (document.get("intent") or {}).get("operation")
            if isinstance(document.get("intent"), Mapping)
            else None
        ),
        "issued_at": _json_safe(document.get("issued_at")),
        "expires_at": _json_safe(document.get("expires_at")),
        "status": document.get("status"),
        "non_recursive": document.get("non_recursive") is True,
        "standing_delegation": document.get("standing_delegation") is True,
    }


def resolve_delegation_principal(
    delegation_id: str | None,
    *,
    delegate_concept_id: str | None = None,
    audience: str | None = None,
    tool_name: str | None = None,
    effect_id: str | None = None,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Resolve only the stored grantor context used to rebuild an exact intent.

    This is not an authority decision: callers must still bind the opaque
    delegation and pass through :func:`verify_agent_delegation`.  It exists for
    sessionless server surfaces such as stdio, where accepting a client-supplied
    actor would be unsafe but rebuilding the originally granted creation scope
    requires the server-stored principal.
    """

    delegation_value = _clean_text(delegation_id)
    if not delegation_value:
        return None, None
    delegate_id = _normalise_concept_id(delegate_concept_id)
    audience_value = _clean_text(audience)
    tool_value = _clean_text(tool_name)
    effect_value = _clean_text(effect_id)
    if not all((delegate_id, audience_value, tool_value, effect_value)):
        return None, None
    checked_at = _as_utc(now or _now())
    try:
        document = _delegation_collection().find_one(
            {
                "delegation_id": delegation_value,
                "status": "active",
                "expires_at": {"$gt": checked_at},
                "delegate_concept_id": delegate_id,
                "audience": audience_value,
                "tool_name": tool_value,
                "effect_id": effect_value,
            },
            {
                "grantor_actor_concept_id": 1,
                "grantor_organisation_concept_id": 1,
            },
        )
    except RuntimeError:
        # Intent rebuilding can proceed actorless; the later authoritative
        # verification still fails closed with store-unavailable semantics.
        return None, None
    if not isinstance(document, Mapping):
        return None, None
    return (
        _normalise_concept_id(document.get("grantor_actor_concept_id")),
        _normalise_concept_id(document.get("grantor_organisation_concept_id")),
    )


def verify_agent_delegation(
    *,
    delegation_id: str,
    intent: OntologyMutationIntent,
    invocation: OntologyInvocationContext,
    actor_concept_id: str | None,
    organisation_concept_id: str | None,
    now: datetime | None = None,
) -> OntologyAuthorityDecision:
    checked_at = _as_utc(now or _now())
    collection = _delegation_collection()
    delegation = collection.find_one({"delegation_id": _clean_text(delegation_id)})
    if not isinstance(delegation, Mapping):
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_invalid",
            message="The ontology delegation is invalid or unavailable.",
            intent=intent,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation",
        )
    if delegation.get("status") != "active":
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_revoked",
            message="The ontology delegation is no longer active.",
            intent=intent,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation",
        )
    expires_at = delegation.get("expires_at")
    if not isinstance(expires_at, datetime) or _as_utc(expires_at) <= checked_at:
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_expired",
            message="The ontology delegation has expired.",
            intent=intent,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation",
        )

    exact_pairs = {
        "delegate_concept_id": invocation.executing_agent_concept_id,
        "audience": invocation.audience,
        "tool_name": intent.tool_name,
        "effect_id": invocation.effect_id,
        "intent_fingerprint": intent.fingerprint,
    }
    for field_name, expected in exact_pairs.items():
        if _clean_text(delegation.get(field_name)) != _clean_text(expected):
            return _decision(
                allowed=False,
                reason_code=f"ontology_delegation_{field_name}_mismatch",
                message="The ontology delegation does not cover this exact effect.",
                intent=intent,
                actor_concept_id=actor_concept_id,
                organisation_concept_id=organisation_concept_id,
                delegation_id=delegation_id,
                invocation=invocation,
                trust_source="server_delegation",
            )
    expected_turn_id = _clean_text(delegation.get("turn_id"))
    if expected_turn_id and expected_turn_id != _clean_text(invocation.turn_id):
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_turn_mismatch",
            message="The ontology delegation does not cover this turn.",
            intent=intent,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation",
        )
    expected_workflow_id = _clean_text(delegation.get("workflow_id"))
    if expected_workflow_id and expected_workflow_id != _clean_text(
        invocation.workflow_id
    ):
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_workflow_mismatch",
            message="The ontology delegation does not cover this workflow.",
            intent=intent,
            actor_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation",
        )

    grantor_id = _normalise_concept_id(delegation.get("grantor_actor_concept_id"))
    grantor_org_id = _normalise_concept_id(
        delegation.get("grantor_organisation_concept_id")
    )
    supplied_actor_id = _normalise_concept_id(actor_concept_id)
    if supplied_actor_id is not None and supplied_actor_id != grantor_id:
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_actor_mismatch",
            message="The ontology delegation belongs to another actor.",
            intent=intent,
            actor_concept_id=supplied_actor_id,
            organisation_concept_id=organisation_concept_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation",
        )

    live = _direct_authority_decision(
        intent=intent,
        actor_concept_id=grantor_id,
        organisation_concept_id=grantor_org_id,
        invocation=invocation,
        trust_source="server_delegation_live_recheck",
    )
    if not live.allowed:
        return _decision(
            allowed=False,
            reason_code="ontology_delegation_grantor_authority_revoked",
            message="The delegator no longer has the authority required.",
            intent=intent,
            actor_concept_id=grantor_id,
            organisation_concept_id=grantor_org_id,
            delegation_id=delegation_id,
            invocation=invocation,
            trust_source="server_delegation_live_recheck",
        )
    return _decision(
        allowed=True,
        reason_code="ontology_agent_delegation_verified",
        message="Exact live ontology agent delegation verified.",
        intent=intent,
        actor_concept_id=grantor_id,
        organisation_concept_id=grantor_org_id,
        role_evidence=live.role_evidence,
        delegation_id=delegation_id,
        invocation=invocation,
        trust_source="server_delegation_live_recheck",
    )


def authorise_ontology_mutation(
    intent: OntologyMutationIntent,
) -> OntologyAuthorityDecision:
    """Resolve direct or delegated semantic authority from trusted context."""

    invocation = current_ontology_invocation()
    actor_id = get_effective_user_concept_id()
    organisation_id = get_effective_organisation_concept_id()
    trust_source = _gateway_actor_trust_source()

    if invocation and invocation.delegation_id:
        try:
            return verify_agent_delegation(
                delegation_id=invocation.delegation_id,
                intent=intent,
                invocation=invocation,
                actor_concept_id=actor_id,
                organisation_concept_id=organisation_id,
            )
        except RuntimeError:
            return _decision(
                allowed=False,
                reason_code="ontology_authority_store_unavailable",
                message="Ontology delegation verification is unavailable.",
                intent=intent,
                actor_concept_id=actor_id,
                organisation_concept_id=organisation_id,
                delegation_id=invocation.delegation_id,
                invocation=invocation,
                trust_source=trust_source,
            )

    if invocation and invocation.executing_agent_concept_id:
        return _decision(
            allowed=False,
            reason_code="ontology_agent_delegation_required",
            message=(
                "An executing agent needs a server-issued delegation bound to "
                "this exact ontology effect."
            ),
            intent=intent,
            actor_concept_id=actor_id,
            organisation_concept_id=organisation_id,
            invocation=invocation,
            trust_source=trust_source,
        )

    if trust_source == "tool_payload_fallback":
        return _decision(
            allowed=False,
            reason_code="client_supplied_identity_is_not_authority",
            message="Client or model supplied identity cannot authorise publication.",
            intent=intent,
            actor_concept_id=None,
            organisation_concept_id=None,
            invocation=invocation,
            trust_source=trust_source,
        )
    if trust_source == "trusted_operator_payload_fallback":
        return _decision(
            allowed=False,
            reason_code="von_operator_is_not_semantic_ontology_authority",
            message=(
                "Von operational authority does not confer semantic ontology "
                "publication authority."
            ),
            intent=intent,
            actor_concept_id=actor_id,
            organisation_concept_id=organisation_id,
            invocation=invocation,
            trust_source=trust_source,
        )

    return _direct_authority_decision(
        intent=intent,
        actor_concept_id=actor_id,
        organisation_concept_id=organisation_id,
        invocation=invocation,
        trust_source=(
            trust_source
            or (
                "authenticated_http_context"
                if has_request_context()
                else "trusted_in_process_actor"
            )
        ),
    )


def ontology_authority_resource_keys(
    intent: OntologyMutationIntent,
) -> tuple[str, ...]:
    """Return locks whose lifecycle can invalidate this exact authority."""

    keys: set[str] = set()
    invocation = current_ontology_invocation()
    if invocation and invocation.delegation_id:
        keys.add(f"ontology-delegation:{_clean_text(invocation.delegation_id)}")
    for context in (*intent.source_contexts, intent.publication_context):
        if context.kind == PublicationContextKind.ORGANISATION and context.concept_id:
            keys.add(
                "ontology-authority-role:"
                f"{ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE}:"
                f"{context.concept_id}"
            )
        elif context.kind in {
            PublicationContextKind.GLOBAL,
            PublicationContextKind.HISTORICAL,
            PublicationContextKind.MIXED,
        }:
            keys.add(
                f"ontology-authority-role:{GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE}:global"
            )
    return tuple(sorted(keys))


def ontology_authority_denial_payload(
    decision: OntologyAuthorityDecision,
    intent: OntologyMutationIntent,
) -> dict[str, Any]:
    return {
        "success": False,
        "effect_status": "not_started",
        "mutation_outcome": "not_started",
        "outcome_finality": "terminal_for_turn",
        "changed": False,
        "error_code": decision.reason_code,
        "error": decision.message,
        "authority_decision": decision.public_projection(),
        "publication_context": intent.publication_context.to_mapping(),
        "recovery_affordances": [
            {"action_type": "create_scoped_assertion"},
            {"action_type": "request_ontology_administrator_delegation"},
        ],
    }


def _bounded_projection(value: Any, *, max_chars: int = 16_000) -> Any:
    safe = _json_safe(value)
    rendered = json.dumps(safe, sort_keys=True, ensure_ascii=True)
    if len(rendered) <= max_chars:
        return safe
    return {
        "truncated": True,
        "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "size_chars": len(rendered),
    }


def _receipt_public_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": document.get("schema_version"),
        "receipt_id": document.get("receipt_id"),
        "decision_id": document.get("decision_id"),
        "status": document.get("status"),
        "changed": document.get("changed"),
        "mutation_outcome": document.get("mutation_outcome"),
        "actor_concept_id": document.get("actor_concept_id"),
        "executing_agent_concept_id": document.get("executing_agent_concept_id"),
        "delegation_id": document.get("delegation_id"),
        "publication_context": document.get("publication_context"),
        "operation": document.get("operation"),
        "target_concept_ids": list(document.get("target_concept_ids") or []),
        "intent_fingerprint": document.get("intent_fingerprint"),
        "idempotency_key": document.get("idempotency_key"),
        "created_at": _json_safe(document.get("created_at")),
        "completed_at": _json_safe(document.get("completed_at")),
        "canonical_read_back": document.get("canonical_read_back"),
        "response_projection_truncated": bool(
            document.get("response_projection_truncated")
        ),
        "response_success": document.get("response_success"),
        "response_effect_status": document.get("response_effect_status"),
        "response_error_code": document.get("response_error_code"),
    }


def begin_mutation_receipt(
    *,
    decision: OntologyAuthorityDecision,
    intent: OntologyMutationIntent,
    before_state: Any = None,
) -> MutationReceiptHandle:
    collection = _receipt_collection()
    idempotency_key = _clean_text(intent.idempotency_key) or None
    receipt_id = _new_identifier("omr")
    if idempotency_key:
        # The deterministic receipt identity makes one actor's idempotency key
        # an atomic claim even while older deployments still have the original
        # compound key/fingerprint index.  A changed intent must never turn the
        # same effect key into a second canonical write.
        receipt_id = (
            "omr_"
            + hashlib.sha256(
                f"{decision.actor_concept_id or ''}\0{idempotency_key}".encode()
            ).hexdigest()
        )
        existing = collection.find_one(
            {
                "receipt_id": receipt_id,
                "actor_concept_id": decision.actor_concept_id,
                "idempotency_key": idempotency_key,
            }
        )
        if isinstance(existing, Mapping):
            existing_status = _clean_text(existing.get("status")) or "authorised"
            return MutationReceiptHandle(
                receipt_id=str(existing.get("receipt_id") or ""),
                replay=True,
                stored_response=(
                    existing.get("response_projection")
                    if isinstance(existing.get("response_projection"), Mapping)
                    else None
                ),
                stored_receipt=_receipt_public_projection(existing),
                existing_status=existing_status,
                intent_conflict=(
                    _clean_text(existing.get("intent_fingerprint"))
                    != intent.fingerprint
                ),
            )

    now = _now()
    invocation = current_ontology_invocation()
    document: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt_id,
        "decision_id": decision.decision_id,
        "decision_reason_code": decision.reason_code,
        "status": "authorised",
        "mutation_outcome": "not_started",
        "changed": False,
        "actor_concept_id": decision.actor_concept_id,
        "organisation_concept_id": decision.organisation_concept_id,
        "executing_agent_concept_id": decision.executing_agent_concept_id,
        "delegation_id": decision.delegation_id,
        "audience": decision.audience,
        "surface": invocation.surface if invocation else None,
        "turn_id": invocation.turn_id if invocation else None,
        "workflow_id": invocation.workflow_id if invocation else None,
        "effect_id": invocation.effect_id if invocation else None,
        "operation": intent.operation,
        "tool_name": intent.tool_name,
        "predicate": intent.predicate,
        "publication_context": intent.publication_context.to_mapping(),
        "source_contexts": [item.to_mapping() for item in intent.source_contexts],
        "target_concept_ids": list(intent.target_concept_ids),
        "intent_fingerprint": intent.fingerprint,
        "authority_evidence": [item.to_mapping() for item in decision.role_evidence],
        "before_state": _bounded_projection(before_state),
        "canonical_read_back": None,
        "response_projection": None,
        "release": _release_identity(),
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
    }
    if idempotency_key:
        document["idempotency_key"] = idempotency_key
    try:
        collection.insert_one(document)
    except DuplicateKeyError:
        existing = collection.find_one(
            {
                "receipt_id": receipt_id,
                "actor_concept_id": decision.actor_concept_id,
            }
        )
        if not isinstance(existing, Mapping):
            raise
        existing_status = _clean_text(existing.get("status")) or "authorised"
        return MutationReceiptHandle(
            receipt_id=str(existing.get("receipt_id") or ""),
            replay=True,
            stored_response=(
                existing.get("response_projection")
                if isinstance(existing.get("response_projection"), Mapping)
                else None
            ),
            stored_receipt=_receipt_public_projection(existing),
            existing_status=existing_status,
            intent_conflict=(
                _clean_text(existing.get("intent_fingerprint")) != intent.fingerprint
            ),
        )
    return MutationReceiptHandle(receipt_id=document["receipt_id"])


def finalise_mutation_receipt(
    *,
    receipt_id: str,
    status: str,
    mutation_outcome: str,
    changed: bool | None,
    canonical_read_back: Any,
    response_projection: Any,
    error_code: str | None = None,
) -> dict[str, Any]:
    collection = _receipt_collection()
    now = _now()
    bounded_response = _bounded_projection(response_projection)
    updated = collection.find_one_and_update(
        {"receipt_id": receipt_id},
        {
            "$set": {
                "status": status,
                "mutation_outcome": mutation_outcome,
                "changed": changed,
                "canonical_read_back": _bounded_projection(canonical_read_back),
                "canonical_read_back_sha256": _sha256_json(canonical_read_back),
                "response_projection": bounded_response,
                "response_projection_truncated": bool(
                    isinstance(bounded_response, Mapping)
                    and bounded_response.get("truncated")
                ),
                "response_success": (
                    bool(response_projection.get("success", True))
                    if isinstance(response_projection, Mapping)
                    else False
                ),
                "response_effect_status": (
                    _clean_text(response_projection.get("effect_status")) or None
                    if isinstance(response_projection, Mapping)
                    else None
                ),
                "response_error_code": (
                    _clean_text(response_projection.get("error_code")) or None
                    if isinstance(response_projection, Mapping)
                    else None
                ),
                "error_code": _clean_text(error_code) or None,
                "updated_at": now,
                "completed_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not isinstance(updated, Mapping):
        raise LookupError(  # noqa: TRY004
            "Ontology mutation receipt read-back failed"
        )
    return _receipt_public_projection(updated)


def get_mutation_receipt_for_actor(
    *,
    receipt_id: str,
    actor_concept_id: str,
) -> dict[str, Any] | None:
    actor_id = _normalise_concept_id(actor_concept_id)
    if not actor_id:
        return None
    document = _receipt_collection().find_one(
        {"receipt_id": _clean_text(receipt_id), "actor_concept_id": actor_id}
    )
    return (
        _receipt_public_projection(document) if isinstance(document, Mapping) else None
    )


def _receipt_finalisation_failure_response(
    *,
    decision: OntologyAuthorityDecision,
    handle: MutationReceiptHandle,
    canonical_state: Any,
    exception: Exception,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Report a post-effect receipt failure without making a success claim."""

    return {
        **dict(result or {}),
        "success": False,
        "effect_status": "indeterminate",
        "mutation_outcome": "unknown",
        "outcome_finality": "requires_receipt_reconciliation",
        "changed": None,
        "retryable": False,
        "error_code": "ontology_mutation_receipt_finalisation_failed",
        "error": (
            "The effect may have occurred, but its durable receipt could not be "
            "finalised. Inspect canonical state and the existing receipt before "
            "any retry."
        ),
        "details": {"exception_type": type(exception).__name__},
        "authority_decision": decision.public_projection(),
        "authority_receipt": {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "receipt_id": handle.receipt_id,
            "status": "authorised",
            "durable_finalisation": False,
            "intent_fingerprint": decision.intent_fingerprint,
        },
        "canonical_read_back": _bounded_projection(canonical_state),
    }


def execute_authorised_ontology_mutation(
    *,
    intent: OntologyMutationIntent,
    mutate: Callable[[], Mapping[str, Any]],
    read_back: Callable[[], Any],
    read_before: Callable[[], Any] | None = None,
    verify_read_back: Callable[[Mapping[str, Any], Any], bool] | None = None,
    preview: bool = False,
) -> dict[str, Any]:
    """Authorise, execute once, and reconcile through canonical read-back."""

    decision = authorise_ontology_mutation(intent)
    if not decision.allowed:
        return ontology_authority_denial_payload(decision, intent)

    try:
        before_state = read_before() if read_before is not None else None
        handle = begin_mutation_receipt(
            decision=decision,
            intent=intent,
            before_state=before_state,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed before mutation
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "changed": False,
            "error_code": "ontology_mutation_receipt_store_unavailable",
            "error": (
                "The mutation was not started because its receipt could not be stored."
            ),
            "details": {"exception_type": type(exc).__name__},
            "authority_decision": decision.public_projection(),
        }

    if handle.intent_conflict:
        return {
            "success": False,
            "effect_status": "not_started",
            "mutation_outcome": "not_started",
            "outcome_finality": "terminal_for_effect_key",
            "changed": False,
            "idempotent_replay": True,
            "error_code": "ontology_mutation_idempotency_intent_conflict",
            "error": (
                "This idempotency key is already bound to a different ontology "
                "mutation intent."
            ),
            "authority_decision": decision.public_projection(),
            "authority_receipt": dict(handle.stored_receipt or {}),
        }

    if handle.replay and handle.stored_response is not None:
        if bool((handle.stored_receipt or {}).get("response_projection_truncated")):
            return {
                "success": False,
                "effect_status": "indeterminate",
                "mutation_outcome": "unknown",
                "outcome_finality": "requires_receipt_reconciliation",
                "changed": None,
                "idempotent_replay": True,
                "retryable": False,
                "error_code": "ontology_mutation_replay_projection_unavailable",
                "error": (
                    "The stored response was too large to replay safely; inspect "
                    "the receipt and canonical read-back."
                ),
                "authority_receipt": dict(handle.stored_receipt or {}),
            }
        replay = dict(handle.stored_response)
        replay["idempotent_replay"] = True
        stored_receipt = dict(handle.stored_receipt or {})
        replay["authority_receipt"] = stored_receipt or {
            "receipt_id": handle.receipt_id,
            "status": handle.existing_status,
            "intent_fingerprint": intent.fingerprint,
        }
        if "canonical_read_back" not in replay and stored_receipt:
            replay["canonical_read_back"] = stored_receipt.get("canonical_read_back")
        if handle.existing_status == "indeterminate":
            replay.update(
                {
                    "success": False,
                    "effect_status": "indeterminate",
                    "mutation_outcome": "unknown",
                    "changed": None,
                    "error_code": "ontology_mutation_outcome_unknown",
                }
            )
        return replay

    if handle.replay:
        # Another caller owns the one durable effect claim and has not yet
        # published a terminal response.  Returning immediately avoids both a
        # duplicate write and a lock-step wait that could deadlock a caller
        # coordinating the original effect.  The receipt endpoint provides the
        # eventual canonical read-back.
        return {
            "success": False,
            "effect_status": "in_progress",
            "mutation_outcome": "not_started",
            "outcome_finality": "pending_receipt_reconciliation",
            "changed": False,
            "idempotent_replay": True,
            "retryable": True,
            "error_code": "ontology_mutation_in_progress",
            "error": (
                "This exact ontology effect is already in progress; inspect its "
                "receipt for the terminal canonical read-back."
            ),
            "authority_decision": decision.public_projection(),
            "authority_receipt": dict(handle.stored_receipt or {}),
        }

    try:
        with bind_authorised_ontology_mutation(decision, intent):
            raw_result = mutate()
        result = dict(raw_result)
    except Exception as exc:  # noqa: BLE001 - effect boundary reconciles outcome
        try:
            canonical_state = read_back()
        except Exception:  # noqa: BLE001 - preserve unknown outcome receipt
            canonical_state = {"read_back_failed": True}
        try:
            receipt = finalise_mutation_receipt(
                receipt_id=handle.receipt_id,
                status="indeterminate",
                mutation_outcome="unknown",
                changed=None,
                canonical_read_back=canonical_state,
                response_projection={"exception_type": type(exc).__name__},
                error_code="ontology_mutation_outcome_unknown",
            )
        except Exception as receipt_exc:  # noqa: BLE001 - effect already crossed
            return _receipt_finalisation_failure_response(
                decision=decision,
                handle=handle,
                canonical_state=canonical_state,
                exception=receipt_exc,
                result={"mutation_exception_type": type(exc).__name__},
            )
        return {
            "success": False,
            "effect_status": "indeterminate",
            "mutation_outcome": "unknown",
            "changed": None,
            "error_code": "ontology_mutation_outcome_unknown",
            "error": (
                "The canonical mutation raised unexpectedly; the read-back receipt "
                "must be inspected before retrying."
            ),
            "authority_decision": decision.public_projection(),
            "authority_receipt": receipt,
        }

    try:
        canonical_state = read_back()
    except Exception as exc:  # noqa: BLE001 - read-back failure is typed below
        try:
            receipt = finalise_mutation_receipt(
                receipt_id=handle.receipt_id,
                status="indeterminate",
                mutation_outcome="unknown",
                changed=None,
                canonical_read_back={"read_back_failed": True},
                response_projection=result,
                error_code="canonical_read_back_failed",
            )
        except Exception as receipt_exc:  # noqa: BLE001 - effect already crossed
            return _receipt_finalisation_failure_response(
                decision=decision,
                handle=handle,
                canonical_state={"read_back_failed": True},
                exception=receipt_exc,
                result=result,
            )
        return {
            **result,
            "success": False,
            "effect_status": "indeterminate",
            "mutation_outcome": "unknown",
            "changed": None,
            "error_code": "canonical_read_back_failed",
            "error": "Canonical read-back failed; success cannot be claimed.",
            "details": {"exception_type": type(exc).__name__},
            "authority_decision": decision.public_projection(),
            "authority_receipt": receipt,
        }

    successful = bool(result.get("success", True))
    if successful and not preview and verify_read_back is not None:
        try:
            postcondition_verified = bool(verify_read_back(result, canonical_state))
        except Exception:  # noqa: BLE001 - a verifier failure cannot imply success
            postcondition_verified = False
        if not postcondition_verified:
            result = {
                **result,
                "success": False,
                "effect_status": "indeterminate",
                "mutation_outcome": "unknown",
                "outcome_finality": "requires_canonical_reconciliation",
                "changed": None,
                "retryable": False,
                "error_code": "ontology_mutation_postcondition_failed",
                "error": (
                    "The canonical read-back did not prove the requested effect; "
                    "success cannot be claimed."
                ),
            }
            successful = False
    changed_value = result.get("changed")
    changed = changed_value if isinstance(changed_value, bool) else successful
    if preview:
        status = "previewed"
        mutation_outcome = "not_started"
        changed = False
    elif successful:
        status = "succeeded"
        mutation_outcome = "succeeded"
    elif result.get("error_code") == "ontology_mutation_postcondition_failed":
        status = "indeterminate"
        mutation_outcome = "unknown"
        changed = None
    else:
        reported_outcome = str(result.get("mutation_outcome") or "").strip()
        reported_changed = result.get("changed")
        if reported_changed is True or reported_outcome in {"partial", "unknown"}:
            status = "indeterminate"
            mutation_outcome = (
                reported_outcome
                if reported_outcome in {"partial", "unknown"}
                else "unknown"
            )
            changed = None
            result = {
                **result,
                "effect_status": "indeterminate",
                "mutation_outcome": mutation_outcome,
                "outcome_finality": "requires_canonical_reconciliation",
                "changed": None,
                "retryable": False,
            }
        else:
            status = "failed"
            mutation_outcome = reported_outcome or "not_started"
    try:
        receipt = finalise_mutation_receipt(
            receipt_id=handle.receipt_id,
            status=status,
            mutation_outcome=mutation_outcome,
            changed=changed,
            canonical_read_back=canonical_state,
            response_projection=result,
            error_code=(str(result.get("error_code")) if not successful else None),
        )
    except Exception as exc:  # noqa: BLE001 - effect already crossed
        return _receipt_finalisation_failure_response(
            decision=decision,
            handle=handle,
            canonical_state=canonical_state,
            exception=exc,
            result=result,
        )
    return {
        **result,
        "authority_decision": decision.public_projection(),
        "authority_receipt": receipt,
        "canonical_read_back": canonical_state,
    }


def revoke_agent_delegation(
    *,
    delegation_id: str,
    acting_actor_concept_id: str,
    reason: str,
) -> dict[str, Any]:
    actor_id = _normalise_concept_id(acting_actor_concept_id)
    if not actor_id:
        raise PermissionError("authenticated_actor_context_required")
    collection = _delegation_collection()
    existing = collection.find_one(
        {
            "delegation_id": _clean_text(delegation_id),
            "grantor_actor_concept_id": actor_id,
        }
    )
    if not isinstance(existing, Mapping):
        raise PermissionError("ontology_delegation_revoke_authority_required")
    grantor_id = _normalise_concept_id(existing.get("grantor_actor_concept_id"))
    if actor_id != grantor_id:
        raise PermissionError("ontology_delegation_revoke_authority_required")
    try:
        with ontology_mutation_resource_lock(
            f"ontology-delegation:{_clean_text(delegation_id)}"
        ):
            now = _now()
            updated = collection.find_one_and_update(
                {"delegation_id": _clean_text(delegation_id), "status": "active"},
                {
                    "$set": {
                        "status": "revoked",
                        "revoked_at": now,
                        "revoked_by_actor_concept_id": actor_id,
                        "revocation_reason": _clean_text(reason) or "revoked",
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
    except OntologyMutationResourceBusy as exc:
        raise PermissionError("ontology_mutation_resource_busy") from exc
    if updated is None:
        updated = collection.find_one({"delegation_id": _clean_text(delegation_id)})
    if not isinstance(updated, Mapping):
        raise LookupError("Delegation revocation read-back failed")  # noqa: TRY004
    return _public_delegation_projection(updated)


def list_actor_delegations(
    *,
    actor_concept_id: str,
    include_inactive: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    actor_id = _normalise_concept_id(actor_concept_id)
    if not actor_id:
        return []
    query: dict[str, Any] = {"grantor_actor_concept_id": actor_id}
    if not include_inactive:
        query.update({"status": "active", "expires_at": {"$gt": _now()}})
    rows = (
        _delegation_collection()
        .find(query)
        .sort("issued_at", -1)
        .limit(max(1, min(int(limit), 250)))
    )
    return [_public_delegation_projection(row) for row in rows]


def list_actor_semantic_authority(actor_concept_id: str) -> dict[str, Any]:
    actor_id = _normalise_concept_id(actor_concept_id)
    roles = resolve_live_semantic_roles(actor_id or "")
    return {
        "schema_version": AUTHORITY_SCHEMA_VERSION,
        "actor_concept_id": actor_id,
        "roles": [item.to_mapping() for item in roles],
        "von_administrator_semantics": {
            "concept_id": VON_ADMINISTRATOR_CONCEPT_ID,
            "authority": "operational_only",
            "implies_semantic_ontology_authority": False,
        },
    }


def actor_can_access_intent_targets(
    *,
    actor_concept_id: str,
    organisation_concept_id: str | None,
    target_concept_ids: Sequence[str],
) -> bool:
    """Check target visibility as the trusted principal without leaking details."""

    actor_id = _normalise_concept_id(actor_concept_id)
    if not actor_id:
        return False
    with override_current_actor(actor_id, organisation_concept_id):
        return all(can_access_concept(item) for item in target_concept_ids)


__all__ = [
    "AUTHORITY_ROLE_PREDICATE",
    "AUTHORITY_SCHEMA_VERSION",
    "GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE",
    "MAX_INTERACTIVE_DELEGATION_TTL_SECONDS",
    "ORGANISATION_ONTOLOGY_ADMINISTRATOR_ROLE",
    "RESERVED_GENERIC_MUTATION_PREDICATES",
    "VON_ADMINISTRATOR_CONCEPT_ID",
    "OntologyAuthorityDecision",
    "OntologyInvocationContext",
    "OntologyMutationResourceBusy",
    "OntologyMutationIntent",
    "PublicationContext",
    "PublicationContextKind",
    "actor_can_access_intent_targets",
    "authority_role_storage_text",
    "authorise_ontology_mutation",
    "bind_authorised_ontology_mutation",
    "bind_ontology_invocation",
    "concept_publication_context",
    "current_authorised_ontology_intent",
    "current_authorised_ontology_mutation",
    "current_ontology_invocation",
    "execute_authorised_ontology_mutation",
    "get_mutation_receipt_for_actor",
    "issue_agent_delegation",
    "list_actor_delegations",
    "list_actor_semantic_authority",
    "ontology_authority_denial_payload",
    "ontology_mutation_resource_lock",
    "ontology_authority_resource_keys",
    "publication_context_for_creation",
    "parse_authority_role_storage_text",
    "resolve_live_semantic_roles",
    "resolve_delegation_principal",
    "revoke_agent_delegation",
    "verify_agent_delegation",
]
