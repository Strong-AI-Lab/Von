"""Unit tests for the Markdown to ADF converter in mcp_server.py."""

import sys
from pathlib import Path

# Allow importing the MCP server module
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src" / "backend"))


def test_markdown_to_adf_plain_text():
    """Plain text becomes a paragraph."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("Hello world")
    assert result["type"] == "doc"
    assert result["version"] == 1
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "paragraph"
    assert result["content"][0]["content"][0]["text"] == "Hello world"


def test_markdown_to_adf_empty():
    """Empty string returns empty paragraph."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("")
    assert result["type"] == "doc"
    assert result["version"] == 1
    assert result["content"][0]["type"] == "paragraph"


def test_markdown_to_adf_heading():
    """Headings are converted correctly."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("# Main Title\n\n## Subtitle")
    assert result["content"][0]["type"] == "heading"
    assert result["content"][0]["attrs"]["level"] == 1
    assert result["content"][0]["content"][0]["text"] == "Main Title"
    assert result["content"][1]["type"] == "heading"
    assert result["content"][1]["attrs"]["level"] == 2


def test_markdown_to_adf_bullet_list():
    """Bullet lists are converted."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("- Item 1\n- Item 2\n- Item 3")
    assert result["content"][0]["type"] == "bulletList"
    items = result["content"][0]["content"]
    assert len(items) == 3
    assert items[0]["type"] == "listItem"


def test_markdown_to_adf_numbered_list():
    """Numbered lists are converted."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("1. First\n2. Second\n3. Third")
    assert result["content"][0]["type"] == "orderedList"
    items = result["content"][0]["content"]
    assert len(items) == 3


def test_markdown_to_adf_code_block():
    """Code blocks are converted."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("```python\ndef hello():\n    pass\n```")
    assert result["content"][0]["type"] == "codeBlock"
    assert result["content"][0]["attrs"]["language"] == "python"
    assert "def hello():" in result["content"][0]["content"][0]["text"]


def test_markdown_to_adf_bold():
    """Bold text is marked."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("This is **bold** text")
    para = result["content"][0]
    assert para["type"] == "paragraph"
    # Find the bold element
    bold_found = False
    for item in para["content"]:
        if item.get("text") == "bold" and item.get("marks"):
            for mark in item["marks"]:
                if mark["type"] == "strong":
                    bold_found = True
    assert bold_found, "Bold mark not found"


def test_markdown_to_adf_italic():
    """Italic text is marked."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("This is *italic* text")
    para = result["content"][0]
    italic_found = False
    for item in para["content"]:
        if item.get("text") == "italic" and item.get("marks"):
            for mark in item["marks"]:
                if mark["type"] == "em":
                    italic_found = True
    assert italic_found, "Italic mark not found"


def test_markdown_to_adf_inline_code():
    """Inline code is marked."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("Use `print()` function")
    para = result["content"][0]
    code_found = False
    for item in para["content"]:
        if item.get("text") == "print()" and item.get("marks"):
            for mark in item["marks"]:
                if mark["type"] == "code":
                    code_found = True
    assert code_found, "Code mark not found"


def test_markdown_to_adf_link():
    """Links are converted."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("Visit [Google](https://google.com)")
    para = result["content"][0]
    link_found = False
    for item in para["content"]:
        if item.get("text") == "Google" and item.get("marks"):
            for mark in item["marks"]:
                if mark["type"] == "link" and mark["attrs"]["href"] == "https://google.com":
                    link_found = True
    assert link_found, "Link mark not found"


def test_markdown_to_adf_multiline_paragraph():
    """Multiple lines without blank become single paragraph."""
    from mcp_server.mcp_server import _markdown_to_adf

    result = _markdown_to_adf("Line one\nLine two\nLine three")
    assert len(result["content"]) == 1
    assert result["content"][0]["type"] == "paragraph"
    text = result["content"][0]["content"][0]["text"]
    assert "Line one" in text and "Line two" in text and "Line three" in text


def test_markdown_to_adf_complex_document():
    """Complex document with mixed elements."""
    from mcp_server.mcp_server import _markdown_to_adf

    md = """# Task Title

Implement diagram-aware extraction.

## Requirements

- Integrate with PDF parsing
- Use open-source tools

## Code Example

```python
def extract():
    pass
```

See [docs](https://example.com) for details.
"""
    result = _markdown_to_adf(md)
    types_found = [c["type"] for c in result["content"]]
    assert "heading" in types_found
    assert "paragraph" in types_found
    assert "bulletList" in types_found
    assert "codeBlock" in types_found


def test_ensure_adf_passthrough():
    """_ensure_adf passes through non-strings."""
    from mcp_server.mcp_server import _ensure_adf

    # Already ADF dict - should pass through
    adf = {"type": "doc", "version": 1, "content": []}
    assert _ensure_adf(adf) is adf

    # None - should pass through
    assert _ensure_adf(None) is None

    # Integer - should pass through
    assert _ensure_adf(42) == 42


def test_ensure_adf_converts_string():
    """_ensure_adf converts strings to ADF."""
    from mcp_server.mcp_server import _ensure_adf

    result = _ensure_adf("Hello")
    assert isinstance(result, dict)
    assert result["type"] == "doc"
