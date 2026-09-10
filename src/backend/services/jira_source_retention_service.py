"""Lossless source capture for the existing Jira task importer.

Operational task projections are deliberately separate from immutable source
copies. JSON values (including ADF) and original pages are preserved; attachment
bytes are retained with SHA-256 hashes. Manifests never infer completeness from
an issue key, HTTP success, or an embedded first page.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import gzip
import hashlib
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from .relationship_extent_index_service import defer_relationship_extent_index_sync


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _source_payload(value):
    # The proxy adds its receipt; it is provenance, not part of the Jira source.
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key != "authority"}
    return value


async def capture_resource(
    proxy, resource, identifier="", secondary_id="", *, item_key=None
):
    """Retain every page, rejecting stalled, inconsistent or duplicate paging."""
    pages, items, errors, seen = [], [], [], set()
    start_at, expected_total = 0, None
    next_page_token, seen_tokens = None, set()
    while True:
        try:
            page = _source_payload(
                await proxy.get_migration_resource(
                    resource=resource,
                    identifier=str(identifier),
                    secondary_id=str(secondary_id),
                    start_at=start_at,
                    max_results=100,
                    **(
                        {"next_page_token": next_page_token}
                        if resource in {"board_issues", "filter_issues"}
                        else {}
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - Retain partial evidence.
            errors.append({"error": type(exc).__name__, "message": str(exc)})
            break
        pages.append(page)
        if isinstance(page, dict) and page.get("success") is False:
            errors.append(
                {"error": page.get("error"), "status_code": page.get("status_code")}
            )
            break
        if item_key is None:
            if isinstance(page, list):
                items.extend(page)
            break
        values = page.get(item_key) if isinstance(page, dict) else None
        if not isinstance(values, list):
            errors.append({"error": "missing_page_items", "item_key": item_key})
            break
        if page.get("startAt", start_at) != start_at:
            errors.append({"error": "unexpected_page_offset"})
            break
        total = page.get("total")
        if isinstance(total, int):
            if expected_total is not None and total != expected_total:
                errors.append({"error": "source_changed_during_paging"})
            expected_total = total
        for item in values:
            identity = (
                str(item.get("id"))
                if isinstance(item, dict) and "id" in item
                else canonical_json(item).hex()
            )
            if identity in seen:
                errors.append({"error": "duplicate_source_item", "id": identity})
            seen.add(identity)
        items.extend(values)
        next_offset = start_at + len(values)
        if page.get("isLast") is True or (
            expected_total is not None and next_offset >= expected_total
        ):
            if expected_total is not None and len(items) != expected_total:
                errors.append(
                    {
                        "error": "source_count_mismatch",
                        "expected": expected_total,
                        "actual": len(items),
                    }
                )
            break
        if not values:
            errors.append({"error": "incomplete_or_unproven_pagination"})
            break
        if resource in {"board_issues", "filter_issues"}:
            token = page.get("nextPageToken")
            if not token or token in seen_tokens:
                errors.append({"error": "missing_or_repeated_page_token"})
                break
            seen_tokens.add(token)
            next_page_token = token
        # A source must expose a terminal indicator, not merely a short page.
        if expected_total is None and "isLast" not in page and not next_page_token:
            errors.append({"error": "missing_pagination_contract"})
            break
        if errors:
            break
        start_at = next_offset
    return {
        "resource": resource,
        "identifier": str(identifier),
        "secondary_id": str(secondary_id),
        "pages": pages,
        "items": items,
        "count": len(items),
        "complete": not errors,
        "errors": errors,
    }


async def capture_issue(
    proxy, issue_key: str, *, attachment_max_size_bytes: int = 100 * 1024 * 1024
):
    """Return an unchanged source archive and a separate hydrated projection."""
    session = getattr(proxy, "source_capture_session", None)
    async with session() if callable(session) else nullcontext():
        return await _capture_issue(
            proxy,
            issue_key,
            attachment_max_size_bytes=attachment_max_size_bytes,
        )


async def _capture_issue(proxy, issue_key, *, attachment_max_size_bytes):
    original = _source_payload(
        await proxy.get_issue(
            issue_key=issue_key,
            fields=["*all"],
            expand=["names", "schema", "renderedFields"],
        )
    )
    if (
        not isinstance(original, dict)
        or original.get("success") is False
        or not isinstance(original.get("fields"), dict)
    ):
        raise ValueError(f"Jira issue capture failed: {issue_key}")
    if str(original.get("key", "")).upper() != issue_key.upper():
        raise ValueError("Jira issue identity mismatch")
    hydrated = copy.deepcopy(original)
    # Some Jira subresource routes fail for a key that get_issue still resolves.
    # Use the captured stable ID for the same issue and retain it in each page.
    issue_identifier = str(original.get("id") or issue_key)
    # Independent source pages can be read together. Limit concurrency to four
    # to bound Jira load; ordering and the final source-version check are stable.
    requests = [
        ("comments", "comments"),
        ("worklogs", "worklogs"),
        ("changelog", "values"),
        ("remote_links", None),
        ("issue_properties", None),
        ("votes", None),
    ]
    resources = {}
    for offset in range(0, len(requests), 4):
        batch = requests[offset : offset + 4]
        results = await asyncio.gather(
            *(
                capture_resource(proxy, name, issue_identifier, item_key=item_key)
                for name, item_key in batch
            )
        )
        resources.update({name: result for (name, _), result in zip(batch, results)})
    for resource, item_key, destination in (
        ("comments", "comments", "comment"),
        ("worklogs", "worklogs", "worklog"),
        ("changelog", "values", None),
    ):
        captured = resources[resource]
        if destination:
            hydrated["fields"][destination] = {
                item_key: captured["items"],
                "total": captured["count"],
                "startAt": 0,
            }
        else:
            hydrated["changelog"] = {
                "histories": captured["items"],
                "total": captured["count"],
                "startAt": 0,
            }
    property_pages = resources["issue_properties"]["pages"]
    properties = (
        property_pages[0].get("keys", [])
        if property_pages and isinstance(property_pages[0], dict)
        else []
    )
    for prop in properties:
        key = prop.get("key")
        if isinstance(key, str):
            resources[f"issue_property:{key}"] = await capture_resource(
                proxy, "issue_property", issue_identifier, key
            )
    try:
        watchers = _source_payload(await proxy.get_watchers(issue_key=issue_key))
        ok = isinstance(watchers, dict) and watchers.get("success") is not False
        if ok and watchers.get("watchCount") is not None:
            ok = watchers["watchCount"] == len(watchers.get("watchers", []))
        resources["watchers"] = {
            "pages": [watchers],
            "complete": ok,
            "errors": [] if ok else [{"error": "watchers_incomplete"}],
        }
        hydrated["watchers"] = watchers
    except Exception as exc:  # noqa: BLE001 - Retain partial evidence.
        resources["watchers"] = {
            "complete": False,
            "errors": [{"error": type(exc).__name__}],
        }
    binaries = []
    for attachment in hydrated["fields"].get("attachment") or []:
        attachment_id = str(attachment.get("id", ""))
        record = {"id": attachment_id, "complete": False}
        try:
            result = _source_payload(
                await proxy.get_attachment_content(
                    attachment_id=attachment_id,
                    max_size_bytes=attachment_max_size_bytes,
                )
            )
            record["source"] = result
            if result.get("success") is not True:
                raise ValueError(result.get("error", "attachment_fetch_failed"))
            data = base64.b64decode(result["content_base64"], validate=True)
            if len(data) != int(
                attachment.get("size", result.get("size_bytes", len(data)))
            ):
                raise ValueError("attachment_size_mismatch")
            record.update(
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
                complete=True,
            )
            attachment["content_base64"] = result["content_base64"]
        except Exception as exc:  # noqa: BLE001 - Retain partial evidence.
            record["error"] = str(exc)
        binaries.append(record)
    after = await proxy.get_issue(issue_key=issue_key, fields=["updated"])
    stable = after.get("fields", {}).get("updated") == original["fields"].get("updated")
    manifest = {
        "schema": "jira_source_archive.v1",
        "kind": "issue",
        "source": original,
        "resources": resources,
        "binaries": binaries,
        "source_stable": stable,
        "complete": stable
        and all(x["complete"] for x in resources.values())
        and all(x["complete"] for x in binaries),
    }
    return manifest, hydrated


async def capture_project(proxy, project_key):
    """Retain project data, configuration and nested properties/role memberships."""
    session = getattr(proxy, "source_capture_session", None)
    async with session() if callable(session) else nullcontext():
        return await _capture_project(proxy, project_key)


async def _capture_project(proxy, project_key):
    resources = {"project": await capture_resource(proxy, "project", project_key)}
    pages = resources["project"]["pages"]
    project = pages[0] if pages and isinstance(pages[0], dict) else {}
    project_id = project.get("id", project_key)
    names = (
        "components",
        "versions",
        "project_properties",
        "project_roles",
        "project_statuses",
        "permission_scheme",
        "security_scheme",
        "notification_scheme",
        "workflow_scheme",
        "issue_type_scheme",
        "field_configuration_scheme",
        "screen_scheme",
    )
    for offset in range(0, len(names), 4):
        batch = names[offset : offset + 4]
        results = await asyncio.gather(
            *(
                capture_resource(
                    proxy,
                    name,
                    project_id,
                    item_key=(
                        "values"
                        if name
                        in {
                            "issue_type_scheme",
                            "field_configuration_scheme",
                            "screen_scheme",
                        }
                        else None
                    ),
                )
                for name in batch
            )
        )
        resources.update(dict(zip(batch, results)))
    security = resources["security_scheme"]
    for page in security["pages"]:
        if isinstance(page, dict) and page.get("status_code") == 404:
            try:
                messages = json.loads(page.get("response", "{}"))["errorMessages"]
            except (ValueError, KeyError, TypeError):
                messages = []
            if messages == [f"Security level for project {project_id} does not exist."]:
                security.update(
                    complete=True, errors=[], disposition="source_reports_absent"
                )
    for prop in (resources["project_properties"]["pages"] or [{}])[0].get("keys", []):
        if prop.get("key"):
            resources[f"property:{prop['key']}"] = await capture_resource(
                proxy, "project_property", project_id, prop["key"]
            )
    role_page = (resources["project_roles"]["pages"] or [{}])[0]
    if isinstance(role_page, dict) and resources["project_roles"]["complete"]:
        for role_name, url in role_page.items():
            if isinstance(url, str) and url.rsplit("/", 1)[-1].isdigit():
                resources[f"role:{role_name}"] = await capture_resource(
                    proxy, "project_role", project_id, url.rsplit("/", 1)[-1]
                )
    return {
        "schema": "jira_source_archive.v1",
        "kind": "project",
        "source": project,
        "resources": resources,
        "complete": all(x["complete"] for x in resources.values()),
    }


async def capture_configuration_details(proxy, *, screens=None):
    """Retain scheme mappings and nested field layouts, not only their names.

    Jira's classic configuration APIs can return an empty set for team-managed
    projects. Preserve the exact responses; these reads do not certify forms or
    app-owned configuration, which still need their own source dispositions.
    This function also supports supplementing an existing site archive without
    downloading every board and issue-membership snapshot again.
    """
    resources = {}
    for resource in (
        "screen_schemes",
        "issue_type_screen_schemes",
        "issue_type_screen_mappings",
        "issue_type_schemes",
        "issue_type_scheme_items",
        "field_configurations",
        "field_configuration_schemes",
        "field_configuration_mappings",
    ):
        resources[resource] = await capture_resource(proxy, resource, item_key="values")
    if screens is None:
        resources["screens"] = await capture_resource(
            proxy, "screens", item_key="values"
        )
        screens = resources["screens"]["items"]
    for screen in screens:
        screen_id = str(screen["id"])
        tabs = await capture_resource(proxy, "screen_tabs", screen_id)
        resources[f"screen_tabs:{screen_id}"] = tabs
        for tab in tabs["items"]:
            tab_id = str(tab["id"])
            resources[f"screen_tab_fields:{screen_id}:{tab_id}"] = (
                await capture_resource(proxy, "screen_tab_fields", screen_id, tab_id)
            )
    for configuration in resources["field_configurations"]["items"]:
        config_id = str(configuration["id"])
        resources[f"field_configuration_items:{config_id}"] = await capture_resource(
            proxy, "field_configuration_items", config_id, item_key="values"
        )
    return resources


async def capture_site_configuration(proxy, *, site_url):
    """Shared definitions are captured once and referenced by project records.

    Separate export surfaces are explicit outstanding dispositions. A successful
    REST crawl alone cannot certify third-party app or Automation retention.
    """
    resources = {}
    for resource in (
        "projects",
        "fields",
        "issue_types",
        "statuses",
        "resolutions",
        "priorities",
        "workflows",
        "screens",
        "filters",
        "dashboards",
        "webhooks",
        "boards",
    ):
        paginated = resource in {
            "projects",
            "workflows",
            "screens",
            "filters",
            "dashboards",
            "webhooks",
            "boards",
        }
        resources[resource] = await capture_resource(
            proxy, resource, item_key="values" if paginated else None
        )
    for field in resources["fields"]["items"]:
        if not field.get("custom"):
            continue
        field_id = field["id"]
        contexts = await capture_resource(
            proxy, "field_contexts", field_id, item_key="values"
        )
        resources[f"field_contexts:{field_id}"] = contexts
        resources[f"field_defaults:{field_id}"] = await capture_resource(
            proxy, "field_defaults", field_id, item_key="values"
        )
        schema = field.get("schema", {})
        if schema.get("type") == "option" or schema.get("items") == "option":
            for context in contexts["items"]:
                resources[f"field_options:{field_id}:{context['id']}"] = (
                    await capture_resource(
                        proxy,
                        "field_options",
                        field_id,
                        context["id"],
                        item_key="values",
                    )
                )
    resources.update(
        await capture_configuration_details(
            proxy, screens=resources["screens"]["items"]
        )
    )
    for board in resources["boards"]["items"]:
        board_id = board["id"]
        for resource in (
            "board_configuration",
            "board_projects",
            "board_issues",
            "board_properties",
            "board_quick_filters",
        ):
            resources[f"{resource}:{board_id}"] = await capture_resource(
                proxy,
                resource,
                board_id,
                item_key=(
                    "issues"
                    if resource in {"board_issues", "filter_issues"}
                    else (
                        "values"
                        if resource in {"board_projects", "board_quick_filters"}
                        else None
                    )
                ),
            )
        for prop in (resources[f"board_properties:{board_id}"].get("pages") or [{}])[
            0
        ].get("keys", []):
            if prop.get("key"):
                resources[f"board_property:{board_id}:{prop['key']}"] = (
                    await capture_resource(
                        proxy, "board_property", board_id, prop["key"]
                    )
                )
        if board.get("type") == "scrum":
            resources[f"board_sprints:{board_id}"] = await capture_resource(
                proxy, "board_sprints", board_id, item_key="values"
            )
    for dashboard in resources["dashboards"]["items"]:
        for resource in ("dashboard", "dashboard_gadgets"):
            resources[f"{resource}:{dashboard['id']}"] = await capture_resource(
                proxy, resource, dashboard["id"]
            )
    for source_filter in resources["filters"]["items"]:
        resources[f"filter:{source_filter['id']}"] = await capture_resource(
            proxy, "filter", source_filter["id"]
        )
        resources[f"filter_issues:{source_filter['id']}"] = await capture_resource(
            proxy, "filter_issues", source_filter["id"], item_key="issues"
        )
    outstanding = [
        {"resource": "automation_rules", "disposition": "separate_export_required"},
        {
            "resource": "third_party_app_data_and_forms",
            "disposition": "app_inventory_and_exports_required",
        },
        {
            "resource": "external_project_documents",
            "disposition": "linked_resource_inventory_required",
        },
    ]
    return {
        "schema": "jira_source_archive.v1",
        "kind": "site",
        "source": {
            "self": site_url,
            "id": hashlib.sha256(site_url.encode()).hexdigest()[:24],
        },
        "resources": resources,
        "outstanding_dispositions": outstanding,
        "complete": False,
        "rest_resources_complete": all(
            value["complete"] for value in resources.values()
        ),
    }


async def capture_linked_atlassian_project(proxy, *, kind, site_id, object_id):
    """Capture named project/goal resources without silently truncating pages.

    This retains source information; it neither copies its permissions nor
    enables imported integrations. Nested pages beyond the returned first page
    remain explicit gaps, so callers cannot certify them from outer pagination.
    """
    from ..integrations.internal_mcp.atlassian_project_migration_queries import (
        GOAL_CONNECTIONS,
        PROJECT_CONNECTIONS,
    )
    from ..integrations.internal_mcp.jira_proxy_mcp import JiraProxyError

    if kind not in {"project", "goal"}:
        raise ValueError("Expected an Atlassian project or goal")
    base_resource = f"atlas_{kind}"
    root_key = f"{kind}s_byId"
    resources = {}

    async def request(resource, cursor=None):
        return _source_payload(
            await proxy.get_migration_resource(
                resource=resource,
                identifier=site_id,
                secondary_id=object_id,
                max_results=100,
                next_page_token=cursor,
            )
        )

    def nested_pages(value, path=""):
        gaps = []
        if isinstance(value, dict):
            if (value.get("pageInfo") or {}).get("hasNextPage"):
                gaps.append(path)
            for name, child in value.items():
                gaps.extend(nested_pages(child, f"{path}/{name}"))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                gaps.extend(nested_pages(child, f"{path}/{index}"))
        return gaps

    first = await request(base_resource)
    source = (first.get("data") or {}).get(root_key)
    errors = list(first.get("errors") or [])
    resources[base_resource] = {
        "pages": [first],
        "complete": bool(source) and not errors,
        "errors": errors,
    }
    if source and not errors:
        connections = PROJECT_CONNECTIONS if kind == "project" else GOAL_CONNECTIONS
        for field in connections:
            resource = f"{base_resource}_{field}"
            record = {
                "pages": [],
                "items": [],
                "errors": [],
                "complete": False,
                "nested_page_gaps": [],
            }
            resources[resource] = record
            cursor = None
            seen = set()
            while True:
                try:
                    page = await request(resource, cursor)
                except (JiraProxyError, TimeoutError, OSError) as exc:
                    record["errors"].append(
                        {"type": type(exc).__name__, "message": str(exc)}
                    )
                    break
                record["pages"].append(page)
                record["errors"].extend(page.get("errors") or [])
                parent = (page.get("data") or {}).get(root_key) or {}
                connection = parent.get(field)
                if not isinstance(connection, dict) or record["errors"]:
                    if not record["errors"]:
                        record["errors"].append(
                            {"message": "Source connection unavailable"}
                        )
                    break
                edges = connection.get("edges") or []
                record["items"].extend(edges)
                record["nested_page_gaps"].extend(nested_pages(edges))
                info = connection.get("pageInfo") or {}
                if "hasNextPage" not in info:
                    record["errors"].append(
                        {"message": "Source pagination metadata missing"}
                    )
                    break
                if not info.get("hasNextPage"):
                    record["complete"] = not record["nested_page_gaps"]
                    break
                cursor = info.get("endCursor")
                if not cursor or cursor in seen:
                    record["errors"].append(
                        {"message": "Source pagination did not advance"}
                    )
                    break
                seen.add(cursor)
        after = await request(base_resource)
        resources[base_resource]["pages"].append(after)
        stable = not after.get("errors") and canonical_json(
            (after.get("data") or {}).get(root_key)
        ) == canonical_json(source)
    else:
        stable = False
    return {
        "schema": "jira_source_archive.v1",
        "kind": base_resource,
        "source": source or {"id": object_id},
        "site_id": site_id,
        "resources": resources,
        "source_stable_during_capture": stable,
        "capture_scope": "Named Atlassian project or goal fields and connections; source permissions and integration settings are retained as data.",
        "complete": stable and all(value["complete"] for value in resources.values()),
    }


@defer_relationship_extent_index_sync()
def store_source_archive(
    archive, *, actor_concept_id, organisation_concept_id=None, namespace=None
):
    """Content-addressed immutable file copy; reruns reuse identical bytes."""
    from .computer_file_copy_service import import_bytes_file_copy

    raw = canonical_json(archive)
    digest = hashlib.sha256(raw).hexdigest()
    data = gzip.compress(raw, mtime=0)
    source = archive.get("source", {})
    source_id = str(source.get("id") or source.get("key") or "site")
    kind = archive["kind"]
    scope = hashlib.sha256(
        f"{actor_concept_id}\n{organisation_concept_id}".encode()
    ).hexdigest()[:24]
    from ..security.access_control import override_current_actor

    # Use the archive's explicit audience context during registration; a
    # selected operational project must not leak its ambient organisation here.
    with override_current_actor(actor_concept_id, organisation_concept_id):
        receipt = import_bytes_file_copy(
            data=data,
            user_concept_id=actor_concept_id,
            organisation_concept_id=organisation_concept_id,
            namespace=namespace,
            original_filename=f"jira-{kind}-{source_id}-{digest[:12]}.json.gz",
            content_type="application/gzip",
            source_system="jira_source_archive",
            source_identifier=source_id,
            source_uri=source.get("self"),
            blob_key=f"jira-source/{scope}/{kind}/{source_id}/{digest}.json.gz",
            metadata={
                "schema": archive["schema"],
                "content_sha256": digest,
                "source_id": source_id,
            },
            metadata_in_attributes=True,
            infer_typing=False,
            maintain_relationship_inverses=False,
            visibility_scope_mode=(
                "user_only_default"
                if not organisation_concept_id
                else "user_org_default"
            ),
        )
    if receipt.get("success") is not True:
        raise RuntimeError(f"Source archive persistence failed: {receipt.get('error')}")
    return {
        "file_copy_concept_id": receipt["concept_id"],
        "content_sha256": digest,
        "blob_sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "complete": bool(archive["complete"]),
        "schema": archive["schema"],
        "captured_at": datetime.now(UTC).isoformat(),
    }


def read_source_archive(
    reference, *, actor_concept_id, organisation_concept_id=None, namespace=None
):
    """Retrieve through file-copy access checks and verify both digest layers."""
    from .computer_file_copy_service import fetch_file_copy_bytes

    result = fetch_file_copy_bytes(
        file_copy_concept_id=reference["file_copy_concept_id"],
        user_concept_id=actor_concept_id,
        organisation_concept_id=organisation_concept_id,
        namespace=namespace,
        allow_large=True,
    )
    # The file-copy service also checks its registered stored-byte digest.
    if result.get("success") is not True:
        raise ValueError("Jira archive is unavailable to this actor")
    data = result["data"]
    if hashlib.sha256(data).hexdigest() != reference["blob_sha256"]:
        raise ValueError("Jira archive blob checksum mismatch")
    raw = gzip.decompress(data)
    if hashlib.sha256(raw).hexdigest() != reference["content_sha256"]:
        raise ValueError("Jira archive content checksum mismatch")
    return json.loads(raw)


def get_retained_task_source(
    task_concept_id, *, actor_concept_id, resource=None, offset=0, limit=100
):
    """Read preserved source through the task and file-copy access boundaries.

    Binary bodies remain in the downloadable original; paged activity can be
    inspected without returning every attachment as base64 in a tool response.
    """
    from urllib.parse import quote

    from .task_management_service import get_task

    task = get_task(task_concept_id)
    reference = (
        task.get("external_references", {}).get("jira", {}).get("source_archive")
    )
    if not reference:
        raise ValueError("No retained Jira source archive for this task")
    archive = read_source_archive(reference, actor_concept_id=actor_concept_id)
    result = {
        "task_concept_id": task_concept_id,
        "reference": reference,
        "download_url": f"/von/api/files/{quote(reference['file_copy_concept_id'], safe='')}/download",
        "complete": archive["complete"],
        "source_stable": archive.get("source_stable"),
        "resources": {
            key: {k: v for k, v in value.items() if k not in {"pages", "items"}}
            for key, value in archive.get("resources", {}).items()
        },
        "binaries": [
            {k: v for k, v in item.items() if k != "source"}
            for item in archive.get("binaries", [])
        ],
    }
    if resource:
        captured = archive.get("resources", {}).get(resource)
        if captured is None:
            raise ValueError("Unknown retained source resource")
        values = captured.get("items") or captured.get("pages", [])
        offset, limit = max(0, int(offset)), max(1, min(200, int(limit)))
        result.update(
            resource=resource,
            items=values[offset : offset + limit],
            total=len(values),
            offset=offset,
            next_offset=offset + limit if offset + limit < len(values) else None,
        )
    else:
        result["source"] = archive["source"]
    return result
