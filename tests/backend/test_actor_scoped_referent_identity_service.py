from __future__ import annotations

import pytest

from src.backend.services.actor_scoped_referent_identity_service import (
    actor_scoped_referent_concept_id,
)


def test_actor_scoped_referent_identity_is_stable_and_actor_bound() -> None:
    first = actor_scoped_referent_concept_id(
        requested_concept_id="#V#Burkhard-Wuensche",
        actor_concept_id="#V#Member-One",
    )
    repeated = actor_scoped_referent_concept_id(
        requested_concept_id="#v#burkhard_wuensche",
        actor_concept_id="#v#member_one",
    )
    other_actor = actor_scoped_referent_concept_id(
        requested_concept_id="#V#burkhard_wuensche",
        actor_concept_id="#V#member_two",
    )
    other_requested_id = actor_scoped_referent_concept_id(
        requested_concept_id="#V#another_person",
        actor_concept_id="#V#member_one",
    )

    assert first == repeated
    assert first.startswith("#V#scoped_referent_burkhard_wuensche_")
    assert first != other_actor
    assert first != other_requested_id
    assert "member_one" not in first


@pytest.mark.parametrize(
    ("requested_concept_id", "actor_concept_id"),
    [("", "#V#member"), ("#V#person", ""), ("###", "#V#member")],
)
def test_actor_scoped_referent_identity_requires_canonical_inputs(
    requested_concept_id: str,
    actor_concept_id: str,
) -> None:
    with pytest.raises(ValueError):
        actor_scoped_referent_concept_id(
            requested_concept_id=requested_concept_id,
            actor_concept_id=actor_concept_id,
        )
