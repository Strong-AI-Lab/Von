import base64
import json
import os
from typing import Any

import requests
from dotenv import load_dotenv


def adf_paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def main() -> None:
    load_dotenv(dotenv_path=".env", override=False)

    base_url = (
        os.getenv("ATLASSIAN_BASE_URL") or os.getenv("ATLASSIAN_SITE_BASE") or ""
    ).rstrip("/")
    email = (os.getenv("ATLASSIAN_EMAIL") or "").strip()
    token = (os.getenv("ATLASSIAN_API_TOKEN") or "").strip()

    if not base_url or not email or not token:
        missing = [
            key
            for key, val in [
                ("ATLASSIAN_BASE_URL/ATLASSIAN_SITE_BASE", base_url),
                ("ATLASSIAN_EMAIL", email),
                ("ATLASSIAN_API_TOKEN", token),
            ]
            if not val
        ]
        raise SystemExit("Missing Jira credentials in .env: " + ", ".join(missing))

    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Best-effort assignee
    account_id = None
    try:
        myself = requests.get(
            f"{base_url}/rest/api/3/myself", headers=headers, timeout=30
        )
        myself.raise_for_status()
        account_id = (myself.json() or {}).get("accountId")
    except Exception:
        account_id = None

    summary = "Vontology-defined concept views + agentic rendering workflows"

    description_lines = [
        "Introduce a flexible, ontology-driven UI view system where what is displayed (and how) for a concept type is defined in the Vontology, and rendered via an agentic workflow.",
        "",
        "Motivation",
        "- Current UI hard-codes which predicates appear in which sections (e.g. Content/Notes/Text Fields).",
        "- We want per-concept-type view definitions that can evolve without frontend code changes.",
        "- Support richer artefacts: figures, code blocks, images, videos, simulations, and interactive components (e.g. maps).",
        "",
        "Proposed Vontology representation (sketch)",
        "- Concepts (examples): #V#ui_view_spec, #V#ui_component, #V#ui_render_workflow, #V#ui_data_binding, #V#map_component",
        "- Predicates (examples): #V#hasViewSpec, #V#hasSection, #V#hasComponent, #V#bindsPredicate, #V#hasRenderPolicy, #V#hasSchema",
        "- Use text relations for JSON config (schemas, props, policies).",
        "",
        "Agentic rendering workflow (sketch)",
        "- Planner reads view spec for (concept, view context) and produces a render plan.",
        "- Executor resolves data bindings, runs transforms (markdown render, summarisation, chart extraction), and returns a safe component model.",
        "- Safe-by-default: sanitised HTML, no remote scripts, explicit allow-list for embeds, provenance badges.",
        "",
        "Example: map component defined in Vontology",
        "- #V#map_component hasSchema: { provider, bbox, layers, markerPredicate, style }",
        "- Bind markers to predicates like #V#hasLocation or derived annotations.",
        "",
        "Deliverables / acceptance criteria",
        "- Minimal Vontology schema for view specs + components + bindings (concepts + predicates + example instances).",
        "- Thin backend endpoint returning a resolved view model for a concept (initially read-only).",
        "- Frontend renderer supports at least: table component + rich text component.",
        "- Demonstrate one concept type using a custom view spec without frontend hard-coding.",
        "",
        "Notes",
        "- Keep this research-prototype friendly: make the core extensible, avoid big framework rewrites.",
    ]

    description_doc = {
        "type": "doc",
        "version": 1,
        "content": [adf_paragraph(line) for line in description_lines],
    }

    payload: dict[str, Any] = {
        "fields": {
            "project": {"key": "JVNAUTOSCI"},
            "summary": summary,
            "issuetype": {"name": "Task"},
            "description": description_doc,
        }
    }

    if account_id:
        payload["fields"]["assignee"] = {"accountId": account_id}

    resp = requests.post(
        f"{base_url}/rest/api/3/issue",
        headers=headers,
        data=json.dumps(payload),
        timeout=30,
    )
    resp.raise_for_status()
    created = resp.json() or {}
    key = created.get("key")

    print(
        json.dumps(
            {
                "created": True,
                "key": key,
                "url": f"{base_url}/browse/{key}" if key else None,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
