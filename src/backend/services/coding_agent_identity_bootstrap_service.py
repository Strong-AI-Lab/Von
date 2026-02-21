from __future__ import annotations

import threading
from typing import Any

from ..vontology.utils_vontology import (
    THING_PRIMARY_ID,
    ensure_thing_exists_and_link_orphans,
)
from . import concept_service
from .relationship_write_service import add_relationship

CODING_AGENT_TYPE_ID = "#V#coding_agent"
GITHUB_COPILOT_INSTANCE_ID = "#V#github_copilot_instance"
AGENT_PARENT_TYPE_ID = "#V#agent"
VON_SYSTEM_ID = "#V#von_system"

_bootstrap_completed = False
_bootstrap_lock = threading.Lock()


def _concept_exists(concept_id: str) -> bool:
    try:
        return concept_service.get_concept_by_concept_id(concept_id) is not None
    except Exception:
        return False


def _ensure_related_to_von_system(*, concept_id: str) -> bool:
    if not _concept_exists(VON_SYSTEM_ID):
        return False
    try:
        result = add_relationship(concept_id, "related_to", VON_SYSTEM_ID)
        return bool(result.get("success"))
    except Exception:
        return False


def _bootstrap_requirements_satisfied(summary: dict[str, Any]) -> bool:
    """Return True only when bootstrap outputs are fully ready for caching."""

    if summary.get("errors"):
        return False
    if not _concept_exists(CODING_AGENT_TYPE_ID):
        return False
    if not _concept_exists(GITHUB_COPILOT_INSTANCE_ID):
        return False
    return bool(summary.get("type_related_to_von_system")) and bool(
        summary.get("instance_related_to_von_system")
    )


def ensure_coding_agent_identity_concepts(*, force: bool = False) -> dict[str, Any]:
    """Best-effort bootstrap for Coding Agent identity concepts.

    Creates and wires:
    - #V#coding_agent (type)
    - #V#github_copilot_instance (instance of #V#coding_agent)
    """

    global _bootstrap_completed

    if _bootstrap_completed and not force:
        return {
            "cached": True,
            "type_created": False,
            "instance_created": False,
            "type_related_to_von_system": False,
            "instance_related_to_von_system": False,
            "errors": [],
        }

    with _bootstrap_lock:
        if _bootstrap_completed and not force:
            return {
                "cached": True,
                "type_created": False,
                "instance_created": False,
                "type_related_to_von_system": False,
                "instance_related_to_von_system": False,
                "errors": [],
            }

        summary: dict[str, Any] = {
            "cached": False,
            "type_concept_id": CODING_AGENT_TYPE_ID,
            "instance_concept_id": GITHUB_COPILOT_INSTANCE_ID,
            "parent_concept_id_used": None,
            "type_created": False,
            "instance_created": False,
            "type_related_to_von_system": False,
            "instance_related_to_von_system": False,
            "errors": [],
        }

        try:
            if not _concept_exists(THING_PRIMARY_ID):
                ensure_thing_exists_and_link_orphans()
        except Exception as exc:
            summary["errors"].append(f"ensure_thing_failed:{exc}")

        parent_id = AGENT_PARENT_TYPE_ID if _concept_exists(AGENT_PARENT_TYPE_ID) else THING_PRIMARY_ID
        summary["parent_concept_id_used"] = parent_id

        try:
            if not _concept_exists(CODING_AGENT_TYPE_ID):
                concept_service.create_concept(
                    name="Coding Agent",
                    concept_id=CODING_AGENT_TYPE_ID,
                    parent_concept_ids=[parent_id],
                    create_as_instance=False,
                    description=(
                        "A software-based agent that participates in coding and engineering workflows."
                    ),
                    notes=(
                        "Created by Von bootstrap flows to support coding-agent identity and workflow attribution."
                    ),
                    system_tags=["agent", "coding", "automation"],
                )
                summary["type_created"] = True
        except Exception as exc:
            summary["errors"].append(f"type_create_failed:{exc}")

        try:
            if not _concept_exists(GITHUB_COPILOT_INSTANCE_ID):
                concept_service.create_concept(
                    name="GitHub Copilot Instance",
                    concept_id=GITHUB_COPILOT_INSTANCE_ID,
                    parent_concept_ids=[CODING_AGENT_TYPE_ID],
                    create_as_instance=True,
                    description=(
                        "A concrete coding-agent identity for GitHub Copilot interactions in Von."
                    ),
                    notes=(
                        "Used for deterministic attribution of coding-agent actions in conversations and workflow traces."
                    ),
                    system_tags=["agent", "copilot", "github", "coding"],
                )
                summary["instance_created"] = True
        except Exception as exc:
            summary["errors"].append(f"instance_create_failed:{exc}")

        summary["type_related_to_von_system"] = _ensure_related_to_von_system(
            concept_id=CODING_AGENT_TYPE_ID
        )
        summary["instance_related_to_von_system"] = _ensure_related_to_von_system(
            concept_id=GITHUB_COPILOT_INSTANCE_ID
        )

        bootstrap_ready = _bootstrap_requirements_satisfied(summary)
        _bootstrap_completed = bool(bootstrap_ready)
        summary["bootstrap_completed"] = bool(bootstrap_ready)

        return summary

