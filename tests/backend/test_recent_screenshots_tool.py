import os
import time


def test_mcp_stdio_server_has_recent_screenshots_handler():
    from src.backend.mcp_server import mcp_stdio_server

    assert "list_recent_screenshots" in mcp_stdio_server._TOOL_HANDLERS


def test_list_recent_screenshots_filters_and_includes_base64(tmp_path):
    from src.backend.integrations.internal_mcp.catalogue import _list_recent_screenshots

    recent_file = tmp_path / "Screenshot 2026-03-02 at 10.00.00.png"
    old_file = tmp_path / "Screenshot 2026-02-20 at 10.00.00.png"

    png_stub = b"\x89PNG\r\n\x1a\nstub"
    recent_file.write_bytes(png_stub)
    old_file.write_bytes(png_stub)

    old_time = time.time() - (72 * 3600)
    os.utime(old_file, (old_time, old_time))

    result = _list_recent_screenshots(
        paths=[str(tmp_path)],
        lookback_hours=24,
        limit=10,
        match_clipboard=False,
        include_base64=True,
    )

    assert result.get("success") is True
    items = result.get("items") or []
    filenames = {item.get("filename") for item in items}
    assert recent_file.name in filenames
    assert old_file.name not in filenames

    recent_item = next(item for item in items if item.get("filename") == recent_file.name)
    assert recent_item.get("mime_type") == "image/png"
    assert isinstance(recent_item.get("content_base64"), str)
    assert recent_item.get("content_base64")

