"""Practical continuity through independent stores and the ordinary gateway."""

import hashlib

import pytest

# pytest imports this fixture by name; function arguments intentionally shadow it.
# ruff: noqa: F811, RUF059
from src.backend.security.access_control import override_current_actor
from src.backend.services.knowledge_federation import continuity as c
from src.backend.services.knowledge_federation import protocol as p
from src.backend.services.knowledge_federation import service
from tests.backend.test_knowledge_federation import nodes, seed  # noqa: F401


def prepare(nodes):
    a, b, da, db = nodes
    seed(da)
    settings = a["exports"]["dgx"]
    settings.update(
        assertion_ids=[],
        assertion_audiences=["user:#V#alice", "org:#V#lab"],
        conversation_scopes=[{"user_id": "#V#alice", "namespace": "#V#alice@#V#lab"}],
        file_audiences=["user:#V#alice", "org:#V#lab"],
    )
    da.chat_history.insert_one(
        {
            "session_id": "meeting",
            "user_id": "#V#alice",
            "namespace": "#V#alice@#V#lab",
            "session_name": "Paper discussion",
            "history": [
                {"role": "system", "content": "HIDDEN"},
                {"role": "user", "content": "Explain this paper"},
                {
                    "role": "assistant",
                    "content": "It explains image generation",
                    "llm_debug_data": "HIDDEN",
                },
            ],
        }
    )
    da.chat_history.insert_one(
        {
            "session_id": "private-other",
            "user_id": "#V#bob",
            "namespace": "#V#bob@#V#lab",
            "history": [{"role": "user", "content": "HIDDEN"}],
        }
    )
    data = b"example research artefact"
    da.concepts.insert_one(
        {
            "concept_id": "#V#deck",
            "name": "deck.txt",
            "attributes": {
                "blob_key": "original/deck",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "content_type": "text/plain",
            },
            "relationships": {"#V#specific_to_user": ["#V#alice"]},
        }
    )
    return a, b, da, db, data


def sync(a, b, da, db):
    envelope = service.export_snapshot(a, "dgx", db=da)
    service.import_snapshot(b, "atlas", envelope, db=db)
    return envelope


def test_automatic_new_assertion_and_native_catalogues(nodes):
    a, b, da, db, data = prepare(nodes)
    envelope = sync(a, b, da, db)
    assert b"HIDDEN" not in p.encode(envelope)
    assert len(envelope["payload"]["records"]) == 5
    with override_current_actor("#V#alice", "#V#lab"):
        result = service.search(config=b, db=db)
        assert len(result["results"]) == 5
        assert result["global_completeness"] is False
        assert len(service.search(kind="conversation", config=b, db=db)["results"]) == 1
        first = service.search(limit=2, config=b, db=db)
        assert first["next_offset"] == 2
        second = service.search(limit=2, offset=2, config=b, db=db)
        assert not {x["id"] for x in first["results"]} & {
            x["id"] for x in second["results"]
        }
    with override_current_actor("#V#bob", None):
        assert len(service.search(config=b, db=db)["results"]) == 1
    additional = da.scoped_knowledge_assertions.find_one({"assertion_id": "private"})
    additional.pop("_id")
    additional["assertion_id"] = "newly-created"
    da.scoped_knowledge_assertions.insert_one(additional)
    updated = sync(a, b, da, db)
    assert any(x["id"] == "newly-created" for x in updated["payload"]["records"])


def wire(monkeypatch, a, b, da, db):
    from src.backend.services.knowledge_federation import transport

    monkeypatch.setattr(service, "database", lambda: db)
    monkeypatch.setattr(p, "load_config", lambda: b)
    monkeypatch.setattr(
        transport,
        "remote",
        lambda config, origin, action, request: c.serve_content(
            a, "dgx", request, db=da
        ),
    )


def find_id(db, kind):
    return "atlas/" + next(
        r["id"] for r in db[service.REPLICAS].find_one()["records"] if r["kind"] == kind
    )


def test_conversation_full_text_paging_revision_and_withdrawal(nodes, monkeypatch):
    a, b, da, db, data = prepare(nodes)
    da.chat_history.update_one(
        {"session_id": "meeting"},
        {"$push": {"history": {"role": "user", "content": "A" * 70000}}},
    )
    sync(a, b, da, db)
    wire(monkeypatch, a, b, da, db)
    fid = find_id(db, "conversation")
    with override_current_actor("#V#alice", "#V#lab"):
        first = c.read_conversation(fid)
        assert first["next_offset"] == c.PAGE_CHARS
        assert "HIDDEN" not in first["text"]
        last = c.read_conversation(
            fid, offset=first["next_offset"], content_digest=first["content_digest"]
        )
        assert last["next_offset"] is None
        da.chat_history.update_one(
            {"session_id": "meeting"},
            {"$push": {"history": {"role": "user", "content": "changed"}}},
        )
        with pytest.raises(ValueError, match="changed between pages"):
            c.read_conversation(
                fid, offset=first["next_offset"], content_digest=first["content_digest"]
            )
        a["exports"]["dgx"]["conversation_scopes"] = []
        with pytest.raises(PermissionError, match="withdrawn"):
            c.read_conversation(fid)


def test_file_materialisation_private_provenance_and_integrity(nodes, monkeypatch):
    from src.backend.services import computer_file_copy_service as files

    a, b, da, db, data = prepare(nodes)
    sync(a, b, da, db)
    wire(monkeypatch, a, b, da, db)
    monkeypatch.setattr(
        files, "fetch_file_copy_bytes", lambda **kw: {"success": True, "data": data}
    )
    calls = []

    def put(**kw):
        calls.append(kw)
        return {"success": True, "concept_id": "#V#local_copy"}

    monkeypatch.setattr(files, "import_bytes_file_copy", put)
    fid = find_id(db, "file_manifest")
    with override_current_actor("#V#alice", "#V#lab"):
        result = c.import_file(fid)
        assert result["success"] and result["local_copy_scope"] == "user:#V#alice"
        assert calls[0]["data"] == data
        assert calls[0]["namespace"] == "#V#alice"
        assert calls[0]["source_identifier"] == fid
        assert calls[0]["metadata"]["federation_origin"] == "atlas"
        monkeypatch.setattr(
            files,
            "fetch_file_copy_bytes",
            lambda **kw: {"success": True, "data": b"corrupt"},
        )
        with pytest.raises(ValueError, match="integrity"):
            c.import_file(fid)
        da.concepts.update_one(
            {"concept_id": "#V#deck"},
            {"$set": {"relationships.#V#specific_to_user": ["#V#bob"]}},
        )
        with pytest.raises(PermissionError, match="withdrawn"):
            c.import_file(fid)
    with override_current_actor("#V#bob", None), pytest.raises(PermissionError):
        c.import_file(fid)


def test_forged_response_and_late_revocation(nodes, monkeypatch):
    from src.backend.services.knowledge_federation import transport

    a, b, da, db, data = prepare(nodes)
    sync(a, b, da, db)
    wire(monkeypatch, a, b, da, db)
    fid = find_id(db, "conversation")

    def forged(config, origin, action, request):
        result = c.serve_content(a, "dgx", request, db=da)
        result["payload"]["body"]["text"] = "forged"
        return result

    monkeypatch.setattr(transport, "remote", forged)
    with override_current_actor("#V#alice", "#V#lab"):
        with pytest.raises(PermissionError, match="authentication"):
            c.read_conversation(fid)

        def revoked(config, origin, action, request):
            result = c.serve_content(a, "dgx", request, db=da)
            b["imports"]["atlas"]["enabled"] = False
            return result

        monkeypatch.setattr(transport, "remote", revoked)
        with pytest.raises(PermissionError, match="expired|not admitted"):
            c.read_conversation(fid)


def test_no_relay_or_legacy_scope_widening_and_protocol_bounds(nodes):
    a, b, da, db, data = prepare(nodes)
    doc = da.concepts.find_one({"concept_id": "#V#deck"})
    doc["attributes"]["source_system"] = "von_federation"
    assert c.file_record(doc, a["exports"]["dgx"]) is None
    doc["attributes"].pop("source_system")
    doc["relationships"] = {}
    assert c.file_record(doc, a["exports"]["dgx"]) is None
    doc["relationships"] = {
        "#V#specific_to_user": ["#V#alice"],
        "#V#specific_to_org": ["#V#lab"],
    }
    assert c.file_record(doc, a["exports"]["dgx"])["audience"] == "user:#V#alice"
    envelope = sync(a, b, da, db)
    row = next(x for x in envelope["payload"]["records"] if x["kind"] == "conversation")
    row["claim"]["user_id"] = "#V#bob"
    with pytest.raises(PermissionError):
        c.validate_catalogue_record(row)


def test_gateway_discovery_and_untrusted_identity_denial(nodes, monkeypatch):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
    )

    a, b, da, db, data = prepare(nodes)
    sync(a, b, da, db)
    wire(monkeypatch, a, b, da, db)
    # service.search imports its own load_config binding.
    monkeypatch.setattr(service, "load_config", lambda: b)
    g = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    with override_current_actor("#V#alice", "#V#lab"):
        r = g.invoke(
            "read_federated_conversation", {"federated_id": find_id(db, "conversation")}
        ).payload
        assert "Explain this paper" in r["text"]
    with override_current_actor("#V#bob", None):
        r = g.invoke(
            "read_federated_conversation", {"federated_id": find_id(db, "conversation")}
        ).payload
        assert "Explain this paper" not in str(r)
    with override_current_actor(None, None):
        from src.backend.integrations.internal_mcp.schemas import SchemaValidationError

        with pytest.raises(SchemaValidationError):
            g.invoke(
                "read_federated_conversation",
                {"federated_id": find_id(db, "conversation"), "user_id": "#V#alice"},
            )


def test_content_listener_requires_signed_bound_request(nodes):
    from src.backend.services.knowledge_federation.content_server import handle_request

    a, b, da, db, data = prepare(nodes)
    envelope = sync(a, b, da, db)
    record = next(
        r for r in envelope["payload"]["records"] if r["kind"] == "conversation"
    )
    payload = {
        "schema_version": "von_federation_content_request.v1",
        "origin": "dgx",
        "recipient": "atlas",
        "checked_at": p.now(),
        "request": {"id": record["id"], "record_digest": p.digest(record)},
    }
    signed = p.sign(payload, p.key(a["exports"]["dgx"]))
    result = handle_request(a, signed, db=da)
    assert "Explain this paper" in result["payload"]["body"]["text"]
    signed["payload"]["recipient"] = "wrong-node"
    with pytest.raises(PermissionError):
        handle_request(a, signed, db=da)
    signed = p.sign(signed["payload"], p.key(a["exports"]["dgx"]))
    with pytest.raises(PermissionError):
        handle_request(a, signed, db=da)


def test_owner_subscription_covers_new_namespaces_but_not_other_owners(nodes):
    a, b, da, db, _ = prepare(nodes)
    a["exports"]["dgx"]["conversation_scopes"] = []
    a["exports"]["dgx"]["conversation_users"] = ["#V#alice"]
    da.chat_history.insert_one(
        {
            "session_id": "new-namespace",
            "user_id": "#V#alice",
            "namespace": "#V#alice@lab",
            "history": [],
        }
    )
    da.chat_history.insert_one(
        {
            "session_id": "conflicting",
            "user_id": "#V#alice",
            "namespace": "#V#bob@lab",
            "history": [],
        }
    )
    envelope = sync(a, b, da, db)
    conversations = [
        r for r in envelope["payload"]["records"] if r["kind"] == "conversation"
    ]
    assert {r["claim"]["source_id"] for r in conversations} == {
        "meeting",
        "new-namespace",
    }
    for record in conversations:
        c.validate_catalogue_record(record)


def test_compact_refresh_reuses_only_authenticated_unchanged_cache(nodes):
    a, b, da, db, _ = prepare(nodes)
    first = sync(a, b, da, db)
    new = service.export_snapshot(a, "dgx", db=da)
    compact = p.compact_snapshot(new, first["payload"]["digest"])
    assert "records" not in compact["payload"]
    receipt = service.import_snapshot(b, "atlas", compact, db=db)
    assert receipt["status"] == "already_applied"
    assert db[service.REPLICAS].find_one()["checked_at"] == new["payload"]["checked_at"]
    compact["payload"]["digest"] = "forged"
    with pytest.raises(ValueError, match="cache mismatch"):
        service.import_snapshot(b, "atlas", compact, db=db)
    compact = p.compact_snapshot(new, first["payload"]["digest"])
    compact["signature"] = "forged"
    with pytest.raises(PermissionError):
        service.import_snapshot(b, "atlas", compact, db=db)


def test_capacity_is_checked_before_filtering_legacy_namespaces(nodes, monkeypatch):
    a, b, da, db, _ = prepare(nodes)
    settings = a["exports"]["dgx"]
    settings.update(
        conversation_scopes=[], conversation_users=["#V#alice"], file_audiences=[]
    )
    da.chat_history.insert_one(
        {"session_id": "legacy", "user_id": "#V#alice", "history": []}
    )
    da.chat_history.insert_one(
        {
            "session_id": "later-valid",
            "user_id": "#V#alice",
            "namespace": "#V#alice",
            "history": [],
        }
    )
    monkeypatch.setattr(c, "MAX_RECORDS", 2)
    with pytest.raises(ValueError, match="capacity exceeded"):
        c.collect_catalogue(da, settings)
