import asyncio

import pytest

from src.backend.integrations.internal_mcp.atlassian_project_migration_queries import (
    project_query_request,
)
from src.backend.services.jira_source_retention_service import (
    capture_linked_atlassian_project,
)

SITE = "b2b12142-9002-4b11-b421-83f073678fd3"
OBJECT = "2b2f3cae-3a8a-4e40-b117-aac7febd44c9"


class Source:
    def __init__(self, mode="complete"):
        self.mode = mode
        self.cursors = []
        self.primary_reads = 0

    async def get_migration_resource(self, **kwargs):
        resource = kwargs["resource"]
        if resource == "atlas_project":
            self.primary_reads += 1
            name = (
                "Changed"
                if self.mode == "changed" and self.primary_reads > 1
                else "Project"
            )
            return {"data": {"projects_byId": {"id": OBJECT, "name": name}}}
        field = resource.removeprefix("atlas_project_")
        connection = {
            "edges": [],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
        if field == "updates":
            cursor = kwargs.get("next_page_token")
            self.cursors.append(cursor)
            connection["edges"] = [
                {"node": {"id": cursor or "first", "summary": "Original rich text"}}
            ]
            connection["pageInfo"] = {
                "hasNextPage": cursor is None,
                "endCursor": "second",
            }
            if self.mode == "repeated_cursor":
                connection["pageInfo"]["hasNextPage"] = True
            if self.mode == "missing_page_info":
                connection.pop("pageInfo")
            if self.mode == "nested_gap":
                connection["edges"][0]["node"]["comments"] = {
                    "pageInfo": {"hasNextPage": True, "endCursor": "more"}
                }
            if self.mode == "denied":
                return {
                    "data": {"projects_byId": None},
                    "errors": [{"message": "Permission denied"}],
                }
        return {"data": {"projects_byId": {"id": OBJECT, field: connection}}}


def capture(source):
    return asyncio.run(
        capture_linked_atlassian_project(
            source, kind="project", site_id=SITE, object_id=OBJECT
        )
    )


def test_capture_retains_all_pages_and_checks_source_version():
    source = Source()
    result = capture(source)
    assert result["complete"]
    updates = result["resources"]["atlas_project_updates"]
    assert [edge["node"]["id"] for edge in updates["items"]] == ["first", "second"]
    assert len(updates["pages"]) == 2
    assert source.cursors == [None, "second"]
    assert source.primary_reads == 2


@pytest.mark.parametrize(
    "mode", ["repeated_cursor", "missing_page_info", "nested_gap", "denied", "changed"]
)
def test_incomplete_or_changing_source_never_certifies_retention(mode):
    source = Source(mode)
    result = capture(source)
    assert not result["complete"]
    if mode != "changed":
        record = result["resources"]["atlas_project_updates"]
        assert not record["complete"]
        assert record["pages"]
        assert record["errors"] or record["nested_page_gaps"]


def test_query_variables_do_not_become_query_text_or_destinations():
    cursor = '") { mutation { deleteAll } } #'
    result = project_query_request(
        "atlas_project_updates", SITE, OBJECT, max_results=10000, next_page_token=cursor
    )
    assert result["variables"]["after"] == cursor
    assert result["variables"]["first"] == 100
    assert cursor not in result["query"]
    assert "mutation" not in result["query"]
    assert result["variables"]["id"] == f"ari:cloud:townsquare:{SITE}:project/{OBJECT}"


@pytest.mark.parametrize(
    "resource,identifier,secondary",
    [
        ("atlas_delete", SITE, OBJECT),
        ("atlas_project_delete", SITE, OBJECT),
        ("atlas_project", "https://example.com", OBJECT),
        ("atlas_project", SITE, "../credentials"),
        ("atlas_type_schema", "Query) { secret", ""),
    ],
)
def test_unknown_resources_and_invalid_source_identifiers_are_rejected(
    resource, identifier, secondary
):
    with pytest.raises(ValueError):
        project_query_request(resource, identifier, secondary)
