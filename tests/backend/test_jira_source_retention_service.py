import base64
import copy
import gzip
import hashlib
import json

import pytest

from src.backend.integrations.internal_mcp.jira_migration_resources import (
    resource_request,
)
from src.backend.services import jira_source_retention_service as retention


class Source:
    def __init__(self):
        self.original = {
            "id": "123",
            "key": "KKAT-1",
            "fields": {
                "updated": "2026-09-10",
                "customfield_42": {"unknown": [None, " Māori\n", 12]},
                "description": {
                    "type": "doc",
                    "content": [{"type": "table", "attrs": {"layout": "wide"}}],
                },
                "attachment": [{"id": "45", "size": 3, "filename": "test.bin"}],
            },
        }

    async def get_issue(self, **kwargs):
        return copy.deepcopy(self.original)

    async def get_migration_resource(self, resource, start_at=0, **kwargs):
        key = {
            "comments": "comments",
            "worklogs": "worklogs",
            "changelog": "values",
        }.get(resource)
        if key:
            # Server pages smaller than requested, including rich activity.
            return {
                "startAt": start_at,
                "maxResults": 1,
                "total": 2,
                key: [
                    {
                        "id": str(start_at),
                        "body": {
                            "type": "doc",
                            "content": [{"type": "text", "text": " x\n"}],
                        },
                    }
                ],
            }
        return {"keys": []}

    async def get_watchers(self, **kwargs):
        return {"watchCount": 0, "watchers": []}

    async def get_attachment_content(self, **kwargs):
        return {
            "success": True,
            "content_base64": base64.b64encode(b"\x00\x01\xff").decode(),
        }


@pytest.mark.asyncio
async def test_capture_preserves_original_and_all_pages_and_binary():
    source = Source()
    expected = copy.deepcopy(source.original)
    archive, projection = await retention.capture_issue(source, "KKAT-1")
    assert archive["complete"]
    assert archive["source"] == source.original == expected
    assert archive["resources"]["comments"]["count"] == 2
    assert len(archive["resources"]["comments"]["pages"]) == 2
    assert len(projection["changelog"]["histories"]) == 2
    assert (
        archive["binaries"][0]["sha256"] == hashlib.sha256(b"\x00\x01\xff").hexdigest()
    )
    assert "content_base64" not in archive["source"]["fields"]["attachment"][0]
    assert (
        json.loads(gzip.decompress(gzip.compress(retention.canonical_json(archive))))
        == archive
    )


@pytest.mark.asyncio
async def test_issue_resources_use_stable_id_when_key_route_is_unavailable():
    class StableIdSource(Source):
        async def get_migration_resource(self, resource, identifier, **kwargs):
            if resource == "worklogs" and identifier != "123":
                return {"success": False, "status_code": 404, "error": "not_found"}
            return await super().get_migration_resource(resource, **kwargs)

    archive, _ = await retention.capture_issue(StableIdSource(), "KKAT-1")
    assert archive["complete"]
    assert archive["resources"]["worklogs"]["identifier"] == "123"
    assert archive["resources"]["worklogs"]["count"] == 2


@pytest.mark.asyncio
async def test_duplicate_page_and_unavailable_binary_cannot_claim_retention():
    source = Source()

    async def pages(**kwargs):
        key = {
            "comments": "comments",
            "worklogs": "worklogs",
            "changelog": "values",
        }.get(kwargs["resource"])
        return {key: [{"id": "repeated"}], "total": 3} if key else {"keys": []}

    async def attachment(**kwargs):
        return {"success": False, "error": "forbidden"}

    source.get_migration_resource = pages
    source.get_attachment_content = attachment
    archive, _ = await retention.capture_issue(source, "KKAT-1")
    assert not archive["complete"]
    assert any(
        error["error"] == "duplicate_source_item"
        for error in archive["resources"]["comments"]["errors"]
    )
    assert not archive["binaries"][0]["complete"]


@pytest.mark.asyncio
async def test_missing_terminal_contract_is_not_a_complete_empty_page():
    source = Source()

    async def pages(**kwargs):
        return {"values": []}

    source.get_migration_resource = pages
    result = await retention.capture_resource(
        source, "changelog", "KKAT-1", item_key="values"
    )
    assert not result["complete"]


def test_resource_paths_cannot_redirect_credentials():
    path, _ = resource_request(
        "project_property", "https://evil.example/a", "../secrets"
    )
    assert path.startswith("/rest/api/3/project/https%3A%2F%2Fevil.example%2Fa/")
    assert "../" not in path
    with pytest.raises(ValueError):
        resource_request("https://evil.example")
    with pytest.raises(ValueError):
        resource_request("project", "..")


@pytest.mark.asyncio
async def test_configuration_capture_retains_nested_layout_and_all_field_pages():
    class ConfigurationSource:
        async def get_migration_resource(
            self, resource, identifier="", start_at=0, **kw
        ):
            if resource == "screen_tabs":
                return [{"id": 17, "name": "Details"}]
            if resource == "screen_tab_fields":
                assert identifier == "12" and kw["secondary_id"] == "17"
                return [{"id": "summary"}, {"id": "customfield_42"}]
            if resource == "field_configurations":
                return {"values": [{"id": 3}], "total": 1, "isLast": True}
            if resource == "field_configuration_items":
                return {
                    "startAt": start_at,
                    "total": 2,
                    "values": [
                        {"id": f"field_{start_at}", "isRequired": bool(start_at)}
                    ],
                }
            return {"values": [], "total": 0, "isLast": True}

    result = await retention.capture_configuration_details(
        ConfigurationSource(), screens=[{"id": 12, "name": "Create"}]
    )
    assert [r["id"] for r in result["screen_tab_fields:12:17"]["items"]] == [
        "summary",
        "customfield_42",
    ]
    assert result["field_configuration_items:3"]["count"] == 2
    assert result["field_configuration_items:3"]["items"][1]["isRequired"] is True
    assert all(r["complete"] for r in result.values())


@pytest.mark.asyncio
async def test_unavailable_screen_layout_stays_an_explicit_capture_gap():
    class UnavailableSource:
        async def get_migration_resource(self, resource, **kw):
            if resource == "screen_tabs":
                return {"success": False, "status_code": 403, "error": "forbidden"}
            return {"values": [], "total": 0, "isLast": True}

    result = await retention.capture_configuration_details(
        UnavailableSource(), screens=[{"id": 12}]
    )
    assert not result["screen_tabs:12"]["complete"]
    assert result["screen_tabs:12"]["pages"][0]["status_code"] == 403
    assert not any(k.startswith("screen_tab_fields:") for k in result)


def test_nested_configuration_resources_use_fixed_read_paths():
    assert resource_request("screen_tab_fields", "12", "17") == (
        "/rest/api/3/screens/12/tabs/17/fields",
        {},
    )
    assert resource_request("field_configuration_items", "3", start_at=100) == (
        "/rest/api/3/fieldconfiguration/3/fields",
        {"startAt": 100, "maxResults": 100},
    )


def test_archive_read_requires_access_and_verifies_bytes(monkeypatch):
    from src.backend.services import computer_file_copy_service as files

    raw = retention.canonical_json({"kind": "issue", "source": {"key": "KKAT-1"}})
    data = gzip.compress(raw, mtime=0)
    ref = {
        "file_copy_concept_id": "#V#file",
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "blob_sha256": hashlib.sha256(data).hexdigest(),
    }
    monkeypatch.setattr(
        files,
        "fetch_file_copy_bytes",
        lambda **kw: (
            {"success": True, "data": data}
            if kw["user_concept_id"] == "#V#owner"
            else {"success": False}
        ),
    )
    assert (
        retention.read_source_archive(ref, actor_concept_id="#V#owner")["source"]["key"]
        == "KKAT-1"
    )
    with pytest.raises(ValueError, match="unavailable"):
        retention.read_source_archive(ref, actor_concept_id="#V#other")
    with pytest.raises(ValueError, match="checksum"):
        retention.read_source_archive(
            {**ref, "blob_sha256": "wrong"}, actor_concept_id="#V#owner"
        )
