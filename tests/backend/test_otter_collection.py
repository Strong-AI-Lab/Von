"""Synthetic outcomes for private collection; no source credentials or live DB."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from src.backend.integrations.internal_mcp.catalogue import build_default_catalogue
from src.backend.integrations.internal_mcp.gateway import InternalMCPGateway
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.integrations.otter_live_client import (
    CredentialStore,
    private_json,
    unwrap,
)
from src.backend.security.access_control import override_current_actor
from src.backend.services import otter_collection_service as service
from src.backend.services import otter_representation_service as representation


@pytest.fixture
def configured(tmp_path, monkeypatch):
    archive = SimpleNamespace(
        database=tmp_path / "archive.sqlite3", owner_user_concept_id="#V#owner"
    )
    root = tmp_path / "collection"
    settings = {"account_email": "owner@example.org"}
    monkeypatch.setattr(service, "config", lambda _: (archive, root, settings))
    return archive, root, settings


def test_queue_daily_deduplicates_and_reports_terminal_state(configured):
    first = service.enqueue("private", daily=True, spawn=False)
    with service.state_db(configured[1]) as db:
        db.execute("UPDATE jobs SET status='completed' WHERE id=?", (first["run_id"],))
    repeated = service.enqueue("private", daily=True, spawn=False)
    assert repeated["run_id"] == first["run_id"]
    assert repeated["status"] == "completed"
    assert repeated["collection_complete"]
    with pytest.raises(ValueError):
        service.enqueue(
            "private", meeting_ids=["https://evil.example/u/id"], spawn=False
        )


def test_denied_actor_cannot_enqueue(monkeypatch):
    monkeypatch.setenv("VON_OTTER_ARCHIVE_OWNER_USER_CONCEPT_ID", "#V#owner")
    monkeypatch.setattr(
        service, "enqueue", lambda *a, **kw: pytest.fail("worker must not launch")
    )
    with override_current_actor("#V#other", None):
        result = (
            InternalMCPGateway(
                catalogue=build_default_catalogue(),
                transport=InternalMCPTransport(),
                enabled=True,
            )
            .invoke("otter_collect_now", {"resource_id": "personal_otter_archive"})
            .payload
        )
    assert result["success"] is False
    assert result["error_code"] == "otter_archive_resource_not_authorised"


def test_source_errors_do_not_discard_partial_progress_and_retry_bindings(
    configured, monkeypatch
):
    calls = []
    fail = {"b"}

    class Session:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            if name == "otter_get_user_info":
                return "Name: Owner\nEmail: owner@example.org"
            if name == "otter_search":
                return {
                    "results": [{"id": "a"}, {"id": "b"}],
                    "pagination_completion_reason": "all_results_returned",
                }
            if arguments["id"] in fail:
                raise RuntimeError("private source text must never enter receipt")
            return {
                "id": arguments["id"],
                "text": "[0:01] Person: Hello",
                "url": "https://otter.ai/u/" + arguments["id"],
            }

    @asynccontextmanager
    async def session(_):
        yield Session()

    monkeypatch.setattr(service, "otter_session", session)
    monkeypatch.setattr(
        service,
        "import_snapshot",
        lambda a, m: {"conversation_id": m["id"], "status": "new"},
    )
    observed = []
    monkeypatch.setattr(
        representation,
        "represent_meeting",
        lambda o, m, a, b: observed.append(b) or {"participants": []},
    )
    first = service.enqueue(
        "private",
        meeting_ids=["a", "b"],
        participant_bindings={"b": {"Person": "#V#person"}},
        spawn=False,
    )
    service.work("private")
    result = service.status("private", first["run_id"])
    assert result["runs"][0]["status"] == "partial"
    assert result["pending_meetings"] == 1
    assert "private source text" not in json.dumps(result)
    fail.clear()
    second = service.enqueue("private", meeting_ids=["b"], spawn=False)
    service.work("private")
    assert (
        service.status("private", second["run_id"])["runs"][0]["status"] == "completed"
    )
    assert observed[-1] == {"Person": "#V#person"}
    assert service.status("private")["pending_meetings"] == 0


def test_missing_pagination_proof_does_not_advance_watermark(configured, monkeypatch):
    class Session:
        async def call_tool(self, name, arguments):
            return (
                "Name: Owner\nEmail: owner@example.org"
                if name == "otter_get_user_info"
                else {"results": []}
            )

    @asynccontextmanager
    async def session(_):
        yield Session()

    monkeypatch.setattr(service, "otter_session", session)
    run = service.enqueue("private", spawn=False)
    service.work("private")
    row = service.status("private", run["run_id"])["runs"][0]
    assert row["status"] == "completed_with_coverage_limit"
    assert row["receipt"]["collection_scope"] == "returned_meetings"
    with service.state_db(configured[1]) as db:
        assert (
            db.execute("SELECT * FROM state WHERE key='last_complete_date'").fetchone()
            is None
        )


def test_account_mismatch_stops_before_fetch(configured, monkeypatch):
    class Session:
        async def call_tool(self, name, arguments):
            assert name == "otter_get_user_info"
            return "Email: other@example.org"

    @asynccontextmanager
    async def session(_):
        yield Session()

    monkeypatch.setattr(service, "otter_session", session)
    run = service.enqueue("private", meeting_ids=["a"], spawn=False)
    service.work("private")
    assert service.status("private", run["run_id"])["runs"][0]["status"] == "failed"


def test_nested_envelopes_and_token_age_survive_restart(tmp_path):
    assert unwrap({"structuredContent": {"result": '{"id":"a"}'}}) == {"id": "a"}
    with pytest.raises(RuntimeError):
        unwrap({"isError": True, "content": []})
    private_json(
        tmp_path / "tokens.json",
        {
            "access_token": "synthetic",
            "token_type": "Bearer",
            "expires_in": 3600,
            "expires_at": 1,
        },
    )
    token = asyncio.run(CredentialStore(tmp_path).get_tokens())
    assert token.expires_in == 0
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600


def test_source_speaker_labels_do_not_invent_identities():
    assert representation.speaker_labels(
        "[0:01] Person: Hello\n[0:01:02] Person: Again\n[0:02:03] Other: Yes"
    ) == ["Person", "Other"]


def test_participant_binding_survives_refresh_and_rebinding_retracts_old_link(
    monkeypatch,
):
    from contextlib import nullcontext

    from src.backend.services import (
        concept_service,
        scoped_assertion_service,
        workflow_event_integration_service,
    )

    concepts = {"#V#person": {}, "#V#corrected": {}}
    assertions = []
    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id_exact",
        lambda cid: concepts.get(cid),
    )
    monkeypatch.setattr(
        concept_service,
        "create_concept",
        lambda **kw: concepts.setdefault(kw["concept_id"], kw),
    )
    monkeypatch.setattr(
        workflow_event_integration_service,
        "suppress_event_workflow_launches",
        lambda _: nullcontext(),
    )
    monkeypatch.setattr(
        scoped_assertion_service,
        "list_visible_scoped_assertions_page",
        lambda **kw: {"items": list(assertions)},
    )

    def upsert(**kw):
        assert kw["scope_mode"] == "user"
        assert kw["acting_user_concept_id"] == "#V#owner"
        row = {
            "assertion_id": str(len(assertions)),
            "predicate": kw["predicate"],
            "object_concept_id": kw["target_concept_id"],
            "provenance": {"evidence": kw["evidence"]},
        }
        if kw["predicate"] == "#V#meeting_participant" and not any(
            r["object_concept_id"] == row["object_concept_id"] for r in assertions
        ):
            assertions.append(row)
        return {"success": True}

    def retract(**kw):
        assertions[:] = [
            r for r in assertions if r["assertion_id"] != kw["assertion_id"]
        ]
        return {"success": True}

    monkeypatch.setattr(scoped_assertion_service, "upsert_scoped_assertion", upsert)
    monkeypatch.setattr(scoped_assertion_service, "retract_scoped_assertion", retract)
    meeting = {"id": "a", "url": "https://otter.ai/u/a", "text": "[0:01] Person: Hello"}
    archived = {
        "source_sha256": "test",
        "artifacts": [{"kind": "transcript", "artifact_id": "test"}],
    }
    first = representation.represent_meeting(
        "#V#owner", meeting, archived, {"Person": "#V#person"}
    )
    repeated = representation.represent_meeting("#V#owner", meeting, archived, {})
    assert first == repeated
    assert len(assertions) == 1
    representation.represent_meeting(
        "#V#owner", meeting, archived, {"Person": "#V#corrected"}
    )
    assert [r["object_concept_id"] for r in assertions] == ["#V#corrected"]
