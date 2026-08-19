"""Registry for virtual concepts resolved on demand from authoritative sources.

A virtual concept is not persisted in MongoDB. It is derived from whatever
already owns the truth — the code registry, the MCP tool catalogue — and
rebuilt on every read, so it cannot go stale and cannot be edited into
disagreement with its source.

This exists because the opposite approach failed concretely (JVNAUTOSCI-2615).
Tool descriptions were copied into #V#mcp_tool concepts at first bootstrap and
never refreshed, so frozen text suppressed later improvements in code and the
published tool surface varied with database state.

Resolution rules:

- A materialised concept always wins. Providers are consulted on the miss path
  only, which makes this a migration route rather than a cutover.
- Providers are read-only. Writes to a virtual concept are refused by
  ``virtual_write_refusal`` with the owning source named.
- A provider must declare ``serves_public_concepts``. Claiming a concept
  currently grants unconditional visibility in access control, so a provider
  serving scoped content must not use this seam until visibility is modelled.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Iterable, Iterator, List, Optional, Protocol, runtime_checkable


class VirtualConceptWriteError(Exception):
    """Raised when a write targets a concept owned by a provider."""

    def __init__(self, concept_id: str, source_id: str):
        self.concept_id = concept_id
        self.source_id = source_id
        super().__init__(
            f"Concept '{concept_id}' is derived from '{source_id}' and cannot be "
            f"written through Vontology. Change it at that source instead."
        )


@runtime_checkable
class VirtualConceptProvider(Protocol):
    """Resolves concepts owned by one authoritative source."""

    @property
    def source_id(self) -> str:
        """Stable identifier for the owning source, used in refusals."""

    @property
    def serves_public_concepts(self) -> bool:
        """Whether every concept served is readable by any actor."""

    def owns(self, concept_id: str) -> bool: ...

    def get(self, concept_id: str) -> Optional[Dict[str, Any]]: ...

    def iter_concepts(self) -> Iterable[Dict[str, Any]]: ...


_providers: List[VirtualConceptProvider] = []
_lock = threading.RLock()
_resolving = threading.local()


def register_provider(provider: VirtualConceptProvider) -> None:
    """Register a provider. Later registrations resolve after earlier ones."""
    source_id = str(getattr(provider, "source_id", "") or "").strip()
    if not source_id:
        raise ValueError("A virtual concept provider must declare a source_id.")
    if not getattr(provider, "serves_public_concepts", False):
        # Claiming a concept grants unconditional visibility in access control,
        # so a provider that cannot make this promise would silently widen scope.
        raise ValueError(
            f"Provider '{source_id}' must declare serves_public_concepts=True. "
            "Scoped virtual content is not supported by this seam yet."
        )
    with _lock:
        if any(existing.source_id == source_id for existing in _providers):
            raise ValueError(f"Duplicate virtual concept provider: '{source_id}'")
        _providers.append(provider)


def unregister_provider(source_id: str) -> bool:
    with _lock:
        for index, provider in enumerate(_providers):
            if provider.source_id == source_id:
                del _providers[index]
                return True
    return False


def registered_source_ids() -> List[str]:
    _ensure_bootstrapped()
    with _lock:
        return [provider.source_id for provider in _providers]


def _ensure_bootstrapped() -> None:
    """Register the built-in providers on first use.

    Imported lazily: the provider implementations import the sources they wrap,
    which would otherwise close an import cycle back to this module.
    """
    with _lock:
        if _providers:
            return
    from .virtual_concept_sources import register_default_virtual_concept_providers

    register_default_virtual_concept_providers()


def _iter_providers() -> Iterator[VirtualConceptProvider]:
    _ensure_bootstrapped()
    with _lock:
        snapshot = tuple(_providers)
    return iter(snapshot)


def owning_provider(concept_id: str) -> Optional[VirtualConceptProvider]:
    if not isinstance(concept_id, str) or not concept_id:
        return None
    # A provider may reach code that asks about virtual concepts again — access
    # control and tool metadata call into each other. Refuse to recurse rather
    # than rebuild the world at every level.
    if getattr(_resolving, "active", False):
        return None
    _resolving.active = True
    try:
        for provider in _iter_providers():
            try:
                if provider.owns(concept_id):
                    return provider
            except Exception:
                # One broken provider must not make every virtual concept
                # unresolvable.
                continue
        return None
    finally:
        _resolving.active = False


def is_virtual_concept_id(concept_id: str) -> bool:
    return owning_provider(concept_id) is not None


def virtual_concept_source(concept_id: str) -> Optional[str]:
    provider = owning_provider(concept_id)
    return provider.source_id if provider is not None else None


def resolve_virtual_concept(concept_id: str) -> Optional[Dict[str, Any]]:
    """Return a Mongo-shaped document for a virtual concept, or None.

    None means no provider claims this id. That is an unresolved virtual
    lookup, not evidence that the concept does not exist.
    """
    provider = owning_provider(concept_id)
    if provider is None:
        return None
    try:
        doc = provider.get(concept_id)
    except Exception:
        return None
    if not isinstance(doc, dict):
        return None
    return _stamp_provenance(doc, provider.source_id)


def iter_virtual_concepts(
    *, source_id: Optional[str] = None
) -> Iterator[Dict[str, Any]]:
    """Yield every virtual concept, optionally from one source."""
    seen: set[str] = set()
    for provider in _iter_providers():
        if source_id is not None and provider.source_id != source_id:
            continue
        try:
            concepts = list(provider.iter_concepts())
        except Exception:
            continue
        for doc in concepts:
            if not isinstance(doc, dict):
                continue
            identifier = doc.get("concept_id")
            if not isinstance(identifier, str) or identifier in seen:
                continue
            seen.add(identifier)
            yield _stamp_provenance(doc, provider.source_id)


def virtual_write_refusal(concept_id: str) -> Optional[VirtualConceptWriteError]:
    """Return the error a caller should raise, or None if the write may proceed."""
    provider = owning_provider(concept_id)
    if provider is None:
        return None
    return VirtualConceptWriteError(concept_id, provider.source_id)


def _stamp_provenance(doc: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    stamped = dict(doc)
    metadata = dict(stamped.get("metadata") or {})
    metadata["virtual"] = True
    metadata["virtual_source"] = source_id
    stamped["metadata"] = metadata
    return stamped
