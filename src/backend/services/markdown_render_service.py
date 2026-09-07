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


def _normalise_bs4_attr_to_str(value: object) -> str | None:
    """Normalise BeautifulSoup attribute values.

    BeautifulSoup may represent attribute values as a string, list-like, or other
    scalar types. Our sanitiser only needs a string (or None) for safety checks.
    """

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item
        return None
    return str(value)


def sanitise_rendered_html(html: str) -> str:
    """Sanitise rendered HTML using an allow-list.

    Treat all markdown input as untrusted. We only preserve a conservative set
    of tags/attributes required for chat and concept notes rendering.
    """

    from bs4 import Tag
    from typing import cast, Any

    soup = BeautifulSoup(html or "", "html.parser")

    for bad in soup.find_all(list(_DISALLOWED_TAGS)):
        bad.decompose()

    for element in soup.find_all(True):
        if not isinstance(element, Tag):
            continue
        tag = cast(Any, element)  # bs4 Tag - type checker doesn't handle find_all well
        if tag.name not in _ALLOWED_TAGS:
            tag.unwrap()
            continue

        allowed_attrs = _ALLOWED_ATTRS.get(tag.name, set())
        for attr in list(tag.attrs.keys()):
            if attr not in allowed_attrs:
                del tag.attrs[attr]

        if tag.name == "a":
            href = _normalise_bs4_attr_to_str(tag.get("href"))
            archive_citation = re.fullmatch(r"otter-archive://artifact/([a-f0-9]{64})", href or "")
            if archive_citation:
                href = "/von/api/otter-archive/artifacts/" + archive_citation[1]
                tag["href"] = href
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


def _ensure_blank_line_before_top_level_lists(markdown_text: str) -> str:
    """Insert a blank line before a top-level list that immediately follows a paragraph.

    Python-Markdown (especially with sane_lists) can fail to recognise a list that starts
    immediately after a paragraph line with no intervening blank line. This normaliser
    makes that intent explicit while preserving fenced code blocks.
    """

    if not markdown_text:
        return markdown_text

    lines = markdown_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_fence = False

    top_level_list_re = re.compile(r"^([-*+])\s+\S")
    top_level_ordered_re = re.compile(r"^(\d+)\.\s+\S")

    for line in lines:
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            out.append(line)
            continue

        if in_fence:
            out.append(line)
            continue

        is_top_level_list = bool(
            top_level_list_re.match(line) or top_level_ordered_re.match(line)
        )
        if is_top_level_list and out:
            prev = out[-1]
            if prev.strip() != "":
                # Avoid injecting extra blanks between successive list blocks.
                if not re.match(r"^\s*([-*+]|\d+\.|>|#{1,6}\s)", prev):
                    out.append("")

        out.append(line)

    return "\n".join(out)


def _escape_vontology_hash_headings(markdown_text: str) -> str:
    """Escape leading #V# tokens so they cannot be parsed as Markdown headings.

    Python-Markdown accepts ATX headings without requiring a space after the
    leading '#'. This means a plain concept ID like:

        #V#disambiguation_workflow

    can get misparsed as a heading (<h1>) instead of plain text. We escape only
    the specific '#V#' prefix, outside fenced code blocks, so that users can
    still write normal headings.

    Future hardening idea (JVNAUTOSCI-818): if more Markdown edge cases appear,
    consider a full encode/decode pipeline: replace '#V#' with a highly unlikely
    sentinel token before Markdown rendering (outside fences), then decode it
    back to '#V#' afterwards. That avoids all Markdown syntax interactions in a
    single, reliable step.
    """

    if not markdown_text:
        return markdown_text

    lines = markdown_text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_fence = False

    # Cases we need to cover:
    # - Start of a line:        "#V#foo"
    # - Start of list content:  "- #V#foo" (can become a heading inside <li>)
    # - Blockquotes:            "> #V#foo" (can become a heading inside <blockquote>)
    start_of_line_re = re.compile(r"^(\s*)(?<!\\)#V#")
    list_item_re = re.compile(r"^(\s*(?:[-*+]|\d+\.))(\s+)(?<!\\)#V#")
    blockquote_re = re.compile(r"^(\s*>\s*)(?<!\\)#V#")

    for line in lines:
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            out.append(line)
            continue

        if in_fence:
            out.append(line)
            continue

        updated = start_of_line_re.sub(r"\1\\#V#", line, count=1)
        updated = list_item_re.sub(r"\1\2\\#V#", updated, count=1)
        updated = blockquote_re.sub(r"\1\\#V#", updated, count=1)
        out.append(updated)

    return "\n".join(out)


def render_markdown_to_safe_html(
    text: str, *, extensions: Iterable[str] | None = None
) -> str:
    """Render markdown to sanitised HTML.

    The output is safe to insert with innerHTML.
    """

    if not text:
        return ""

    text = _ensure_blank_line_before_top_level_lists(str(text))
    text = _escape_vontology_hash_headings(text)
    text = _normalise_nested_list_indentation(text)

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
