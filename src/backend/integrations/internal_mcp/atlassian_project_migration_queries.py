"""Named read-only Atlas project queries for Jira-linked source retention.

Queries use the configured Atlassian site and existing account. Source IDs and
cursors are variables, never executable GraphQL or credential destinations.
"""

import re
from uuid import UUID

USER = "id accountId canonicalAccountId accountStatus name picture"
DATE = "confidence dateRange { start end }"
COMMENT = f"id uuid url commentText creationDate editDate creator {{ {USER} }}"
HIGHLIGHT = f"id summary description creationDate lastEditedDate creator {{ {USER} }} lastEditedBy {{ {USER} }}"
NOTE = f"id summary description creationDate index archived creator {{ {USER} }}"
MILESTONE = "id uuid title status completionDate targetDate targetDateType lastModifiedDatetime creationDatetime"


def _connection(selection, *, arguments="first: $first, after: $after", edge_fields=""):
    return f"({arguments}) {{ edges {{ {edge_fields} node {{ {selection} }} }} pageInfo {{ hasNextPage endCursor }} }}"


_DEFINITION_FIELDS = f"""id name token description type creationDate lastModifiedDate
    linkedEntityTypes creator {{ {USER} }}"""
_DEFINITION = "__typename " + " ".join(
    f"... on Townsquare{kind}CustomFieldDefinition {{ {_DEFINITION_FIELDS} }}"
    for kind in ("User", "Text", "Number", "TextSelect")
)
_CUSTOM_COMMON = f"""uuid creationDate lastModifiedDate creator {{ {USER} }}
    definition {{ {_DEFINITION} }}"""
CUSTOM_FIELDS = f"""__typename
    ... on TownsquareTextCustomField {{ {_CUSTOM_COMMON} textValue: value {{ id value }} }}
    ... on TownsquareNumberCustomField {{ {_CUSTOM_COMMON} numberValue: value {{ id value }} }}
    ... on TownsquareTextSelectCustomField {{ {_CUSTOM_COMMON}
        textValues: values {_connection('id value', arguments='first: 100')} }}
    ... on TownsquareUserCustomField {{ {_CUSTOM_COMMON}
        userValues: values {_connection(USER, arguments='first: 100')} }}
"""


_UPDATE_COMMON = f"""id ari uuid url creationDate editDate summary updateType
    missedUpdate creator {{ {USER} }} lastEditedBy {{ {USER} }}
    newTargetDate newTargetDateConfidence oldTargetDate oldTargetDateConfidence
    newDueDate {{ {DATE} }} oldDueDate {{ {DATE} }}
    comments {_connection(COMMENT, arguments='first: 100')}
    updateNotes {_connection(NOTE, arguments='first: 100')}
    highlights {_connection(HIGHLIGHT, arguments='first: 100')}
"""
PROJECT_UPDATE = _UPDATE_COMMON + f"""
    newState {{ label value }} oldState {{ label value }}
    newPhaseNew {{ id name displayName }} oldPhaseNew {{ id name displayName }}
    milestones {_connection(MILESTONE, arguments='first: 100')}
    changelog {{ accountId creationDate field newValue oldValue }}
"""
GOAL_UPDATE = _UPDATE_COMMON + """
    newState { label value score } oldState { label value score }
    newScore oldScore newProgress { percentage type } oldProgress { percentage type }
"""
SHARED_CONNECTIONS = {
    "customFields": CUSTOM_FIELDS,
    "access": "__typename",
    "teams": "id displayName description state membershipSettings organizationId",
    "comments": COMMENT,
    "watchers": USER,
    "tags": "id name description creationDate iconData url",
    "highlights": HIGHLIGHT,
    "risks": HIGHLIGHT + " resolvedDate",
    "slackChannels": f"""slackConnectionId types updatedAt creationDate
        subscriber {{ {USER} }} metadata {{ fieldTypes }}
        channel {{ channelId name numMembers private slackTeamName slackTeamId }}""",
    "msteamsChannels": f"""creationDate subscriber {{ {USER} }}
        tenant {{ microsoftTenantId displayName }}
        team {{ internalId displayName visibility isPrivate }}
        channel {{ channelId displayName isPrivate membershipType }}""",
}
PROJECT_CONNECTIONS = {
    **SHARED_CONNECTIONS,
    "updates": PROJECT_UPDATE,
    "changelog": "accountId creationDate field newValue oldValue",
    "dependencies": "created linkType incomingProject { id key name } outgoingProject { id key name }",
    "goals": "id key name",
    "links": "id name url iconUrl provider type",
    "contributors": f"""userContributor {{ {USER} }} teamContributor {{
        team {{ id displayName }}
        contributingMembers {_connection(USER, arguments='first: 100')}
    }}""",
}
GOAL_CONNECTIONS = {
    **SHARED_CONNECTIONS,
    "updates": GOAL_UPDATE,
    "projects": "id key name",
    "parentGoals": "id key name",
    "subGoals": "id key name",
    "successMeasures": "id key name",
}

_DRAFT = f"""id summary status modifiedDate author {{ {USER} }}
    targetDate {{ date confidence }}
    updateNotes {{ summary description uuid archived updateNoteId }}
    highlights {{ summary description type }}"""
PROJECT_FIELDS = f"""id key uuid name isArchived isPrivate iconData
    creationDate url startDate latestUpdateDate userUpdateCount watcherCount accessLevel
    owner {{ {USER} }} description {{ what why measurement }}
    dueDate {{ {DATE} }} state {{ label value }}
    fusion {{ issueAri synced }} latestDraftUpdate {{ {_DRAFT} }}
"""
GOAL_FIELDS = f"""id key uuid name isArchived isPrivate iconData
    creationDate url startDate latestUpdateDate accessLevel softDeleted
    description descriptionWhy descriptionMeasurement scoringMode
    owner {{ {USER} }} dueDate {{ {DATE} }} targetDate {{ {DATE} }}
    state {{ label value score }} status {{ value score }}
    progress {{ percentage type }} latestDraftUpdate {{ {_DRAFT} score }}
"""


def project_query_request(
    resource,
    identifier="",
    secondary_id="",
    start_at=0,
    max_results=100,
    next_page_token=None,
):
    variables = {"first": max(1, min(int(max_results), 100)), "after": next_page_token}
    if resource == "atlas_projects":
        site_id = str(UUID(str(identifier)))
        variables["container"] = f"ari:cloud:townsquare::site/{site_id}"
        query = """query MigrationProjects($container: String!, $first: Int!, $after: String) {
          projects_search(containerId: $container, searchString: "", first: $first, after: $after) {
            edges { node { __typename id name } }
            pageInfo { hasNextPage endCursor }
          }
        }"""
    elif resource in {"atlas_project", "atlas_goal"} or resource.startswith(
        ("atlas_project_", "atlas_goal_")
    ):
        kind = "goal" if resource.startswith("atlas_goal") else "project"
        site_id = str(UUID(str(identifier)))
        object_id = str(UUID(str(secondary_id)))
        variables["id"] = f"ari:cloud:townsquare:{site_id}:{kind}/{object_id}"
        root = f"{kind}s_byId({kind}Id: $id)"
        variable_type = "ID!" if kind == "goal" else "String!"
        if resource == f"atlas_{kind}":
            variables = {"id": variables["id"]}
            declarations = f"$id: {variable_type}"
            selection = GOAL_FIELDS if kind == "goal" else PROJECT_FIELDS
        else:
            field = resource.removeprefix(f"atlas_{kind}_")
            connections = GOAL_CONNECTIONS if kind == "goal" else PROJECT_CONNECTIONS
            if field not in connections:
                raise ValueError("Unsupported Atlassian project migration resource")
            declarations = f"$id: {variable_type}, $first: Int!, $after: String"
            edge_fields = (
                "principalAri role isFollower isOwner appLevelEffectiveRole"
                if field == "access"
                else ""
            )
            selection = (
                "id " + field + _connection(connections[field], edge_fields=edge_fields)
            )
        query = (
            f"query MigrationProjectResource({declarations}) {{ "
            f'{root} @optIn(to: "Townsquare") {{ {selection} }} }}'
        )
    elif resource == "atlas_type_schema":
        if not re.fullmatch(r"[_A-Za-z][_0-9A-Za-z]*", str(identifier)):
            raise ValueError("A GraphQL type name is required")
        variables = {"name": identifier}
        query = """query MigrationTypeSchema($name: String!) {
          __type(name: $name) {
            name kind possibleTypes { name } fields(includeDeprecated: true) {
              name description args { name defaultValue type {
                kind name ofType { kind name ofType { kind name } }
              } }
              type { kind name ofType { kind name ofType { kind name } } }
            }
          }
        }"""
    else:
        raise ValueError("Unsupported Atlassian project migration resource")
    return {"query": query, "variables": variables}
