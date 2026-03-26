from __future__ import annotations

from flask import Flask


def test_validate_person_concept_uses_raw_exact_lookup(monkeypatch) -> None:
    import src.backend.security.access_control as access_control
    import src.backend.services.concept_service as concept_service

    monkeypatch.setattr(
        concept_service,
        "get_concept_by_concept_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity validation should not use recursive concept resolution")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        concept_service,
        "_find_concept_by_exact_concept_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity validation should not use finalised concept lookup")
        ),
    )
    monkeypatch.setattr(
        concept_service,
        "_find_raw_concept_by_exact_concept_id",
        lambda concept_id: {
            "concept_id": concept_id,
            "relationships": {"is_an_instance_of": ["#V#person"]},
        },
    )

    assert access_control._validate_person_concept("#V#michael_witbrock") == "#V#michael_witbrock"


def test_get_effective_user_concept_id_caches_validated_header(monkeypatch) -> None:
    import src.backend.security.access_control as access_control

    app = Flask(__name__)
    calls: list[str] = []

    def _fake_validate(concept_id: str | None) -> str | None:
        if concept_id is None:
            return None
        calls.append(concept_id)
        return concept_id

    monkeypatch.setattr(access_control, "_validate_person_concept", _fake_validate)

    with app.test_request_context(
        "/von/generate",
        headers={"X-User-Concept-ID": "#V#michael_witbrock"},
    ):
        cache_token = access_control._HEADER_CACHE.set(None)
        try:
            assert (
                access_control.get_effective_user_concept_id()
                == "#V#michael_witbrock"
            )
            assert (
                access_control.get_effective_user_concept_id()
                == "#V#michael_witbrock"
            )
        finally:
            access_control._HEADER_CACHE.reset(cache_token)

    assert calls == ["#V#michael_witbrock"]
