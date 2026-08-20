"""Presentation and planning metadata resolves from code, not stored copies.

JVNAUTOSCI-2652. #V#mcp_tool concepts record display_template, user_salience,
category and planner_hint once at bootstrap and are never refreshed, so a stored
copy silently outranked every later change in code. Twenty of thirty-nine named
tools matched their code defaults exactly and nineteen had drifted, in both
directions, with nothing recording which was intended.

Reconciliation was decided on evidence rather than taste: a display template is
only useful if the renderer can resolve its placeholders, and
_apply_vontology_template discards a template outright when fewer than half
resolve. Several templates on both sides referenced fields the resolver has no
mapping for and were therefore dead.
"""

import os


import pytest

os.environ.setdefault("ATLASSIAN_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("ATLASSIAN_EMAIL", "agent@example.com")
os.environ.setdefault("ATLASSIAN_API_TOKEN", "token-for-import")

import src.backend.services.tool_metadata_service as tms  # noqa: E402
from src.backend.services.tool_metadata_service import (  # noqa: E402
    _DEFAULT_TOOL_METADATA,
    ToolMetadata,
    get_tool_metadata,
)


def _render(template, payload):
    from src.backend.integrations.internal_mcp.orchestrator import (
        InternalMCPChatOrchestrator,
    )

    return InternalMCPChatOrchestrator._apply_vontology_template(template, payload)


# ---------------------------------------------------------
# Templates must actually render
# ---------------------------------------------------------


@pytest.mark.parametrize(
    "template,payload,expected",
    [
        ("Deleted: {concept_id}", {"concept_id": "#V#thing"}, "Deleted: thing"),
        ("Comments: {issue_key}", {"issue_key": "PROJ-1"}, "Comments: PROJ-1"),
        ("Downloaded: {arxiv_id}", {"arxiv_id": "2401.1"}, "Downloaded: 2401.1"),
        (
            "Renamed: {old_name} to {new_name}",
            {"old_name": "a", "new_name": "b"},
            "Renamed: a to b",
        ),
        ("Removed: {removed_count}", {"removed_count": 7}, "Removed: 7"),
    ],
)
def test_a_placeholder_naming_a_payload_field_resolves(template, payload, expected):
    """The named mappings are an arbitrary list; the payload is the real source.

    Twenty-one code templates referenced ordinary identifier fields the mapping
    happened to omit, so the renderer discarded them and fell through to a
    hardcoded handler. That failure is invisible in the interface.
    """
    assert _render(template, payload) == expected


def test_a_template_naming_absent_fields_is_still_discarded():
    """The fallback must not make every template render regardless of payload."""
    assert _render("Deleted: {concept_id}", {"unrelated": "value"}) is None


def test_named_mappings_still_win_over_the_raw_payload():
    """count is derived from several field names and must not be overwritten."""
    rendered = _render("{count} found", {"results": [1, 2, 3]})

    assert rendered == "3 found"


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("read_paper", "Read: {title}"),
        ("finalise_cached_paper", "Finalised: {filename}"),
        (
            "materialise_scholarly_representation_for_file_copy",
            "Materialised paper representation",
        ),
        ("create_task", "Created task: {task_id}"),
    ],
)
def test_reconciled_templates_are_the_resolvable_ones(tool, expected):
    assert _DEFAULT_TOOL_METADATA[tool]["display_template"] == expected


def test_create_task_has_its_own_entry():
    """It does not alias to task_create, so the stored row was its only source."""
    from src.backend.services.tool_metadata_service import (
        preferred_tool_metadata_surface_name,
    )

    assert preferred_tool_metadata_surface_name("create_task") == "create_task"
    assert "create_task" in _DEFAULT_TOOL_METADATA
    assert _DEFAULT_TOOL_METADATA["create_task"]["salience"] == "high"


# ---------------------------------------------------------
# Precedence
# ---------------------------------------------------------


@pytest.fixture
def stored_override(monkeypatch):
    """Simulate a stale #V#mcp_tool row disagreeing with code."""

    def _apply(tool_name, **fields):
        stored = ToolMetadata(tool_name=tool_name, **fields)
        monkeypatch.setattr(
            tms, "_load_from_vontology", lambda: {tool_name: stored}
        )
        monkeypatch.setattr(tms, "_cache_loaded", False)
        monkeypatch.setattr(tms, "_cache_timestamp", 0.0)
        return stored

    return _apply


@pytest.mark.parametrize(
    "field,stored_value",
    [
        ("display_template", "Stale: {name}"),
        ("salience", "low"),
        ("category", "stale_category"),
    ],
)
def test_a_stored_value_cannot_override_code(stored_override, field, stored_value):
    tool = "read_paper"
    expected = _DEFAULT_TOOL_METADATA[tool].get(field)
    assert expected, f"test needs a code value for {field}"

    stored_override(tool, **{field: stored_value})

    assert getattr(get_tool_metadata(tool), field) == expected


def test_planner_hint_remains_represented_first_by_decision(stored_override):
    """Deliberately outside the inversion, and asserted so it stays deliberate.

    planner_hint is model-facing guidance rather than presentation, so runtime
    curation is defensible, and alias inheritance of represented hints is an
    existing tested contract. The cost, recorded on JVNAUTOSCI-2652, is that a
    stale represented hint still outranks a better one in code.
    """
    stored_override("search_concepts", planner_hint="represented hint")

    assert get_tool_metadata("search_concepts").planner_hint == "represented hint"


def test_a_stored_value_still_fills_a_gap_code_leaves(stored_override, monkeypatch):
    """Vontology remains a source for what code does not specify."""
    tool = "read_paper"
    monkeypatch.setitem(
        _DEFAULT_TOOL_METADATA, tool, dict(_DEFAULT_TOOL_METADATA[tool])
    )
    _DEFAULT_TOOL_METADATA[tool].pop("planner_hint", None)

    stored_override(tool, planner_hint="only the row has this")

    assert get_tool_metadata(tool).planner_hint == "only the row has this"


def test_description_precedence_is_unchanged(stored_override):
    """Contract description is governed by PR #401 and out of scope here."""
    stored_override("read_paper", description="from the row")

    assert get_tool_metadata("read_paper").description == "from the row"
