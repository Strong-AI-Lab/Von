from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from scripts import publish_kr_materialisation_workflows as publish_cli
from src.backend.services import kr_materialisation_workflow_vontology_service as mod

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SEED_BUNDLE_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "kr_materialisation_workflow_seed_bundle.json"
)
_PLAN_PROMPT_SEED_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_kr_design_materialisation_plan_seed.md"
)
_RELATIONSHIP_PROMPT_SEED_PATH = (
    _PROJECT_ROOT
    / "src"
    / "backend"
    / "workflows"
    / "repo_seed_bundles"
    / "prompt_kr_relationship_endpoint_resolution_seed.md"
)


def _iter_publication_steps(bundle: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    for workflow in bundle.get("workflows") or []:
        if not isinstance(workflow, Mapping):
            continue
        publication_spec = workflow.get("publication_spec")
        if not isinstance(publication_spec, Mapping):
            continue
        for step in publication_spec.get("steps") or []:
            if isinstance(step, Mapping):
                yield step


def test_kr_seed_bundle_uses_prompt_concepts_not_inline_prompt_text() -> None:
    bundle = json.loads(_SEED_BUNDLE_PATH.read_text(encoding="utf-8"))
    rendered_bundle = json.dumps(bundle, sort_keys=True)

    assert "prompt_text" not in rendered_bundle
    assert "response_contract_text" not in rendered_bundle

    llm_steps = [
        step
        for step in _iter_publication_steps(bundle)
        if step.get("action_id") == "llm.action"
    ]
    assert len(llm_steps) == 2
    prompt_ids_by_state = {
        step.get("state_id"): tuple(step.get("prompt_concept_ids") or ())
        for step in llm_steps
    }
    assert prompt_ids_by_state["extract_kr_materialisation_plan"] == (
        mod.PROMPT_KR_DESIGN_MATERIALISATION_PLAN_ID,
    )
    assert prompt_ids_by_state["resolve_relationship_specs"] == (
        mod.PROMPT_KR_RELATIONSHIP_ENDPOINT_RESOLUTION_ID,
    )


def test_kr_prompt_seed_assets_hold_operational_prompt_content() -> None:
    plan_prompt = _PLAN_PROMPT_SEED_PATH.read_text(encoding="utf-8")
    relationship_prompt = _RELATIONSHIP_PROMPT_SEED_PATH.read_text(encoding="utf-8")

    assert "materialise bounded knowledge representation designs" in plan_prompt
    assert (
        "Return JSON only with keys: decision ('materialise' or 'block')"
        in plan_prompt
    )
    assert "Resolve KR relationship endpoint references" in relationship_prompt
    assert (
        "Return JSON only with keys: decision ('assert', 'skip', or 'block')"
        in relationship_prompt
    )


def test_kr_seed_bundle_validates_as_workflow_contracts() -> None:
    report = mod.validate_kr_materialisation_seed_bundle()

    assert report["success"] is True
    assert report["workflow_ids"] == list(mod.KR_MATERIALISATION_WORKFLOW_IDS)
    assert report["invalid_workflow_ids"] == []
    for validation in report["validation_by_workflow_id"].values():
        assert validation["valid"] is True


def test_publish_script_validate_all_uses_seed_bundle(capsys) -> None:
    exit_code = publish_cli.main(["validate-all"])
    output = capsys.readouterr().out

    assert exit_code == 0
    payload = json.loads(output)
    assert payload["success"] is True
    assert payload["workflow_ids"] == list(mod.KR_MATERIALISATION_WORKFLOW_IDS)