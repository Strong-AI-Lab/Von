from __future__ import annotations

from pathlib import Path

from src.backend.workflows.action_registry import WorkflowEnvironment
from src.backend.workflows.durable.registry_factory import build_durable_action_registry
from src.backend.workflows.skill_interop import (
    SKILL_EXECUTE_ACTION_ID,
    SkillArtefact,
    build_skill_vontology_projection,
    discover_skill_catalogue,
    execute_skill_direct,
    load_skill_artefact,
    load_skill_resource,
    transpile_skill_to_workflow_definition,
)


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def generate(self, prompt, context=None, model=None, llm_params=None) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "context": context,
                "model": model,
            }
        )
        return f"handled:{prompt}"


def _write_skill(
    root: Path,
    *,
    skill_name: str = "triage-issue",
    body: str | None = None,
) -> Path:
    skill_dir = root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "\n".join(
            [
                "---",
                f"name: {skill_name}",
                "description: Triage an issue deterministically.",
                "argument-hint: issue key and expected outcome",
                "user-invokable: true",
                "disable-model-invocation: false",
                "---",
                body or "Follow the checklist in [guide](guide.md).",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (skill_dir / "guide.md").write_text(
        "Use the repository issue template before proposing changes.",
        encoding="utf-8",
    )
    return skill_file


def test_discover_skill_catalogue_reads_metadata_and_provenance(tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    skill_file = _write_skill(project_root)

    discovered = discover_skill_catalogue({"project": [str(project_root)]})

    assert len(discovered) == 1
    record = discovered[0]
    assert record.name == "triage-issue"
    assert record.description == "Triage an issue deterministically."
    assert record.argument_hint == "issue key and expected outcome"
    assert record.source_scope == "project"
    assert record.discovery_root == str(project_root.resolve())
    assert record.skill_file == str(skill_file.resolve())


def test_load_skill_resource_blocks_directory_escape(tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    skill_file = _write_skill(project_root)
    result = load_skill_artefact(
        str(skill_file),
        source_scope="project",
        discovery_root=str(project_root),
    )
    assert result.artefact is not None
    skill = result.artefact

    assert load_skill_resource(skill, "guide.md").startswith("Use the repository")

    try:
        load_skill_resource(skill, "..\\..\\secret.txt")
    except ValueError as exc:
        assert str(exc) == "skill_resource_outside_skill_directory"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected path-escape validation failure")


def test_transpile_skill_to_workflow_definition_carries_provenance(tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    skill_file = _write_skill(project_root)
    result = load_skill_artefact(
        str(skill_file),
        source_scope="project",
        discovery_root=str(project_root),
    )
    assert result.validation_report.valid is True
    assert result.artefact is not None

    definition = transpile_skill_to_workflow_definition(result.artefact)

    assert definition.workflow_id == "#V#skill_triage_issue_workflow"
    assert definition.metadata["skill_interop_contract"]["source_scope"] == "project"
    assert (
        definition.metadata["completion_gate"]["required_context_keys"]
        == ["skill_execution.executed"]
    )
    execute_state = definition.states["execute_skill"]
    assert execute_state.metadata["checkpoint_policy"]["plan_item_updates"] == [
        {"item_id": "execute_skill", "status": "done"}
    ]
    action_inputs = execute_state.actions[0].inputs
    assert action_inputs["skill_name"] == "triage-issue"
    assert action_inputs["skill_prompt"] == {"$context_key": "skill_prompt"}
    assert action_inputs["skill_requested_resources"] == {
        "$context_key": "skill_requested_resources"
    }


def test_execute_skill_direct_runs_via_workflow_runtime(tmp_path: Path) -> None:
    project_root = tmp_path / ".github" / "skills"
    skill_file = _write_skill(project_root)
    result = load_skill_artefact(
        str(skill_file),
        source_scope="project",
        discovery_root=str(project_root),
    )
    assert result.artefact is not None
    fake_llm = _FakeLLM()

    workflow_result = execute_skill_direct(
        result.artefact,
        prompt="JVNAUTOSCI-1360",
        requested_resources=["guide.md"],
        environment=WorkflowEnvironment(
            llm_client=fake_llm,
            model="test-model",
            user_namespace="#V#test_user",
        ),
    )

    assert workflow_result.completed is True
    assert workflow_result.final_state == "execute_skill"
    assert workflow_result.data["skill_response_text"] == "handled:JVNAUTOSCI-1360"
    assert workflow_result.data["skill_execution"]["executed"] is True
    assert workflow_result.data["workflow_completion_gate"]["safe_to_claim_completion"] is True
    assert fake_llm.calls
    system_prompt = fake_llm.calls[0]["context"][0]["content"]  # type: ignore[index]
    assert "Follow the checklist" in system_prompt
    assert "guide.md" in system_prompt


def test_execute_skill_direct_fails_closed_for_automatic_disabled_skill(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / ".github" / "skills"
    skill_file = _write_skill(
        project_root,
        body="Use this skill only when a human invokes it directly.",
    )
    skill_text = skill_file.read_text(encoding="utf-8").replace(
        "disable-model-invocation: false",
        "disable-model-invocation: true",
    )
    skill_file.write_text(skill_text, encoding="utf-8")
    result = load_skill_artefact(
        str(skill_file),
        source_scope="project",
        discovery_root=str(project_root),
    )
    assert result.artefact is not None

    workflow_result = execute_skill_direct(
        result.artefact,
        prompt="auto-run",
        invocation_mode="automatic",
        environment=WorkflowEnvironment(llm_client=_FakeLLM()),
    )

    assert workflow_result.completed is False
    assert workflow_result.error == "skill_requires_manual_invocation"


def test_skill_projection_and_registry_surface_are_available(tmp_path: Path) -> None:
    skill = SkillArtefact(
        name="triage-issue",
        description="Triage an issue deterministically.",
        argument_hint="issue key",
        user_invokable=True,
        disable_model_invocation=False,
        body="Follow the checklist.",
        source_scope="project",
        discovery_root=str(tmp_path),
        skill_directory=str(tmp_path / "triage-issue"),
        skill_file=str(tmp_path / "triage-issue" / "SKILL.md"),
        frontmatter={"name": "triage-issue", "description": "Triage an issue deterministically."},
    )

    projection = build_skill_vontology_projection(skill)

    assert projection["#V#has_skill_name"] == "triage-issue"
    assert projection["#V#has_skill_description"] == "Triage an issue deterministically."
    assert projection["#V#is_user_invokable"] == "true"
    assert build_durable_action_registry().has(SKILL_EXECUTE_ACTION_ID) is True
