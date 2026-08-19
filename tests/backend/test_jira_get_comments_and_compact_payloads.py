"""Tests for paginated Jira comment reads and compact MCP payload encoding.

Background: fetching a heavily commented issue through jira_get_issue with
fields=['comment'] returned every comment at once as an ADF node tree inside a
pretty-printed envelope. On JVNAUTOSCI-2615 that turned ~53K of prose into
~296K of payload and tripped the 100K stdio guard, leaving the issue
unreadable through MCP. These tests cover the two fixes: compact serialisation
and a paginated, text-flattening comment reader.
"""

import json
import os

import pytest

os.environ.setdefault("ATLASSIAN_BASE_URL", "https://example.atlassian.net")
os.environ.setdefault("ATLASSIAN_EMAIL", "agent@example.com")
os.environ.setdefault("ATLASSIAN_API_TOKEN", "token-for-import")

from src.backend.mcp_server import mcp_server  # noqa: E402
from src.backend.mcp_server import mcp_stdio_server  # noqa: E402
from src.backend.integrations.internal_mcp.catalogue import (  # noqa: E402
    _jira_get_comments,
)

# ---------------------------------------------------------
# ADF flattening
# ---------------------------------------------------------


def test_adf_to_text_renders_headings_lists_and_code_blocks():
    body = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Delivery phases"}],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Phase 0 is planning only."}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "classify data"}],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "measure Atlas"}],
                            }
                        ],
                    },
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [{"type": "text", "text": "x = 1"}],
            },
        ],
    }

    text = mcp_server._adf_to_text(body)

    assert "## Delivery phases" in text
    assert "Phase 0 is planning only." in text
    assert "- classify data" in text
    assert "- measure Atlas" in text
    assert "```python\nx = 1\n```" in text


def test_adf_to_text_preserves_inline_marks_as_markdown():
    body = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "bold",
                        "marks": [{"type": "strong"}],
                    },
                    {"type": "text", "text": " and "},
                    {"type": "text", "text": "code", "marks": [{"type": "code"}]},
                    {"type": "text", "text": " and "},
                    {"type": "text", "text": "italic", "marks": [{"type": "em"}]},
                    {"type": "text", "text": " and "},
                    {
                        "type": "text",
                        "text": "a link",
                        "marks": [
                            {"type": "link", "attrs": {"href": "https://example.com"}}
                        ],
                    },
                ],
            }
        ],
    }

    text = mcp_server._adf_to_text(body)

    assert "**bold**" in text
    assert "`code`" in text
    assert "*italic*" in text
    assert "[a link](https://example.com)" in text


def test_adf_to_text_round_trips_markdown_built_by_the_inverse():
    """_adf_to_text is the inverse of _markdown_to_adf for supported syntax."""
    source = "# Title\n\nSome prose here.\n\n- first\n- second"

    text = mcp_server._adf_to_text(mcp_server._markdown_to_adf(source))

    assert "# Title" in text
    assert "Some prose here." in text
    assert "- first" in text
    assert "- second" in text


def test_adf_to_text_renders_ordered_lists_and_nested_items():
    body = {
        "type": "doc",
        "content": [
            {
                "type": "orderedList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": "outer"}],
                            },
                            {
                                "type": "bulletList",
                                "content": [
                                    {
                                        "type": "listItem",
                                        "content": [
                                            {
                                                "type": "paragraph",
                                                "content": [
                                                    {"type": "text", "text": "inner"}
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
    }

    text = mcp_server._adf_to_text(body)

    assert "1. outer" in text
    # Nested list keeps its own marker and is indented under the parent item.
    assert "- inner" in text
    assert "  - inner" in text


def test_adf_to_text_renders_tables_as_pipe_rows():
    body = {
        "type": "doc",
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "Dependency"}
                                        ],
                                    }
                                ],
                            },
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "Policy"}],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ],
    }

    text = mcp_server._adf_to_text(body)

    assert "| Dependency | Policy |" in text


@pytest.mark.parametrize("body", [None, "", 17, [], {"type": "doc"}])
def test_adf_to_text_tolerates_missing_or_malformed_bodies(body):
    """A malformed body must degrade to empty text, never raise."""
    assert isinstance(mcp_server._adf_to_text(body), str)


def test_adf_to_text_passes_through_plain_string_bodies():
    """Older Jira REST versions return plain-text comment bodies."""
    assert mcp_server._adf_to_text("already plain") == "already plain"


# ---------------------------------------------------------
# Comment page projection
# ---------------------------------------------------------


def _comment_page():
    return {
        "startAt": 0,
        "maxResults": 50,
        "total": 2,
        "comments": [
            {
                "id": "36692",
                "self": "https://example.atlassian.net/rest/api/3/issue/1/comment/36692",
                "author": {
                    "accountId": "acc-1",
                    "displayName": "Michael Witbrock",
                    "emailAddress": "m@example.com",
                    "active": True,
                    "avatarUrls": {
                        "48x48": "https://avatar.example/48",
                        "24x24": "https://avatar.example/24",
                        "16x16": "https://avatar.example/16",
                        "32x32": "https://avatar.example/32",
                    },
                },
                "updateAuthor": {
                    "accountId": "acc-1",
                    "displayName": "Michael Witbrock",
                    "avatarUrls": {"48x48": "https://avatar.example/48"},
                },
                "created": "2026-07-30T20:54:55.347+1200",
                "updated": "2026-07-30T20:54:55.347+1200",
                "body": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Part 2 of 4"}],
                        }
                    ],
                },
            },
            {
                "id": "36693",
                "author": {"accountId": "acc-2", "displayName": "Reviewer"},
                "created": "2026-07-30T20:55:53.310+1200",
                "visibility": {"type": "role", "value": "Administrators"},
                "body": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Restricted note"}],
                        }
                    ],
                },
            },
        ],
    }


def test_flatten_comment_page_flattens_bodies_and_keeps_pagination():
    flattened = mcp_server._flatten_comment_page(_comment_page())

    assert flattened["startAt"] == 0
    assert flattened["maxResults"] == 50
    assert flattened["total"] == 2
    assert flattened["body_format"] == "text"
    assert flattened["comments"][0]["body"] == "Part 2 of 4"
    assert flattened["comments"][0]["id"] == "36692"
    assert flattened["comments"][0]["created"] == "2026-07-30T20:54:55.347+1200"


def test_flatten_comment_page_drops_avatar_metadata_but_keeps_identity():
    flattened = mcp_server._flatten_comment_page(_comment_page())

    author = flattened["comments"][0]["author"]
    assert author["displayName"] == "Michael Witbrock"
    assert author["accountId"] == "acc-1"
    assert "avatarUrls" not in author
    assert "avatarUrls" not in flattened["comments"][0]["updateAuthor"]
    assert "avatar" not in json.dumps(flattened)


def test_flatten_comment_page_preserves_visibility_restrictions():
    """Visibility marks role-restricted comments and is not a size optimisation."""
    flattened = mcp_server._flatten_comment_page(_comment_page())

    assert flattened["comments"][1]["visibility"] == {
        "type": "role",
        "value": "Administrators",
    }


def test_flatten_comment_page_passes_through_pages_without_comments():
    page = {"error": "not found"}

    assert mcp_server._flatten_comment_page(page) == page


def _realistic_comment_page(count=3):
    """A page whose bodies have the block structure real Jira comments carry.

    Size assertions need this shape: ADF's cost is per node, so a fixture of
    one-line bodies understates the overhead that motivated the change.
    """
    body = mcp_server._markdown_to_adf(
        "## Delivery phases\n\n"
        "Phase 0 is planning only.\n\n"
        "- classify every data class\n"
        "- measure a tuned Atlas baseline\n"
        "- draft the decision record"
    )
    return {
        "startAt": 0,
        "maxResults": 50,
        "total": count,
        "comments": [
            {
                "id": str(index),
                "self": f"https://example.atlassian.net/rest/api/3/issue/1/comment/{index}",
                "author": {
                    "accountId": "acc-1",
                    "displayName": "Michael Witbrock",
                    "avatarUrls": {
                        size: f"https://avatar.example/{size}"
                        for size in ("16x16", "24x24", "32x32", "48x48")
                    },
                },
                "created": "2026-07-30T20:54:55.347+1200",
                "body": body,
            }
            for index in range(count)
        ],
    }


def test_flatten_comment_page_materially_shrinks_the_payload():
    page = _realistic_comment_page()

    raw = mcp_server._tool_json(page)
    flattened = mcp_server._tool_json(mcp_server._flatten_comment_page(page))

    assert len(flattened) < len(raw) / 2


# ---------------------------------------------------------
# Compact serialisation
# ---------------------------------------------------------


def test_tool_json_is_compact():
    payload = {"a": 1, "b": [1, 2], "c": {"d": "e"}}

    text = mcp_server._tool_json(payload)

    assert text == '{"a":1,"b":[1,2],"c":{"d":"e"}}'
    assert "\n" not in text
    assert json.loads(text) == payload


def test_stdio_json_text_is_compact_and_still_parses():
    payload = {"success": True, "rows": [{"id": 1}, {"id": 2}]}

    content = mcp_stdio_server._json_text(payload)

    assert "\n" not in content.text
    assert ": " not in content.text
    assert json.loads(content.text) == payload


def test_stdio_json_text_compaction_lowers_payload_against_the_guard():
    """The guard measures serialised size, so indentation cost was real.

    ADF is many small nodes rather than a few long strings, which is exactly
    the shape where per-line indentation dominates.
    """
    payload = _realistic_comment_page(count=50)

    compact = len(mcp_stdio_server._json_text(payload).text)
    indented = len(json.dumps(payload, indent=2, default=str))

    assert compact < indented / 2


def test_stdio_json_text_still_guards_oversized_payloads(monkeypatch):
    monkeypatch.setenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS", "10000")
    payload = {"rows": ["x" * 100 for _ in range(500)]}

    content = mcp_stdio_server._json_text(payload)
    decoded = json.loads(content.text)

    assert decoded["error_code"] == "payload_too_large"
    assert decoded["error_details"]["max_response_chars"] == 10000


def test_payload_too_large_guidance_points_at_the_paginated_comment_tool(monkeypatch):
    monkeypatch.setenv("VON_MCP_STDIO_MAX_RESPONSE_CHARS", "10000")
    payload = {"rows": ["x" * 100 for _ in range(500)]}

    decoded = json.loads(mcp_stdio_server._json_text(payload).text)

    assert any(
        "jira_get_comments" in suggestion
        for suggestion in decoded.get("suggestions") or []
    )


# ---------------------------------------------------------
# Catalogue handler contract
# ---------------------------------------------------------


def test_jira_get_comments_requires_issue_key():
    result = _jira_get_comments()

    assert result["success"] is False
    assert result["error_code"] == "missing_parameter"


@pytest.mark.parametrize("body_format", ["html", "markdown", "ADF"])
def test_jira_get_comments_rejects_unknown_body_format(body_format):
    result = _jira_get_comments(issue_key="JVNAUTOSCI-2615", body_format=body_format)

    assert result["success"] is False
    assert result["error_code"] == "invalid_parameter"


def test_jira_get_comments_rejects_unknown_order_by():
    result = _jira_get_comments(issue_key="JVNAUTOSCI-2615", order_by="updated")

    assert result["success"] is False
    assert result["error_code"] == "invalid_parameter"


def test_jira_get_comments_forwards_pagination_to_the_proxy(monkeypatch):
    captured = {}

    class _FakeProxy:
        async def get_comments(self, **kwargs):
            captured.update(kwargs)
            return {"total": 6, "comments": []}

    async def _fake_get_jira_proxy():
        return _FakeProxy()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.jira_proxy_mcp.get_jira_proxy",
        _fake_get_jira_proxy,
    )

    result = _jira_get_comments(
        issue_key="JVNAUTOSCI-2615",
        start_at=100,
        max_results=50,
        order_by="created",
    )

    assert result == {"total": 6, "comments": []}
    assert captured["issue_key"] == "JVNAUTOSCI-2615"
    assert captured["start_at"] == 100
    assert captured["max_results"] == 50
    assert captured["order_by"] == "created"


# ---------------------------------------------------------
# Backing server request shaping
# ---------------------------------------------------------


def _call_backing_tool(monkeypatch, arguments, page=None):
    captured = {}

    def _fake_jira_get(endpoint, params=None):
        captured["endpoint"] = endpoint
        captured["params"] = params
        return page if page is not None else _comment_page()

    monkeypatch.setattr(mcp_server, "jira_get", _fake_jira_get)
    import anyio

    content = anyio.run(mcp_server.call_tool, "jira_get_comments", arguments)
    return captured, json.loads(content[0].text)


def test_backing_tool_requests_the_comment_endpoint_with_pagination(monkeypatch):
    captured, _ = _call_backing_tool(
        monkeypatch,
        {
            "issue_key": "JVNAUTOSCI-2615",
            "start_at": 50,
            "max_results": 25,
            "order_by": "-created",
        },
    )

    assert captured["endpoint"] == "issue/JVNAUTOSCI-2615/comment"
    assert captured["params"]["startAt"] == 50
    assert captured["params"]["maxResults"] == 25
    assert captured["params"]["orderBy"] == "-created"


def test_backing_tool_defaults_to_flattened_text_bodies(monkeypatch):
    _, decoded = _call_backing_tool(monkeypatch, {"issue_key": "JVNAUTOSCI-2615"})

    assert decoded["body_format"] == "text"
    assert decoded["comments"][0]["body"] == "Part 2 of 4"


def test_backing_tool_returns_raw_adf_on_request(monkeypatch):
    _, decoded = _call_backing_tool(
        monkeypatch,
        {"issue_key": "JVNAUTOSCI-2615", "body_format": "adf"},
    )

    assert decoded["comments"][0]["body"]["type"] == "doc"


def test_backing_tool_omits_start_at_when_not_requested(monkeypatch):
    captured, _ = _call_backing_tool(monkeypatch, {"issue_key": "JVNAUTOSCI-2615"})

    assert "startAt" not in captured["params"]
    assert captured["params"]["maxResults"] == 50


def test_backing_tool_emits_compact_json(monkeypatch):
    import anyio

    monkeypatch.setattr(mcp_server, "jira_get", lambda e, params=None: _comment_page())
    content = anyio.run(mcp_server.call_tool, "jira_get_comments", {"issue_key": "X-1"})

    assert "\n  " not in content[0].text
    assert json.loads(content[0].text)["total"] == 2


# ---------------------------------------------------------
# Surface exposure
# ---------------------------------------------------------


def test_jira_get_comments_is_exposed_on_read_surfaces():
    from src.backend.integrations.internal_mcp.tool_contract_registry import (
        SURFACE_JIRA_FAMILY_SERVER,
        SURFACE_MANIFEST,
        SURFACE_VONTOLOGY_STDIO,
        get_surface_tool_payloads,
    )

    for surface in (
        SURFACE_VONTOLOGY_STDIO,
        SURFACE_MANIFEST,
        SURFACE_JIRA_FAMILY_SERVER,
    ):
        names = {tool["name"] for tool in get_surface_tool_payloads(surface)}
        assert "jira_get_comments" in names, f"missing on {surface}"


def test_jira_get_comments_is_registered_as_a_read_tool():
    """A comment reader must not land in the write-tool set."""
    from src.backend.services.tool_metadata_service import (
        _DEFAULT_WRITE_TOOL_NAMES,
        _DEFAULT_VERIFICATION_READ_TOOL_NAMES,
    )

    assert "jira_get_comments" not in _DEFAULT_WRITE_TOOL_NAMES
    assert "jira_get_comments" in _DEFAULT_VERIFICATION_READ_TOOL_NAMES


def test_stdio_server_dispatches_jira_get_comments():
    assert "jira_get_comments" in mcp_stdio_server._TOOL_HANDLERS
