import asyncio


def test_catalogue_resilient_extract_url_falls_back_to_search(monkeypatch):
    from src.backend.integrations.internal_mcp import catalogue
    from src.backend.integrations.internal_mcp import search_proxy_mcp

    primary_url = "https://profiles.canterbury.ac.nz/Richard-Green"
    fallback_url = "https://example.com/richard-green"

    class _DummyProxy:
        def __init__(self):
            self.extract_calls = []
            self.search_calls = []

        async def extract(self, *, url):
            self.extract_calls.append(url)
            if url == primary_url:
                return {
                    "success": False,
                    "url": url,
                    "title": None,
                    "content": "",
                    "error": "No extractable content returned",
                }
            if url == fallback_url:
                return {
                    "success": True,
                    "url": url,
                    "title": "Richard Green",
                    "content": "Biography. Research interests. Publications. Education.",
                }
            return {
                "success": False,
                "url": url,
                "title": None,
                "content": "",
                "error": "Not found",
            }

        async def search(
            self,
            *,
            query,
            max_results=10,
            search_depth="basic",
            include_domains=None,
            exclude_domains=None,
            include_answer=False,
            include_raw_content=False,
            include_images=False,
        ):
            self.search_calls.append(
                {
                    "query": query,
                    "max_results": max_results,
                    "search_depth": search_depth,
                    "include_domains": include_domains,
                    "exclude_domains": exclude_domains,
                    "include_answer": include_answer,
                }
            )
            return {
                "success": True,
                "results": [
                    {
                        "title": "Alt profile",
                        "url": fallback_url,
                        "content": "A profile page",
                        "score": 0.9,
                    }
                ],
            }

    dummy_proxy = _DummyProxy()

    async def _fake_get_search_proxy():
        return dummy_proxy

    monkeypatch.setattr(search_proxy_mcp, "get_search_proxy", _fake_get_search_proxy)

    async def _runner():
        return catalogue._resilient_extract_url(
            url=primary_url,
            fallback_query="Richard Green Canterbury",
            max_fallback_results=3,
            max_extracts=2,
            min_content_chars=10,
        )

    result = asyncio.run(_runner())

    assert result["success"] is True
    assert result["primary_url"] == primary_url
    assert result["extracted_from_url"] == fallback_url
    assert "Biography" in result["content"]

    assert dummy_proxy.search_calls, "expected fallback search to be used"
    assert primary_url in dummy_proxy.extract_calls
    assert fallback_url in dummy_proxy.extract_calls

    attempted_urls = [attempt["url"] for attempt in result["attempted"]]
    assert primary_url in attempted_urls
    assert fallback_url in attempted_urls


def test_catalogue_resilient_extract_url_caps_public_external_fanout(
    monkeypatch,
):
    from src.backend.integrations.internal_mcp import catalogue

    extracted_urls: list[str] = []

    def _failed_extract(*, url):
        extracted_urls.append(url)
        return {"success": False, "url": url, "content": ""}

    def _many_search_results(**_kwargs):
        return {
            "success": True,
            "results": [
                {"url": f"https://example.test/{index}"}
                for index in range(250)
            ],
        }

    monkeypatch.setattr(catalogue, "_extract_url", _failed_extract)
    monkeypatch.setattr(catalogue, "_search_web", _many_search_results)

    result = catalogue._resilient_extract_url(
        url="https://primary.example.test/profile",
        max_fallback_results=250,
        max_extracts=250,
    )

    assert result["success"] is False
    assert len(extracted_urls) == 9
