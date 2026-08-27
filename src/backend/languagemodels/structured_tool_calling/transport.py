"""Profile-driven structured-tool transport resolution.

The resolver is intentionally provider-neutral and support-only.  Represented
model API profiles advertise which surface can carry structured tools; Python
validates that agreement and produces a deterministic wire decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from ...services.model_registry_service import resolve_model_api_profiles


STRUCTURED_TOOL_TRANSPORT_DECISION_SCHEMA = "structured_tool_transport_decision.v1"
API_SURFACE_CHAT_COMPLETIONS = "chat_completions"
API_SURFACE_RESPONSES = "responses"
_OPENAI_STRUCTURED_TOOL_API_SURFACES = {
    API_SURFACE_CHAT_COMPLETIONS,
    API_SURFACE_RESPONSES,
}
_STRUCTURED_TOOL_CAPABILITIES = {"required", "supported", "unsupported"}
_TOOL_CONTINUATION_MODES = {"stateless", "provider_managed"}
_RESPONSE_STORAGE_POLICIES = {
    "disabled",
    "enabled",
    "allowed",
    "provider_managed",
}
_PROVIDER_MANAGED_STORAGE_POLICIES = {
    "enabled",
    "allowed",
    "provider_managed",
}


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


_SENSITIVE_TELEMETRY_KEY_RE = re.compile(
    r"(?:^|_)(?:api_?key|private_?key|secret|password|credential|token|authorization|cookie|bearer)(?:$|_)",
    flags=re.IGNORECASE,
)


def sanitise_transport_telemetry_text(value: str) -> str:
    """Redact common credential forms embedded inside diagnostic text."""

    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted-api-key]", value)
    text = re.sub(
        r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b((?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{6,}",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(cookie\s*:\s*)[^\r\n]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|private[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret)"
        r"(\s*[:=]\s*)[^&\s,;]+",
        r"\1\2[redacted]",
        text,
    )
    return text


def sanitise_transport_telemetry_value(
    value: Any, *, key: str = "", depth: int = 0, max_depth: int = 3
) -> Any:
    if _SENSITIVE_TELEMETRY_KEY_RE.search(key):
        return "[redacted]"
    if depth >= max_depth:
        return "[bounded]"
    if isinstance(value, Mapping):
        return {
            str(item_key)[:120]: sanitise_transport_telemetry_value(
                item_value,
                key=str(item_key),
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item_key, item_value in list(value.items())[:40]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            sanitise_transport_telemetry_value(
                item,
                key=key,
                depth=depth + 1,
                max_depth=max_depth,
            )
            for item in list(value)[:40]
        ]
    if isinstance(value, str):
        text = sanitise_transport_telemetry_text(value)
        if "://" in text:
            return "[redacted-url]"
        return text if len(text) <= 512 else f"{text[:509]}..."
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(type(value).__name__)


@dataclass(frozen=True)
class StructuredToolTransportDecision:
    """Inspectible decision for one provider/model/tool request."""

    provider: str
    model: str
    effective_api_surface: str
    status: str
    reason: str
    capability_class: str
    tools_present: bool
    requested_api_surface: str = "auto"
    capability_source: str = "conservative_client_default"
    registry_concept_id: str | None = None
    registry_entry_id: str | None = None
    model_concept_id: str | None = None
    profile_concept_id: str | None = None
    connection_id: str | None = None
    deployment_id: str | None = None
    continuation_mode: str = "stateless"
    store: bool = False
    advertised_alternatives: tuple[str, ...] = ()
    parameter_projection: Mapping[str, Any] = field(default_factory=dict)

    @property
    def capability_key(self) -> str:
        raw = "|".join(
            (
                self.provider,
                self.connection_id or "",
                self.deployment_id or "",
                self.model,
                self.effective_api_surface,
                "tools" if self.tools_present else "no_tools",
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    @property
    def decision_key(self) -> str:
        projection = json.dumps(
            dict(self.parameter_projection),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        raw = f"{self.capability_key}|{projection}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def to_telemetry(self) -> dict[str, Any]:
        """Return a bounded, credential-free telemetry projection."""

        return {
            "schema_version": STRUCTURED_TOOL_TRANSPORT_DECISION_SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "capability_class": self.capability_class,
            "requested_provider": sanitise_transport_telemetry_value(self.provider),
            "effective_provider": sanitise_transport_telemetry_value(self.provider),
            "requested_model": sanitise_transport_telemetry_value(self.model),
            "effective_model": sanitise_transport_telemetry_value(self.model),
            "requested_api_surface": self.requested_api_surface,
            "effective_api_surface": self.effective_api_surface,
            "tools_present": self.tools_present,
            "capability_source": self.capability_source,
            "registry_concept_id": self.registry_concept_id,
            "registry_entry_id": self.registry_entry_id,
            "model_concept_id": self.model_concept_id,
            "profile_concept_id": self.profile_concept_id,
            "connection_id": sanitise_transport_telemetry_value(self.connection_id),
            "deployment_id": sanitise_transport_telemetry_value(self.deployment_id),
            "requested_client_type": self.provider,
            "effective_client_type": (
                f"{self.provider}_{self.effective_api_surface}"
                if self.effective_api_surface != "provider_native"
                else self.provider
            ),
            "requested_connection_id": sanitise_transport_telemetry_value(
                self.connection_id
            ),
            "effective_connection_id": sanitise_transport_telemetry_value(
                self.connection_id
            ),
            "requested_deployment_id": sanitise_transport_telemetry_value(
                self.deployment_id
            ),
            "effective_deployment_id": sanitise_transport_telemetry_value(
                self.deployment_id
            ),
            "continuation_mode": self.continuation_mode,
            "store": self.store,
            "advertised_alternatives": list(self.advertised_alternatives),
            "parameter_projection": sanitise_transport_telemetry_value(
                dict(self.parameter_projection)
            ),
            "capability_key": self.capability_key,
            "decision_key": self.decision_key,
        }


def _profile_metadata(
    profile: Mapping[str, Any],
) -> tuple[str, str, str, str, bool, str | None]:
    surface = _normalise(profile.get("api_surface"))
    capability = _normalise(profile.get("structured_tool_calling"))
    raw_continuation_mode = _normalise(profile.get("tool_continuation_mode"))
    raw_storage_policy = _normalise(profile.get("response_storage_policy"))

    # Older API profiles pre-date structured-tool transport metadata.  An
    # absent capability is unknown authority, not a malformed declaration;
    # retain the conservative client default rather than interpreting the
    # profile's other fields as structured-tool policy.
    if not capability:
        return surface, "", "stateless", "disabled", False, None

    continuation_mode = raw_continuation_mode or "stateless"
    storage_policy = raw_storage_policy or "disabled"
    if capability not in _STRUCTURED_TOOL_CAPABILITIES:
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_invalid_structured_tool_capability",
        )
    if surface not in _OPENAI_STRUCTURED_TOOL_API_SURFACES:
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_invalid_api_surface",
        )
    if raw_continuation_mode and continuation_mode not in _TOOL_CONTINUATION_MODES:
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_invalid_continuation_mode",
        )
    if raw_storage_policy and storage_policy not in _RESPONSE_STORAGE_POLICIES:
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_invalid_response_storage_policy",
        )
    if (
        surface == API_SURFACE_CHAT_COMPLETIONS
        and continuation_mode == "provider_managed"
    ):
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_chat_provider_managed_continuation",
        )
    if continuation_mode == "provider_managed" and (
        storage_policy not in _PROVIDER_MANAGED_STORAGE_POLICIES
    ):
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_contradictory_continuation_storage",
        )
    if storage_policy == "provider_managed" and continuation_mode != "provider_managed":
        return (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            False,
            "represented_profile_contradictory_continuation_storage",
        )

    store = (
        continuation_mode == "provider_managed"
        and storage_policy in _PROVIDER_MANAGED_STORAGE_POLICIES
    )
    return (
        surface,
        capability,
        continuation_mode,
        storage_policy,
        store,
        None,
    )


def resolve_structured_tool_transport(
    *,
    provider: str,
    model: str,
    tools_present: bool,
    requested_api_surface: str | None = None,
    connection_id: str | None = None,
    deployment_id: str | None = None,
    parameter_projection: Mapping[str, Any] | None = None,
    allow_advertised_surface_override: bool = False,
) -> StructuredToolTransportDecision:
    """Resolve a compatible API surface without model-name heuristics.

    Unknown profiles preserve existing provider behaviour.  For OpenAI and
    OpenAI-compatible clients that means Chat Completions.  Responses is chosen
    only when a matching represented profile explicitly advertises structured
    tool support (or requires it).  A represented required surface also remains
    authoritative for text-only calls so that surface-specific parameter
    constraints are not lost when a caller deliberately supplies no tools.
    """

    provider_key = _normalise(provider) or "unknown"
    model_name = str(model or "").strip()
    requested_surface = _normalise(requested_api_surface) or "auto"
    projection = dict(parameter_projection or {})

    if provider_key not in {"openai", "openrouter"}:
        return StructuredToolTransportDecision(
            provider=provider_key,
            model=model_name,
            effective_api_surface="provider_native",
            status="compatible",
            reason="provider_native_structured_tool_surface",
            capability_class="provider_native",
            tools_present=tools_present,
            requested_api_surface=requested_surface,
            connection_id=connection_id,
            deployment_id=deployment_id,
            parameter_projection=projection,
        )

    resolved = resolve_model_api_profiles(model=model_name, provider=provider_key)
    profiles: Sequence[Mapping[str, Any]] = ()
    if isinstance(resolved, Mapping):
        raw_profiles = resolved.get("api_profiles")
        if isinstance(raw_profiles, Sequence) and not isinstance(raw_profiles, str):
            profiles = tuple(
                profile for profile in raw_profiles if isinstance(profile, Mapping)
            )

    applicable: list[
        tuple[
            Mapping[str, Any],
            str,
            str,
            str,
            str,
            bool,
            tuple[int, int, int],
        ]
    ] = []
    invalid_applicable: list[
        tuple[Mapping[str, Any], str, str, tuple[int, int, int]]
    ] = []
    for profile in profiles:
        profile_connection_id = str(profile.get("connection_id") or "").strip()
        profile_deployment_id = str(profile.get("deployment_id") or "").strip()
        if profile_connection_id and profile_connection_id != connection_id:
            continue
        if profile_deployment_id and profile_deployment_id != deployment_id:
            continue
        if (
            isinstance(connection_id, str)
            and connection_id.startswith("openai_compatible:")
            and not profile_connection_id
        ):
            # A native-provider model default must not imply Responses support
            # on an arbitrary OpenAI-compatible connection.
            continue
        if (
            isinstance(deployment_id, str)
            and deployment_id
            and deployment_id != model_name
            and not profile_deployment_id
        ):
            # A named deployment override needs its own represented profile;
            # model-level provider metadata is only a default.
            continue
        (
            surface,
            capability,
            continuation_mode,
            storage_policy,
            store,
            invalid_reason,
        ) = _profile_metadata(profile)
        if not capability:
            continue
        constraint_count = int(bool(profile_connection_id)) + int(
            bool(profile_deployment_id)
        )
        specificity = (
            constraint_count,
            int(bool(profile_connection_id)),
            int(bool(profile_deployment_id)),
        )
        if invalid_reason is not None:
            invalid_applicable.append((profile, surface, invalid_reason, specificity))
            continue
        applicable.append(
            (
                profile,
                surface,
                capability,
                continuation_mode,
                storage_policy,
                store,
                specificity,
            )
        )

    # Compare capability precedence only inside the most-specific represented
    # profile set.  Explicit connection/deployment authority must override a
    # broader model default regardless of graph/list ordering.
    represented_specificities = [item[6] for item in applicable] + [
        item[3] for item in invalid_applicable
    ]
    if represented_specificities:
        max_specificity = max(represented_specificities)
        applicable = [item for item in applicable if item[6] == max_specificity]
        invalid_applicable = [
            item for item in invalid_applicable if item[3] == max_specificity
        ]

    provenance = dict(resolved or {}) if isinstance(resolved, Mapping) else {}
    common = {
        "provider": provider_key,
        "model": model_name,
        "tools_present": tools_present,
        "requested_api_surface": requested_surface,
        "registry_concept_id": provenance.get("registry_concept_id"),
        "registry_entry_id": provenance.get("registry_entry_id"),
        "model_concept_id": provenance.get("concept_id"),
        "connection_id": connection_id,
        "deployment_id": deployment_id,
        "parameter_projection": projection,
    }

    if invalid_applicable:
        invalid_profile, invalid_surface, invalid_reason, _ = invalid_applicable[0]
        return StructuredToolTransportDecision(
            **common,
            effective_api_surface=invalid_surface,
            status="unsupported",
            reason=invalid_reason,
            capability_class="invalid_represented_authority",
            capability_source=str(provenance.get("source") or "model_registry"),
            profile_concept_id=(
                str(invalid_profile.get("profile_concept_id"))
                if invalid_profile.get("profile_concept_id")
                else None
            ),
            advertised_alternatives=(),
        )

    advertised = [
        (item[0], item[1], item[2], item[3], item[5])
        for item in applicable
        if item[2] != "unsupported"
    ]
    explicitly_unsupported = [
        item[1] for item in applicable if item[2] == "unsupported"
    ]
    conflicting_surface = next(
        (
            surface
            for surface in sorted(_OPENAI_STRUCTURED_TOOL_API_SURFACES)
            if surface in explicitly_unsupported
            and any(item[1] == surface for item in advertised)
        ),
        None,
    )
    required_surfaces = {item[1] for item in advertised if item[2] == "required"}

    if provider_key == "openrouter":
        non_chat_required_surfaces = required_surfaces - {
            API_SURFACE_CHAT_COMPLETIONS
        }
        if non_chat_required_surfaces:
            return StructuredToolTransportDecision(
                **common,
                effective_api_surface=sorted(non_chat_required_surfaces)[0],
                status="unsupported",
                reason="openrouter_chat_completions_only",
                capability_class="provider_surface_not_enabled",
                capability_source=str(
                    provenance.get("source") or "model_registry"
                ),
                advertised_alternatives=(),
            )
        # OpenRouter's Responses-compatible beta is outside this bounded
        # release. Non-required advertisements must not opt a call into it.
        advertised = [
            item
            for item in advertised
            if item[1] == API_SURFACE_CHAT_COMPLETIONS
        ]

    # Contradictory represented authority is invalid independently of whether
    # this particular request carries tools.  Answer-only traffic must not
    # silently choose one side of a same-surface contradiction.
    if conflicting_surface is not None:
        return StructuredToolTransportDecision(
            **common,
            effective_api_surface=conflicting_surface,
            status="unsupported",
            reason="represented_profiles_conflicting_surface_capability",
            capability_class="invalid_represented_authority",
            capability_source=str(provenance.get("source") or "model_registry"),
            advertised_alternatives=(),
        )

    if len(required_surfaces) > 1:
        return StructuredToolTransportDecision(
            **common,
            effective_api_surface=requested_surface,
            status="unsupported",
            reason="represented_profiles_multiple_required_surfaces",
            capability_class="invalid_represented_authority",
            capability_source=str(provenance.get("source") or "model_registry"),
            advertised_alternatives=(),
        )

    if not tools_present:
        required = [item for item in applicable if item[2] == "required"]
        selected_no_tool: tuple[
            Mapping[str, Any],
            str,
            str,
            str,
            str,
            bool,
            tuple[int, int, int],
        ] | None = None
        if requested_surface != "auto" and allow_advertised_surface_override:
            selected_no_tool = next(
                (item for item in applicable if item[1] == requested_surface),
                None,
            )
        elif required:
            selected_no_tool = required[0]
        elif requested_surface != "auto":
            selected_no_tool = next(
                (item for item in applicable if item[1] == requested_surface),
                None,
            )

        if selected_no_tool is not None:
            (
                profile,
                surface,
                capability,
                continuation_mode,
                _storage_policy,
                store,
                _specificity,
            ) = selected_no_tool
            alternatives = tuple(
                item[1] for item in applicable if item[1] != surface
            )
            return StructuredToolTransportDecision(
                **common,
                effective_api_surface=surface,
                status="compatible",
                reason=f"represented_profile_{capability}",
                capability_class=(
                    "responses_required"
                    if surface == API_SURFACE_RESPONSES
                    and capability == "required"
                    else "represented_text_surface"
                ),
                capability_source=str(
                    provenance.get("source") or "model_registry"
                ),
                profile_concept_id=(
                    str(profile.get("profile_concept_id"))
                    if profile.get("profile_concept_id")
                    else None
                ),
                continuation_mode=continuation_mode,
                store=store,
                advertised_alternatives=alternatives,
            )

        # A profile can prohibit structured tools while still governing ordinary
        # text generation on its surface. Preserve that exact profile when the
        # conservative answer-only surface is Chat Completions so downstream
        # parameter policy cannot drift to another connection's Chat profile.
        conservative_chat_profile = next(
            (
                item
                for item in applicable
                if item[1] == API_SURFACE_CHAT_COMPLETIONS
            ),
            None,
        )
        if conservative_chat_profile is not None:
            (
                profile,
                surface,
                _capability,
                continuation_mode,
                _storage_policy,
                store,
                _specificity,
            ) = conservative_chat_profile
            return StructuredToolTransportDecision(
                **common,
                effective_api_surface=surface,
                status="compatible",
                reason="no_structured_tools_in_request",
                capability_class="no_tool_generation",
                capability_source=str(
                    provenance.get("source") or "model_registry"
                ),
                profile_concept_id=(
                    str(profile.get("profile_concept_id"))
                    if profile.get("profile_concept_id")
                    else None
                ),
                continuation_mode=continuation_mode,
                store=store,
                advertised_alternatives=tuple(
                    item[1] for item in applicable if item[1] != surface
                ),
            )

        return StructuredToolTransportDecision(
            **common,
            effective_api_surface=API_SURFACE_CHAT_COMPLETIONS,
            status="compatible",
            reason="no_structured_tools_in_request",
            capability_class="no_tool_generation",
            capability_source="conservative_client_default",
        )

    selected: tuple[Mapping[str, Any], str, str, str, bool] | None = None
    required = [item for item in advertised if item[2] == "required"]
    if requested_surface != "auto" and allow_advertised_surface_override:
        selected = next(
            (item for item in advertised if item[1] == requested_surface),
            None,
        )
    elif required:
        selected = required[0]
    elif requested_surface != "auto":
        selected = next(
            (item for item in advertised if item[1] == requested_surface),
            None,
        )
    if selected is None and advertised:
        selected = next(
            (item for item in advertised if item[1] == API_SURFACE_CHAT_COMPLETIONS),
            advertised[0],
        )

    if selected is not None:
        profile, surface, capability, continuation_mode, store = selected
        alternatives = tuple(item[1] for item in advertised if item[1] != surface)
        return StructuredToolTransportDecision(
            **common,
            effective_api_surface=surface,
            status="compatible",
            reason=f"represented_profile_{capability}",
            capability_class=(
                "responses_required"
                if surface == API_SURFACE_RESPONSES and capability == "required"
                else "chat_completions_compatible"
                if surface == API_SURFACE_CHAT_COMPLETIONS
                else "represented_surface_compatible"
            ),
            capability_source=str(provenance.get("source") or "model_registry"),
            profile_concept_id=(
                str(profile.get("profile_concept_id"))
                if profile.get("profile_concept_id")
                else None
            ),
            continuation_mode=continuation_mode,
            store=store,
            advertised_alternatives=alternatives,
        )

    unsupported_requested_surface = (
        requested_surface
        if requested_surface != "auto"
        else API_SURFACE_CHAT_COMPLETIONS
    )
    if (
        applicable and len(explicitly_unsupported) == len(applicable)
    ) or unsupported_requested_surface in explicitly_unsupported:
        return StructuredToolTransportDecision(
            **common,
            effective_api_surface=requested_surface,
            status="unsupported",
            reason="represented_profiles_explicitly_unsupported",
            capability_class="explicitly_unsupported",
            capability_source=str(provenance.get("source") or "model_registry"),
            advertised_alternatives=(),
        )

    return StructuredToolTransportDecision(
        **common,
        effective_api_surface=API_SURFACE_CHAT_COMPLETIONS,
        status="compatible",
        reason="conservative_chat_completions_default",
        capability_class="unknown_conservative_default",
        capability_source=(
            str(provenance.get("source"))
            if provenance
            else "conservative_client_default"
        ),
    )
