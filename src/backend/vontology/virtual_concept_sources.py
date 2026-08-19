"""Concrete virtual concept providers.

Two sources are registered here. The code concept registry is the pre-existing
behaviour, re-expressed as a provider. The MCP tool catalogue is new: every
registered tool becomes fetchable as a concept without being copied into the
database, which is the arrangement that went stale in JVNAUTOSCI-2615.

Both are imported for their registration side effect by
``register_default_virtual_concept_providers``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, Iterable, Optional

from .virtual_concept_providers import register_provider

MCP_TOOL_TYPE_ID = "#V#mcp_tool"
MCP_TOOL_SOURCE_ID = "mcp_tool_catalogue"
CODE_CONCEPT_SOURCE_ID = "von_code_registry"


class CodeConceptProvider:
    """Serves the concepts handled directly in Von's code."""

    source_id = CODE_CONCEPT_SOURCE_ID
    serves_public_concepts = True

    def owns(self, concept_id: str) -> bool:
        from .code_concepts_registry import is_code_concept_id

        return is_code_concept_id(concept_id)

    def get(self, concept_id: str) -> Optional[Dict[str, Any]]:
        from .code_concepts_registry import build_code_concept_doc

        return build_code_concept_doc(concept_id)

    def iter_concepts(self) -> Iterable[Dict[str, Any]]:
        from .code_concepts_registry import (
            build_code_concept_doc,
            iter_code_concepts,
        )

        for code_concept in iter_code_concepts():
            doc = build_code_concept_doc(code_concept.concept_id)
            if isinstance(doc, dict):
                yield doc


@lru_cache(maxsize=1)
def _registered_tool_names() -> frozenset:
    """Registered tool names, resolved without building contracts.

    Kept separate from the contract registry so ownership checks stay free of
    the metadata and access-control machinery. See McpToolConceptProvider.owns.
    """
    from ..integrations.internal_mcp.catalogue import build_default_catalogue
    from ..integrations.internal_mcp.tool_contract_registry import (
        supplemental_tool_names,
    )

    return frozenset(build_default_catalogue().list_methods()) | supplemental_tool_names()


def invalidate_registered_tool_names() -> None:
    _registered_tool_names.cache_clear()


def tool_concept_id(tool_name: str) -> str:
    """Map a tool name to its concept id.

    Deterministic and matching the convention the existing materialised
    #V#mcp_tool concepts already use, so a virtual concept and the row it will
    eventually replace share one identity and stored relations survive.
    """
    return f"#V#{tool_name}_tool"


def tool_name_from_concept_id(concept_id: str) -> Optional[str]:
    if not isinstance(concept_id, str):
        return None
    if not concept_id.startswith("#V#") or not concept_id.endswith("_tool"):
        return None
    name = concept_id[len("#V#") : -len("_tool")]
    return name or None


class McpToolConceptProvider:
    """Serves one concept per registered MCP tool, derived from the contract.

    The contract is code-authoritative (JVNAUTOSCI-2615, PR #401), so these
    documents are a pure function of source and cannot drift from what the
    tool actually is.
    """

    source_id = MCP_TOOL_SOURCE_ID
    serves_public_concepts = True  # Tool contracts are published in the manifest.

    def _contracts(self) -> Dict[str, Any]:
        from ..integrations.internal_mcp.tool_contract_registry import (
            get_canonical_tool_registry,
        )

        return get_canonical_tool_registry()

    def owns(self, concept_id: str) -> bool:
        name = tool_name_from_concept_id(concept_id)
        if name is None:
            return False
        # Deliberately the name list rather than the contract registry.
        # Building contracts loads tool metadata from Vontology, which runs an
        # access-control check, which consults this provider again. lru_cache
        # does not guard re-entrancy, so that cycle never terminates.
        return name in _registered_tool_names()

    def get(self, concept_id: str) -> Optional[Dict[str, Any]]:
        name = tool_name_from_concept_id(concept_id)
        if name is None:
            return None
        contract = self._contracts().get(name)
        if contract is None:
            return None
        return _tool_concept_doc(name, contract)

    def iter_concepts(self) -> Iterable[Dict[str, Any]]:
        for name, contract in sorted(self._contracts().items()):
            yield _tool_concept_doc(name, contract)


def _tool_concept_doc(tool_name: str, contract: Any) -> Dict[str, Any]:
    concept_id = tool_concept_id(tool_name)
    display_name = f"{tool_name} tool"
    description = getattr(contract, "description", "") or ""
    return {
        "concept_id": concept_id,
        "name": display_name,
        "names": [{"name": display_name, "type": "NL", "language": "en-NZ"}],
        "relationships": {
            "is_a_type_of": [],
            "is_an_instance_of": [MCP_TOOL_TYPE_ID],
        },
        "path": concept_id,
        "md_content": description,
        "attributes": {
            "mcp_tool_name": tool_name,
            "category": getattr(contract, "category", None),
            "family": getattr(contract, "family", None),
        },
        "metadata": {
            "concept_type": "instance",
            "mcp_tool_name": tool_name,
            "operation_category": getattr(contract, "category", None),
        },
    }


_registered = False


def register_default_virtual_concept_providers() -> None:
    """Register the built-in providers once per process."""
    global _registered
    if _registered:
        return
    register_provider(CodeConceptProvider())
    register_provider(McpToolConceptProvider())
    _registered = True
