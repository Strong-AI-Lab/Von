from __future__ import annotations

from contextlib import ExitStack, contextmanager
from threading import Event, Thread, Timer
from types import SimpleNamespace

import mongomock
import pytest


@pytest.fixture
def governed_command(monkeypatch):
    """Real command, authority, lease and receipt path; isolated domain storage."""
    from src.backend.services import ontology_mutation_command_service as command
    from src.backend.services import ontology_publication_authority_service as authority

    database = mongomock.MongoClient()["test_contention"]
    receipts = database["receipts"]
    receipts.create_index("receipt_id", unique=True)
    monkeypatch.setattr(
        authority, "get_ontology_mutation_receipts_collection", lambda: receipts
    )
    monkeypatch.setattr(authority, "_gateway_actor_trust_source", lambda: None)
    role = authority.AuthorityRoleEvidence(
        role=authority.GLOBAL_ONTOLOGY_ADMINISTRATOR_ROLE,
        actor_concept_id="#V#admin",
        organisation_concept_id=None,
        relation_id="role:admin",
        revision="1",
    )
    state = {"relationship_present": False, "scope_fingerprint": "original"}
    roles = [role]
    monkeypatch.setattr(
        authority, "resolve_live_semantic_roles", lambda _: tuple(roles)
    )
    intent = authority.OntologyMutationIntent(
        operation="relationship.add",
        publication_context=authority.PublicationContext.global_context(),
        target_concept_ids=("#V#paper", "#V#author"),
        tool_name="add_relationship",
        predicate="#V#has_author",
        delta={
            "target": "#V#author",
            "affected_scope_fingerprints": {"#V#paper": "original"},
        },
        idempotency_key="contention-effect",
    )
    monkeypatch.setattr(command, "build_ontology_mutation_intent", lambda **_: intent)
    monkeypatch.setattr(command, "scope_read_back", lambda _: dict(state))
    monkeypatch.setattr(
        command, "canonical_read_back_for_method", lambda **_: dict(state)
    )
    # Recovery suggestion discovery is a separate domain read, not this test's
    # authority or lease boundary.
    monkeypatch.setattr(
        command, "_scoped_assertion_recovery_is_executable", lambda **_: False
    )
    writes = []

    def mutate():
        writes.append("effect")
        state["relationship_present"] = True
        return {"success": True, "changed": True, "added": True}

    def run(effect=mutate):
        with authority.override_current_actor("#V#admin", None):
            return command.execute_governed_ontology_method(
                method_name="add_relationship",
                arguments={
                    "source_id": "#V#paper",
                    "predicate": "#V#has_author",
                    "target": "#V#author",
                },
                mutate=effect,
            )

    with authority.override_current_actor("#V#admin", None):
        resource_key = authority.ontology_authority_resource_keys(intent)[0]
    return command, authority, receipts, state, roles, writes, run, resource_key


def test_short_contention_completes_once_and_replays_same_receipt(governed_command):
    _, authority, receipts, _, _, writes, run, key = governed_command
    held = ExitStack()
    held.enter_context(authority.ontology_mutation_resource_lock(key))
    release = Timer(0.1, held.close)
    release.start()
    try:
        first = run()
    finally:
        release.join(timeout=2)
        held.close()
    assert first["success"] is True, first
    assert first["canonical_read_back"]["relationship_present"] is True
    replay = run()
    assert replay["idempotent_replay"] is True
    assert writes == ["effect"]
    assert receipts.count_documents({}) == 1


@pytest.fixture
def clock(monkeypatch, governed_command):
    command = governed_command[0]
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    fake = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
    monkeypatch.setattr(command, "time", fake)
    return fake, sleeps


@pytest.mark.parametrize("change", ["scope", "authority"])
def test_scope_and_authority_are_rechecked_after_waiting(
    governed_command, clock, change
):
    _, authority, _, state, roles, writes, run, key = governed_command
    fake, _ = clock
    with ExitStack() as held:
        held.enter_context(authority.ontology_mutation_resource_lock(key))

        def release_and_change(_seconds):
            if change == "scope":
                state["scope_fingerprint"] = "changed"
            else:
                roles.clear()
            held.close()

        fake.sleep = release_and_change
        result = run()
    assert result["success"] is False
    assert result["changed"] is False
    assert result["effect_status"] == "not_started"
    if change == "scope":
        assert result["error_code"] == "ontology_mutation_scope_precondition_failed"
    else:
        assert "authority" in result["error_code"]
    assert writes == []


def test_partial_lock_set_is_released_before_waiting(governed_command, clock):
    command, authority = governed_command[:2]
    fake, _ = clock
    with ExitStack() as held:
        held.enter_context(authority.ontology_mutation_resource_lock("z"))

        def release(_seconds):
            # Another operation can acquire the earlier key during the wait.
            with authority.ontology_mutation_resource_lock("a"):
                pass
            held.close()

        fake.sleep = release
        with (
            command._acquire_mutation_resource_locks(("z", "a")),
            pytest.raises(authority.OntologyMutationResourceBusy),
            authority.ontology_mutation_resource_lock("a"),
        ):
            pass
    with authority.ontology_mutation_resource_lock("a"):
        pass


def test_persistent_contention_stops_before_effect(
    governed_command, clock, monkeypatch
):
    command, authority, _, _, _, writes, run, key = governed_command
    fake, sleeps = clock
    monkeypatch.setattr(command, "_MUTATION_CONTENTION_WAIT_SECONDS", 2.0)
    with authority.ontology_mutation_resource_lock(key):
        result = run()
    assert fake.monotonic() == 2.0
    assert sleeps == [0.25, 0.5, 1.0, 0.25]
    assert result["error_code"] == "ontology_mutation_resource_busy"
    assert result["effect_status"] == "not_started"
    assert result["changed"] is False
    assert writes == []


def test_unrelated_acquisition_error_is_not_retried(
    governed_command, clock, monkeypatch
):
    command, authority = governed_command[:2]
    _, sleeps = clock
    real_lock = authority.ontology_mutation_resource_lock

    @contextmanager
    def lock(key):
        if key == "z":
            raise ConnectionError("store unavailable")
        with real_lock(key):
            yield

    monkeypatch.setattr(command, "ontology_mutation_resource_lock", lock)
    with (
        pytest.raises(ConnectionError),
        command._acquire_mutation_resource_locks(("a", "z")),
    ):
        pytest.fail("must not enter mutation body")
    with real_lock("a"):
        pass
    assert sleeps == []


def test_body_busy_exception_is_not_retried(governed_command, clock):
    command, authority = governed_command[:2]
    _, sleeps = clock
    calls = []
    with (
        pytest.raises(authority.OntologyMutationResourceBusy),
        command._acquire_mutation_resource_locks(("a",)),
    ):
        calls.append("effect may have started")
        raise authority.OntologyMutationResourceBusy("body failure")
    assert len(calls) == 1
    assert sleeps == []
    with authority.ontology_mutation_resource_lock("a"):
        pass


def test_same_effect_during_wait_does_not_launch_another_mutation(
    governed_command, monkeypatch
):
    command, authority, receipts, _, _, writes, run, key = governed_command
    waiting = Event()
    resume = Event()
    results = []

    def sleep(_seconds):
        waiting.set()
        assert resume.wait(timeout=3)

    monkeypatch.setattr(
        command, "time", SimpleNamespace(monotonic=command.time.monotonic, sleep=sleep)
    )
    worker = Thread(target=lambda: results.append(run()))
    with ExitStack() as held:
        held.enter_context(authority.ontology_mutation_resource_lock(key))
        worker.start()
        try:
            assert waiting.wait(timeout=3)
            second = run()
            assert second["effect_status"] == "in_progress"
            assert writes == []
        finally:
            held.close()
            resume.set()
            worker.join(timeout=3)
    assert not worker.is_alive()
    assert results[0]["success"] is True
    assert writes == ["effect"]
    assert receipts.count_documents({}) == 1
