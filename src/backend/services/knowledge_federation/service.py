"""Actor-bound read projection and operator-only exchange orchestration."""

from __future__ import annotations

from datetime import UTC, datetime

from .protocol import apply_snapshot, encode, load_config, make_snapshot
from .source import stable_records

EXPORTS = "knowledge_federation_exports"
REPLICAS = "knowledge_federation_replicas"


def database():
    from ...db.mongo_client import get_db

    db = get_db()
    if db is None:
        raise RuntimeError("federation database unavailable")
    return db


def export_snapshot(config: dict, peer: str, *, db=None) -> dict:
    db = database() if db is None else db
    return make_snapshot(
        db[EXPORTS], config, peer, lambda: stable_records(db, config, peer)
    )


def import_snapshot(config: dict, origin: str, envelope: dict, *, db=None) -> dict:
    db = database() if db is None else db
    return apply_snapshot(db[REPLICAS], config, origin, envelope)


def search(
    *,
    query: str = "",
    limit: int = 50,
    source_concept_id: str | None = None,
    config: dict | None = None,
    db=None,
) -> dict:
    """Private audience comes exclusively from the already-bound trusted actor.

    Public records need no actor. No argument can select a different reader.
    Origin-local identifiers are never implicitly resolved as local identities.
    """
    from ...security.access_control import (
        get_effective_organisation_concept_id,
        get_effective_user_concept_id,
    )

    config = load_config() if config is None else config
    if not 1 <= limit <= 200 or len(query) > 2000:
        raise ValueError("federation query/limit outside bound")
    audiences = {"public"}
    user, org = get_effective_user_concept_id(), get_effective_organisation_concept_id()
    if user:
        audiences.add(f"user:{user}")
        if org:
            audiences.add(f"org:{org}")
    rows, coverage = [], []
    if not config.get("imports"):
        return {
            "results": [],
            "coverage": [],
            "complete_for": "configured_received_slices_only",
            "global_completeness": False,
            "enabled": False,
        }
    db = database() if db is None else db
    terms = query.casefold().split()
    for origin, settings in config["imports"].items():
        if settings.get("enabled") is not True:
            continue
        allowed = {
            a
            for a in settings["audiences"]
            if settings.get("audience_map", {}).get(a) in audiences
        }
        if not allowed:
            continue
        snapshot = db[REPLICAS].find_one({"_id": origin})
        if not snapshot:
            # Do not reveal private origins to actors without their subscription.
            coverage.append({"origin": origin, "state": "not_received"})
            continue
        age = max(
            0,
            (
                datetime.now(UTC) - datetime.fromisoformat(snapshot["checked_at"])
            ).total_seconds(),
        )
        stale = age > settings.get("max_age_seconds", 3600)
        visible = [r for r in snapshot["records"] if r["audience"] in allowed]
        # Expired private replicas are unavailable until an authenticated fresh
        # observation. Public background remains readable but labelled stale.
        if stale:
            visible = [r for r in visible if r["audience"] == "public"]
        coverage.append(
            {
                "origin": origin,
                "state": "stale" if stale else "current",
                "source_checked_at": snapshot["checked_at"],
                "received_at": snapshot["received_at"],
                "coverage": "explicit_selected_slice",
                "private_reads_expire": True,
            }
        )
        for record in visible:
            if record["claim"]["status"] != "asserted":
                continue
            if source_concept_id and not any(
                v.get("source_concept_id") == source_concept_id
                for v in record["vocabulary"]
            ):
                continue
            text = (
                encode({"claim": record["claim"], "vocabulary": record["vocabulary"]})
                .decode()
                .casefold()
            )
            if not all(term in text for term in terms):
                continue
            rows.append(
                {
                    **record,
                    "origin": origin,
                    "federated_id": f"{origin}/{record['id']}",
                    "replica_observed_at": snapshot["checked_at"],
                    "stale": stale,
                    "local_audience": settings["audience_map"][record["audience"]],
                    "canonical_publication": False,
                    "execution_authority": False,
                }
            )
    rows.sort(key=lambda row: row["federated_id"])
    return {
        "results": rows[:limit],
        "returned": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "coverage": coverage,
        "complete_for": "configured_received_slices_only",
        "global_completeness": False,
        "enabled": True,
        "identity_semantics": "source IDs are origin-qualified; no automatic local identity merge",
    }
