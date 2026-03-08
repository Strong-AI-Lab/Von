from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Sequence


MARKER_COST_NORMAL = "cost_normal"
MARKER_COST_HEAVY = "cost_heavy"
MARKER_MANUAL_ONLY = "manual_only"
MARKER_TRAIT_EXTERNAL_LIKE = "trait_external_like"
MARKER_TRAIT_INTEGRATION_SURFACE = "trait_integration_surface"


@dataclass(frozen=True)
class SuiteDefinition:
    suite_id: str
    description: str
    cost_profile: str
    required_markers: tuple[str, ...]
    excluded_markers: tuple[str, ...] = ()
    aggregate_order: int | None = None
    automated: bool = True
    kind: str = "primary"

    @property
    def marker_expression(self) -> str:
        parts = list(self.required_markers)
        parts.extend(f"not {marker}" for marker in self.excluded_markers)
        return " and ".join(parts)


@dataclass(frozen=True)
class SourceAreaRule:
    prefixes: tuple[str, ...]
    primary_suites: tuple[str, ...]
    normal_overlay_suites: tuple[str, ...] = ()
    high_overlay_suites: tuple[str, ...] = ()


@dataclass(frozen=True)
class TestClassification:
    path: str
    primary_suite_id: str
    markers: tuple[str, ...]


@dataclass(frozen=True)
class Recommendation:
    changed_paths: tuple[str, ...]
    direct_test_targets: tuple[str, ...]
    primary_suites: tuple[str, ...]
    overlay_suites: tuple[str, ...]
    notes: tuple[str, ...]


LANE_BACKEND_CORE = "backend-core"
LANE_BACKEND_ROUTES = "backend-routes"
LANE_BACKEND_MCP = "backend-mcp"
LANE_BACKEND_WORKFLOWS = "backend-workflows"
LANE_INFRA = "infra"
LANE_MANUAL = "manual"

SUITE_BACKEND_INTEGRATION = "backend-integration"
SUITE_BACKEND_EXTERNAL_LIKE = "backend-external-like"
SUITE_BACKEND_HEAVY = "backend-heavy"

SUITES: dict[str, SuiteDefinition] = {
    LANE_BACKEND_CORE: SuiteDefinition(
        suite_id=LANE_BACKEND_CORE,
        description="Core backend services, utilities, models, and general regressions.",
        cost_profile="normal",
        required_markers=("lane_backend_core",),
        aggregate_order=10,
    ),
    LANE_BACKEND_ROUTES: SuiteDefinition(
        suite_id=LANE_BACKEND_ROUTES,
        description="HTTP routes, endpoints, and request/response surface regressions.",
        cost_profile="normal",
        required_markers=("lane_backend_routes",),
        aggregate_order=20,
    ),
    LANE_BACKEND_MCP: SuiteDefinition(
        suite_id=LANE_BACKEND_MCP,
        description="Internal MCP, stdio server, orchestrator, and tool wiring regressions.",
        cost_profile="heavy",
        required_markers=("lane_backend_mcp",),
        aggregate_order=30,
    ),
    LANE_BACKEND_WORKFLOWS: SuiteDefinition(
        suite_id=LANE_BACKEND_WORKFLOWS,
        description="Workflow execution, scheduling, transition, and durable state regressions.",
        cost_profile="heavy",
        required_markers=("lane_backend_workflows",),
        aggregate_order=40,
    ),
    LANE_INFRA: SuiteDefinition(
        suite_id=LANE_INFRA,
        description="Infrastructure and deployment-oriented pytest coverage.",
        cost_profile="normal",
        required_markers=("lane_infra",),
        aggregate_order=50,
    ),
    LANE_MANUAL: SuiteDefinition(
        suite_id=LANE_MANUAL,
        description="Manual-only pytest coverage that is excluded from automated aggregate lanes.",
        cost_profile="manual",
        required_markers=(MARKER_MANUAL_ONLY,),
        automated=False,
        kind="primary",
    ),
    SUITE_BACKEND_INTEGRATION: SuiteDefinition(
        suite_id=SUITE_BACKEND_INTEGRATION,
        description="Broader call-path and end-to-end style coverage across backend lanes.",
        cost_profile="heavy",
        required_markers=(MARKER_TRAIT_INTEGRATION_SURFACE,),
        excluded_markers=(MARKER_MANUAL_ONLY,),
        kind="overlay",
    ),
    SUITE_BACKEND_EXTERNAL_LIKE: SuiteDefinition(
        suite_id=SUITE_BACKEND_EXTERNAL_LIKE,
        description="Tests that touch external-like boundaries such as Jira, Gmail, GitHub, arXiv, or stdio.",
        cost_profile="heavy",
        required_markers=(MARKER_TRAIT_EXTERNAL_LIKE,),
        excluded_markers=(MARKER_MANUAL_ONLY,),
        kind="overlay",
    ),
    SUITE_BACKEND_HEAVY: SuiteDefinition(
        suite_id=SUITE_BACKEND_HEAVY,
        description="Large or slower suites that are best run as a dedicated lane.",
        cost_profile="heavy",
        required_markers=(MARKER_COST_HEAVY,),
        excluded_markers=(MARKER_MANUAL_ONLY, "lane_infra"),
        kind="overlay",
    ),
}

PRIMARY_LANE_IDS: tuple[str, ...] = (
    LANE_BACKEND_CORE,
    LANE_BACKEND_ROUTES,
    LANE_BACKEND_MCP,
    LANE_BACKEND_WORKFLOWS,
    LANE_INFRA,
    LANE_MANUAL,
)

AUTOMATED_AGGREGATE_LANE_IDS: tuple[str, ...] = tuple(
    suite.suite_id
    for suite in sorted(
        (definition for definition in SUITES.values() if definition.aggregate_order is not None),
        key=lambda definition: definition.aggregate_order or 0,
    )
)

MARKER_DESCRIPTIONS: dict[str, str] = {
    "lane_backend_core": "Primary aggregate shard for core backend and general regression tests.",
    "lane_backend_routes": "Primary aggregate shard for route and endpoint regressions.",
    "lane_backend_mcp": "Primary aggregate shard for MCP, stdio, orchestrator, and tool regressions.",
    "lane_backend_workflows": "Primary aggregate shard for workflow and durable execution regressions.",
    "lane_infra": "Primary aggregate shard for infrastructure and deployment pytest coverage.",
    MARKER_COST_NORMAL: "Expected to fit normal interactive execution budgets.",
    MARKER_COST_HEAVY: "Large or slower suites that should be run as a dedicated lane.",
    MARKER_MANUAL_ONLY: "Manual-only coverage that is excluded from automated aggregate lanes.",
    MARKER_TRAIT_INTEGRATION_SURFACE: "Broader call-path or end-to-end style test coverage.",
    MARKER_TRAIT_EXTERNAL_LIKE: "Touches external-like boundaries such as Jira, Gmail, GitHub, arXiv, stdio, or deployment surfaces.",
}

SOURCE_AREA_RULES: tuple[SourceAreaRule, ...] = (
    SourceAreaRule(
        prefixes=(
            "src/backend/integrations/internal_mcp/",
            "src/backend/mcp_server/",
            "scripts/query_vontology_mcp.py",
            "scripts/query_vonrag_mcp.py",
        ),
        primary_suites=(LANE_BACKEND_MCP,),
        high_overlay_suites=(SUITE_BACKEND_HEAVY, SUITE_BACKEND_EXTERNAL_LIKE),
    ),
    SourceAreaRule(
        prefixes=("src/backend/workflows/",),
        primary_suites=(LANE_BACKEND_WORKFLOWS,),
        high_overlay_suites=(SUITE_BACKEND_HEAVY, SUITE_BACKEND_INTEGRATION),
    ),
    SourceAreaRule(
        prefixes=(
            "src/backend/server/routes/",
            "src/backend/server/utils_flask.py",
            "src/backend/auth_service.py",
        ),
        primary_suites=(LANE_BACKEND_ROUTES,),
        normal_overlay_suites=(SUITE_BACKEND_INTEGRATION,),
        high_overlay_suites=(SUITE_BACKEND_HEAVY,),
    ),
    SourceAreaRule(
        prefixes=("src/backend/integrations/",),
        primary_suites=(LANE_BACKEND_CORE,),
        normal_overlay_suites=(SUITE_BACKEND_EXTERNAL_LIKE,),
        high_overlay_suites=(SUITE_BACKEND_HEAVY,),
    ),
    SourceAreaRule(
        prefixes=(
            "src/backend/",
            "scripts/",
            "pyproject.toml",
            "tests/conftest.py",
        ),
        primary_suites=(LANE_BACKEND_CORE,),
    ),
    SourceAreaRule(
        prefixes=("tests/infra/",),
        primary_suites=(LANE_INFRA,),
    ),
    SourceAreaRule(
        prefixes=("tests/manual/",),
        primary_suites=(LANE_MANUAL,),
    ),
)

_GENERIC_TOKENS = {
    "test",
    "tests",
    "src",
    "backend",
    "frontend",
    "python",
    "py",
}
_SUFFIX_VARIANTS = ("_service", "_workflow", "_workflows", "_routes", "_route", "_endpoint")

_MCP_EXACT_STEMS = {
    "test_file_copy_ingestion_mcp_tools",
    "test_finalise_cached_paper_tool",
    "test_read_file_copy_tool",
    "test_recent_screenshots_tool",
    "test_tool_progress_liveness",
}
_INTEGRATION_EXACT_STEMS = {
    "test_semantic_search_integration",
    "test_von_generate_presenter_protocol",
    "test_von_generate_rag_trace",
    "test_von_generate_render_plan_debug",
}


def normalise_repo_path(path: str | Path, repo_root: Path | None = None) -> str:
    text = str(path).strip().replace("\\", "/")
    if repo_root is not None:
        candidate = Path(text)
        if candidate.is_absolute():
            try:
                text = candidate.resolve().relative_to(repo_root.resolve()).as_posix()
            except ValueError:
                pass
    while text.startswith("./"):
        text = text[2:]
    return PurePosixPath(text).as_posix()


def iter_registered_markers() -> tuple[tuple[str, str], ...]:
    return tuple(sorted(MARKER_DESCRIPTIONS.items()))


def list_python_test_files(repo_root: Path) -> list[str]:
    tests_root = repo_root / "tests"
    if not tests_root.exists():
        return []
    return sorted(
        normalise_repo_path(path.relative_to(repo_root))
        for path in tests_root.rglob("test_*.py")
    )


def classify_test_path(path: str | Path) -> TestClassification | None:
    normalised = normalise_repo_path(path)
    file_name = PurePosixPath(normalised).name
    if (
        not normalised.startswith("tests/")
        or not normalised.endswith(".py")
        or not file_name.startswith("test_")
    ):
        return None

    stem = PurePosixPath(normalised).stem.lower()
    markers: set[str] = set()

    if normalised.startswith("tests/manual/"):
        primary_suite = LANE_MANUAL
    elif normalised.startswith("tests/infra/"):
        primary_suite = LANE_INFRA
    elif _is_route_test(stem):
        primary_suite = LANE_BACKEND_ROUTES
    elif _is_mcp_test(stem):
        primary_suite = LANE_BACKEND_MCP
    elif _is_workflow_test(stem):
        primary_suite = LANE_BACKEND_WORKFLOWS
    else:
        primary_suite = LANE_BACKEND_CORE

    markers.update(SUITES[primary_suite].required_markers)

    if primary_suite == LANE_MANUAL:
        markers.add(MARKER_MANUAL_ONLY)

    if _is_integration_surface_test(stem):
        markers.add(MARKER_TRAIT_INTEGRATION_SURFACE)

    if _is_external_like_test(stem) or primary_suite == LANE_INFRA:
        markers.add(MARKER_TRAIT_EXTERNAL_LIKE)

    if (
        primary_suite in {LANE_BACKEND_MCP, LANE_BACKEND_WORKFLOWS}
        or MARKER_TRAIT_EXTERNAL_LIKE in markers
        or MARKER_TRAIT_INTEGRATION_SURFACE in markers
    ):
        markers.add(MARKER_COST_HEAVY)
    elif MARKER_MANUAL_ONLY not in markers:
        markers.add(MARKER_COST_NORMAL)

    return TestClassification(
        path=normalised,
        primary_suite_id=primary_suite,
        markers=tuple(sorted(markers)),
    )


def aggregate_lane_ids(include_manual: bool = False) -> tuple[str, ...]:
    if include_manual:
        return AUTOMATED_AGGREGATE_LANE_IDS + (LANE_MANUAL,)
    return AUTOMATED_AGGREGATE_LANE_IDS


def count_suite_members(repo_root: Path) -> dict[str, int]:
    counts = {suite_id: 0 for suite_id in SUITES}
    for test_path in list_python_test_files(repo_root):
        classification = classify_test_path(test_path)
        if classification is None:
            continue
        marker_set = set(classification.markers)
        for suite_id, definition in SUITES.items():
            if _markers_match_suite(marker_set, definition):
                counts[suite_id] += 1
    return counts


def recommend_for_changed_paths(
    repo_root: Path,
    changed_paths: Sequence[str | Path],
    risk: str = "normal",
) -> Recommendation:
    if risk not in {"smoke", "normal", "high"}:
        raise ValueError(f"Unsupported risk profile: {risk}")

    normalised_paths = tuple(
        dict.fromkeys(
            normalise_repo_path(path, repo_root=repo_root)
            for path in changed_paths
            if str(path).strip()
        )
    )

    direct_targets: set[str] = set()
    primary_suites: set[str] = set()
    overlay_suites: set[str] = set()
    notes: set[str] = set()
    python_tests = list_python_test_files(repo_root)

    for changed_path in normalised_paths:
        if changed_path.startswith("tests/frontend/"):
            notes.add(
                "Frontend test changes are outside pytest lanes; use `npm run test:frontend` for those files."
            )
            continue

        classification = classify_test_path(changed_path)
        if classification is not None:
            direct_targets.add(changed_path)
            if classification.primary_suite_id != LANE_MANUAL:
                primary_suites.add(classification.primary_suite_id)
                if risk != "smoke":
                    overlay_suites.update(_overlay_suites_for_markers(classification.markers))
            continue

        rule = _match_source_area_rule(changed_path)
        if rule is not None:
            primary_suites.update(rule.primary_suites)
            if risk in {"normal", "high"}:
                overlay_suites.update(rule.normal_overlay_suites)
            if risk == "high":
                overlay_suites.update(rule.high_overlay_suites)

        direct_targets.update(_infer_related_python_tests(changed_path, python_tests))

    if risk == "smoke" and direct_targets:
        primary_suites.clear()
        overlay_suites.clear()

    if not direct_targets and not primary_suites:
        notes.add(
            "No backend pytest targets were inferred from the supplied paths; review the aggregate lane plan if the change still affects Python behaviour."
        )

    return Recommendation(
        changed_paths=normalised_paths,
        direct_test_targets=tuple(sorted(direct_targets)),
        primary_suites=tuple(sorted(primary_suites, key=_suite_sort_key)),
        overlay_suites=tuple(sorted(overlay_suites, key=_suite_sort_key)),
        notes=tuple(sorted(notes)),
    )


def get_git_changed_paths(repo_root: Path, base_ref: str = "origin/main") -> list[str]:
    changed_paths: list[str] = []
    changed_paths.extend(
        _run_git_name_only(
            repo_root,
            ["diff", "--name-only", "--diff-filter=ACMRTUXB", f"{base_ref}...HEAD"],
        )
    )
    changed_paths.extend(
        _run_git_name_only(repo_root, ["diff", "--name-only", "--diff-filter=ACMRTUXB", "--cached"])
    )
    changed_paths.extend(
        _run_git_name_only(repo_root, ["diff", "--name-only", "--diff-filter=ACMRTUXB"])
    )
    changed_paths.extend(
        _run_git_name_only(repo_root, ["ls-files", "--others", "--exclude-standard"])
    )
    return list(dict.fromkeys(changed_paths))


def build_pytest_command_for_suite(suite_id: str, extra_args: Sequence[str] = ()) -> list[str]:
    definition = SUITES[suite_id]
    return ["pdm", "run", "pytest", "tests", "-m", definition.marker_expression, "-q", *extra_args]


def build_pytest_command_for_targets(targets: Sequence[str], extra_args: Sequence[str] = ()) -> list[str]:
    ordered_targets = [normalise_repo_path(path) for path in targets]
    return ["pdm", "run", "pytest", *ordered_targets, "-q", *extra_args]


def render_command(command: Sequence[str]) -> str:
    return " ".join(_quote_argument(part) for part in command)


def _suite_sort_key(suite_id: str) -> tuple[int, str]:
    definition = SUITES[suite_id]
    if definition.aggregate_order is not None:
        return (definition.aggregate_order, suite_id)
    return (10_000, suite_id)


def _match_source_area_rule(path: str) -> SourceAreaRule | None:
    for rule in SOURCE_AREA_RULES:
        if any(path == prefix or path.startswith(prefix) for prefix in rule.prefixes):
            return rule
    return None


def _markers_match_suite(markers: set[str], definition: SuiteDefinition) -> bool:
    return set(definition.required_markers).issubset(markers) and markers.isdisjoint(
        definition.excluded_markers
    )


def _overlay_suites_for_markers(markers: Sequence[str]) -> tuple[str, ...]:
    marker_set = set(markers)
    overlay_ids = [
        suite_id
        for suite_id, definition in SUITES.items()
        if definition.kind == "overlay" and _markers_match_suite(marker_set, definition)
    ]
    return tuple(sorted(overlay_ids, key=_suite_sort_key))


def _infer_related_python_tests(changed_path: str, python_tests: Sequence[str]) -> tuple[str, ...]:
    if not changed_path.endswith(".py") or changed_path.startswith("tests/"):
        return ()

    stem = PurePosixPath(changed_path).stem.lower()
    normalised_stem = _normalise_stem(stem)
    variants = {normalised_stem}
    for suffix in _SUFFIX_VARIANTS:
        if normalised_stem.endswith(suffix):
            variants.add(normalised_stem[: -len(suffix)])

    significant_tokens = {
        token
        for token in _split_tokens(normalised_stem)
        if token and token not in _GENERIC_TOKENS and len(token) > 2
    }

    scored_candidates: list[tuple[int, str]] = []
    for test_path in python_tests:
        test_stem = PurePosixPath(test_path).stem.lower()
        normalised_test_stem = _normalise_stem(test_stem)
        score = 0

        if any(variant and (variant in normalised_test_stem or normalised_test_stem in variant) for variant in variants):
            score += 100

        test_tokens = {
            token
            for token in _split_tokens(normalised_test_stem)
            if token and token not in _GENERIC_TOKENS and len(token) > 2
        }
        overlap = significant_tokens & test_tokens
        if significant_tokens:
            overlap_ratio = len(overlap) / len(significant_tokens)
            if overlap_ratio >= 0.75:
                score += 35
            elif overlap_ratio >= 0.5:
                score += 20
            elif overlap_ratio >= 0.34:
                score += 10

        if score >= 40:
            scored_candidates.append((score, test_path))

    scored_candidates.sort(key=lambda item: (-item[0], item[1]))
    return tuple(path for _, path in scored_candidates[:8])


def _normalise_stem(stem: str) -> str:
    if stem.startswith("test_"):
        stem = stem[5:]
    return stem.replace("-", "_")


def _split_tokens(stem: str) -> tuple[str, ...]:
    return tuple(token for token in re.split(r"[^a-z0-9]+", stem.lower()) if token)


def _is_route_test(stem: str) -> bool:
    return (
        any(token in stem for token in ("route", "routes", "endpoint"))
        or stem.startswith("test_von_generate_")
        or stem.startswith("test_von_file_upload_")
        or stem.startswith("test_von_history_")
    )


def _is_mcp_test(stem: str) -> bool:
    return (
        stem.startswith("test_internal_mcp_")
        or stem.startswith("test_mcp_")
        or stem.startswith("test_orchestrator_")
        or "_mcp_" in stem
        or "stdio" in stem
        or "catalogue" in stem
        or stem in _MCP_EXACT_STEMS
    )


def _is_workflow_test(stem: str) -> bool:
    return any(
        token in stem
        for token in (
            "workflow",
            "workflows",
            "subworkflow",
            "planning",
            "rumination",
            "turn_execution",
            "todo_refresh",
            "parent_specificity",
        )
    )


def _is_integration_surface_test(stem: str) -> bool:
    return (
        "integration" in stem
        or "e2e" in stem
        or stem.startswith("test_von_generate_")
        or stem.startswith("test_von_file_upload_")
        or stem in _INTEGRATION_EXACT_STEMS
        or "gateway_e2e" in stem
        or "stdio" in stem
    )


def _is_external_like_test(stem: str) -> bool:
    return any(
        token in stem
        for token in (
            "jira",
            "github",
            "gmail",
            "arxiv",
            "google_oauth",
            "openstack",
            "office_document",
            "semantic_search",
            "scholarly",
            "room_device",
            "linkedin",
            "process_guard",
            "stdio",
            "ollama",
        )
    )


def _quote_argument(argument: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=+-]+", argument):
        return argument
    return "'" + argument.replace("'", "''") + "'"


def _run_git_name_only(repo_root: Path, git_args: Sequence[str]) -> list[str]:
    result = subprocess.run(
        ["git", *git_args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]
