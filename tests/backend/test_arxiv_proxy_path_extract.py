from src.backend.integrations.internal_mcp import arxiv_proxy, arxiv_proxy_mcp


def _assert_extract(module, payload):
    extracted = module._extract_download_file_path(payload)
    assert extracted is not None
    return extracted.replace("\\", "/")


def test_extract_download_file_path_from_text():
    payload = {"text": "data/arxiv_cache/2506.16596.pdf"}

    assert _assert_extract(arxiv_proxy, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )
    assert _assert_extract(arxiv_proxy_mcp, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )


def test_extract_download_file_path_from_nested_payload():
    payload = {"data": {"file_path": "data/arxiv_cache/2506.16596.pdf"}}

    assert _assert_extract(arxiv_proxy, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )
    assert _assert_extract(arxiv_proxy_mcp, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )


def test_extract_download_file_path_from_resource_uri():
    payload = {"resource": {"uri": "file:///data/arxiv_cache/2506.16596.pdf"}}

    assert _assert_extract(arxiv_proxy, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )
    assert _assert_extract(arxiv_proxy_mcp, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )


def test_extract_download_file_path_from_item_list():
    payload = {
        "items": [
            {"type": "text", "text": "downloaded"},
            {"type": "resource", "resource": {"uri": "file:///data/arxiv_cache/2506.16596.pdf"}},
        ]
    }

    assert _assert_extract(arxiv_proxy, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )
    assert _assert_extract(arxiv_proxy_mcp, payload).endswith(
        "data/arxiv_cache/2506.16596.pdf"
    )
