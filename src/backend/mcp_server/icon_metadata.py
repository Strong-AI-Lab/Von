"""Shared MCP icon metadata for Von stdio servers."""

from __future__ import annotations

import base64
import struct
from pathlib import Path

from mcp.types import Icon


def _extract_largest_png_from_ico(icon_path: Path) -> bytes | None:
    try:
        raw_icon = icon_path.read_bytes()
    except OSError:
        return None

    if len(raw_icon) < 6:
        return None

    reserved, icon_type, image_count = struct.unpack_from("<HHH", raw_icon, 0)
    if reserved != 0 or icon_type != 1 or image_count < 1:
        return None

    best_png: bytes | None = None
    best_area = -1
    directory_offset = 6
    for index in range(image_count):
        entry_offset = directory_offset + (index * 16)
        if entry_offset + 16 > len(raw_icon):
            break

        width_byte, height_byte, _colour_count, _reserved = struct.unpack_from(
            "<BBBB", raw_icon, entry_offset
        )
        size, image_offset = struct.unpack_from("<II", raw_icon, entry_offset + 8)
        if size <= 0 or image_offset < 0 or image_offset + size > len(raw_icon):
            continue

        image = raw_icon[image_offset : image_offset + size]
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            continue

        width = 256 if width_byte == 0 else width_byte
        height = 256 if height_byte == 0 else height_byte
        area = width * height
        if area > best_area:
            best_area = area
            best_png = image

    return best_png


def von_mcp_icons(project_root: str | Path) -> list[Icon]:
    """Return compact Von icon metadata for MCP clients."""
    root = Path(project_root)
    favicon_path = root / "src/frontend/web/von_interface/static/favicon.ico"
    png_icon = _extract_largest_png_from_ico(favicon_path)
    if png_icon is None:
        return []

    encoded_icon = base64.b64encode(png_icon).decode("ascii")
    return [
        Icon(
            src=f"data:image/png;base64,{encoded_icon}",
            mimeType="image/png",
            sizes=["32x32"],
        )
    ]
