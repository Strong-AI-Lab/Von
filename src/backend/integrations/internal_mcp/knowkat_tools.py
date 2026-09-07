"""Thin source retrieval and read-only adoption proposal over standard MCP."""

from __future__ import annotations
from functools import partial
from .gateway import MethodDefinition
from .schemas import Schema, validate_payload
from .knowkat_proxy_mcp import get_knowkat_client, public_source_enabled

TOOL_SPECS = {
    "list_knowledge_bases": (
        {},
        {},
        "List configured public ontology sources, exact versions, licences and coverage.",
    ),
    "search_concepts": (
        {"kb_id": str, "query": str},
        {"limit": int},
        "Search existing external concepts by IRI, names and descriptions; matching names do not establish identity.",
    ),
    "get_concept": (
        {"kb_id": str, "identifier": str},
        {"limit": int, "cursor": (str, type(None))},
        "Read an external concept and paginated source assertions with licence attribution.",
    ),
    "get_statements": (
        {"kb_id": str},
        {
            "subject": (str, type(None)),
            "predicate": (str, type(None)),
            "object": (str, type(None)),
            "limit": int,
            "cursor": (str, type(None)),
        },
        "Read source RDF statements without flattening blank nodes or multiple parents. Preserve pagination and provenance.",
    ),
    "get_ontology_neighbourhood": (
        {"kb_id": str, "identifier": str},
        {"depth": int, "limit": int},
        "Inspect a bounded source neighbourhood, including explicit truncation; no inference or full closure is implied.",
    ),
}


def tool_schema(name):
    required, optional, _ = TOOL_SPECS[name]
    return Schema(required=required, optional=optional, allow_unknown=False)


def call_knowkat(tool_name, **kwargs):
    from .catalogue import _run_async_compat, make_error_response

    try:
        valid, errors = validate_payload(tool_schema(tool_name), kwargs)
        if not valid:
            return make_error_response("invalid_knowkat_arguments", "; ".join(errors))
        client = get_knowkat_client()

        async def invoke():
            return await client.call_tool(tool_name, kwargs)

        return _run_async_compat(invoke)
    except Exception as exc:
        return make_error_response("knowkat_unavailable", str(exc))


def prepare_adoption(
    kb_id: str,
    identifier: str,
    *,
    local_name: str,
    local_kind: str,
    parent_id: str,
    local_description: str,
    **kwargs,
):
    """Produce exact, bounded source evidence for existing governed write tools."""
    from .catalogue import make_error_response
    from ...services.ontology_source_adoption_service import build_adoption_proposal

    if kwargs:
        return make_error_response(
            "invalid_adoption_arguments", "Unknown adoption arguments"
        )
    source = call_knowkat("get_concept", kb_id=kb_id, identifier=identifier, limit=200)
    try:
        return build_adoption_proposal(
            source,
            identifier=identifier,
            local_name=local_name,
            local_kind=local_kind,
            parent_id=parent_id,
            local_description=local_description,
        )
    except ValueError as exc:
        return make_error_response("knowkat_adoption_unavailable", str(exc))


def definitions():
    public = public_source_enabled()
    result = [
        MethodDefinition(
            name="knowkat_" + name,
            handler=partial(call_knowkat, name),
            input_schema=tool_schema(name),
            output_schema=Schema(required={}, allow_unknown=True),
            category="read",
            ordinary_turn_public=public,
            description=description,
        )
        for name, (_, _, description) in TOOL_SPECS.items()
    ]
    result.append(
        MethodDefinition(
            name="knowkat_prepare_adoption",
            handler=prepare_adoption,
            input_schema=Schema(
                required={
                    "kb_id": str,
                    "identifier": str,
                    "local_name": str,
                    "local_kind": str,
                    "parent_id": str,
                    "local_description": str,
                },
                allow_unknown=False,
            ),
            output_schema=Schema(required={}, allow_unknown=True),
            category="read",
            ordinary_turn_public=public,
            description="Prepare a source-attributed concept adoption using a grounded local parent and interpretation. Search existing Vontology and inspect represented #V#kr_design_materialisation_workflow and #V#prompt_kr_design_materialisation_plan when relevant. Returns create_concepts and exact provenance text arguments, not a write or authority grant. Execute through governed tools, read back all text, verify repeat identity reuse and export. Source statements remain quoted evidence; local interpretation is separately labelled.",
        )
    )
    return result
