"""Run the public A-C developmental rehearsal with a real model and isolated DB.

This deliberately imports the integration-test world: canonical workflow/text
services execute, Jira/repository sources are fictional adapters, and no shared
Vontology or real organisation is modified. This is not a blind efficacy study.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests/backend")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument(
        "--scheduled-middle",
        action="store_true",
        help="Run B through the real scheduler and durable worker in the isolated database.",
    )
    args = parser.parse_args()
    # Must precede all backend imports, including test-support imports.
    os.environ["VON_USE_MOCK_DB"] = "1"
    os.environ["VON_DB_NAME"] = "test_role_continuity_rehearsal"
    os.environ["VON_DEBUG_LLM_IO"] = "0"
    logging.basicConfig(level=logging.ERROR)
    from test_lab_status_digest_continuity import DigestWorld, ACTOR, PRODUCT, texts
    from src.backend.services import concept_service
    from src.backend.services.lab_status_digest_workflow_vontology_service import (
        bootstrap_canonical_lab_status_digest_workflow,
        LAB_STATUS_DIGEST_WORKFLOW_ID,
    )
    from src.backend.services.settings_service import set_user_enabled_llm_settings
    from src.backend.services.text_value_service import upsert_singleton_text_relation
    from src.backend.security.access_control import override_current_actor
    from src.backend.workflows.engine import WorkflowExecutor
    from src.backend.workflows.action_registry import WorkflowEnvironment
    from src.backend.workflows.vontology_loader import (
        load_workflow_definition_from_vontology,
    )
    from src.backend.languagemodels.llm_interface import OpenAIClient

    model = args.model
    if "sol" in model.lower():
        raise ValueError("Sol-family models are prohibited for this rehearsal")

    class FixedOpenAIClient(OpenAIClient):
        """Experiment-local model pin; reject another model before requesting it."""

        def _get_structured_client_config(self, selected_model):
            from dataclasses import replace

            return replace(
                super()._get_structured_client_config(selected_model),
                requested_api_surface="responses",
            )

        def generate(self, prompt, *pos, **kw):
            selected = kw.get("model", pos[0] if pos else None)
            if selected != model:
                raise RuntimeError("rehearsal_model_drift_before_request")
            return super().generate(prompt, *pos, **kw)

        def generate_with_tools(self, prompt, *pos, **kw):
            selected = kw.get("model")
            if selected != model:
                raise RuntimeError("rehearsal_model_drift_before_request")
            return super().generate_with_tools(prompt, *pos, **kw)

    from src.backend.services.conversation_turn_workflow_vontology_service import (
        _ensure_conversation_turn_prompt_support,
    )

    _ensure_conversation_turn_prompt_support()
    report = bootstrap_canonical_lab_status_digest_workflow()
    if not report["success"]:
        raise RuntimeError("rehearsal_seed_materialisation_failed")
    for cid in (ACTOR, PRODUCT):
        concept_service.create_concept(
            name=cid[3:],
            concept_id=cid,
            created_by_concept_id=ACTOR if cid != ACTOR else None,
        )
    assert set_user_enabled_llm_settings(
        ACTOR, [{"provider": "openai", "model": model}]
    )
    world = DigestWorld()
    client = FixedOpenAIClient()
    sources = {}

    def source(key, text):
        cid = "#V#rehearsal_" + key
        if key not in sources:
            concept_service.create_concept(
                name=key, concept_id=cid, created_by_concept_id=ACTOR
            )
        with override_current_actor(ACTOR):
            upsert_singleton_text_relation(
                subject_concept_id=cid, predicate="hasContent", text=text, lang="en-NZ"
            )
        sources[key] = {"concept_id": cid, "text": text}
        return cid

    source(
        "call",
        "Harbour Fund call-v1, observed 6 October 2026 NZ: external deadline 30 October 17:00. Budget and collaborator statement required. No funding awarded.",
    )
    source(
        "office",
        "office-v1, observed 6 October: internal deadline 23 October 12:00. Starting the budget does not require a polished narrative.",
    )
    source(
        "project",
        "Project Kestrel: draft narrative; compute cost unknown. Ari owns budget. Bo owns collaborator statement, not yet received; no promised delivery date. Canonical commitment ID TASK-1 remains open.",
    )
    events = [
        (
            "A",
            "6 October 2026 NZ",
            "Maintain this research-funding readiness brief. Read the named sources and preserve TASK-1; permitted effect is updating this internal brief. No messages, submission or spending.",
        ),
        (
            "B",
            "14 October 2026 NZ",
            "Revisit the continuing responsibility from its saved brief and current sources.",
        ),
        (
            "C",
            "15 October 2026 NZ",
            "A compute quote is now available among the named sources. Revisit readiness and consequential remaining gaps.",
        ),
    ]
    evidence = {
        "kind": "fictional_development_rehearsal",
        "assessment": "Requires review of retrieved evidence and canonical work products; completion alone is not a pass.",
        "model_requested": model,
        "runtime": "real represented workflow/model; in-process mock database; fictional external adapters",
        "stages": [],
        "limitations": [
            "No real lab pilot, blind comparison, learned benefit or deployed scheduler is claimed.",
            "A and C are manual fresh executions; B uses the isolated scheduler only when schedule_evidence is present. No shared worker or running service is activated.",
        ],
    }
    definition = load_workflow_definition_from_vontology(LAB_STATUS_DIGEST_WORKFLOW_ID)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for stage, clock, instruction in events:
        if stage == "B":
            source(
                "office",
                "office-v2, observed 14 October: internal deadline now 20 October 12:00, supersedes office-v1 23 October. External fund deadline unchanged. Collaborator statement remains missing.",
            )
        if stage == "C":
            source(
                "compute_quote",
                "compute-quote-v1: estimate 8,000 budget units, valid until 31 October. This is a quote, not an allocation, purchase approval or authority to spend.",
            )
        inputs = {
            "prompt": f"Fictional clock: {clock}. {instruction}",
            "work_product_id": PRODUCT,
            "evidence_sources": [row["concept_id"] for row in sources.values()],
            "jira_jql": "key = TASK-1",
            "requested_model": model,
            "user_concept_id": ACTOR,
            "requested_client_type": "openai",
            "prefer_default_model": True,
            "requested_model_parameters": {"reasoning_effort": "none"},
        }
        before = len(world.calls)
        start = time.monotonic()
        schedule_evidence = None
        if stage == "B" and args.scheduled_middle:
            from datetime import datetime, timedelta, timezone
            from unittest.mock import patch
            from src.backend.workflows.durable.models import WorkflowSchedule
            from src.backend.workflows.durable.instance_manager import (
                WorkflowInstanceManager,
            )
            from src.backend.workflows.durable.scheduler import WorkflowScheduler
            from src.backend.workflows.durable.worker import (
                DurableWorkflowWorker,
            )
            from src.backend.server import utils_flask
            from src.backend.workflows.durable.registry_factory import (
                build_vontology_workflow_registry_snapshot,
            )

            concept_service.create_concept(
                name="triggers workflow", concept_id="#V#triggers_workflow"
            )
            manager = WorkflowInstanceManager()
            schedule = WorkflowSchedule.create_once(
                LAB_STATUS_DIGEST_WORKFLOW_ID,
                run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                user_id=ACTOR,
                org_id=None,
                namespace=ACTOR,
                default_inputs=inputs,
            )
            with override_current_actor(ACTOR):
                schedule_id = manager.create_schedule(schedule)
                scheduler = WorkflowScheduler(manager)
                scheduler._process_due_schedules()
                instances = manager.list_instances(
                    user_id=ACTOR,
                    namespace=ACTOR,
                    workflow_id=LAB_STATUS_DIGEST_WORKFLOW_ID,
                )
            if len(instances) != 1:
                raise RuntimeError("rehearsal_schedule_did_not_create_one_instance")
            workflow_registry = build_vontology_workflow_registry_snapshot(
                workflow_ids=[LAB_STATUS_DIGEST_WORKFLOW_ID]
            )
            with patch.object(
                utils_flask, "_durable_workflow_registry", workflow_registry
            ):
                definition_loader = utils_flask._get_durable_definition_loader()
            worker = DurableWorkflowWorker(
                instance_manager=manager,
                registry=world.registry,
                definition_loader=definition_loader,
            )
            completed_results = []
            worker.set_callbacks(
                on_completed=lambda _instance_id, outcome: completed_results.append(
                    outcome
                )
            )
            instance = manager.find_and_claim_instance(
                worker.worker_id, worker_build_identity=worker.worker_build_identity
            )
            if instance is None:
                raise RuntimeError("rehearsal_worker_could_not_claim_instance")
            # Dependency injection confines this real worker to the fictional
            # gateway and pinned real provider, rather than the shared server.
            with (
                patch.object(
                    utils_flask, "_durable_workflow_registry", workflow_registry
                ),
                patch(
                    "src.backend.workflows.durable.registry_factory._get_or_build_durable_mcp_gateway",
                    return_value=world.gateway,
                ),
                patch(
                    "src.backend.languagemodels.llm_interface.get_llm_client",
                    return_value=client,
                ),
            ):
                worker._process_instance(instance)
            with override_current_actor(ACTOR):
                canonical = manager.get_instance(instance.instance_id)
                scheduler._process_due_schedules()
                count = len(
                    manager.list_instances(
                        user_id=ACTOR,
                        namespace=ACTOR,
                        workflow_id=LAB_STATUS_DIGEST_WORKFLOW_ID,
                    )
                )
            schedule_evidence = dict(
                schedule_id=schedule_id,
                instance_id=instance.instance_id,
                occurrence_id=instance.inputs.get("schedule_occurrence_id"),
                canonical_status=canonical.status.value,
                instances_after_second_poll=count,
            )
            if not completed_results:
                evidence["scheduled_worker_failure"] = schedule_evidence
                args.output.write_text(
                    json.dumps(evidence, indent=2, default=str) + "\n"
                )
                raise RuntimeError("rehearsal_worker_did_not_complete")
            result = completed_results[0]
        else:
            with override_current_actor(ACTOR):
                result = WorkflowExecutor(registry=world.registry).run(
                    definition,
                    environment=WorkflowEnvironment(
                        llm_client=client,
                        gateway=world.gateway,
                        model=model,
                        model_parameters={"reasoning_effort": "none"},
                        user_concept_id=ACTOR,
                        user_namespace=ACTOR,
                    ),
                    data=inputs,
                )
        row = {
            "schedule_evidence": schedule_evidence,
            "stage": stage,
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "completed": result.completed,
            "error": result.error,
            "exact_readback_verified": result.data.get("text_effect_readback_verified"),
            "source_sha256": hashlib.sha256(
                json.dumps(sources, sort_keys=True).encode()
            ).hexdigest(),
            "tool_calls": world.calls[before:],
            "canonical_text": texts(PRODUCT),
            "llm_calls": result.data.get("llm_calls"),
            "response_metadata": client.last_response_metadata,
        }
        evidence["stages"].append(row)
        args.output.write_text(json.dumps(evidence, indent=2, default=str) + "\n")
        print(
            json.dumps(
                {
                    "stage": stage,
                    "completed": result.completed,
                    "seconds": row["elapsed_seconds"],
                    "error": result.error,
                }
            ),
            flush=True,
        )
        if not result.completed:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
