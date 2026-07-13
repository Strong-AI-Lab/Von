"""Actor-safe workflow-definition resolution for nested durable execution.

Nested workflow actions execute inside an already-authenticated workflow
environment.  Their child-definition lookup must therefore honour that same
actor before consulting legacy unpartitioned loaders, otherwise a definition
warmed by one actor can be reused for another actor.

This module is intentionally policy-free support code: it validates actor
plumbing, delegates access decisions to the authoritative Vontology resolver,
and returns bounded diagnostics to the calling VWL primitive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ...services.namespace_service import (
    derive_actor_context_from_namespace,
    resolve_canonical_namespace,
)
from ..action_registry import WorkflowEnvironment
from ..engine import WorkflowDefinition


NESTED_WORKFLOW_ACTOR_CONTEXT_MISMATCH = (
    "nested_workflow_actor_context_mismatch"
)
NESTED_WORKFLOW_NAMESPACE_INVALID = "nested_workflow_namespace_invalid"
NESTED_WORKFLOW_AUTHORITY_RESOLUTION_FAILED = (
    "nested_workflow_authority_resolution_failed"
)
NESTED_WORKFLOW_DEFINITION_IDENTITY_MISMATCH = (
    "nested_workflow_definition_identity_mismatch"
)
NESTED_WORKFLOW_DEFINITION_NOT_FOUND = "nested_workflow_definition_not_found"


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_concept_id(value: Any) -> str | None:
    text = _normalise_text(value)
    if not text:
        return None
    return text if text.startswith("#V#") else f"#V#{text}"


@dataclass(frozen=True)
class NestedWorkflowActorContext:
    user_id: str | None
    org_id: str | None
    namespace: str | None
    source: str
    error_code: str | None = None
    mismatch_fields: tuple[str, ...] = ()

    @property
    def actor_scoped(self) -> bool:
        return bool(self.user_id or self.org_id)

    @property
    def valid(self) -> bool:
        return self.error_code is None

    def to_projection(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "actor_scoped": self.actor_scoped,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "namespace": self.namespace,
            "valid": self.valid,
            "error_code": self.error_code,
            "mismatch_fields": list(self.mismatch_fields),
        }


@dataclass(frozen=True)
class NestedWorkflowDefinitionResolution:
    workflow_id: str
    definition: WorkflowDefinition | None
    actor_context: NestedWorkflowActorContext
    authority_source: str
    error_code: str | None = None
    authority_diagnostics: Mapping[str, Any] | None = None
    used_fallback_loader: bool = False

    @property
    def success(self) -> bool:
        return self.definition is not None and self.error_code is None

    def to_projection(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "nested_workflow_definition_resolution.v1",
            "workflow_id": self.workflow_id,
            "success": self.success,
            "authority_source": self.authority_source,
            "used_fallback_loader": self.used_fallback_loader,
            "actor_context": self.actor_context.to_projection(),
        }
        if self.error_code:
            payload["error_code"] = self.error_code
        if isinstance(self.authority_diagnostics, Mapping):
            payload["authority_diagnostics"] = dict(self.authority_diagnostics)
        definition_id = _normalise_text(
            getattr(self.definition, "workflow_id", None)
        )
        if definition_id:
            payload["resolved_workflow_id"] = definition_id
        return payload


def resolve_nested_workflow_actor_context(
    environment: WorkflowEnvironment,
) -> NestedWorkflowActorContext:
    """Resolve one actor context and reject contradictory namespace fields."""

    raw_namespace = _normalise_text(environment.user_namespace)
    canonical_namespace = (
        resolve_canonical_namespace(raw_namespace) if raw_namespace else None
    )
    if raw_namespace and canonical_namespace is None:
        return NestedWorkflowActorContext(
            user_id=None,
            org_id=None,
            namespace=None,
            source="invalid_namespace",
            error_code=NESTED_WORKFLOW_NAMESPACE_INVALID,
        )

    namespace_user_id, namespace_org_id = derive_actor_context_from_namespace(
        canonical_namespace
    )
    explicit_user_id = _normalise_concept_id(environment.user_concept_id)
    explicit_org_id = _normalise_concept_id(environment.org_concept_id)

    mismatch_fields: list[str] = []
    if (
        namespace_user_id
        and explicit_user_id
        and namespace_user_id != explicit_user_id
    ):
        mismatch_fields.append("user_id")
    if (
        namespace_org_id
        and explicit_org_id
        and namespace_org_id != explicit_org_id
    ):
        # A namespace-encoded organisation is authoritative when present.  A
        # user-only namespace may still be paired with the independently
        # authenticated organisation carried by the parent environment.
        mismatch_fields.append("org_id")

    resolved_user_id = namespace_user_id or explicit_user_id
    resolved_org_id = namespace_org_id or explicit_org_id
    source = (
        "namespace_and_environment"
        if canonical_namespace and (explicit_user_id or explicit_org_id)
        else "namespace"
        if canonical_namespace
        else "environment"
        if (explicit_user_id or explicit_org_id)
        else "unscoped"
    )

    if mismatch_fields:
        return NestedWorkflowActorContext(
            user_id=resolved_user_id,
            org_id=resolved_org_id,
            namespace=canonical_namespace,
            source=source,
            error_code=NESTED_WORKFLOW_ACTOR_CONTEXT_MISMATCH,
            mismatch_fields=tuple(mismatch_fields),
        )

    if canonical_namespace is None and resolved_user_id:
        canonical_namespace = resolve_canonical_namespace(
            None,
            resolved_user_id,
            resolved_org_id,
        )

    return NestedWorkflowActorContext(
        user_id=resolved_user_id,
        org_id=resolved_org_id,
        namespace=canonical_namespace,
        source=source,
    )


def _definition_identity_error(
    *,
    requested_workflow_id: str,
    definition: WorkflowDefinition | None,
) -> str | None:
    if definition is None:
        return None
    resolved_workflow_id = _normalise_text(definition.workflow_id)
    if resolved_workflow_id == requested_workflow_id:
        return None
    return NESTED_WORKFLOW_DEFINITION_IDENTITY_MISMATCH


def resolve_nested_workflow_definition(
    *,
    workflow_id: str,
    environment: WorkflowEnvironment,
    fallback_loader: Callable[[str], WorkflowDefinition | None] | None,
) -> NestedWorkflowDefinitionResolution:
    """Resolve a child definition under the parent request's actor authority.

    Actor-scoped requests never consult ``fallback_loader``.  The authoritative
    resolver performs a request-scoped Vontology load and is deliberately
    called without ``promote_to_registry``.  The fallback remains only for
    genuinely unscoped legacy/test executions where no actor authority exists.
    """

    workflow_id_text = _normalise_text(workflow_id)
    actor_context = resolve_nested_workflow_actor_context(environment)
    if not actor_context.valid:
        return NestedWorkflowDefinitionResolution(
            workflow_id=workflow_id_text,
            definition=None,
            actor_context=actor_context,
            authority_source="actor_context_validation",
            error_code=actor_context.error_code,
        )

    if actor_context.actor_scoped:
        try:
            from .registry_factory import resolve_workflow_definition_from_authority

            authority_resolution = resolve_workflow_definition_from_authority(
                workflow_id_text,
                use_current_shared_registry=True,
                register_authoritative_fallback=True,
                actor_user_id=actor_context.user_id,
                actor_org_id=actor_context.org_id,
            )
        except Exception as exc:
            return NestedWorkflowDefinitionResolution(
                workflow_id=workflow_id_text,
                definition=None,
                actor_context=actor_context,
                authority_source="vontology_actor_authority",
                error_code=NESTED_WORKFLOW_AUTHORITY_RESOLUTION_FAILED,
                authority_diagnostics={"exception_type": type(exc).__name__},
            )

        definition = authority_resolution.definition
        authority_payload = authority_resolution.to_dict()
        identity_error = _definition_identity_error(
            requested_workflow_id=workflow_id_text,
            definition=definition,
        )
        if identity_error:
            return NestedWorkflowDefinitionResolution(
                workflow_id=workflow_id_text,
                definition=None,
                actor_context=actor_context,
                authority_source="vontology_actor_authority",
                error_code=identity_error,
                authority_diagnostics=authority_payload,
            )
        if definition is None:
            return NestedWorkflowDefinitionResolution(
                workflow_id=workflow_id_text,
                definition=None,
                actor_context=actor_context,
                authority_source="vontology_actor_authority",
                error_code=(
                    _normalise_text(authority_resolution.error_code)
                    or NESTED_WORKFLOW_DEFINITION_NOT_FOUND
                ),
                authority_diagnostics=authority_payload,
            )
        return NestedWorkflowDefinitionResolution(
            workflow_id=workflow_id_text,
            definition=definition,
            actor_context=actor_context,
            authority_source="vontology_actor_authority",
            authority_diagnostics=authority_payload,
        )

    definition: WorkflowDefinition | None = None
    if fallback_loader is not None:
        try:
            definition = fallback_loader(workflow_id_text)
        except Exception as exc:
            return NestedWorkflowDefinitionResolution(
                workflow_id=workflow_id_text,
                definition=None,
                actor_context=actor_context,
                authority_source="unscoped_fallback_loader",
                error_code=NESTED_WORKFLOW_AUTHORITY_RESOLUTION_FAILED,
                authority_diagnostics={"exception_type": type(exc).__name__},
                used_fallback_loader=True,
            )

    identity_error = _definition_identity_error(
        requested_workflow_id=workflow_id_text,
        definition=definition,
    )
    if identity_error:
        return NestedWorkflowDefinitionResolution(
            workflow_id=workflow_id_text,
            definition=None,
            actor_context=actor_context,
            authority_source="unscoped_fallback_loader",
            error_code=identity_error,
            used_fallback_loader=True,
        )
    return NestedWorkflowDefinitionResolution(
        workflow_id=workflow_id_text,
        definition=definition,
        actor_context=actor_context,
        authority_source="unscoped_fallback_loader",
        error_code=(None if definition is not None else NESTED_WORKFLOW_DEFINITION_NOT_FOUND),
        used_fallback_loader=fallback_loader is not None,
    )


__all__ = [
    "NESTED_WORKFLOW_ACTOR_CONTEXT_MISMATCH",
    "NESTED_WORKFLOW_AUTHORITY_RESOLUTION_FAILED",
    "NESTED_WORKFLOW_DEFINITION_IDENTITY_MISMATCH",
    "NESTED_WORKFLOW_DEFINITION_NOT_FOUND",
    "NESTED_WORKFLOW_NAMESPACE_INVALID",
    "NestedWorkflowActorContext",
    "NestedWorkflowDefinitionResolution",
    "resolve_nested_workflow_actor_context",
    "resolve_nested_workflow_definition",
]
