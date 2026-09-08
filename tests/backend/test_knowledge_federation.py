"""Independent-store, trust-boundary and interrupted reconciliation acceptance."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import mongomock
import pytest

from src.backend.security.access_control import override_current_actor
from src.backend.services.knowledge_federation import protocol as p
from src.backend.services.knowledge_federation import service


@pytest.fixture
def nodes(tmp_path):
    secret = tmp_path / "pair.key"
    secret.write_text("ab" * 32)
    secret.chmod(0o600)
    audiences = ["public", "user:#V#alice", "org:#V#lab"]

    def config(node, peer):
        settings = {
            "enabled": True,
            "key_file": str(secret),
            "audiences": audiences,
            "audience_map": {a: a for a in audiences},
            "assertion_ids": ["private", "org"],
            "public_relations": [
                {
                    "subject": "#V#paper",
                    "predicate": "#V#related_to",
                    "object": "#V#topic",
                }
            ],
        }
        return {
            "schema_version": p.CONFIG_VERSION,
            "node_id": node,
            "imports": {peer: copy.deepcopy(settings)},
            "exports": {peer: copy.deepcopy(settings)},
        }

    return (
        config("atlas", "dgx"),
        config("dgx", "atlas"),
        mongomock.MongoClient().atlas,
        mongomock.MongoClient().dgx,
    )


def seed(db):
    for cid in ("#V#paper", "#V#related_to", "#V#topic"):
        db.concepts.insert_one(
            {
                "concept_id": cid,
                "relationships": (
                    {"#V#related_to": ["#V#topic"]} if cid == "#V#paper" else {}
                ),
                "provenance": {
                    "source_uri": "https://example.org/paper",
                    "licence": "CC-BY-3.0",
                },
            }
        )
    for identifier, audience in (("private", "user:#V#alice"), ("org", "org:#V#lab")):
        db.scoped_knowledge_assertions.insert_one(
            {
                "assertion_id": identifier,
                "assertion_revision": 1,
                "assertion_form": "standalone_text",
                "object_kind": "text",
                "object_text": {
                    "text": "Alice read the Rhet2Pix paper",
                    "language": "en",
                },
                "scope": {
                    "mode": "user" if identifier == "private" else "organisation",
                    "user_concept_id": "#V#alice",
                    "organisation_concept_id": (
                        "#V#lab" if identifier == "org" else None
                    ),
                    "audience_keys": [audience],
                },
                "status": "asserted",
                "provenance": {"source_event_id": "meeting-123"},
                "rag_index": {"private_runtime_data": "never export"},
            }
        )


def exchange(nodes):
    a, b, db_a, db_b = nodes
    envelope = service.export_snapshot(a, "dgx", db=db_a)
    receipt = service.import_snapshot(b, "atlas", envelope, db=db_b)
    return envelope, receipt


def read(nodes, user=None, org=None, **kwargs):
    _, b, _, db_b = nodes
    with override_current_actor(user, org):
        return service.search(config=b, db=db_b, **kwargs)


def test_two_stores_scope_provenance_and_no_execution(nodes):
    seed(nodes[2])
    envelope, receipt = exchange(nodes)
    assert receipt["status"] == "applied"
    assert len(read(nodes)["results"]) == 1  # public needs no user
    assert len(read(nodes, "#V#outsider", "#V#elsewhere")["results"]) == 1
    assert len(read(nodes, "#V#bob", "#V#lab")["results"]) == 2
    own = read(nodes, "#V#alice", "#V#lab")
    assert len(own["results"]) == 3
    assert own["global_completeness"] is False
    assert all(
        r["origin"] == "atlas" and not r["execution_authority"] for r in own["results"]
    )
    assert "private_runtime_data" not in p.encode(envelope).decode()
    assert "CC-BY-3.0" in p.encode(own).decode()
    assert nodes[3].scoped_knowledge_assertions.count_documents({}) == 0
    assert nodes[3].workflow_instances.count_documents({}) == 0
    assert nodes[3].concepts.count_documents({}) == 0


def test_duplicate_retraction_reselection_and_offline_catchup(nodes):
    seed(nodes[2])
    first, _ = exchange(nodes)
    assert (
        service.import_snapshot(nodes[1], "atlas", first, db=nodes[3])["status"]
        == "already_applied"
    )
    nodes[2].scoped_knowledge_assertions.update_one(
        {"assertion_id": "private"},
        {"$set": {"status": "retracted"}, "$inc": {"assertion_revision": 1}},
    )
    nodes[0]["exports"]["dgx"]["assertion_ids"] = ["private"]
    latest, receipt = exchange(nodes)
    assert receipt["withdrawn"] == 1
    assert len(read(nodes, "#V#alice", "#V#lab")["results"]) == 1
    with pytest.raises(ValueError, match="stale"):
        service.import_snapshot(nodes[1], "atlas", first, db=nodes[3])
    assert (
        service.import_snapshot(nodes[1], "atlas", latest, db=nodes[3])["status"]
        == "already_applied"
    )
    nodes[0]["exports"]["dgx"]["public_relations"] = []
    exchange(nodes)
    assert read(nodes, "#V#alice", "#V#lab")["results"] == []


@pytest.mark.parametrize(
    "mutation",
    ["signature", "origin", "recipient", "protocol", "audience", "equivocation"],
)
def test_hostile_or_incompatible_snapshot_does_not_change_head(nodes, mutation):
    seed(nodes[2])
    envelope, _ = exchange(nodes)
    before = nodes[3][service.REPLICAS].find_one()
    forged = copy.deepcopy(envelope)
    if mutation == "signature":
        forged["payload"]["records"][0]["claim"]["status"] = "retracted"
    else:
        payload = forged["payload"]
        if mutation == "origin":
            payload["origin"] = "rogue"
        elif mutation == "recipient":
            payload["recipient"] = "sc"
        elif mutation == "protocol":
            payload["schema_version"] = "v0"
        elif mutation == "audience":
            payload["records"][0]["audience"] = "user:#V#bob"
        else:
            payload["records"][0]["claim"]["status"] = "retracted"
        payload["digest"] = p.digest(payload["records"])
        forged = p.sign(payload, p.key(nodes[0]["exports"]["dgx"]))
    with pytest.raises((PermissionError, ValueError)):
        service.import_snapshot(nodes[1], "atlas", forged, db=nodes[3])
    assert nodes[3][service.REPLICAS].find_one() == before


def test_interrupted_transfer_and_failed_source_scan_keep_previous_generation(
    nodes, monkeypatch
):
    seed(nodes[2])
    envelope, _ = exchange(nodes)
    before = nodes[3][service.REPLICAS].find_one()
    with pytest.raises(json.JSONDecodeError):
        json.loads(p.encode(envelope)[:-20])
    assert nodes[3][service.REPLICAS].find_one() == before
    before_export = nodes[2][service.EXPORTS].find_one()

    def broken(*args):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(service, "stable_records", broken)
    with pytest.raises(RuntimeError):
        service.export_snapshot(nodes[0], "dgx", db=nodes[2])
    assert nodes[2][service.EXPORTS].find_one() == before_export


def test_revocation_staleness_and_audience_narrowing(nodes):
    seed(nodes[2])
    envelope, _ = exchange(nodes)
    payload = copy.deepcopy(envelope["payload"])
    payload["generation"] += 1
    payload["checked_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    # A fresh receiving node can receive an old snapshot but cannot read its expired private content.
    nodes[3][service.REPLICAS].delete_many({})
    service.import_snapshot(
        nodes[1],
        "atlas",
        p.sign(payload, p.key(nodes[0]["exports"]["dgx"])),
        db=nodes[3],
    )
    assert len(read(nodes, "#V#alice", "#V#lab")["results"]) == 1
    assert read(nodes, "#V#alice")["coverage"][0]["state"] == "stale"
    nodes[1]["imports"]["atlas"]["enabled"] = False
    assert read(nodes, "#V#alice")["results"] == []
    with pytest.raises(PermissionError):
        service.import_snapshot(nodes[1], "atlas", envelope, db=nodes[3])


def test_source_scope_change_and_hidden_predicates_withdraw(nodes):
    seed(nodes[2])
    exchange(nodes)
    nodes[2].concepts.update_one(
        {"concept_id": "#V#paper"},
        {"$set": {"relationships.#V#specific_to_user": ["#V#alice"]}},
    )
    exchange(nodes)
    assert read(nodes)["results"] == []
    nodes[0]["exports"]["dgx"]["public_relations"] = [
        {
            "subject": "#V#paper",
            "predicate": "#V#specific_to_user",
            "object": "#V#alice",
        }
    ]
    with pytest.raises(PermissionError):
        service.export_snapshot(nodes[0], "dgx", db=nodes[2])


def test_independent_origins_preserve_conflicting_assertions(nodes):
    seed(nodes[2])
    exchange(nodes)
    c = copy.deepcopy(nodes[0])
    c["node_id"] = "office"
    nodes[1]["imports"]["office"] = copy.deepcopy(nodes[1]["imports"]["atlas"])
    other = mongomock.MongoClient().office
    seed(other)
    other.scoped_knowledge_assertions.update_one(
        {"assertion_id": "private"},
        {"$set": {"object_text.text": "Alice has NOT read the paper"}},
    )
    envelope = service.export_snapshot(c, "dgx", db=other)
    service.import_snapshot(nodes[1], "office", envelope, db=nodes[3])
    results = read(nodes, "#V#alice", query="paper")["results"]
    assert {r["origin"] for r in results} == {"atlas", "office"}
    assert any("NOT" in str(r) for r in results)


def test_config_permission_and_private_to_public_mapping_rejected(nodes, tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(nodes[1]))
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        p.load_config(str(path))
    path.chmod(0o600)
    assert p.load_config(str(path))["node_id"] == "dgx"
    c = copy.deepcopy(nodes[1])
    c["imports"]["atlas"]["audience_map"]["user:#V#alice"] = "public"
    path.write_text(json.dumps(c))
    with pytest.raises(ValueError):
        p.load_config(str(path))


def test_bounds_fail_without_partial_publish(nodes):
    seed(nodes[2])
    envelope, _ = exchange(nodes)
    too_many = [
        copy.deepcopy(envelope["payload"]["records"][0])
        for _ in range(p.MAX_RECORDS + 1)
    ]
    with pytest.raises(ValueError):
        p.make_snapshot(nodes[2][service.EXPORTS], nodes[0], "dgx", too_many)
    assert nodes[2][service.EXPORTS].find_one()["generation"] == 1


def test_normal_mcp_gateway_actor_binding_and_forged_payload(nodes, monkeypatch):
    from src.backend.integrations.internal_mcp import (
        InternalMCPGateway,
        InternalMCPTransport,
        build_default_catalogue,
        catalogue,
    )

    seed(nodes[2])
    exchange(nodes)
    monkeypatch.setattr(service, "load_config", lambda: nodes[1])
    monkeypatch.setattr(service, "database", lambda: nodes[3])
    gateway = InternalMCPGateway(
        catalogue=build_default_catalogue(),
        transport=InternalMCPTransport(),
        enabled=True,
    )
    from src.backend.services.adaptive_turn_service import (
        ordinary_turn_capability_delegation,
    )

    assert "search_federated_knowledge" in ordinary_turn_capability_delegation(
        gateway, user_concept_id=None
    )
    assert "search_federated_knowledge" in ordinary_turn_capability_delegation(
        gateway, user_concept_id="#V#alice"
    )
    with override_current_actor("#V#alice", "#V#lab"):
        result = gateway.invoke("search_federated_knowledge", {}).payload
    assert len(result["results"]) == 3
    with override_current_actor(None, None):
        public = gateway.invoke("search_federated_knowledge", {}).payload
    assert len(public["results"]) == 1
    # Direct handler also refuses to turn a model-supplied identity into authority.
    from src.backend.integrations.internal_mcp.gateway import (
        bind_internal_mcp_actor_context_source,
    )

    with (
        override_current_actor(None, None),
        bind_internal_mcp_actor_context_source("tool_payload_fallback"),
    ):
        forged = catalogue._search_federated_knowledge(
            user_concept_id="#V#alice", organisation_concept_id="#V#lab"
        )
    assert len(forged.get("results", [])) <= 1


def test_vocabulary_is_bounded_canonical_text_not_private_context(nodes):
    seed(nodes[2])
    for text, context in [
        ("Public paper title", {}),
        ("PRIVATE CONTACT", {"user_id": "#V#alice"}),
    ]:
        tid = nodes[2].text_values.insert_one({"text": text, "lang": "en"}).inserted_id
        nodes[2].text_relations.insert_one(
            {
                "subject_concept_id": "#V#paper",
                "predicate": "hasName",
                "object_text_id": str(tid),
                "context": context,
            }
        )
    envelope, _ = exchange(nodes)
    assert "Public paper title" in p.encode(envelope).decode()
    assert "PRIVATE CONTACT" not in p.encode(envelope).decode()
    assert len(read(nodes, query="Public paper title")["results"]) == 1


def test_export_contention_recaptures_source_and_receiver_failure_is_atomic(
    nodes, monkeypatch
):
    seed(nodes[2])
    exchange(nodes)
    collection = nodes[2][service.EXPORTS]
    replace = collection.replace_one
    calls = 0

    def contend(query, document):
        nonlocal calls
        calls += 1
        if calls == 1:
            nodes[2].scoped_knowledge_assertions.update_one(
                {"assertion_id": "private"}, {"$set": {"status": "retracted"}}
            )
            from types import SimpleNamespace

            return SimpleNamespace(matched_count=0)
        return replace(query, document)

    monkeypatch.setattr(collection, "replace_one", contend)
    payload = service.export_snapshot(nodes[0], "dgx", db=nodes[2])["payload"]
    assert calls == 2
    assert (
        next(r for r in payload["records"] if r["id"] == "private")["claim"]["status"]
        == "retracted"
    )
    previous = nodes[3][service.REPLICAS].find_one()

    def unavailable(*args):
        raise RuntimeError("connection lost before atomic replacement")

    monkeypatch.setattr(nodes[3][service.REPLICAS], "replace_one", unavailable)
    with pytest.raises(RuntimeError):
        service.import_snapshot(
            nodes[1],
            "atlas",
            p.sign(payload, p.key(nodes[0]["exports"]["dgx"])),
            db=nodes[3],
        )
    assert nodes[3][service.REPLICAS].find_one() == previous


def test_scope_forgery_with_valid_peer_signature_denied(nodes):
    seed(nodes[2])
    envelope, _ = exchange(nodes)
    forged = copy.deepcopy(envelope["payload"])
    private = next(r for r in forged["records"] if r["id"] == "private")
    private["claim"]["scope"]["user_concept_id"] = "#V#victim"
    forged["digest"] = p.digest(forged["records"])
    forged["generation"] += 1
    with pytest.raises(PermissionError, match="mismatch"):
        service.import_snapshot(
            nodes[1],
            "atlas",
            p.sign(forged, p.key(nodes[0]["exports"]["dgx"])),
            db=nodes[3],
        )
