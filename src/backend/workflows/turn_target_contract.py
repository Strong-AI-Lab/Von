"""Serialisable target contracts for turn expected-outcome state.

The contract is intentionally generic. It records whether the target is only
natural language, already resolved to symbolic Vontology IDs, or a hybrid with
candidate/resolution lineage. Policy about what the target should be remains in
workflow and prompt artefacts; Python uses this only to preserve and validate
already-authored target state at tool boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

TURN_TARGET_CONTRACT_SCHEMA_VERSION = "turn_target_contract.v1"

TURN_TARGET_CONTRACTS_CONTEXT_KEY = "turn_expected_target_contracts"

TARGET_KIND_NATURAL_LANGUAGE = "natural_language"
TARGET_KIND_SYMBOLIC = "symbolic"
TARGET_KIND_HYBRID = "hybrid"
TARGET_KINDS = frozenset(
    {
        TARGET_KIND_NATURAL_LANGUAGE,
        TARGET_KIND_SYMBOLIC,
        TARGET_KIND_HYBRID,
    }
)

TARGET_BINDING_ENTITY = "entity"
TARGET_BINDING_TYPE = "type"
TARGET_BINDING_UNKNOWN = "unknown"

TARGET_MATCH_EXACT = "exact"
TARGET_MATCH_EXACT_OR_SUBTYPE = "exact_or_subtype"
TARGET_MATCH_EXACT_OR_SUPERTYPE = "exact_or_supertype"
TARGET_MATCH_EXACT_OR_TYPE_HIERARCHY = "exact_or_type_hierarchy"
TARGET_MATCH_POLICIES = frozenset(
    {
        TARGET_MATCH_EXACT,
        TARGET_MATCH_EXACT_OR_SUBTYPE,
        TARGET_MATCH_EXACT_OR_SUPERTYPE,
        TARGET_MATCH_EXACT_OR_TYPE_HIERARCHY,
    }
)

RESOLUTION_STATUS_RESOLVED = "resolved"
RESOLUTION_STATUS_UNRESOLVED = "unresolved"
RESOLUTION_STATUS_CANDIDATE_ONLY = "candidate_only"
RESOLUTION_STATUS_AMBIGUOUS = "ambiguous"
RESOLUTION_STATUSES_REQUIRING_RESOLUTION = frozenset(
    {
        "",
        RESOLUTION_STATUS_UNRESOLVED,
        RESOLUTION_STATUS_CANDIDATE_ONLY,
        RESOLUTION_STATUS_AMBIGUOUS,
        "candidate",
        "candidates",
        "partial",
        "needs_resolution",
    }
)

VONTOLOGY_CONCEPT_ID_PATTERN = re.compile(r"#V#[A-Za-z0-9_][A-Za-z0-9_.:/-]*")

TARGET_CONTRACT_FIELDS: tuple[str, ...] = (
    "target_contracts",
    TURN_TARGET_CONTRACTS_CONTEXT_KEY,
    "targets",
    "turn_targets",
)

TARGET_CONCEPT_ID_FIELDS: tuple[str, ...] = (
    "concept_id",
    "concept_ids",
    "target_concept_id",
    "target_concept_ids",
    "target_concepts",
    "resolved_concept_id",
    "resolved_concept_ids",
    "symbolic_concept_id",
    "symbolic_concept_ids",
    "id",
    "ids",
)

TARGET_TYPE_ID_FIELDS: tuple[str, ...] = (
    "target_type_id",
    "target_type_ids",
    "target_types",
    "type_id",
    "type_ids",
    "class_id",
    "class_ids",
    "instance_of",
    "requested_type_id",
    "requested_type_ids",
    "requested_extent_type_id",
    "requested_extent_type_ids",
)

CANDIDATE_CONCEPT_ID_FIELDS: tuple[str, ...] = (
    "candidate_concept_id",
    "candidate_concept_ids",
    "candidate_ids",
    "candidate_targets",
    "candidates",
)

TEXT_FIELDS: tuple[str, ...] = (
    "text",
    "natural_language",
    "natural_language_target",
    "target_text",
    "label",
    "name",
    "description",
    "query",
)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _dedupe_strings(values: Sequence[Any]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(cleaned)
    return tuple(ordered)


def _copy_mapping(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _normalise_kind(value: Any, *, has_symbols: bool, has_candidates: bool) -> str:
    cleaned = (_clean_text(value) or "").lower().replace("-", "_")
    aliases = {
        "nl": TARGET_KIND_NATURAL_LANGUAGE,
        "natural_language_only": TARGET_KIND_NATURAL_LANGUAGE,
        "symbolic_only": TARGET_KIND_SYMBOLIC,
        "resolved_symbolic": TARGET_KIND_SYMBOLIC,
        "mixed": TARGET_KIND_HYBRID,
        "candidate": TARGET_KIND_HYBRID,
    }
    kind = aliases.get(cleaned, cleaned)
    if kind in TARGET_KINDS:
        return kind
    if has_symbols and has_candidates:
        return TARGET_KIND_HYBRID
    if has_symbols:
        return TARGET_KIND_SYMBOLIC
    if has_candidates:
        return TARGET_KIND_HYBRID
    return TARGET_KIND_NATURAL_LANGUAGE


def _normalise_binding_kind(value: Any) -> str:
    cleaned = (_clean_text(value) or "").lower().replace("-", "_")
    if cleaned in {"class", "concept_type", "extent_type", "instance_type"}:
        return TARGET_BINDING_TYPE
    if cleaned in {"entity", "concept", "individual", "instance", "focal_concept"}:
        return TARGET_BINDING_ENTITY
    if cleaned == TARGET_BINDING_TYPE:
        return TARGET_BINDING_TYPE
    if cleaned == TARGET_BINDING_ENTITY:
        return TARGET_BINDING_ENTITY
    return TARGET_BINDING_UNKNOWN


def _normalise_matching_policy(value: Any) -> str:
    cleaned = (_clean_text(value) or "").lower().replace("-", "_")
    aliases = {
        "": TARGET_MATCH_EXACT,
        "exact_only": TARGET_MATCH_EXACT,
        "subtype": TARGET_MATCH_EXACT_OR_SUBTYPE,
        "allow_subtype": TARGET_MATCH_EXACT_OR_SUBTYPE,
        "subtype_allowed": TARGET_MATCH_EXACT_OR_SUBTYPE,
        "supertype": TARGET_MATCH_EXACT_OR_SUPERTYPE,
        "allow_supertype": TARGET_MATCH_EXACT_OR_SUPERTYPE,
        "supertype_allowed": TARGET_MATCH_EXACT_OR_SUPERTYPE,
        "type_hierarchy": TARGET_MATCH_EXACT_OR_TYPE_HIERARCHY,
        "allow_type_hierarchy": TARGET_MATCH_EXACT_OR_TYPE_HIERARCHY,
    }
    policy = aliases.get(cleaned, cleaned)
    return policy if policy in TARGET_MATCH_POLICIES else TARGET_MATCH_EXACT


def _normalise_resolution_status(
    value: Any,
    *,
    has_symbols: bool,
    has_candidates: bool,
) -> str:
    cleaned = (_clean_text(value) or "").lower().replace("-", "_")
    aliases = {
        "exact": RESOLUTION_STATUS_RESOLVED,
        "resolved_symbolic": RESOLUTION_STATUS_RESOLVED,
        "symbolic_resolved": RESOLUTION_STATUS_RESOLVED,
        "candidate": RESOLUTION_STATUS_CANDIDATE_ONLY,
        "candidates": RESOLUTION_STATUS_CANDIDATE_ONLY,
        "candidate_target": RESOLUTION_STATUS_CANDIDATE_ONLY,
        "candidate_targets": RESOLUTION_STATUS_CANDIDATE_ONLY,
        "needs_resolution": RESOLUTION_STATUS_UNRESOLVED,
    }
    status = aliases.get(cleaned, cleaned)
    if status:
        return status
    if has_symbols:
        return RESOLUTION_STATUS_RESOLVED
    if has_candidates:
        return RESOLUTION_STATUS_CANDIDATE_ONLY
    return RESOLUTION_STATUS_UNRESOLVED


def extract_vontology_concept_ids_from_text(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    return _dedupe_strings(
        [
            match.group(0).rstrip(".,;:!?)]}'\"")
            for match in VONTOLOGY_CONCEPT_ID_PATTERN.finditer(value)
        ]
    )


def _concept_id_values(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return extract_vontology_concept_ids_from_text(value)
    if isinstance(value, Mapping):
        ordered: list[str] = []
        for key in (
            *TARGET_CONCEPT_ID_FIELDS,
            *TARGET_TYPE_ID_FIELDS,
            *CANDIDATE_CONCEPT_ID_FIELDS,
        ):
            if key in value:
                ordered.extend(_concept_id_values(value.get(key)))
        return _dedupe_strings(ordered)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return ()
    ordered = []
    for item in value:
        ordered.extend(_concept_id_values(item))
    return _dedupe_strings(ordered)


def _extract_concept_ids(
    payload: Mapping[str, Any],
    fields: Sequence[str],
) -> tuple[str, ...]:
    ordered: list[str] = []
    for field_name in fields:
        if field_name in payload:
            ordered.extend(_concept_id_values(payload.get(field_name)))
    return _dedupe_strings(ordered)


def _extract_lineage(value: Any) -> tuple[dict[str, Any], ...]:
    raw_lineage = value
    if isinstance(value, Mapping):
        raw_lineage = (
            value.get("resolution_lineage")
            or value.get("lineage")
            or value.get("resolution_evidence")
        )
    if isinstance(raw_lineage, Mapping):
        raw_lineage = [raw_lineage]
    if not isinstance(raw_lineage, Sequence) or isinstance(
        raw_lineage, (str, bytes, bytearray)
    ):
        return ()
    lineage: list[dict[str, Any]] = []
    for item in raw_lineage:
        copied = _copy_mapping(item)
        if copied:
            lineage.append(copied)
    return tuple(lineage)


@dataclass(frozen=True)
class TurnTargetContract:
    kind: str = TARGET_KIND_NATURAL_LANGUAGE
    binding_kind: str = TARGET_BINDING_UNKNOWN
    text: str | None = None
    concept_ids: tuple[str, ...] = ()
    candidate_concept_ids: tuple[str, ...] = ()
    resolution_status: str = RESOLUTION_STATUS_UNRESOLVED
    matching_policy: str = TARGET_MATCH_EXACT
    resolution_lineage: tuple[Mapping[str, Any], ...] = ()
    source: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Any,
        *,
        binding_kind: str | None = None,
        source: str | None = None,
    ) -> TurnTargetContract | None:
        if isinstance(value, str):
            concept_ids = extract_vontology_concept_ids_from_text(value)
            clean_value = _clean_text(value)
            inferred_kind = (
                TARGET_KIND_SYMBOLIC if concept_ids else TARGET_KIND_NATURAL_LANGUAGE
            )
            return cls(
                kind=inferred_kind,
                binding_kind=_normalise_binding_kind(binding_kind),
                text=clean_value if not concept_ids else None,
                concept_ids=concept_ids,
                resolution_status=(
                    RESOLUTION_STATUS_RESOLVED
                    if concept_ids
                    else RESOLUTION_STATUS_UNRESOLVED
                ),
                source=source,
            )

        payload = _copy_mapping(value)
        if payload is None:
            return None
        if payload.get("schema_version") == TURN_TARGET_CONTRACT_SCHEMA_VERSION:
            fields = payload.get("fields")
            if isinstance(fields, Mapping):
                payload = _copy_mapping(fields) or {}

        concept_ids = _extract_concept_ids(payload, TARGET_CONCEPT_ID_FIELDS)
        type_ids = _extract_concept_ids(payload, TARGET_TYPE_ID_FIELDS)
        candidate_ids = _extract_concept_ids(payload, CANDIDATE_CONCEPT_ID_FIELDS)
        all_concept_ids = _dedupe_strings([*concept_ids, *type_ids])
        resolved_binding_kind = _normalise_binding_kind(
            binding_kind or payload.get("binding_kind") or payload.get("target_kind")
        )
        if resolved_binding_kind == TARGET_BINDING_UNKNOWN and type_ids:
            resolved_binding_kind = TARGET_BINDING_TYPE

        text = None
        for field_name in TEXT_FIELDS:
            text = _clean_text(payload.get(field_name))
            if text:
                break
        if text is None:
            target_value = payload.get("target")
            if isinstance(target_value, str) and not _concept_id_values(target_value):
                text = _clean_text(target_value)

        kind = _normalise_kind(
            payload.get("kind") or payload.get("target_contract_kind"),
            has_symbols=bool(all_concept_ids),
            has_candidates=bool(candidate_ids),
        )
        resolution_status = _normalise_resolution_status(
            payload.get("resolution_status") or payload.get("status"),
            has_symbols=bool(all_concept_ids),
            has_candidates=bool(candidate_ids),
        )
        return cls(
            kind=kind,
            binding_kind=resolved_binding_kind,
            text=text,
            concept_ids=all_concept_ids,
            candidate_concept_ids=candidate_ids,
            resolution_status=resolution_status,
            matching_policy=_normalise_matching_policy(
                payload.get("matching_policy")
                or payload.get("target_matching_policy")
                or payload.get("match_policy")
            ),
            resolution_lineage=_extract_lineage(payload),
            source=source or _clean_text(payload.get("source")),
        )

    @classmethod
    def symbolic(
        cls,
        *,
        concept_ids: Sequence[Any],
        binding_kind: str,
        source: str | None = None,
    ) -> TurnTargetContract | None:
        cleaned = _dedupe_strings(concept_ids)
        if not cleaned:
            return None
        return cls(
            kind=TARGET_KIND_SYMBOLIC,
            binding_kind=_normalise_binding_kind(binding_kind),
            concept_ids=cleaned,
            resolution_status=RESOLUTION_STATUS_RESOLVED,
            matching_policy=TARGET_MATCH_EXACT,
            source=source,
        )

    def is_symbolically_resolved(self) -> bool:
        if not self.concept_ids:
            return False
        if self.kind == TARGET_KIND_NATURAL_LANGUAGE:
            return False
        return self.resolution_status not in RESOLUTION_STATUSES_REQUIRING_RESOLUTION

    def requires_resolution_for_symbolic_tool(self) -> bool:
        return not self.is_symbolically_resolved()

    def to_state_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": TURN_TARGET_CONTRACT_SCHEMA_VERSION,
            "kind": self.kind,
            "binding_kind": self.binding_kind,
            "resolution_status": self.resolution_status,
            "matching_policy": self.matching_policy,
        }
        if self.text:
            payload["text"] = self.text
        if self.concept_ids:
            payload["concept_ids"] = list(self.concept_ids)
        if self.candidate_concept_ids:
            payload["candidate_concept_ids"] = list(self.candidate_concept_ids)
        if self.resolution_lineage:
            payload["resolution_lineage"] = [
                dict(item)
                for item in self.resolution_lineage
                if isinstance(item, Mapping)
            ]
        if self.source:
            payload["source"] = self.source
        return payload

    def fingerprint(self) -> tuple[Any, ...]:
        return (
            self.kind,
            self.binding_kind,
            self.text or "",
            tuple(item.lower() for item in self.concept_ids),
            tuple(item.lower() for item in self.candidate_concept_ids),
            self.resolution_status,
            self.matching_policy,
        )


def _target_contracts_from_sequence(
    value: Any,
    *,
    source: str,
) -> tuple[TurnTargetContract, ...]:
    if isinstance(value, Mapping) or isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        return ()
    contracts: list[TurnTargetContract] = []
    for item in values:
        contract = TurnTargetContract.from_mapping(item, source=source)
        if contract is not None:
            contracts.append(contract)
    return dedupe_target_contracts(contracts)


def dedupe_target_contracts(
    contracts: Sequence[TurnTargetContract],
) -> tuple[TurnTargetContract, ...]:
    seen: set[tuple[Any, ...]] = set()
    ordered: list[TurnTargetContract] = []
    for contract in contracts:
        fingerprint = contract.fingerprint()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ordered.append(contract)
    return tuple(ordered)


def extract_target_contracts_from_payload(value: Any) -> tuple[TurnTargetContract, ...]:
    payload = _copy_mapping(value)
    if payload is None:
        return ()
    field_payload = payload
    if isinstance(payload.get("fields"), Mapping):
        field_payload = _copy_mapping(payload.get("fields")) or {}

    contracts: list[TurnTargetContract] = []
    for source_payload in (payload, field_payload):
        for field_name in TARGET_CONTRACT_FIELDS:
            if field_name in source_payload:
                contracts.extend(
                    _target_contracts_from_sequence(
                        source_payload.get(field_name),
                        source=field_name,
                    )
                )
        if "target" in source_payload:
            contracts.extend(
                _target_contracts_from_sequence(
                    source_payload.get("target"),
                    source="target",
                )
            )

    concept_ids = _extract_concept_ids(payload, TARGET_CONCEPT_ID_FIELDS)
    concept_ids = _dedupe_strings(
        [*concept_ids, *_extract_concept_ids(field_payload, TARGET_CONCEPT_ID_FIELDS)]
    )
    type_ids = _extract_concept_ids(payload, TARGET_TYPE_ID_FIELDS)
    type_ids = _dedupe_strings(
        [*type_ids, *_extract_concept_ids(field_payload, TARGET_TYPE_ID_FIELDS)]
    )
    concept_contract = TurnTargetContract.symbolic(
        concept_ids=concept_ids,
        binding_kind=TARGET_BINDING_ENTITY,
        source="target_concept_ids",
    )
    type_contract = TurnTargetContract.symbolic(
        concept_ids=type_ids,
        binding_kind=TARGET_BINDING_TYPE,
        source="target_type_ids",
    )
    if concept_contract is not None:
        contracts.append(concept_contract)
    if type_contract is not None:
        contracts.append(type_contract)
    return dedupe_target_contracts(contracts)


__all__ = [
    "RESOLUTION_STATUS_AMBIGUOUS",
    "RESOLUTION_STATUS_CANDIDATE_ONLY",
    "RESOLUTION_STATUS_RESOLVED",
    "RESOLUTION_STATUS_UNRESOLVED",
    "TARGET_BINDING_ENTITY",
    "TARGET_BINDING_TYPE",
    "TARGET_BINDING_UNKNOWN",
    "TARGET_KIND_HYBRID",
    "TARGET_KIND_NATURAL_LANGUAGE",
    "TARGET_KIND_SYMBOLIC",
    "TARGET_MATCH_EXACT",
    "TARGET_MATCH_EXACT_OR_SUBTYPE",
    "TARGET_MATCH_EXACT_OR_SUPERTYPE",
    "TARGET_MATCH_EXACT_OR_TYPE_HIERARCHY",
    "TURN_TARGET_CONTRACTS_CONTEXT_KEY",
    "TURN_TARGET_CONTRACT_SCHEMA_VERSION",
    "TurnTargetContract",
    "dedupe_target_contracts",
    "extract_target_contracts_from_payload",
    "extract_vontology_concept_ids_from_text",
]
