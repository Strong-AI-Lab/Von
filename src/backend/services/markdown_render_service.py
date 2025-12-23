from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup
import markdown as markdown_lib


_DISALLOWED_TAGS = {
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "link",
    "meta",
}

_ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "blockquote",
    "ul",
    "ol",
    "li",
    "a",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}

_ALLOWED_ATTRS = {
    "a": {"href", "title", "target", "rel"},
    "code": {"class"},
    "pre": {"class"},
    "th": {"colspan", "rowspan"},
    "td": {"colspan", "rowspan"},
}


def _is_safe_href(href: str | None) -> bool:
    if not href:
        return False

    raw = str(href).strip()
    lower = raw.lower()

    if lower.startswith(("javascript:", "data:", "vbscript:")):
        return False

    # Allow anchors and relative paths.
    if raw.startswith(("#", "/", "./", "../")):
        return True

    return lower.startswith(("http://", "https://", "mailto:"))


def _is_external_http_href(href: str | None) -> bool:
    if not href:
        return False
    lower = str(href).strip().lower()
    return lower.startswith(("http://", "https://"))


def sanitise_rendered_html(html: str) -> str:
    """Sanitise rendered HTML using an allow-list.

    Treat all markdown input as untrusted. We only preserve a conservative set
    of tags/attributes required for chat and concept notes rendering.
    """

    soup = BeautifulSoup(html or "", "html.parser")

    for bad in soup.find_all(list(_DISALLOWED_TAGS)):
        bad.decompose()

    for tag in soup.find_all(True):
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue

        allowed_attrs = _ALLOWED_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs.keys()):
            if attr not in allowed_attrs:
                del tag.attrs[attr]

        if tag.name == "a":
            href = tag.get("href")
            if not _is_safe_href(href):
                if "href" in tag.attrs:
                    del tag.attrs["href"]
            else:
                tag["rel"] = "noopener noreferrer"
                if _is_external_http_href(href):
                    tag["target"] = "_blank"
                elif "target" in tag.attrs:
                    del tag.attrs["target"]

    return str(soup)


_LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+\.)\s+")
_NESTED_LIST_MARKER_2SP_RE = re.compile(r"^  (?:[-*+]|\d+\.)\s+")


def _normalise_nested_list_indentation(text: str) -> str:
    """Normalise some common nested-list markdown patterns.

    Python-Markdown expects nested lists to be indented by at least four
    spaces. Users often write nested list items with two spaces (CommonMark-
    style), e.g.:

    - read:
      - a
      - b

    Without normalisation this gets flattened into a single list. This helper
    promotes two-space-indented list items to four spaces when they immediately
    follow a top-level list item.
    """

    lines = text.splitlines()

    seen_top_level_list_item = False
    in_promoted_nested_block = False

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if not stripped:
            continue

        is_top_level_list_item = (not line.startswith(" ")) and bool(
            _LIST_MARKER_RE.match(line)
        )
        if is_top_level_list_item:
            seen_top_level_list_item = True
            in_promoted_nested_block = False
            continue

        is_two_space_list_item = bool(_NESTED_LIST_MARKER_2SP_RE.match(line))
        if is_two_space_list_item and (
            seen_top_level_list_item or in_promoted_nested_block
        ):
            lines[idx] = "    " + line[2:]
            in_promoted_nested_block = True
            continue

        # Any non-indented, non-list content breaks the list nesting context.
        if not line.startswith(" "):
            seen_top_level_list_item = False
            in_promoted_nested_block = False

    return "\n".join(lines)


def render_markdown_to_safe_html(
    text: str, *, extensions: Iterable[str] | None = None
) -> str:
    """Render markdown to sanitised HTML.

    The output is safe to insert with innerHTML.
    """

    if not text:
        return ""

    text = _normalise_nested_list_indentation(str(text))

    exts = (
        list(extensions)
        if extensions is not None
        else [
            "fenced_code",
            "tables",
            "sane_lists",
            "nl2br",
        ]
    )

    rendered = markdown_lib.markdown(str(text), extensions=exts, output_format="html")
    return sanitise_rendered_html(rendered)
