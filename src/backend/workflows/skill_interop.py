"""Deterministic interoperability layer for external SKILL artefacts.

Compatible ``SKILL.md`` artefacts are parsed into a constrained, auditable
contract and then either:

1. transpiled into an ordinary VWL definition, or
2. executed immediately by running that transpiled definition through the
   existing workflow runtime.

This keeps SKILL support inside VWL semantics rather than introducing a second
workflow-orchestration subsystem in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .action_registry import (
    ActionRegistry,
    ActionSpec,
    WorkflowActionRequest,
    WorkflowActionResult,
    WorkflowEnvironment,
)
from .engine import (
    WorkflowActionInvocation,
    WorkflowDefinition,
    WorkflowExecutor,
    WorkflowResult,
    WorkflowStateSpec,
)
from .plan_state_runtime import (
    WORKFLOW_COMPLETION_GATE_SCHEMA_VERSION,
    WORKFLOW_PLAN_STATE_POLICY_SCHEMA_VERSION,
    WORKFLOW_STEP_CHECKPOINT_POLICY_SCHEMA_VERSION,
)

SKILL_INTEROP_SCHEMA_VERSION = "workflow_skill_interop.v1"
SKILL_MARKDOWN_DIALECT_SCHEMA_VERSION = "agent_skill_markdown.v1"
SKILL_EXECUTE_ACTION_ID = "skill.execute_markdown"
SKILL_EXECUTE_STATE_ID = "execute_skill"
SKILL_PROMPT_CONTEXT_KEY = "skill_prompt"
SKILL_REQUESTED_RESOURCES_CONTEXT_KEY = "skill_requested_resources"
SKILL_INVOCATION_MODE_CONTEXT_KEY = "skill_invocation_mode"
SKILL_ALLOWED_SOURCE_SCOPES: tuple[str, ...] = (
    "project",
    "personal",
    "extension",
    "shared",
)
SKILL_ALLOWED_INVOCATION_MODES: tuple[str, ...] = ("manual", "automatic")

SKILL_VONTOLOGY_PREDICATE_IDS: tuple[str, ...] = (
    "#V#has_skill_name",
    "#V#has_skill_description",
    "#V#has_skill_argument_hint",
    "#V#is_user_invokable",
    "#V#disables_model_invocation",
    "#V#has_skill_source_scope",
    "#V#has_skill_discovery_location",
)

_SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True)
class SkillValidationIssue:
    reason_code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "reason_code": self.reason_code,
            "message": self.message,
        }
        if self.field:
            payload["field"] = self.field
        return payload


@dataclass(frozen=True)
class SkillValidationReport:
    errors: tuple[SkillValidationIssue, ...] = ()
    warnings: tuple[SkillValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [item.to_dict() for item in self.errors],
            "warnings": [item.to_dict() for item in self.warnings],
        }


@dataclass(frozen=True)
class SkillDiscoveryRecord:
    name: str
    description: str
    argument_hint: str
    user_invokable: bool
    disable_model_invocation: bool
    source_scope: str
    discovery_root: str
    skill_directory: str
    skill_file: str
    dialect_schema_version: str = SKILL_MARKDOWN_DIALECT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "argument_hint": self.argument_hint,
            "user_invokable": self.user_invokable,
            "disable_model_invocation": self.disable_model_invocation,
            "source_scope": self.source_scope,
            "discovery_root": self.discovery_root,
            "skill_directory": self.skill_directory,
            "skill_file": self.skill_file,
            "dialect_schema_version": self.dialect_schema_version,
        }


@dataclass(frozen=True)
class SkillArtefact:
    name: str
    description: str
    argument_hint: str
    user_invokable: bool
    disable_model_invocation: bool
    body: str
    source_scope: str
    discovery_root: str
    skill_directory: str
    skill_file: str
    frontmatter: Mapping[str, Any]
    referenced_resources: tuple[str, ...] = ()
    dialect_schema_version: str = SKILL_MARKDOWN_DIALECT_SCHEMA_VERSION

    def to_discovery_record(self) -> SkillDiscoveryRecord:
        return SkillDiscoveryRecord(
            name=self.name,
            description=self.description,
            argument_hint=self.argument_hint,
            user_invokable=self.user_invokable,
            disable_model_invocation=self.disable_model_invocation,
            source_scope=self.source_scope,
            discovery_root=self.discovery_root,
            skill_directory=self.skill_directory,
            skill_file=self.skill_file,
            dialect_schema_version=self.dialect_schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self.to_discovery_record().to_dict()
        payload.update(
            {
                "body": self.body,
                "frontmatter": dict(self.frontmatter),
                "referenced_resources": list(self.referenced_resources),
            }
        )
        return payload


@dataclass(frozen=True)
class SkillParseResult:
    artefact: SkillArtefact | None
    validation_report: SkillValidationReport


def _normalise_text(value: Any) -> str:
    return str(value or "").strip()


def _normalise_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"1", "true", "yes", "on"}:
            return True
        if token in {"0", "false", "no", "off"}:
            return False
    return default


def _normalise_scope(value: Any) -> str:
    scope = _normalise_text(value).lower()
    if scope in SKILL_ALLOWED_SOURCE_SCOPES:
        return scope
    return "project"


def _normalise_invocation_mode(value: Any) -> str:
    token = _normalise_text(value).lower()
    if token in SKILL_ALLOWED_INVOCATION_MODES:
        return token
    return "manual"


def _coerce_string_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [
        _normalise_text(item)
        for item in value
        if _normalise_text(item)
    ]


def _split_frontmatter(markdown_text: str) -> tuple[str | None, str]:
    text = str(markdown_text or "").replace("\r\n", "\n")
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---\n"):
        return None, text
    lines = text.split("\n")
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            frontmatter = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :]).lstrip("\n")
            return frontmatter, body
    return None, text


def _parse_scalar_frontmatter(frontmatter_text: str) -> tuple[dict[str, Any], list[SkillValidationIssue]]:
    parsed: dict[str, Any] = {}
    errors: list[SkillValidationIssue] = []
    for raw_line in str(frontmatter_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            errors.append(
                SkillValidationIssue(
                    reason_code="skill_frontmatter_line_invalid",
                    message=f"Frontmatter line must contain ':'; got '{raw_line}'.",
                )
            )
            continue
        key, raw_value = line.split(":", 1)
        key_text = _normalise_text(key)
        value_text = raw_value.strip()
        if not key_text:
            errors.append(
                SkillValidationIssue(
                    reason_code="skill_frontmatter_key_missing",
                    message="Frontmatter keys must be non-empty.",
                )
            )
            continue
        if value_text in {"|", ">", "|-", ">-"}:
            errors.append(
                SkillValidationIssue(
                    reason_code="skill_frontmatter_multiline_unsupported",
                    message=(
                        "Supported SKILL frontmatter currently accepts only scalar "
                        "values."
                    ),
                    field=key_text,
                )
            )
            continue
        if (
            len(value_text) >= 2
            and value_text[0] == value_text[-1]
            and value_text[0] in {'"', "'"}
        ):
            value: Any = value_text[1:-1]
        else:
            lowered = value_text.lower()
            if lowered in {"true", "false"}:
                value = lowered == "true"
            else:
                value = value_text
        parsed[key_text] = value
    return parsed, errors


def _collect_referenced_resources(body: str) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for match in _MARKDOWN_LINK_PATTERN.findall(str(body or "")):
        target = _normalise_text(match)
        if not target or "://" in target or target.startswith("#"):
            continue
        if target in seen:
            continue
        seen.add(target)
        ordered.append(target)
    return tuple(ordered)


def parse_skill_markdown(
    markdown_text: str,
    *,
    skill_file: str,
    source_scope: str = "project",
    discovery_root: str | None = None,
) -> SkillParseResult:
    errors: list[SkillValidationIssue] = []
    warnings: list[SkillValidationIssue] = []
    frontmatter_text, body = _split_frontmatter(markdown_text)
    if frontmatter_text is None:
        report = SkillValidationReport(
            errors=(
                SkillValidationIssue(
                    reason_code="skill_frontmatter_missing",
                    message="Supported SKILL artefacts must start with YAML frontmatter.",
                ),
            )
        )
        return SkillParseResult(artefact=None, validation_report=report)

    frontmatter, parse_errors = _parse_scalar_frontmatter(frontmatter_text)
    errors.extend(parse_errors)

    skill_path = Path(skill_file).resolve()
    skill_directory = skill_path.parent
    directory_name = skill_directory.name.strip()
    name = _normalise_text(frontmatter.get("name"))
    description = _normalise_text(frontmatter.get("description"))
    argument_hint = _normalise_text(frontmatter.get("argument-hint"))
    user_invokable = _normalise_bool(
        frontmatter.get("user-invokable", frontmatter.get("user-invocable")),
        default=True,
    )
    disable_model_invocation = _normalise_bool(
        frontmatter.get("disable-model-invocation"),
        default=False,
    )

    if not name:
        errors.append(
            SkillValidationIssue(
                reason_code="skill_name_missing",
                message="Frontmatter field 'name' is required.",
                field="name",
            )
        )
    elif not _SKILL_NAME_PATTERN.fullmatch(name):
        errors.append(
            SkillValidationIssue(
                reason_code="skill_name_invalid",
                message=(
                    "Skill names must be lowercase kebab-case so they map "
                    "deterministically to directory and workflow identities."
                ),
                field="name",
            )
        )
    elif directory_name and name != directory_name:
        errors.append(
            SkillValidationIssue(
                reason_code="skill_name_directory_mismatch",
                message="Frontmatter 'name' must match the parent skill directory.",
                field="name",
            )
        )

    if not description:
        errors.append(
            SkillValidationIssue(
                reason_code="skill_description_missing",
                message="Frontmatter field 'description' is required.",
                field="description",
            )
        )

    body_text = str(body or "").strip()
    if not body_text:
        errors.append(
            SkillValidationIssue(
                reason_code="skill_body_missing",
                message="Supported SKILL artefacts must contain instruction body text.",
            )
        )

    recognised_fields = {
        "name",
        "description",
        "argument-hint",
        "user-invokable",
        "user-invocable",
        "disable-model-invocation",
    }
    for field_name in frontmatter:
        if field_name in recognised_fields:
            continue
        warnings.append(
            SkillValidationIssue(
                reason_code="skill_frontmatter_field_ignored",
                message=f"Unsupported frontmatter field '{field_name}' was ignored.",
                field=field_name,
            )
        )

    report = SkillValidationReport(
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    if not report.valid:
        return SkillParseResult(artefact=None, validation_report=report)

    artefact = SkillArtefact(
        name=name,
        description=description,
        argument_hint=argument_hint,
        user_invokable=user_invokable,
        disable_model_invocation=disable_model_invocation,
        body=body_text,
        source_scope=_normalise_scope(source_scope),
        discovery_root=str(
            Path(discovery_root).resolve() if discovery_root else skill_directory.parent
        ),
        skill_directory=str(skill_directory),
        skill_file=str(skill_path),
        frontmatter=dict(frontmatter),
        referenced_resources=_collect_referenced_resources(body_text),
    )
    return SkillParseResult(artefact=artefact, validation_report=report)


def load_skill_artefact(
    skill_file: str,
    *,
    source_scope: str = "project",
    discovery_root: str | None = None,
) -> SkillParseResult:
    text = Path(skill_file).read_text(encoding="utf-8")
    return parse_skill_markdown(
        text,
        skill_file=skill_file,
        source_scope=source_scope,
        discovery_root=discovery_root,
    )


def discover_skill_catalogue(
    roots_by_scope: Mapping[str, Sequence[str] | str],
) -> list[SkillDiscoveryRecord]:
    discovered: list[SkillDiscoveryRecord] = []
    seen_files: set[str] = set()
    for raw_scope, raw_roots in roots_by_scope.items():
        scope = _normalise_scope(raw_scope)
        if isinstance(raw_roots, str):
            scope_roots: list[str] = [raw_roots]
        elif isinstance(raw_roots, Sequence):
            scope_roots = [str(item) for item in raw_roots]
        else:
            scope_roots = []
        for root in scope_roots:
            root_path = Path(root).resolve()
            if not root_path.exists():
                continue
            for skill_path in root_path.rglob("SKILL.md"):
                skill_file = str(skill_path.resolve())
                if skill_file in seen_files:
                    continue
                seen_files.add(skill_file)
                result = load_skill_artefact(
                    skill_file,
                    source_scope=scope,
                    discovery_root=str(root_path),
                )
                if result.artefact is None:
                    continue
                discovered.append(result.artefact.to_discovery_record())
    discovered.sort(key=lambda item: (item.source_scope, item.name, item.skill_file))
    return discovered


def _safe_skill_resource_path(skill: SkillArtefact, relative_path: str) -> Path:
    candidate = (Path(skill.skill_directory) / relative_path).resolve()
    skill_root = Path(skill.skill_directory).resolve()
    try:
        candidate.relative_to(skill_root)
    except ValueError as exc:
        raise ValueError("skill_resource_outside_skill_directory") from exc
    return candidate


def load_skill_resource(skill: SkillArtefact, relative_path: str) -> str:
    candidate = _safe_skill_resource_path(skill, relative_path)
    if not candidate.exists() or not candidate.is_file():
        raise ValueError("skill_resource_missing")
    return candidate.read_text(encoding="utf-8")


def build_skill_vontology_projection(skill: SkillArtefact) -> dict[str, str]:
    projection = {
        "#V#has_skill_name": skill.name,
        "#V#has_skill_description": skill.description,
        "#V#is_user_invokable": str(skill.user_invokable).lower(),
        "#V#disables_model_invocation": str(skill.disable_model_invocation).lower(),
        "#V#has_skill_source_scope": skill.source_scope,
        "#V#has_skill_discovery_location": skill.skill_file,
    }
    if skill.argument_hint:
        projection["#V#has_skill_argument_hint"] = skill.argument_hint
    return projection


def build_skill_interop_metadata(skill: SkillArtefact) -> dict[str, Any]:
    return {
        "schema_version": SKILL_INTEROP_SCHEMA_VERSION,
        "dialect_schema_version": skill.dialect_schema_version,
        "skill_name": skill.name,
        "source_scope": skill.source_scope,
        "provenance": {
            "discovery_root": skill.discovery_root,
            "skill_directory": skill.skill_directory,
            "skill_file": skill.skill_file,
            "referenced_resources": list(skill.referenced_resources),
        },
        "vontology_projection": build_skill_vontology_projection(skill),
    }


def transpile_skill_to_workflow_definition(
    skill: SkillArtefact,
    *,
    workflow_id: str | None = None,
) -> WorkflowDefinition:
    workflow_id_value = _normalise_text(workflow_id)
    if not workflow_id_value:
        workflow_id_value = f"#V#skill_{skill.name.replace('-', '_')}_workflow"
    skill_contract = build_skill_interop_metadata(skill)
    metadata = {
        "skill_interop_contract": skill_contract,
    }
    metadata["plan_state_policy"] = {
        "schema_version": WORKFLOW_PLAN_STATE_POLICY_SCHEMA_VERSION,
        "plan_items": [SKILL_EXECUTE_STATE_ID],
    }
    metadata["completion_gate"] = {
        "schema_version": WORKFLOW_COMPLETION_GATE_SCHEMA_VERSION,
        "required_done_plan_items": [SKILL_EXECUTE_STATE_ID],
        "required_context_keys": ["skill_execution.executed"],
    }
    state_metadata = {
        "checkpoint_policy": {
            "schema_version": WORKFLOW_STEP_CHECKPOINT_POLICY_SCHEMA_VERSION,
            "plan_item_updates": [
                {"item_id": SKILL_EXECUTE_STATE_ID, "status": "done"}
            ],
            "progress_message": f"Executing skill {skill.name}",
        },
        "skill_interop_contract": skill_contract,
    }
    return WorkflowDefinition(
        workflow_id=workflow_id_value,
        initial_state=SKILL_EXECUTE_STATE_ID,
        states={
            SKILL_EXECUTE_STATE_ID: WorkflowStateSpec(
                state_id=SKILL_EXECUTE_STATE_ID,
                actions=(
                    WorkflowActionInvocation(
                        action_id=SKILL_EXECUTE_ACTION_ID,
                        inputs={
                            "skill_name": skill.name,
                            "skill_description": skill.description,
                            "skill_argument_hint": skill.argument_hint,
                            "skill_body": skill.body,
                            "skill_user_invokable": skill.user_invokable,
                            "skill_disable_model_invocation": (
                                skill.disable_model_invocation
                            ),
                            "skill_source_scope": skill.source_scope,
                            "skill_discovery_root": skill.discovery_root,
                            "skill_directory": skill.skill_directory,
                            "skill_file": skill.skill_file,
                            "skill_referenced_resources": list(
                                skill.referenced_resources
                            ),
                            "skill_frontmatter": dict(skill.frontmatter),
                            "skill_prompt": {
                                "$context_key": SKILL_PROMPT_CONTEXT_KEY
                            },
                            "skill_requested_resources": {
                                "$context_key": SKILL_REQUESTED_RESOURCES_CONTEXT_KEY
                            },
                            "skill_invocation_mode": {
                                "$context_key": SKILL_INVOCATION_MODE_CONTEXT_KEY
                            },
                        },
                    ),
                ),
                terminal=True,
                metadata=state_metadata,
            )
        },
        termination_states=(SKILL_EXECUTE_STATE_ID,),
        purpose=skill.description,
        metadata=metadata,
    )


def _build_skill_system_prompt(
    *,
    skill: SkillArtefact,
    loaded_resources: Mapping[str, str],
) -> str:
    sections = [
        f"Skill name: {skill.name}",
        f"Skill description: {skill.description}",
        "Skill instructions:",
        skill.body,
    ]
    if skill.argument_hint:
        sections.extend(
            [
                "Argument hint:",
                skill.argument_hint,
            ]
        )
    if loaded_resources:
        sections.append("Loaded skill resources:")
        for resource_path, resource_text in loaded_resources.items():
            sections.extend(
                [
                    f"Resource: {resource_path}",
                    resource_text,
                ]
            )
    return "\n\n".join(section for section in sections if _normalise_text(section))


def execute_markdown_skill_action(request: WorkflowActionRequest) -> WorkflowActionResult:
    inputs = request.inputs
    skill = SkillArtefact(
        name=_normalise_text(inputs.get("skill_name")),
        description=_normalise_text(inputs.get("skill_description")),
        argument_hint=_normalise_text(inputs.get("skill_argument_hint")),
        user_invokable=_normalise_bool(
            inputs.get("skill_user_invokable"),
            default=True,
        ),
        disable_model_invocation=_normalise_bool(
            inputs.get("skill_disable_model_invocation"),
            default=False,
        ),
        body=_normalise_text(inputs.get("skill_body")),
        source_scope=_normalise_scope(inputs.get("skill_source_scope")),
        discovery_root=_normalise_text(inputs.get("skill_discovery_root")),
        skill_directory=_normalise_text(inputs.get("skill_directory")),
        skill_file=_normalise_text(inputs.get("skill_file")),
        frontmatter=_coerce_string_mapping(inputs.get("skill_frontmatter")),
        referenced_resources=tuple(
            _coerce_string_list(inputs.get("skill_referenced_resources"))
        ),
    )
    if not skill.body:
        return WorkflowActionResult(
            status="failed",
            error="skill_body_missing",
        )

    invocation_mode = _normalise_invocation_mode(inputs.get("skill_invocation_mode"))
    if skill.disable_model_invocation and invocation_mode == "automatic":
        return WorkflowActionResult(
            status="failed",
            error="skill_requires_manual_invocation",
        )

    llm_client = request.environment.llm_client
    if llm_client is None:
        return WorkflowActionResult(
            status="failed",
            error="skill_llm_client_unavailable",
        )

    requested_resources = [
        _normalise_text(item)
        for item in (inputs.get("skill_requested_resources") or [])
        if _normalise_text(item)
    ]
    loaded_resources: dict[str, str] = {}
    for resource_path in requested_resources:
        try:
            loaded_resources[resource_path] = load_skill_resource(skill, resource_path)
        except ValueError as exc:
            return WorkflowActionResult(
                status="failed",
                error=f"skill_resource_load_failed:{resource_path}:{exc}",
            )

    prompt = _normalise_text(inputs.get("skill_prompt")) or (
        skill.argument_hint or "Follow the skill instructions."
    )
    system_prompt = _build_skill_system_prompt(
        skill=skill,
        loaded_resources=loaded_resources,
    )
    response_text = llm_client.generate(
        prompt,
        context=[{"role": "system", "content": system_prompt}],
        model=request.environment.model,
    )
    execution_summary = {
        "executed": True,
        "schema_version": SKILL_INTEROP_SCHEMA_VERSION,
        "skill_name": skill.name,
        "invocation_mode": invocation_mode,
        "source_scope": skill.source_scope,
        "discovery_root": skill.discovery_root,
        "skill_directory": skill.skill_directory,
        "skill_file": skill.skill_file,
        "requested_resources": requested_resources,
        "loaded_resources": sorted(loaded_resources.keys()),
        "referenced_resources": list(skill.referenced_resources),
    }
    return WorkflowActionResult(
        outputs={
            "skill_response_text": response_text,
            "skill_execution": execution_summary,
            "skill_vontology_projection": build_skill_vontology_projection(skill),
        }
    )


def register_skill_interop_actions(registry: ActionRegistry) -> None:
    registry.register_if_absent(
        ActionSpec(
            action_id=SKILL_EXECUTE_ACTION_ID,
            handler=execute_markdown_skill_action,
            description=(
                "Execute a compatible markdown SKILL artefact through the VWL "
                "runtime."
            ),
        )
    )


def execute_skill_direct(
    skill: SkillArtefact,
    *,
    prompt: str,
    environment: WorkflowEnvironment,
    registry: ActionRegistry | None = None,
    workflow_id: str | None = None,
    data: Mapping[str, Any] | None = None,
    requested_resources: Sequence[str] | None = None,
    invocation_mode: str = "manual",
    max_transitions: int = 5,
) -> WorkflowResult:
    action_registry = registry or ActionRegistry()
    register_skill_interop_actions(action_registry)
    definition = transpile_skill_to_workflow_definition(
        skill,
        workflow_id=workflow_id,
    )
    context = dict(data or {})
    context[SKILL_PROMPT_CONTEXT_KEY] = prompt
    context[SKILL_REQUESTED_RESOURCES_CONTEXT_KEY] = [
        _normalise_text(item)
        for item in (requested_resources or [])
        if _normalise_text(item)
    ]
    context[SKILL_INVOCATION_MODE_CONTEXT_KEY] = _normalise_invocation_mode(
        invocation_mode
    )
    return WorkflowExecutor(
        registry=action_registry,
        max_transitions=max_transitions,
    ).run(
        definition,
        environment=environment,
        data=context,
    )


__all__ = [
    "SKILL_ALLOWED_INVOCATION_MODES",
    "SKILL_ALLOWED_SOURCE_SCOPES",
    "SKILL_EXECUTE_ACTION_ID",
    "SKILL_EXECUTE_STATE_ID",
    "SKILL_INTEROP_SCHEMA_VERSION",
    "SKILL_INVOCATION_MODE_CONTEXT_KEY",
    "SKILL_MARKDOWN_DIALECT_SCHEMA_VERSION",
    "SKILL_PROMPT_CONTEXT_KEY",
    "SKILL_REQUESTED_RESOURCES_CONTEXT_KEY",
    "SKILL_VONTOLOGY_PREDICATE_IDS",
    "SkillArtefact",
    "SkillDiscoveryRecord",
    "SkillParseResult",
    "SkillValidationIssue",
    "SkillValidationReport",
    "build_skill_interop_metadata",
    "build_skill_vontology_projection",
    "discover_skill_catalogue",
    "execute_markdown_skill_action",
    "execute_skill_direct",
    "load_skill_artefact",
    "load_skill_resource",
    "parse_skill_markdown",
    "register_skill_interop_actions",
    "transpile_skill_to_workflow_definition",
]
