from __future__ import annotations

import subprocess
from types import SimpleNamespace


def test_repo_dossier_file_snapshot_blocks_env_paths():
    from src.backend.services import repo_dossier_service as svc

    result = svc.repo_dossier_file_snapshot(path=".env")

    assert result["success"] is False
    assert result["error"] == "path_blocked"


def test_repo_dossier_file_snapshot_sanitises_secret_assignment(monkeypatch):
    from src.backend.services import repo_dossier_service as svc

    monkeypatch.setattr(
        svc,
        "_normalise_repo_relative_path",
        lambda value, field_name: "src/backend/demo.py",
    )
    monkeypatch.setattr(svc, "_ensure_tracked_file", lambda path: "blob-sha-1")
    monkeypatch.setattr(
        svc,
        "_read_worktree_text",
        lambda path: "OPENAI_API_KEY=sk-real-secret\nprint('ok')\n",
    )

    result = svc.repo_dossier_file_snapshot(path="src/backend/demo.py")

    assert result["success"] is True
    assert result["sanitised"] is True
    assert "[REDACTED]" in result["content"]
    assert "sk-real-secret" not in result["content"]


def test_repo_dossier_search_returns_bounded_matches_and_receipt(monkeypatch):
    from src.backend.services import repo_dossier_service as svc

    monkeypatch.setattr(
        svc,
        "_normalise_repo_relative_paths",
        lambda value, field_name: ["src/backend"],
    )
    monkeypatch.setattr(
        svc,
        "_get_tracked_paths",
        lambda prefixes=(): ["src/backend/demo.py"],
    )
    monkeypatch.setattr(
        svc,
        "_stream_git_lines",
        lambda args, allowed_returncodes, max_lines: (
            ["src/backend/demo.py:12:api_token='live-token'"],
            False,
        ),
    )
    monkeypatch.setattr(
        svc,
        "_get_tracked_blob_map",
        lambda paths: {"src/backend/demo.py": "blob-sha-1"},
    )

    def _fake_run_git(args, allowed_returncodes=(0,)):
        if "--count" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="src/backend/demo.py:3\n",
                stderr="",
            )
        raise AssertionError(f"Unexpected git args: {args}")

    monkeypatch.setattr(svc, "_run_git", _fake_run_git)

    result = svc.repo_dossier_search(query="api_token", path_prefixes=["src/backend"])

    assert result["success"] is True
    assert result["match_count_total"] == 3
    assert result["match_count_returned"] == 1
    assert result["sanitised"] is True
    assert result["matches"][0]["path"] == "src/backend/demo.py"
    assert "[REDACTED]" in result["matches"][0]["line_text"]


def test_repo_dossier_workflow_definition_get_uses_registry_identity(monkeypatch):
    from src.backend.services import repo_dossier_service as svc
    from src.backend.workflows.engine import WorkflowDefinition, WorkflowStateSpec

    definition = WorkflowDefinition(
        workflow_id="#V#demo_workflow",
        initial_state="start",
        states={"start": WorkflowStateSpec(state_id="start", terminal=True)},
        termination_states=("start",),
        purpose="Demo workflow",
    )
    registry = SimpleNamespace(
        get_registration=lambda workflow_id: SimpleNamespace(
            definition=definition,
            source="vontology",
        )
    )
    monkeypatch.setattr(
        svc,
        "build_durable_workflow_registry_read_only",
        lambda defer_parity_work=True: registry,
    )
    monkeypatch.setattr(
        svc,
        "build_workflow_definition_identity",
        lambda **kwargs: {
            "schema_version": "workflow_definition_identity.v1",
            "workflow_source": kwargs.get("source"),
        },
    )

    result = svc.repo_dossier_workflow_definition_get(workflow_id="#V#demo_workflow")

    assert result["success"] is True
    assert result["definition_summary"]["state_count"] == 1
    assert result["definition_identity"]["workflow_source"] == "vontology"


def test_repo_dossier_prompt_definition_get_returns_prompt_text(monkeypatch):
    from src.backend.services import repo_dossier_service as svc

    monkeypatch.setattr(
        svc,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(
        svc,
        "get_texts_for_concept",
        lambda concept_id, limit=20: [
            {
                "predicate": "hasContent",
                "lang": "en-NZ",
                "text": "System prompt body",
            }
        ],
    )

    result = svc.repo_dossier_prompt_definition_get(
        prompt_concept_id="#V#demo_prompt"
    )

    assert result["success"] is True
    assert result["selected_predicate"] == "hasContent"
    assert result["text"] == "System prompt body"


def test_repo_dossier_prompt_definition_get_reports_missing_prompt_concept(monkeypatch):
    from src.backend.services import repo_dossier_service as svc

    def _raise_not_found(concept_id):
        raise svc.ConceptNotFoundError(concept_id)

    monkeypatch.setattr(svc, "get_concept_by_concept_id", _raise_not_found)

    result = svc.repo_dossier_prompt_definition_get(
        prompt_concept_id="#V#missing_prompt"
    )

    assert result["success"] is False
    assert result["error"] == "prompt_concept_not_found"


def test_repo_dossier_prompt_definition_get_reports_missing_prompt_content(monkeypatch):
    from src.backend.services import repo_dossier_service as svc

    monkeypatch.setattr(
        svc,
        "get_concept_by_concept_id",
        lambda concept_id: {"concept_id": concept_id},
    )
    monkeypatch.setattr(svc, "get_texts_for_concept", lambda concept_id, limit=20: [])

    result = svc.repo_dossier_prompt_definition_get(
        prompt_concept_id="#V#empty_prompt"
    )

    assert result["success"] is False
    assert result["error"] == "prompt_content_missing"


def test_repo_dossier_git_metadata_rejects_untracked_paths(monkeypatch):
    from src.backend.services import repo_dossier_service as svc

    monkeypatch.setattr(
        svc,
        "_normalise_repo_relative_paths",
        lambda value, field_name: ["src/backend/demo.py"],
    )
    monkeypatch.setattr(svc, "_git_rev_parse", lambda ref: "resolved")
    monkeypatch.setattr(svc, "_get_tracked_blob_map", lambda paths: {})

    result = svc.repo_dossier_git_metadata(paths=["src/backend/demo.py"])

    assert result["success"] is False
    assert result["error"] == "tracked_file_required"
