from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager

from src.backend.services import (
    ontology_authority_membership_coordination_service as coordination,
)
from src.backend.services.ontology_publication_authority_service import (
    OntologyMutationResourceBusy,
)


def _install_fake_resource_locks(monkeypatch):
    locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    @contextmanager
    def fake_resource_lock(resource_key: str, **_kwargs):
        lock = locks[resource_key]
        if not lock.acquire(blocking=False):
            raise OntologyMutationResourceBusy("ontology_mutation_resource_busy")
        try:
            yield
        finally:
            lock.release()

    monkeypatch.setattr(
        coordination,
        "ontology_mutation_resource_lock",
        fake_resource_lock,
    )
    return locks


def test_unrelated_actor_org_scope_recoveries_do_not_contend(monkeypatch) -> None:
    _install_fake_resource_locks(monkeypatch)
    all_entered = threading.Event()
    release = threading.Event()
    state_lock = threading.Lock()
    entered: set[str] = set()
    errors: list[BaseException] = []

    def recover(label: str, user_id: str, org_id: str) -> None:
        try:
            with coordination.organisation_membership_scope_barrier(
                user_id,
                org_id,
            ):
                with state_lock:
                    entered.add(label)
                    if len(entered) == 3:
                        all_entered.set()
                assert release.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            errors.append(exc)

    threads = [
        threading.Thread(
            target=recover,
            args=("first", "#V#actor_a", "#V#org_a"),
        ),
        threading.Thread(
            target=recover,
            args=("second", "#V#actor_a", "#V#org_b"),
        ),
        threading.Thread(
            target=recover,
            args=("third", "#V#actor_b", "#V#org_a"),
        ),
    ]
    for thread in threads:
        thread.start()

    assert all_entered.wait(timeout=2)
    release.set()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert entered == {"first", "second", "third"}
    assert all(not thread.is_alive() for thread in threads)


def test_same_actor_org_scope_waits_for_inflight_mutation(monkeypatch) -> None:
    _install_fake_resource_locks(monkeypatch)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []

    def first() -> None:
        try:
            with coordination.organisation_membership_scope_barrier(
                "#V#actor",
                "#V#org",
            ):
                first_entered.set()
                assert release_first.wait(timeout=2)
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            errors.append(exc)

    def second() -> None:
        try:
            assert first_entered.wait(timeout=2)
            with coordination.organisation_membership_scope_barrier(
                "actor",
                "org",
            ):
                second_entered.set()
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            errors.append(exc)

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=2)
    assert not second_entered.wait(timeout=0.1)
    release_first.set()
    assert second_entered.wait(timeout=2)
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert errors == []
    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
