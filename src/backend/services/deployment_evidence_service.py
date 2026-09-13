"""Represented build identities, deployment attempts and immutable observations.

Attributes hold exact receipt fields; typed relationships carry domain links.
An observation reports evidence, not authority to deploy or complete a task.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from ..db.repositories.concepts_repository import ConceptsRepository
from ..security.access_control import can_access_concept
from . import concept_service
from .relationship_write_service import add_relationship

BUILD = "#V#software_build"
DEPLOYMENT = "#V#software_deployment"
OBSERVATION = "#V#deployment_observation"
BUILD_LINK = "#V#deploys_build"
IMPLEMENTS = "#V#implements_task"
VERIFIES = "#V#verifies_task"
OBSERVES = "#V#observes_deployment"
SUPERSEDES = "#V#supersedes_deployment"
VOCABULARY = {
    BUILD: (
        "Software build",
        "An immutable artefact identity, distinct from its source revision and deployment attempts.",
    ),
    DEPLOYMENT: (
        "Software deployment",
        "One planned or actual attempt to serve a build in an environment.",
    ),
    OBSERVATION: (
        "Deployment observation",
        "An immutable attributed status receipt; verification describes observed evidence, not task completion.",
    ),
    BUILD_LINK: (
        "deploys build",
        "Links a deployment attempt to its immutable build identity.",
    ),
    IMPLEMENTS: (
        "implements task",
        "Links a deployment to work it delivers; does not assert completion.",
    ),
    VERIFIES: (
        "verifies task",
        "Links a deployment to a task whose outcome its evidence checks.",
    ),
    OBSERVES: (
        "observes deployment",
        "Links a status receipt to its deployment attempt.",
    ),
    SUPERSEDES: (
        "supersedes deployment",
        "Links a replacement or rollback attempt to the prior deployment in the same environment and target.",
    ),
}
STATUSES = {
    "planned",
    "built",
    "deployed",
    "verified",
    "failed",
    "rolled_back",
    "superseded",
}


class DeploymentEvidenceError(ValueError):
    pass


def _required(data, key, maximum=2048):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DeploymentEvidenceError(
            f"{key} must be a non-empty string (maximum {maximum})"
        )
    return value.strip()


def _date(data, key):
    value = _required(data, key, 80)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise DeploymentEvidenceError(
            f"{key} must include an ISO 8601 timezone"
        ) from None


def _identity(kind, *parts):
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()
    return f"#V#{kind}_{digest}"


def _payload(doc):
    return (doc.get("attributes") or {}).get("deployment_evidence") or {}


def _read(concept_id, kind=None):
    if not can_access_concept(concept_id):
        raise DeploymentEvidenceError("Record is unavailable")
    doc = ConceptsRepository.find_one({"concept_id": concept_id})
    if not doc or (
        kind
        and kind not in (doc.get("relationships") or {}).get("is_an_instance_of", [])
    ):
        raise DeploymentEvidenceError("Record is unavailable")
    return doc


def _ensure(concept_id, kind, payload, actor, organisation, name=None):
    doc = ConceptsRepository.find_one({"concept_id": concept_id})
    if not doc:
        try:
            concept_service.create_concept(
                name=name
                or payload.get("deployment_id")
                or payload.get("build_id")
                or "Deployment evidence",
                concept_id=concept_id,
                parent_concept_ids=[kind],
                attributes={"deployment_evidence": payload},
                created_by_concept_id=actor,
                organisation_concept_id=organisation,
                maintain_relationship_inverses=False,
            )
        except concept_service.ConceptServiceError:
            # A concurrent identical ingestion may have won the unique-ID insert.
            if not ConceptsRepository.find_one({"concept_id": concept_id}):
                raise
        doc = _read(concept_id, kind)
    if _payload(doc) != payload:
        raise DeploymentEvidenceError(
            "Immutable identifier already has different evidence"
        )
    return concept_id


def _link(source, predicate, target):
    result = add_relationship(source, predicate, target, maintain_inverse=False)
    if not result.get("success"):
        raise DeploymentEvidenceError(
            "Relationship write failed; retry the same receipt"
        )
    if target not in (_read(source).get("relationships") or {}).get(predicate, []):
        raise DeploymentEvidenceError(
            "Relationship read-back failed; retry the same receipt"
        )


def bootstrap_vocabulary(*, actor, organisation=None):
    """Explicit scoped release input, never run as a side effect of a read.

    Use the canonical publication route separately if shared vocabulary needs
    wider visibility. Operational administration alone does not grant that scope.
    """
    for cid, (name, description) in VOCABULARY.items():
        if ConceptsRepository.find_one({"concept_id": cid}):
            continue
        concept_service.create_concept(
            name=name,
            concept_id=cid,
            description=description,
            parent_concept_ids=[
                (
                    "#V#thing"
                    if cid in {BUILD, DEPLOYMENT, OBSERVATION}
                    else "#V#binary_predicate"
                )
            ],
            create_as_instance=cid not in {BUILD, DEPLOYMENT, OBSERVATION},
            created_by_concept_id=actor,
            organisation_concept_id=organisation,
            maintain_relationship_inverses=False,
        )


def ingest_deployment(data, *, actor, organisation=None):
    """Ingest an exact receipt under trusted actor scope; safe to retry after partial writes."""
    from .task_management_service import _get_task_doc

    if not actor or not isinstance(data, dict):
        raise DeploymentEvidenceError("Authenticated actor and object receipt required")
    allowed = {
        "build_id",
        "source_revision",
        "deployment_id",
        "environment",
        "target",
        "status",
        "observed_at",
        "deployed_at",
        "evidence",
        "implements_tasks",
        "verifies_tasks",
        "supersedes",
        "receipt_id",
    }
    if set(data) - allowed:
        raise DeploymentEvidenceError("Unknown receipt fields")
    build = {key: _required(data, key) for key in ("build_id", "source_revision")}
    deployment = {key: _required(data, key) for key in ("deployment_id", "environment")}
    if deployment["environment"] not in {"local", "staging", "production"}:
        raise DeploymentEvidenceError(
            "environment must be local, staging or production"
        )
    deployment["target"] = (
        _required(data, "target") if data.get("target") is not None else None
    )
    status = _required(data, "status", 40)
    if status not in STATUSES:
        raise DeploymentEvidenceError("Unknown deployment status")
    observation = {
        "receipt_id": _required(data, "receipt_id"),
        "status": status,
        "observed_at": _date(data, "observed_at"),
        "evidence": _required(data, "evidence", 16000),
    }
    observation["deployed_at"] = (
        _date(data, "deployed_at") if data.get("deployed_at") else None
    )
    if (
        status in {"deployed", "verified", "rolled_back", "superseded"}
        and not observation["deployed_at"]
    ):
        raise DeploymentEvidenceError("deployed_at required for this status")
    if (
        observation["deployed_at"]
        and observation["deployed_at"] > observation["observed_at"]
    ):
        raise DeploymentEvidenceError("deployed_at cannot follow observed_at")
    task_links = {}
    for field, predicate in (
        ("implements_tasks", IMPLEMENTS),
        ("verifies_tasks", VERIFIES),
    ):
        ids = data.get(field, [])
        if (
            not isinstance(ids, list)
            or len(ids) > 100
            or any(not isinstance(cid, str) for cid in ids)
        ):
            raise DeploymentEvidenceError(
                f"{field} must be a list of at most 100 task IDs"
            )
        task_links[predicate] = sorted(set(_get_task_doc(cid)[0] for cid in ids))
    build_id = _identity("build", actor, organisation, build["build_id"])
    deployment_id = _identity(
        "deployment", actor, organisation, deployment["deployment_id"]
    )
    deployment.update(
        build_concept_id=build_id, recorded_by=actor, organisation=organisation
    )
    supersedes = data.get("supersedes")
    if supersedes is not None:
        previous = _payload(_read(_required(data, "supersedes"), DEPLOYMENT))
        if supersedes == deployment_id or any(
            previous.get(key) != deployment[key] for key in ("environment", "target")
        ):
            raise DeploymentEvidenceError(
                "Replacement must refer to a different attempt at the same environment and target"
            )
    deployment["supersedes"] = supersedes
    observation.update(
        recorded_by=actor, deployment_concept_id=deployment_id, task_links=task_links
    )
    observation_id = _identity(
        "deployment_observation", actor, organisation, observation["receipt_id"]
    )
    # Validate immutable IDs before any writes, then read back after each create.
    for cid, payload in (
        (build_id, build),
        (deployment_id, deployment),
        (observation_id, observation),
    ):
        existing = ConceptsRepository.find_one({"concept_id": cid})
        if existing and _payload(existing) != payload:
            raise DeploymentEvidenceError(
                "Immutable identifier already has different evidence"
            )
    for cid in VOCABULARY:
        _read(cid)
    _ensure(build_id, BUILD, build, actor, organisation)
    _ensure(deployment_id, DEPLOYMENT, deployment, actor, organisation)
    _link(deployment_id, BUILD_LINK, build_id)
    if supersedes:
        _link(deployment_id, SUPERSEDES, supersedes)
    for predicate, ids in task_links.items():
        for cid in ids:
            _link(deployment_id, predicate, cid)
    _ensure(observation_id, OBSERVATION, observation, actor, organisation)
    _link(observation_id, OBSERVES, deployment_id)
    result = get_deployment(deployment_id)
    if observation_id not in {row["concept_id"] for row in result["observations"]}:
        raise DeploymentEvidenceError(
            "Receipt read-back failed; retry the same receipt"
        )
    return result


def get_deployment(concept_id):
    doc = _read(concept_id, DEPLOYMENT)
    payload = _payload(doc)
    build = _read(payload["build_concept_id"], BUILD)
    observations = [
        {**_payload(row), "concept_id": row["concept_id"]}
        for row in ConceptsRepository.find({f"relationships.{OBSERVES}": concept_id})
        if OBSERVATION in (row.get("relationships") or {}).get("is_an_instance_of", [])
    ]
    observations.sort(key=lambda row: (row["observed_at"], row["concept_id"]))
    latest = observations[-1] if observations else None
    tied = [
        row
        for row in observations
        if latest and row["observed_at"] == latest["observed_at"]
    ]
    status = latest["status"] if latest else "unreported"
    if len({(row["status"], row["deployed_at"]) for row in tied}) > 1:
        status = "ambiguous"
    tasks = {}
    for predicate, field in (
        (IMPLEMENTS, "implements_tasks"),
        (VERIFIES, "verifies_tasks"),
    ):
        tasks[field] = [
            cid
            for cid in (doc.get("relationships") or {}).get(predicate, [])
            if can_access_concept(cid)
        ]
    # Do not leak subsequently hidden task references through raw receipts.
    for row in observations:
        row["task_links"] = {
            pred: [cid for cid in ids if can_access_concept(cid)]
            for pred, ids in row.get("task_links", {}).items()
        }
    return {
        **payload,
        "concept_id": concept_id,
        "build": {**_payload(build), "concept_id": build["concept_id"]},
        "status": status,
        "deployed_at": latest["deployed_at"] if latest else None,
        "observations": observations,
        **tasks,
    }


def list_task_deployments(task_id):
    from .task_management_service import _get_task_doc

    task_id, _ = _get_task_doc(task_id)
    docs = ConceptsRepository.find(
        {
            "$or": [
                {f"relationships.{IMPLEMENTS}": task_id},
                {f"relationships.{VERIFIES}": task_id},
            ]
        }
    )
    result = []
    for doc in docs:
        if DEPLOYMENT not in (doc.get("relationships") or {}).get(
            "is_an_instance_of", []
        ):
            continue
        try:
            result.append(get_deployment(doc["concept_id"]))
        except DeploymentEvidenceError:
            # A link does not grant access to the build or keep it alive.
            continue
    return result


def list_build_deployments(build_id):
    _read(build_id, BUILD)
    return [
        get_deployment(doc["concept_id"])
        for doc in ConceptsRepository.find({f"relationships.{BUILD_LINK}": build_id})
    ]
