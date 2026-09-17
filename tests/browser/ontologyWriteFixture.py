"""Disposable authenticated ontology-write browser fixture.

Run with ``pdm run python tests/browser/ontologyWriteFixture.py``. Uses the real
application, localhost fixture login, canonical services and in-memory MongoDB.
No .env, production database, provider requests or real-user roles are used.
POST /fixture/stop to stop the fixture, including across sandbox PID namespaces.
"""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
os.environ.update(
    {
        "PYTHON_DOTENV_DISABLED": "1",
        "VON_USE_MOCK_DB": "1",
        "VON_DB_NAME": "test_ontology_browser_verification",
        "VON_AGENT_TEST_INSTANCE": "1",
        "VON_BROWSER_TEST_AUTH_ENABLED": "1",
        "VON_BROWSER_TEST_PSEUDOUSER_NAME": "Ontology Fixture Author",
        "VON_BROWSER_TEST_PSEUDOUSER_EMAIL": "ontology-fixture@example.test",
        "VON_BROWSER_TEST_PSEUDOUSER_CONCEPT_ID": "#V#ontology_fixture_author",
        "VON_BROWSER_TEST_ORGANISATION_CONCEPT_ID": "#V#ontology_fixture_sail",
        "VON_EVENT_WORKFLOW_INTEGRATION_ENABLE": "0",
        "VON_DURABLE_WORKFLOWS_ENABLE": "0",
        "VON_INTERNAL_MCP_ENABLE": "0",
        "VON_PREWARM_DISABLE": "1",
        "VON_WEB_PUSH_ENABLED": "0",
        "VON_CHAT_PROMPT_DISPATCHER_ENABLE": "0",
        "PYTEST_CURRENT_TEST": "isolated-browser-acceptance",
    }
)
from src.backend.server.utils_flask import create_flask_app


def no_generation(*args, **kwargs):
    raise RuntimeError("Generation is disabled in this disposable fixture")


app = create_flask_app(lambda: [], no_generation)
from src.backend.security.access_control import bypass_access_control
from src.backend.services.concept_service import create_concept

with bypass_access_control():
    for concept_id, name, parents in [
        ("#V#thing", "Thing", []),
        ("#V#person", "Person", ["#V#thing"]),
        ("#V#organisation", "Organisation", ["#V#thing"]),
        ("#V#ontology_fixture_sail", "Fixture SAIL", ["#V#organisation"]),
    ]:
        create_concept(concept_id=concept_id, name=name, parent_concept_ids=parents)
from flask import render_template

for name in [
    "settings",
    "chat",
    "vontology",
    "import_export",
    "entity",
    "concept",
    "annotation",
]:
    path = "/settings" if name == "settings" else f"/{name}_tab"
    app.add_url_rule(
        path,
        endpoint=f"serve_{name}_tab_route",
        view_func=lambda template=f"{name}_tab.html": render_template(template),
    )


@app.get("/fixture/evidence")
def fixture_evidence():
    import json
    from flask import session, Response
    from src.backend.services import ontology_publication_authority_service as authority
    from src.backend.services.concept_service import get_concept_by_concept_id
    from src.backend.services.text_value_service import get_texts_for_concept

    actor = session.get("user_concept_id")
    if actor != "#V#ontology_fixture_author":
        return {"error": "fixture_login_required"}, 403
    receipt_ids = [
        row["receipt_id"]
        for row in authority.get_ontology_mutation_receipts_collection().find(
            {"actor_concept_id": actor}
        )
    ]
    receipts = [
        authority.get_mutation_receipt_for_actor(receipt_id=rid, actor_concept_id=actor)
        for rid in receipt_ids
    ]
    ids = {
        cid
        for receipt in receipts
        for cid in receipt.get("target_concept_ids", [])
        if cid != "#V#thing"
    }
    concepts = {cid: get_concept_by_concept_id(cid) for cid in sorted(ids)}
    texts = {
        cid: get_texts_for_concept(cid, predicate="hasDescription")
        for cid in sorted(ids)
    }
    return Response(
        json.dumps(
            {"receipts": receipts, "concepts": concepts, "descriptions": texts},
            default=str,
        ),
        mimetype="application/json",
    )


@app.post("/fixture/stop")
def stop_fixture():
    import threading

    threading.Timer(0.2, lambda: os._exit(0)).start()
    return {"stopping": True}


app.run(host="127.0.0.1", port=5099, use_reloader=False)
