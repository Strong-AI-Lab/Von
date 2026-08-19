"""Virtual concepts resolve from registered providers rather than stored rows.

JVNAUTOSCI-2650. Copying derived data into the store that describes it is what
went wrong in JVNAUTOSCI-2615: tool descriptions were written into #V#mcp_tool
concepts once at bootstrap and never refreshed, so frozen text suppressed later
code changes and the published surface varied with database state.

Resolving on demand removes the copy. These tests pin the properties that make
that safe: a materialised concept still wins, providers are read-only, a
provider must promise public visibility, and resolution cannot be influenced by
database state.
"""

import os

import pytest

os.environ.setdefault("ATLASSIAN_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("ATLASSIAN_EMAIL", "agent@example.com")
os.environ.setdefault("ATLASSIAN_API_TOKEN", "token-for-import")

from src.backend.vontology import virtual_concept_providers as vcp  # noqa: E402
from src.backend.vontology.virtual_concept_sources import (  # noqa: E402
    CODE_CONCEPT_SOURCE_ID,
    MCP_TOOL_SOURCE_ID,
    MCP_TOOL_TYPE_ID,
    tool_concept_id,
    tool_name_from_concept_id,
)


class _StubProvider:
    def __init__(self, source_id="stub", public=True, ids=("#V#stub_thing",)):
        self.source_id = source_id
        self.serves_public_concepts = public
        self._ids = set(ids)

    def owns(self, concept_id):
        return concept_id in self._ids

    def get(self, concept_id):
        if concept_id not in self._ids:
            return None
        return {"concept_id": concept_id, "name": "Stub"}

    def iter_concepts(self):
        return [self.get(i) for i in sorted(self._ids)]


@pytest.fixture
def stub_registered():
    provider = _StubProvider()
    vcp.register_provider(provider)
    try:
        yield provider
    finally:
        vcp.unregister_provider(provider.source_id)


# ---------------------------------------------------------
# Registration contract
# ---------------------------------------------------------


def test_provider_must_declare_public_visibility():
    """Claiming a concept grants unconditional visibility in access control."""
    with pytest.raises(ValueError, match="serves_public_concepts"):
        vcp.register_provider(_StubProvider(source_id="scoped", public=False))

    assert "scoped" not in vcp.registered_source_ids()


def test_provider_must_declare_a_source_id():
    with pytest.raises(ValueError, match="source_id"):
        vcp.register_provider(_StubProvider(source_id="  "))


def test_duplicate_source_ids_are_refused(stub_registered):
    with pytest.raises(ValueError, match="Duplicate"):
        vcp.register_provider(_StubProvider(source_id=stub_registered.source_id))


def test_built_in_providers_are_registered():
    sources = vcp.registered_source_ids()

    assert CODE_CONCEPT_SOURCE_ID in sources
    assert MCP_TOOL_SOURCE_ID in sources


# ---------------------------------------------------------
# Resolution
# ---------------------------------------------------------


def test_unknown_id_resolves_to_none_not_an_error():
    """A provider miss is an unresolved lookup, not proof of non-existence."""
    assert vcp.resolve_virtual_concept("#V#definitely_not_registered") is None
    assert vcp.is_virtual_concept_id("#V#definitely_not_registered") is False


@pytest.mark.parametrize("bad", [None, "", 17, [], {}])
def test_resolution_tolerates_malformed_ids(bad):
    assert vcp.owning_provider(bad) is None


def test_resolved_documents_carry_provider_provenance(stub_registered):
    doc = vcp.resolve_virtual_concept("#V#stub_thing")

    assert doc["metadata"]["virtual"] is True
    assert doc["metadata"]["virtual_source"] == "stub"


def test_a_broken_provider_does_not_break_the_others(monkeypatch, stub_registered):
    def _explode(_concept_id):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(stub_registered, "owns", _explode)

    # The code registry must still resolve despite the broken provider.
    assert vcp.is_virtual_concept_id(_a_code_concept_id()) is True


def _a_code_concept_id():
    from src.backend.vontology.code_concepts_registry import iter_code_concepts

    return next(iter(iter_code_concepts())).concept_id


# ---------------------------------------------------------
# Read-only enforcement
# ---------------------------------------------------------


def test_writes_to_a_virtual_concept_are_refused_naming_the_source():
    refusal = vcp.virtual_write_refusal(tool_concept_id("fetch_concept"))

    assert isinstance(refusal, vcp.VirtualConceptWriteError)
    assert refusal.source_id == MCP_TOOL_SOURCE_ID
    assert MCP_TOOL_SOURCE_ID in str(refusal)


def test_writes_to_an_ordinary_concept_are_not_refused():
    assert vcp.virtual_write_refusal("#V#person") is None


# ---------------------------------------------------------
# Code concept provider preserves prior behaviour
# ---------------------------------------------------------


def test_code_concepts_still_resolve_through_the_registry():
    concept_id = _a_code_concept_id()

    doc = vcp.resolve_virtual_concept(concept_id)

    assert doc is not None
    assert doc["concept_id"] == concept_id
    assert doc["metadata"]["virtual_source"] == CODE_CONCEPT_SOURCE_ID


def test_build_virtual_concept_doc_still_serves_code_concepts():
    from src.backend.vontology.code_concepts_registry import build_virtual_concept_doc

    concept_id = _a_code_concept_id()
    doc = build_virtual_concept_doc(concept_id)

    assert doc is not None
    assert doc["concept_id"] == concept_id


# ---------------------------------------------------------
# MCP tool provider
# ---------------------------------------------------------


def test_tool_concept_identity_is_deterministic_and_round_trips():
    assert tool_concept_id("fetch_concept") == "#V#fetch_concept_tool"
    assert tool_name_from_concept_id("#V#fetch_concept_tool") == "fetch_concept"
    assert tool_name_from_concept_id("#V#person") is None
    assert tool_name_from_concept_id("not_a_concept_id") is None
    assert tool_name_from_concept_id(None) is None


def test_every_registered_tool_resolves_as_a_concept():
    from src.backend.integrations.internal_mcp.tool_contract_registry import (
        get_canonical_tool_registry,
    )

    contracts = get_canonical_tool_registry()
    assert contracts

    unresolved = [
        name
        for name in contracts
        if vcp.resolve_virtual_concept(tool_concept_id(name)) is None
    ]

    assert not unresolved, f"tools not resolvable as concepts: {unresolved[:5]}"


def test_tool_concept_matches_the_code_authoritative_contract():
    from src.backend.integrations.internal_mcp.tool_contract_registry import (
        get_canonical_tool_registry,
    )

    contract = get_canonical_tool_registry()["fetch_concept"]
    doc = vcp.resolve_virtual_concept(tool_concept_id("fetch_concept"))

    assert doc["md_content"] == contract.description
    assert doc["metadata"]["operation_category"] == contract.category
    assert doc["attributes"]["mcp_tool_name"] == "fetch_concept"
    assert doc["relationships"]["is_an_instance_of"] == [MCP_TOOL_TYPE_ID]


def test_tool_concepts_are_not_influenced_by_database_state(monkeypatch):
    """The whole point of deriving them: no stored row can change the answer."""
    import src.backend.services.tool_metadata_service as tms

    before = vcp.resolve_virtual_concept(tool_concept_id("fetch_concept"))

    hostile = tms.ToolMetadata(
        tool_name="fetch_concept",
        description="Replaced by a database row.",
        operation_category="write",
    )
    monkeypatch.setitem(tms._tool_metadata_cache, "fetch_concept", hostile)
    monkeypatch.setattr(tms, "_cache_loaded", True)
    monkeypatch.setattr(tms, "_cache_timestamp", float("inf"))

    from src.backend.integrations.internal_mcp import tool_contract_registry

    tool_contract_registry.invalidate_canonical_tool_registry()
    try:
        after = vcp.resolve_virtual_concept(tool_concept_id("fetch_concept"))
        assert after["md_content"] == before["md_content"]
        assert after["metadata"]["operation_category"] == "read"
    finally:
        tool_contract_registry.invalidate_canonical_tool_registry()


def test_resolution_is_repeatable():
    first = vcp.resolve_virtual_concept(tool_concept_id("fetch_concept"))
    second = vcp.resolve_virtual_concept(tool_concept_id("fetch_concept"))

    assert first == second


def test_iteration_covers_both_sources_without_duplicates():
    docs = list(vcp.iter_virtual_concepts())
    ids = [doc["concept_id"] for doc in docs]

    assert len(ids) == len(set(ids))
    sources = {doc["metadata"]["virtual_source"] for doc in docs}
    assert {CODE_CONCEPT_SOURCE_ID, MCP_TOOL_SOURCE_ID} <= sources


def test_iteration_can_be_filtered_to_one_source():
    docs = list(vcp.iter_virtual_concepts(source_id=MCP_TOOL_SOURCE_ID))

    assert docs
    assert all(
        doc["metadata"]["virtual_source"] == MCP_TOOL_SOURCE_ID for doc in docs
    )


def test_owns_short_circuits_before_consulting_the_catalogue(monkeypatch):
    """An ordinary concept id must not pay for a catalogue build."""
    from src.backend.vontology.virtual_concept_sources import McpToolConceptProvider

    provider = McpToolConceptProvider()

    def _fail():
        raise AssertionError("catalogue consulted for a non-tool id")

    monkeypatch.setattr(provider, "_contracts", _fail)

    assert provider.owns("#V#person") is False
    assert provider.owns("not-a-concept") is False


def test_owns_never_builds_the_contract_registry(monkeypatch):
    """Regression: building contracts here recurses without terminating.

    Contract construction loads tool metadata from Vontology, which runs an
    access-control check, which asks whether the concept is virtual, which
    returns here. lru_cache does not guard re-entrancy, so every level rebuilt
    from scratch and a single fetch never completed.
    """
    from src.backend.vontology.virtual_concept_sources import McpToolConceptProvider

    provider = McpToolConceptProvider()

    def _fail():
        raise AssertionError("owns() must not build the contract registry")

    monkeypatch.setattr(provider, "_contracts", _fail)

    assert provider.owns(tool_concept_id("fetch_concept")) is True
    assert provider.owns("#V#no_such_tool_tool") is False


def test_cheap_tool_name_set_matches_the_full_registry():
    """The name set used by owns() must not drift from the built contracts."""
    from src.backend.integrations.internal_mcp.tool_contract_registry import (
        get_canonical_tool_registry,
    )
    from src.backend.vontology.virtual_concept_sources import _registered_tool_names

    assert _registered_tool_names() == frozenset(get_canonical_tool_registry())


def test_resolution_refuses_to_recurse(monkeypatch, stub_registered):
    """A provider that re-enters resolution gets a miss, not a stack overflow."""
    seen = []

    def _reentrant(concept_id):
        seen.append(concept_id)
        # Simulates access control asking about virtual concepts mid-resolution.
        vcp.is_virtual_concept_id("#V#stub_thing")
        return concept_id == "#V#stub_thing"

    monkeypatch.setattr(stub_registered, "owns", _reentrant)

    assert vcp.is_virtual_concept_id("#V#stub_thing") is True
    assert len(seen) < 5, f"re-entered {len(seen)} times"
