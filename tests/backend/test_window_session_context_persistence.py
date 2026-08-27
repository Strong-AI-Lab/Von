from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta

import mongomock
import pytest
from flask import Flask

from src.backend.services import organisation_membership_service
from src.backend.services import window_session_context_service as window_context
from src.backend.services.window_session_binding_store_service import (
    MongoWindowSessionBindingRepository,
    WindowSessionBindingStoreUnavailable,
)


@pytest.fixture()
def durable_store(monkeypatch: pytest.MonkeyPatch):
    from src.backend.services import (
        ontology_authority_membership_coordination_service as coordination,
    )

    monkeypatch.setattr(
        coordination,
        "ontology_authority_membership_mutation_barrier",
        nullcontext,
    )
    monkeypatch.setattr(
        coordination,
        "organisation_membership_scope_barrier",
        lambda *_args, **_kwargs: nullcontext(),
    )
    collection = mongomock.MongoClient()["window_binding_test"]["bindings"]
    repository = MongoWindowSessionBindingRepository(
        ttl_seconds=3600,
        collection_getter=lambda: collection,
    )
    store = window_context.WindowSessionStore(binding_repository=repository)
    monkeypatch.setattr(window_context, "_window_session_store", store)
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda user_id, org_id: {
            "user_concept_id": user_id,
            "organisation_concept_id": org_id,
            "role": "admin" if org_id == "#V#lab" else "member",
        },
    )
    return collection, repository


def _restart_with_repository(
    monkeypatch: pytest.MonkeyPatch,
    repository: MongoWindowSessionBindingRepository,
) -> window_context.WindowSessionStore:
    restarted = window_context.WindowSessionStore(binding_repository=repository)
    monkeypatch.setattr(window_context, "_window_session_store", restarted)
    return restarted


def test_restart_recovers_org_and_revalidates_current_role(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, repository = durable_store
    window_context.set_window_organisation(
        "ws-restart-org",
        "lab",
        "member",
        "#V#actor@lab",
        "#V#actor",
    )

    document = collection.find_one({})
    assert document is not None
    assert document["_id"] != "ws-restart-org"
    assert document["organisation_concept_id"] == "#V#lab"
    assert "role_in_org" not in document
    assert "namespace" not in document

    restarted = _restart_with_repository(monkeypatch, repository)
    effective = window_context.get_effective_context(
        "ws-restart-org",
        {"organisation_concept_id": "wrong_flask_org"},
        "#V#actor",
        require_known_window=True,
    )

    assert effective == {
        "user_id": "#V#actor",
        "organisation_id": "#V#lab",
        "role": "admin",
        "namespace": "#V#actor@lab",
        "chat_session_id": None,
        "source": "window_session",
    }
    assert restarted.count() == 1


def test_restart_preserves_two_org_tabs_and_authoritative_personal_tab(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    _, repository = durable_store
    window_context.set_window_organisation(
        "ws-org-a", "org_a", "member", "#V#actor@org_a", "#V#actor"
    )
    window_context.set_window_organisation(
        "ws-org-b", "org_b", "member", "#V#actor@org_b", "#V#actor"
    )
    window_context.clear_window_organisation("ws-personal", "#V#actor", "#V#actor")
    _restart_with_repository(monkeypatch, repository)

    contexts = {
        window_id: window_context.get_effective_context(
            window_id,
            {
                "organisation_concept_id": "wrong_flask_org",
                "namespace": "#V#actor@wrong_flask_org",
            },
            "#V#actor",
            require_known_window=True,
        )
        for window_id in ("ws-org-a", "ws-org-b", "ws-personal")
    }

    assert contexts["ws-org-a"]["namespace"] == "#V#actor@org_a"
    assert contexts["ws-org-b"]["namespace"] == "#V#actor@org_b"
    assert contexts["ws-personal"]["organisation_id"] is None
    assert contexts["ws-personal"]["namespace"] == "#V#actor"
    assert contexts["ws-personal"]["source"] == "window_session"


def test_warm_worker_observes_org_switch_from_shared_binding(durable_store) -> None:
    _collection, repository = durable_store
    first_worker = window_context.WindowSessionStore(binding_repository=repository)
    second_worker = window_context.WindowSessionStore(binding_repository=repository)

    initial = window_context.WindowSessionContext(
        window_session_id="ws-cross-worker",
        user_id="#V#actor",
        organisation_concept_id="#V#org_a",
        role_in_org="member",
        namespace="#V#actor@org_a",
    )
    first_worker.persist_authoritative_binding(
        initial,
        scope_kind=window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
    )
    first_worker.set(initial)
    cached = second_worker.get_owned("ws-cross-worker", "#V#actor")
    assert cached is not None
    cached.role_in_org = "member"
    cached.namespace = "#V#actor@org_a"
    cached.durable_recovered = False
    second_worker.set(cached)

    switched = window_context.WindowSessionContext(
        window_session_id="ws-cross-worker",
        user_id="#V#actor",
        organisation_concept_id="#V#org_b",
        role_in_org="admin",
        namespace="#V#actor@org_b",
    )
    first_worker.persist_authoritative_binding(
        switched,
        scope_kind=window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
    )
    first_worker.set(switched)

    observed = second_worker.get_owned("ws-cross-worker", "#V#actor")
    assert observed is not None
    assert observed.organisation_concept_id == "#V#org_b"
    assert observed.namespace is None
    assert observed.role_in_org is None
    assert observed.durable_recovered is True


def test_warm_worker_rejects_shared_binding_invalidation(durable_store) -> None:
    _collection, repository = durable_store
    first_worker = window_context.WindowSessionStore(binding_repository=repository)
    second_worker = window_context.WindowSessionStore(binding_repository=repository)
    selected = window_context.WindowSessionContext(
        window_session_id="ws-cross-worker-revoked",
        user_id="#V#actor",
        organisation_concept_id="#V#org",
        role_in_org="member",
        namespace="#V#actor@org",
    )
    first_worker.persist_authoritative_binding(
        selected,
        scope_kind=window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
    )
    first_worker.set(selected)
    assert second_worker.get_owned("ws-cross-worker-revoked", "#V#actor") is not None

    assert first_worker.delete_all_owned("#V#actor") == 1

    assert second_worker.get_owned("ws-cross-worker-revoked", "#V#actor") is None
    assert second_worker.count() == 0


def test_membership_invalidation_preserves_other_org_and_personal_tabs(
    durable_store,
) -> None:
    collection, repository = durable_store
    store = window_context.WindowSessionStore(binding_repository=repository)
    for window_id, org_id, scope_kind in (
        (
            "ws-org-a-invalidate",
            "#V#org_a",
            window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
        ),
        (
            "ws-org-b-preserve",
            "#V#org_b",
            window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
        ),
        (
            "ws-personal-preserve",
            None,
            window_context.WINDOW_SESSION_SCOPE_PERSONAL,
        ),
    ):
        selected = window_context.WindowSessionContext(
            window_session_id=window_id,
            user_id="#V#actor",
            organisation_concept_id=org_id,
        )
        store.persist_authoritative_binding(selected, scope_kind=scope_kind)
        store.set(selected)

    assert store.delete_owned_for_organisation("#V#actor", "#V#org_a") == 1

    assert collection.count_documents({}) == 2
    assert store.get_owned("ws-org-a-invalidate", "#V#actor") is None
    assert store.get_owned("ws-org-b-preserve", "#V#actor") is not None
    assert store.get_owned("ws-personal-preserve", "#V#actor") is not None


def test_recovery_cannot_rebind_between_membership_invalidation_and_commit(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, repository = durable_store
    from src.backend.services import (
        ontology_authority_membership_coordination_service as coordination,
    )

    store = window_context.WindowSessionStore(binding_repository=repository)
    monkeypatch.setattr(window_context, "_window_session_store", store)
    selected = window_context.WindowSessionContext(
        window_session_id="ws-revocation-race",
        user_id="#V#actor",
        organisation_concept_id="#V#org",
    )
    store.persist_authoritative_binding(
        selected,
        scope_kind=window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
    )
    stale_recovery = window_context.WindowSessionContext(
        window_session_id="ws-revocation-race",
        user_id="#V#actor",
        organisation_concept_id="#V#org",
        durable_scope_kind=window_context.WINDOW_SESSION_SCOPE_ORGANISATION,
        durable_recovered=True,
        durably_persisted=True,
    )

    membership_active = [True]
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda user_id, org_id: (
            {
                "user_concept_id": user_id,
                "organisation_concept_id": org_id,
                "role": "admin",
            }
            if membership_active[0]
            else None
        ),
    )
    shared_lock = threading.Lock()
    invalidated = threading.Event()
    recovery_waiting = threading.Event()
    permit_commit = threading.Event()

    @contextmanager
    def shared_barrier(*_args, **_kwargs):
        if threading.current_thread().name == "binding-recovery":
            recovery_waiting.set()
        with shared_lock:
            yield

    monkeypatch.setattr(
        coordination,
        "organisation_membership_scope_barrier",
        shared_barrier,
    )
    recovery_errors: list[BaseException] = []

    def mutate_membership() -> None:
        with shared_barrier():
            store.delete_owned_for_organisation("#V#actor", "#V#org")
            invalidated.set()
            assert permit_commit.wait(timeout=2)
            membership_active[0] = False

    def recover_binding() -> None:
        try:
            window_context._recover_authoritative_scope(
                stale_recovery,
                user_id="#V#actor",
            )
        except Exception as exc:  # noqa: BLE001 - asserted in parent thread
            recovery_errors.append(exc)

    mutation_thread = threading.Thread(target=mutate_membership)
    recovery_thread = threading.Thread(
        target=recover_binding,
        name="binding-recovery",
    )
    mutation_thread.start()
    assert invalidated.wait(timeout=2)
    recovery_thread.start()
    assert recovery_waiting.wait(timeout=2)
    assert recovery_thread.is_alive()
    permit_commit.set()
    mutation_thread.join(timeout=2)
    recovery_thread.join(timeout=2)

    assert not mutation_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert len(recovery_errors) == 1
    assert isinstance(
        recovery_errors[0],
        window_context.WindowSessionContextUnavailable,
    )
    assert collection.count_documents({}) == 0


def test_other_actor_and_unknown_selector_remain_indistinguishable_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    _, repository = durable_store
    window_context.set_window_organisation(
        "ws-owned", "secret_org", "owner", "#V#owner@secret_org", "#V#owner"
    )
    _restart_with_repository(monkeypatch, repository)

    for selector in ("ws-owned", "ws-unknown"):
        with pytest.raises(
            window_context.WindowSessionContextUnavailable,
            match="window_session_context_unavailable",
        ):
            window_context.get_effective_context(
                selector,
                {
                    "organisation_concept_id": "other_org",
                    "namespace": "#V#other@other_org",
                },
                "#V#other",
                require_known_window=True,
            )


def test_restart_recovery_fails_closed_and_deletes_revoked_membership(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, repository = durable_store
    window_context.set_window_organisation(
        "ws-revoked", "org", "member", "#V#actor@org", "#V#actor"
    )
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args, **_kwargs: None,
    )
    _restart_with_repository(monkeypatch, repository)

    with pytest.raises(window_context.WindowSessionContextUnavailable):
        window_context.get_effective_context(
            "ws-revoked",
            {"namespace": "#V#actor@wrong_org"},
            "#V#actor",
            require_known_window=True,
        )
    assert collection.count_documents({}) == 0


def test_bound_selector_never_falls_back_to_flask_after_revocation(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    _collection, repository = durable_store
    window_context.set_window_organisation(
        "ws-revoked-compat", "org", "member", "#V#actor@org", "#V#actor"
    )
    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        lambda *_args, **_kwargs: None,
    )
    _restart_with_repository(monkeypatch, repository)

    with pytest.raises(window_context.WindowSessionContextUnavailable):
        window_context.get_effective_context(
            "ws-revoked-compat",
            {
                "organisation_concept_id": "wrong_flask_org",
                "namespace": "#V#actor@wrong_flask_org",
            },
            "#V#actor",
            require_known_window=False,
        )


def test_membership_read_failure_is_recovery_unavailable_not_flask_fallback(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    _collection, repository = durable_store
    window_context.set_window_organisation(
        "ws-membership-down", "org", "member", "#V#actor@org", "#V#actor"
    )

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("membership store unavailable")

    monkeypatch.setattr(
        organisation_membership_service,
        "resolve_user_organisation_membership",
        unavailable,
    )
    _restart_with_repository(monkeypatch, repository)

    with pytest.raises(window_context.WindowSessionContextRecoveryUnavailable):
        window_context.get_effective_context(
            "ws-membership-down",
            {
                "organisation_concept_id": "wrong_flask_org",
                "namespace": "#V#actor@wrong_flask_org",
            },
            "#V#actor",
            require_known_window=False,
        )


def test_expired_binding_is_rejected_before_ttl_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, repository = durable_store
    window_context.set_window_organisation(
        "ws-expired", "org", "member", "#V#actor@org", "#V#actor"
    )
    collection.update_one(
        {},
        {"$set": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}},
    )
    _restart_with_repository(monkeypatch, repository)

    with pytest.raises(window_context.WindowSessionContextUnavailable):
        window_context.get_effective_context(
            "ws-expired",
            {"namespace": "#V#actor@wrong_org"},
            "#V#actor",
            require_known_window=True,
        )


def test_durable_store_outage_cannot_fall_back_to_flask_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableRepository:
        def load_owned(self, *_args, **_kwargs):
            raise WindowSessionBindingStoreUnavailable("unavailable")

    store = window_context.WindowSessionStore(
        binding_repository=UnavailableRepository()  # type: ignore[arg-type]
    )
    monkeypatch.setattr(window_context, "_window_session_store", store)

    with pytest.raises(window_context.WindowSessionContextRecoveryUnavailable):
        window_context.get_effective_context(
            "ws-store-down",
            {
                "organisation_concept_id": "wrong_flask_org",
                "namespace": "#V#actor@wrong_flask_org",
            },
            "#V#actor",
            require_known_window=True,
        )


def test_failed_durable_selection_does_not_replace_working_l1_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnavailableRepository:
        def save(self, **_kwargs):
            raise WindowSessionBindingStoreUnavailable("unavailable")

    store = window_context.WindowSessionStore(
        binding_repository=UnavailableRepository()  # type: ignore[arg-type]
    )
    store.set(
        window_context.WindowSessionContext(
            window_session_id="ws-preserve",
            user_id="#V#actor",
            organisation_concept_id="old_org",
            role_in_org="member",
            namespace="#V#actor@old_org",
        )
    )
    monkeypatch.setattr(window_context, "_window_session_store", store)

    with pytest.raises(WindowSessionBindingStoreUnavailable):
        window_context.set_window_organisation(
            "ws-preserve",
            "new_org",
            "member",
            "#V#actor@new_org",
            "#V#actor",
        )
    preserved = store.get("ws-preserve")
    assert preserved is not None
    assert preserved.organisation_concept_id == "old_org"


def test_actor_owned_delete_clears_durable_binding_after_l1_restart(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, repository = durable_store
    window_context.set_window_organisation(
        "ws-delete", "org", "member", "#V#actor@org", "#V#actor"
    )
    _restart_with_repository(monkeypatch, repository)

    assert window_context.delete_window_context_if_owned("ws-delete", "#V#actor")
    assert collection.count_documents({}) == 0


def test_old_tab_recovers_from_exact_owned_conversation_metadata(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, repository = durable_store
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summary",
        lambda *_args, **_kwargs: {
            "session_id": "conversation-1",
            "namespace": "#V#actor@lab",
            "organisation_concept_id": "#V#lab",
        },
    )
    app = Flask(__name__)
    with app.app_context():
        assert von_routes._recover_window_context_from_owned_conversation(
            window_session_id="ws-pre-upgrade",
            user_concept_id="#V#actor",
            conversation_session_id="conversation-1",
        )

    assert collection.count_documents({}) == 1
    _restart_with_repository(monkeypatch, repository)
    effective = window_context.get_effective_context(
        "ws-pre-upgrade",
        {"namespace": "#V#actor@wrong_org"},
        "#V#actor",
        require_known_window=True,
    )
    assert effective["organisation_id"] == "#V#lab"
    assert effective["namespace"] == "#V#actor@lab"


def test_owned_conversation_without_org_or_personal_namespace_is_not_scope_proof(
    monkeypatch: pytest.MonkeyPatch,
    durable_store,
) -> None:
    collection, _repository = durable_store
    from src.backend.server.routes import von_routes

    monkeypatch.setattr(
        von_routes.chat_history_service,
        "get_chat_history_session_summary",
        lambda *_args, **_kwargs: {
            "session_id": "ambiguous-legacy-conversation",
            "namespace": None,
            "organisation_concept_id": None,
        },
    )
    app = Flask(__name__)
    with app.app_context():
        assert not von_routes._recover_window_context_from_owned_conversation(
            window_session_id="ws-ambiguous",
            user_concept_id="#V#actor",
            conversation_session_id="ambiguous-legacy-conversation",
        )
    assert collection.count_documents({}) == 0
