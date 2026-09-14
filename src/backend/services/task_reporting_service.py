"""Resolve reporting responsibility without granting execution or data authority.

Explicit task reports-to wins. An unset value uses the assignee's represented
supervisor, for people and agents alike. Reads use the caller's actor scope.
Defaults stay defaults: they are not copied into the task's explicit relation.
"""

from pymongo.errors import PyMongoError

from src.backend.db.repositories.concepts_repository import ConceptsRepository
from src.backend.utils.concept_id_utils import ensure_v_concept_prefix


def _resolve_task_reporting(task):
    explicit = task.get("report_to_concept_id")
    assignee = task.get("assignee_concept_id")
    source = "task.report_to_concept_id" if explicit else "assignee.#V#has_supervisor"
    targets = [explicit] if explicit else []
    if not explicit and assignee:
        doc = ConceptsRepository.find_one(
            {"concept_id": assignee}, {"relationships": 1}
        )
        if not doc:
            return {
                "status": "assignee_inaccessible",
                "source": source,
                "concept_id": None,
            }
        targets = (doc.get("relationships") or {}).get("#V#has_supervisor", [])
        if isinstance(targets, str):
            targets = [targets]
    targets = list(
        dict.fromkeys(ensure_v_concept_prefix(v) for v in targets if isinstance(v, str))
    )
    status = "resolved" if len(targets) == 1 else "ambiguous" if targets else "unset"
    target = targets[0] if len(targets) == 1 else None
    if target == assignee:
        status = "self_reference"
    if target and not ConceptsRepository.find_one(
        {"concept_id": target}, {"concept_id": 1}
    ):
        status = "supervisor_inaccessible"
    return {"status": status, "source": source, "concept_id": target}


def resolve_task_reporting(task):
    # Reporting discovery is useful context, not a reason to turn a successfully
    # persisted task creation into an ambiguous failure after the write.
    try:
        return _resolve_task_reporting(task)
    except (PyMongoError, RuntimeError) as exc:
        return {
            "status": "lookup_failed",
            "source": "canonical ontology",
            "concept_id": None,
            "error_type": type(exc).__name__,
        }
