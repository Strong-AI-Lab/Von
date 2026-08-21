"""Deterministic identities for actor-private concept referent recovery.

This module owns only the mechanical identity rule used after an exact
concept-ID collision.  A derived identifier denotes a new actor-private
referent.  It does not alias, merge, or assert equivalence with the concept
occupying the requested identifier.  The requested identifier is only a seed;
this module does not inspect or confirm whether it is occupied.
"""

from __future__ import annotations

import hashlib
import json

from ..utils.concept_id_utils import canonicalise_vontology_concept_id

ACTOR_SCOPED_REFERENT_COLLISION_MODE = "actor_scoped_referent"
ACTOR_SCOPED_REFERENT_RECOVERY_ACTION = "create_actor_scoped_referent_after_id_conflict"
ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT = "ontology_actor_scoped_instance_referent.v1"
ACTOR_SCOPED_REFERENT_SCOPE_MODE = "user_only_default"


def actor_scoped_referent_concept_id(
    *,
    requested_concept_id: str,
    actor_concept_id: str,
) -> str:
    """Return a stable actor-bound ID without exposing the actor identity.

    The fixed scope and instance-only contract are included in the digest so a
    future, semantically different recovery mode cannot accidentally share the
    same identity space.
    """

    requested_id = canonicalise_vontology_concept_id(requested_concept_id)
    actor_id = canonicalise_vontology_concept_id(actor_concept_id)
    if not requested_id or not actor_id:
        raise ValueError(
            "requested_concept_id and trusted actor_concept_id are required"
        )

    material = json.dumps(
        {
            "actor_concept_id": actor_id,
            "kind": "instance",
            "requested_concept_id": requested_id,
            "schema_version": ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT,
            "scope_mode": ACTOR_SCOPED_REFERENT_SCOPE_MODE,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:24]
    requested_slug = requested_id.removeprefix("#V#")[:48].strip("_")
    if not requested_slug:
        requested_slug = "concept"
    return f"#V#scoped_referent_{requested_slug}_{digest}"


__all__ = [
    "ACTOR_SCOPED_REFERENT_COLLISION_MODE",
    "ACTOR_SCOPED_REFERENT_RECOVERY_ACTION",
    "ACTOR_SCOPED_REFERENT_RECOVERY_CONTRACT",
    "ACTOR_SCOPED_REFERENT_SCOPE_MODE",
    "actor_scoped_referent_concept_id",
]
