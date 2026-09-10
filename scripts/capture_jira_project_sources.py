"""Capture source project/configuration evidence using the canonical Jira proxy.

This command preserves source information and associations; it never switches
the writer or activates source workflows, permissions or automation rules.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


async def capture(args):
    from src.backend.integrations.internal_mcp.jira_proxy_mcp import get_jira_proxy
    from src.backend.services.concept_service import update_concept
    from src.backend.services.jira_source_retention_service import (
        capture_project,
        capture_site_configuration,
        store_source_archive,
    )
    from src.backend.services.task_project_service import (
        associate_jira_source_collection,
        ensure_jira_project,
    )

    proxy = await get_jira_proxy()
    destination = args.output_dir
    destination.mkdir(parents=True, exist_ok=True)
    # The configured proxy account determines the source site, never a source
    # document or a caller-provided credential-bearing destination.
    source_site = proxy._auth_identity[0]
    projects, errors = {}, []
    for key in dict.fromkeys(args.project_key):
        if args.reuse_projects:
            contexts = json.loads((destination / "project-contexts.json").read_text())
            projects[key] = contexts[key]
            continue
        archive = await capture_project(proxy, key)
        (destination / f"project-{key}.json").write_text(json.dumps(archive, indent=2))
        if not archive.get("source", {}).get("id"):
            errors.append({"project": key, "error": "project_source_unavailable"})
            continue
        reference = store_source_archive(
            archive, actor_concept_id=args.actor_concept_id
        )
        org = (
            args.organisation_concept_id
            if key == args.organisation_project_key
            else None
        )
        context = ensure_jira_project(
            archive["source"],
            actor_concept_id=args.actor_concept_id,
            organisation_concept_id=org,
            archive_reference=reference,
        )
        projects[key] = context
        (destination / "project-contexts.json").write_text(
            json.dumps(projects, indent=2)
        )
        if not archive["complete"]:
            errors.append({"project": key, "error": "incomplete_project_source"})
        print(
            json.dumps(
                {
                    "project": key,
                    "complete": archive["complete"],
                    "project_concept_id": context["project_concept_id"],
                }
            ),
            flush=True,
        )
    if args.projects_only:
        return {"projects": projects, "errors": errors}
    site = await capture_site_configuration(proxy, site_url=source_site)
    (destination / "site-source.json").write_text(json.dumps(site, indent=2))
    site_reference = store_source_archive(site, actor_concept_id=args.actor_concept_id)
    (destination / "site-archive-reference.json").write_text(
        json.dumps(site_reference, indent=2)
    )
    for context in projects.values():
        update_concept(
            context["project_concept_id"],
            {"attributes.task_project.site_source_archive": site_reference},
        )
    collections = []
    resources = site["resources"]
    for board in resources["boards"]["items"]:
        board_id = str(board["id"])
        membership = resources.get(f"board_issues:{board_id}", {})
        covered = resources.get(f"board_projects:{board_id}", {}).get("items", [])
        keys = {project["key"] for project in covered if project.get("key") in projects}
        keys.update(
            issue.get("fields", {}).get("project", {}).get("key")
            for issue in membership.get("items", [])
        )
        project_ids = [
            projects[key]["project_concept_id"]
            for key in sorted(keys - {None})
            if key in projects
        ]
        if not project_ids:
            continue
        if not membership.get("complete"):
            errors.append(
                {"board_id": board_id, "error": "incomplete_board_membership"}
            )
            continue
        collection = associate_jira_source_collection(
            name=board["name"],
            source_id=board_id,
            source_kind="board",
            project_concept_ids=project_ids,
            issue_keys=[issue["key"] for issue in membership["items"]],
            archive_reference=site_reference,
            actor_concept_id=args.actor_concept_id,
        )
        collections.append(collection)
    for source_filter in resources["filters"]["items"]:
        filter_id = str(source_filter["id"])
        membership = resources.get(f"filter_issues:{filter_id}", {})
        keys = {
            issue.get("fields", {}).get("project", {}).get("key")
            for issue in membership.get("items", [])
        }
        project_ids = [
            projects[key]["project_concept_id"]
            for key in sorted(keys - {None})
            if key in projects
        ]
        if not project_ids:
            continue
        if not membership.get("complete"):
            errors.append(
                {"filter_id": filter_id, "error": "incomplete_filter_membership"}
            )
            continue
        collections.append(
            associate_jira_source_collection(
                name=source_filter["name"],
                source_id=filter_id,
                source_kind="filter",
                project_concept_ids=project_ids,
                issue_keys=[issue["key"] for issue in membership["items"]],
                archive_reference=site_reference,
                actor_concept_id=args.actor_concept_id,
            )
        )
    result = {
        "projects": projects,
        "collections": collections,
        "site_archive": site_reference,
        "errors": errors,
        "cutover_ready": False,
    }
    (destination / "source-capture-report.json").write_text(
        json.dumps(result, indent=2)
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-concept-id", required=True)
    parser.add_argument("--project-key", action="append", required=True)
    parser.add_argument("--organisation-project-key", default="JVNAUTOSCI")
    parser.add_argument("--organisation-concept-id")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--projects-only", action="store_true")
    parser.add_argument(
        "--reuse-projects",
        action="store_true",
        help="Refresh site and collection evidence using previously captured project contexts.",
    )
    args = parser.parse_args()
    from src.backend.integrations.internal_mcp.gateway import (
        INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE,
        bind_internal_mcp_actor_context_source,
    )
    from src.backend.security.access_control import (
        force_access_control_enforcement,
        override_current_actor,
    )

    with (
        bind_internal_mcp_actor_context_source(
            INTERNAL_MCP_TRUSTED_LOCAL_OPERATOR_SOURCE
        ),
        override_current_actor(args.actor_concept_id, None),
        force_access_control_enforcement(),
    ):
        result = asyncio.run(capture(args))
    print(
        json.dumps(
            {"project_count": len(result["projects"]), "errors": result["errors"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
