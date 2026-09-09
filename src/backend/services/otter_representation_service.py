"""Canonical private meeting representation from preserved Otter evidence.

Exact caller-reviewed participant bindings connect existing people. Unresolved
source labels remain meeting-local participant concepts, never global identity
merges. Summaries and action items do not execute instructions.
"""

from __future__ import annotations

import hashlib
import json
import re


def speaker_labels(text):
    return list(
        dict.fromkeys(
            re.findall(
                r"^\[(?:\d+:)?\d{1,2}:\d{2}\]\s+([^:\n]{1,200}):", text, re.MULTILINE
            )
        )
    )


def stable_id(kind, *parts):
    material = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return "#V#otter_" + kind + "_" + hashlib.sha256(material.encode()).hexdigest()


def represent_meeting(owner, meeting, archived, bindings):
    from ..security.access_control import override_current_actor
    from .concept_service import (
        ConceptNotFoundError,
        create_concept,
        get_concept_by_concept_id_exact,
    )
    from .scoped_assertion_service import (
        list_visible_scoped_assertions_page,
        retract_scoped_assertion,
        upsert_scoped_assertion,
    )
    from .workflow_event_integration_service import suppress_event_workflow_launches

    cid = meeting["id"]
    meeting_concept = stable_id("meeting", owner, cid)
    labels = speaker_labels(meeting["text"])
    if set(bindings) - set(labels):
        raise ValueError("participant_binding_label_not_in_source")
    source = {
        "source": "otter_live_collection",
        "source_url": meeting["url"],
        "otter_id": cid,
        "source_sha256": archived["source_sha256"],
    }
    with (
        override_current_actor(owner, None),
        suppress_event_workflow_launches("otter_source_collection"),
    ):

        def lookup(concept_id):
            try:
                return get_concept_by_concept_id_exact(concept_id)
            except ConceptNotFoundError:
                return None

        def ensure(concept_id, name, type_id, note):
            existing = lookup(concept_id)
            if existing is None:
                create_concept(
                    name=name,
                    concept_id=concept_id,
                    parent_concept_ids=[type_id],
                    create_as_instance=True,
                    created_by_concept_id=owner,
                    notes=note,
                    maintain_relationship_inverses=False,
                )
                existing = lookup(concept_id)
            if existing is None:
                raise RuntimeError("otter_concept_readback_failed")
            return existing

        def assert_fact(subject, predicate, *, target=None, text=None, evidence=None):
            result = upsert_scoped_assertion(
                subject_concept_id=subject,
                predicate=predicate,
                target_concept_id=target,
                target_text=text,
                acting_user_concept_id=owner,
                scope_mode="user",
                evidence=evidence or source,
            )
            if not result.get("success"):
                raise RuntimeError("otter_assertion_readback_failed")
            return result

        ensure(
            meeting_concept,
            meeting.get("title") or cid,
            "#V#meeting",
            "Otter source meeting " + meeting["url"],
        )
        prior_page = list_visible_scoped_assertions_page(
            subject_concept_ids=[meeting_concept],
            predicates=["#V#meeting_participant", "hasContent"],
            user_concept_id=owner,
            limit=200,
        )
        prior_bindings = {}
        for row in prior_page["items"]:
            evidence = row.get("provenance", {}).get("evidence", {})
            label = evidence.get("speaker_label")
            if (
                row.get("predicate") == "#V#meeting_participant"
                and label
                and evidence.get("identity_basis") == "caller_reviewed_identity"
            ):
                prior_bindings[label] = row["object_concept_id"]
        bindings = {**prior_bindings, **bindings}
        assert_fact(meeting_concept, "hasNote", text=json.dumps(source, sort_keys=True))
        # Source timestamps have no offset; preserve them without inventing UTC.
        if meeting.get("metadata", {}).get("start_time"):
            assert_fact(
                meeting_concept,
                "hasNote",
                text="Otter recorded start (source timezone unspecified): "
                + meeting["metadata"]["start_time"],
            )
        transcript = next(
            (x for x in archived["artifacts"] if x["kind"] == "transcript"), None
        )
        if transcript:
            assert_fact(
                meeting_concept,
                "hasContent",
                text="Transcript: /von/api/otter-archive/artifacts/"
                + transcript["artifact_id"],
            )
        else:
            assert_fact(
                meeting_concept,
                "hasNote",
                text="Otter reports no transcript available; source metadata preserved and collection will retry.",
            )
            for row in prior_page["items"]:
                if (
                    row.get("predicate") == "hasContent"
                    and row.get("provenance", {}).get("evidence", {}).get("source")
                    == "otter_live_collection"
                ):
                    retract_scoped_assertion(
                        assertion_id=row["assertion_id"], acting_user_concept_id=owner
                    )
        participants = []
        unresolved = []
        for label in labels:
            if re.match(r"(?i)^(unknown speaker|speaker\s*\d+)", label):
                unresolved.append(label)
                continue
            target = bindings.get(label)
            if target:
                if lookup(target) is None:
                    raise ValueError("participant_binding_target_not_visible")
                basis = "caller_reviewed_identity"
            else:
                target = stable_id("participant", owner, cid, label)
                ensure(
                    target,
                    label + " (Otter participant)",
                    "#V#person",
                    "Source label in "
                    + meeting["url"]
                    + "; real-world identity not yet reconciled. This concept is local to this meeting.",
                )
                basis = "meeting_local_source_label"
            assert_fact(
                meeting_concept,
                "#V#meeting_participant",
                target=target,
                evidence={**source, "speaker_label": label, "identity_basis": basis},
            )
            for row in prior_page["items"]:
                evidence = row.get("provenance", {}).get("evidence", {})
                if (
                    row.get("predicate") == "#V#meeting_participant"
                    and evidence.get("source") == "otter_live_collection"
                    and evidence.get("speaker_label") == label
                    and row.get("object_concept_id") != target
                ):
                    result = retract_scoped_assertion(
                        assertion_id=row["assertion_id"], acting_user_concept_id=owner
                    )
                    if not result.get("success"):
                        raise RuntimeError("otter_participant_rebinding_failed")
            participants.append(
                {"label": label, "concept_id": target, "identity_basis": basis}
            )
        return {
            "meeting_concept_id": meeting_concept,
            "participants": participants,
            "unresolved_speaker_labels": unresolved,
            "transcript_url": "/von/api/otter-archive/artifacts/"
            + transcript["artifact_id"]
            if transcript
            else None,
        }
