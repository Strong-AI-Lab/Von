"""Named, read-only Jira source resources needed by the existing importer.

Paths are relative to the configured Jira site. Source-provided URLs are never
used as credential-bearing request destinations. Source configuration is data;
reading a scheme, role or automation reference does not activate it in Von.
"""

from urllib.parse import quote
from uuid import UUID

# API family, path, pagination. Keep the source names in retention manifests.
RESOURCES = {
    "projects": ("api/3", "project/search", True),
    "project": ("api/3", "project/{id}", False),
    "components": ("api/3", "project/{id}/components", False),
    "versions": ("api/3", "project/{id}/versions", False),
    "project_properties": ("api/3", "project/{id}/properties", False),
    "project_property": ("api/3", "project/{id}/properties/{secondary}", False),
    "project_roles": ("api/3", "project/{id}/role", False),
    "project_role": ("api/3", "project/{id}/role/{secondary}", False),
    "project_statuses": ("api/3", "project/{id}/statuses", False),
    "permission_scheme": ("api/3", "project/{id}/permissionscheme", False),
    "security_scheme": ("api/3", "project/{id}/issuesecuritylevelscheme", False),
    "notification_scheme": ("api/3", "project/{id}/notificationscheme", False),
    "workflow_scheme": ("api/3", "workflowscheme/project", False),
    "issue_type_scheme": ("api/3", "issuetypescheme/project", True),
    "field_configuration_scheme": ("api/3", "fieldconfigurationscheme/project", True),
    "screen_scheme": ("api/3", "issuetypescreenscheme/project", True),
    "fields": ("api/3", "field", False),
    "field_contexts": ("api/3", "field/{id}/context", True),
    "field_options": ("api/3", "field/{id}/context/{secondary}/option", True),
    "field_defaults": ("api/3", "field/{id}/context/defaultValue", True),
    "issue_types": ("api/3", "issuetype", False),
    "statuses": ("api/3", "status", False),
    "resolutions": ("api/3", "resolution", False),
    "priorities": ("api/3", "priority", False),
    "workflows": ("api/3", "workflow/search", True),
    "screens": ("api/3", "screens", True),
    "filters": ("api/3", "filter/search", True),
    "filter_issues": ("api/3", "search/jql", True),
    "filter": ("api/3", "filter/{id}", False),
    "dashboards": ("api/3", "dashboard/search", True),
    "dashboard": ("api/3", "dashboard/{id}", False),
    "dashboard_gadgets": ("api/3", "dashboard/{id}/gadget", False),
    "webhooks": ("api/3", "webhook", True),
    "boards": ("agile/1.0", "board", True),
    "board_issues": ("software/1.0", "board/{id}/issue", True),
    "board_properties": ("agile/1.0", "board/{id}/properties", False),
    "board_property": ("agile/1.0", "board/{id}/properties/{secondary}", False),
    "board_quick_filters": ("agile/1.0", "board/{id}/quickfilter", True),
    "board_configuration": ("agile/1.0", "board/{id}/configuration", False),
    "board_sprints": ("agile/1.0", "board/{id}/sprint", True),
    "board_projects": ("agile/1.0", "board/{id}/project", True),
    "comments": ("api/3", "issue/{id}/comment", True),
    "worklogs": ("api/3", "issue/{id}/worklog", True),
    "changelog": ("api/3", "issue/{id}/changelog", True),
    "remote_links": ("api/3", "issue/{id}/remotelink", False),
    "issue_properties": ("api/3", "issue/{id}/properties", False),
    "issue_property": ("api/3", "issue/{id}/properties/{secondary}", False),
    "votes": ("api/3", "issue/{id}/votes", False),
}


def resource_request(
    resource,
    identifier="",
    secondary_id="",
    start_at=0,
    max_results=100,
    next_page_token=None,
):
    try:
        family, template, paginated = RESOURCES[resource]
    except KeyError as exc:
        raise ValueError("Unsupported Jira migration resource") from exc
    for marker, value in (("{id}", identifier), ("{secondary}", secondary_id)):
        if marker in template and (
            not isinstance(value, str) or not value or value in {".", ".."}
        ):
            raise ValueError("Missing or invalid resource identifier")
    path = template.format(
        id=quote(str(identifier), safe=""), secondary=quote(str(secondary_id), safe="")
    )
    params = {}
    if paginated:
        params.update(
            startAt=max(0, int(start_at)), maxResults=max(1, min(int(max_results), 100))
        )
    if resource in {"board_issues", "filter_issues"}:
        params.pop("startAt", None)
        params["fields"] = "key,project,updated"
        if next_page_token:
            params["nextPageToken"] = next_page_token
    if resource == "filter_issues":
        if not str(identifier).isdigit():
            raise ValueError("A Jira filter ID is required")
        params["jql"] = f"filter = {identifier} ORDER BY key ASC"
    if resource == "project":
        params["expand"] = (
            "description,lead,issueTypes,url,projectKeys,permissions,insight"
        )
    elif resource == "permission_scheme":
        params["expand"] = "permissions,user,group,projectRole,field,all"
    elif resource == "notification_scheme":
        params["expand"] = "all"
    elif resource in {
        "workflow_scheme",
        "issue_type_scheme",
        "field_configuration_scheme",
        "screen_scheme",
    }:
        if not str(identifier).isdigit():
            raise ValueError("A Jira project ID is required")
        params["projectId"] = identifier
    elif resource == "workflows":
        params["expand"] = "transitions,statuses,operations,schemes"
    elif resource == "filters":
        params["expand"] = "jql,description,owner,sharePermissions,editPermissions"
    return f"/rest/{family}/{path}", params


def export_download_path(kind, export_id, cloud_id=None):
    """Resolve only already-created exports on the configured source site."""
    export_id = str(UUID(str(export_id)))
    if kind == "cloud_backup":
        return f"/plugins/servlet/export/download/?fileId={export_id}"
    if kind == "automation":
        cloud_id = str(UUID(str(cloud_id)))
        return (
            f"/gateway/api/automation/internal-api/jira/{cloud_id}/pro/rest/"
            f"GLOBAL/rule/export/{export_id}/download"
        )
    raise ValueError("Unsupported Jira export kind")
