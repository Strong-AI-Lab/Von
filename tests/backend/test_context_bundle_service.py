from __future__ import annotations

from src.backend.services import context_bundle_service as svc
from src.backend.services.context_bundle_contracts import (
    CONTEXT_DOSSIER_BRANCH_KIND_THEORY_LOCAL,
)


def test_resolve_effective_context_reads_string_relationship_targets_and_returns_facet_states(
    monkeypatch,
) -> None:
    concepts = {
        "#V#subject": {
            "concept_id": "#V#subject",
            "relationships": {
                "#V#has_context_bundle": "#V#bundle_direct",
                "is_an_instance_of": "#V#type_child",
            },
        },
        "#V#type_child": {
            "concept_id": "#V#type_child",
            "relationships": {"is_a_type_of": "#V#type_parent"},
        },
        "#V#type_parent": {
            "concept_id": "#V#type_parent",
            "relationships": {"#V#has_context_bundle": "#V#bundle_ancestor"},
        },
        "#V#facet_direct": {"concept_id": "#V#facet_direct", "relationships": {}},
        "#V#facet_ancestor": {"concept_id": "#V#facet_ancestor", "relationships": {}},
    }
    bundle_states = {
        "#V#bundle_direct": {
            "bundle_id": "#V#bundle_direct",
            "facet_ids": ["#V#facet_direct"],
            "bundle_policy": {"budget_cap": 3},
        },
        "#V#bundle_ancestor": {
            "bundle_id": "#V#bundle_ancestor",
            "facet_ids": ["#V#facet_ancestor"],
            "bundle_policy": {"budget_cap": 5, "merge_policy": "nearest_first"},
        },
    }
    facet_states = {
        "#V#facet_direct": {
            "facet_id": "#V#facet_direct",
            "facet_kind": "summary",
            "content": "Direct concept context.",
        },
        "#V#facet_ancestor": {
            "facet_id": "#V#facet_ancestor",
            "facet_kind": "policy",
            "content": "Ancestor type context.",
        },
    }

    bootstrap_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        svc,
        "ensure_canonical_context_bundle_ontology",
        lambda **kwargs: bootstrap_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(svc, "_get_concept_or_none", lambda concept_id: concepts.get(concept_id))
    monkeypatch.setattr(svc, "load_context_bundle_state", lambda bundle_id: bundle_states.get(bundle_id))
    monkeypatch.setattr(svc, "load_context_facet_state", lambda facet_id: facet_states.get(facet_id))

    result = svc.resolve_effective_context(
        subject_kind="concept",
        subject_id="#V#subject",
    )

    assert result["success"] is True
    assert result["effective_context_bundle_ids"] == [
        "#V#bundle_direct",
        "#V#bundle_ancestor",
    ]
    assert result["effective_context_facet_ids"] == [
        "#V#facet_direct",
        "#V#facet_ancestor",
    ]
    assert result["facet_states"][0]["content"] == "Direct concept context."
    assert result["diagnostics"]["effective_policy"]["budget_cap"] == 3
    assert result["diagnostics"]["counts"]["effective_facet_count"] == 2
    assert bootstrap_calls == []


def test_assemble_context_dossier_reuses_testing_theory_state_and_normalises_branch_kinds(
    monkeypatch,
) -> None:
    persisted_by_concept_id: dict[str, dict[str, object]] = {}
    relationship_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        svc, "ensure_canonical_context_bundle_ontology", lambda **kwargs: {"success": True}
    )
    monkeypatch.setattr(
        svc,
        "resolve_effective_context",
        lambda **kwargs: {
            "success": True,
            "effective_context_bundle_ids": ["#V#bundle_parent_specificity"],
            "effective_context_facet_ids": ["#V#facet_parent_specificity"],
        },
    )
    monkeypatch.setattr(svc, "_ensure_context_instance", lambda **kwargs: None)
    monkeypatch.setattr(
        svc,
        "_persist_context_state",
        lambda *, concept_id, attribute_prefix, state_key, state: persisted_by_concept_id.setdefault(
            concept_id, {"attribute_prefix": attribute_prefix, "state_key": state_key, "state": dict(state)}
        )["state"],
    )
    monkeypatch.setattr(
        svc,
        "add_relationship",
        lambda source_id, predicate, target: relationship_calls.append(
            (source_id, predicate, target)
        )
        or {"success": True},
    )
    monkeypatch.setattr(
        svc,
        "get_testing_theory_state",
        lambda theory_id: {
            "theory_id": theory_id,
            "included_canonical_concept_ids": ["#V#concept_a"],
            "local_assertions": [{"assertion_id": "#V#assertion_a"}],
            "lifecycle_state": "active",
            "expires_at_utc": "2026-04-05T00:00:00+00:00",
        },
    )

    result = svc.assemble_context_dossier(
        name="Ontology refinement dossier",
        subject_kind="concept",
        subject_id="#V#graph_theorist",
        dossier_kind="ontology_refinement",
        effective_context_bundle_ids=["#V#bundle_parent_specificity"],
        open_questions=["Should this concept gain a narrower parent?"],
        immediate_context={"summary": "Parent-specificity evidence"},
        search_history=[{"query": "graph theorist"}],
        testing_theory_ids=["#V#theory_graph_theorist"],
        local_assertions=[{"claim": "candidate stronger type"}],
        hypotheses=[{"hypothesis": "intervening subtype needed"}],
        promotion_candidates=[{"target_id": "#V#theoretical_graph_scientist"}],
        branch_specs=[{"branch_kind": "unknown_branch_kind", "title": "Counter-check"}],
    )

    assert result["success"] is True
    dossier_state = result["context_dossier"]
    assert dossier_state["theory_state_summaries"] == [
        {
            "theory_id": "#V#theory_graph_theorist",
            "included_canonical_concept_ids": ["#V#concept_a"],
            "local_assertion_count": 1,
            "lifecycle_state": "active",
            "expires_at_utc": "2026-04-05T00:00:00+00:00",
        }
    ]
    assert dossier_state["branches"][0]["branch_kind"] == CONTEXT_DOSSIER_BRANCH_KIND_THEORY_LOCAL
    assert dossier_state["immediate_context"]["summary"] == "Parent-specificity evidence"
    assert ("#V#graph_theorist", "#V#has_context_dossier", result["dossier_id"]) in relationship_calls
    assert (
        result["dossier_id"],
        "#V#has_context_bundle",
        "#V#bundle_parent_specificity",
    ) in relationship_calls


def test_build_reconstructed_workspace_backfills_report_text_and_receipts_and_persists_workspace_state(
    monkeypatch,
) -> None:
    persisted_state: dict[str, object] = {}

    monkeypatch.setattr(
        svc,
        "load_context_dossier_state",
        lambda dossier_id: {
            "dossier_id": dossier_id,
            "receipt_ids": ["#V#receipt_dossier"],
            "latest_report_revision_id": "#V#revision_1",
            "open_questions": ["What is the stronger parent?"],
            "search_history": [{"query": "graph theory specificity"}],
            "immediate_context": {"summary": "Existing dossier"},
            "branches": [{"branch_id": "branch-1"}] * 10,
        },
    )
    monkeypatch.setattr(
        svc,
        "load_workflow_report_revision_state",
        lambda revision_id: {
            "revision_id": revision_id,
            "receipt_ids": ["#V#receipt_report"],
            "report_text": "Revision scaffold text.",
            "summary": {"status": "active"},
        },
    )
    monkeypatch.setattr(
        svc,
        "load_evidence_receipt_state",
        lambda receipt_id: {
            "receipt_id": receipt_id,
            "source_system": "test",
            "locator": {"id": receipt_id},
        },
    )
    monkeypatch.setattr(
        svc,
        "resolve_effective_context",
        lambda **kwargs: {
            "success": True,
            "effective_context_bundle_ids": ["#V#bundle_context"],
            "effective_context_facet_ids": ["#V#facet_context"],
            "diagnostics": {
                "guardrail_events": [{"event": "bounded_workspace"}],
                "counts": {"effective_bundle_count": 1},
            },
        },
    )
    monkeypatch.setattr(
        svc,
        "_persist_context_state",
        lambda *, concept_id, attribute_prefix, state_key, state: persisted_state.update(
            {
                "concept_id": concept_id,
                "attribute_prefix": attribute_prefix,
                "state_key": state_key,
                "state": dict(state),
            }
        )
        or dict(state),
    )

    result = svc.build_reconstructed_workspace(
        subject_kind="concept",
        subject_id="#V#graph_theorist",
        dossier_id="#V#context_dossier_graph_theorist",
        question="What is the best stronger parent?",
        task="Prepare a bounded ontology-refinement workspace.",
    )

    assert result["success"] is True
    workspace = result["workspace"]
    assert workspace["report_revision"]["report_text"] == "Revision scaffold text."
    assert {item["receipt_id"] for item in workspace["evidence_receipts"]} == {
        "#V#receipt_dossier",
        "#V#receipt_report",
    }
    assert workspace["workspace_reconstruction_telemetry"]["guardrail_events"] == [
        {"event": "bounded_workspace"}
    ]
    assert (
        persisted_state["state"]["workspace_state"]["workspace_fingerprint"]
        == workspace["workspace_fingerprint"]
    )


def test_ensure_canonical_context_bundle_ontology_repairs_missing_parent_links(
    monkeypatch,
) -> None:
    add_calls: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        svc,
        "_get_concept_or_none",
        lambda concept_id: (
            {"concept_id": concept_id, "relationships": {"is_a_type_of": []}}
            if concept_id == "#V#has_context_bundle"
            else {"concept_id": concept_id, "relationships": {"is_a_type_of": ["#V#thing"]}}
        ),
    )
    monkeypatch.setattr(svc, "_ensure_text_description", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        svc,
        "add_relationship",
        lambda source_id, predicate, target: add_calls.append(
            (source_id, predicate, target)
        )
        or {"success": True},
    )

    result = svc.ensure_canonical_context_bundle_ontology(
        concept_ids=["#V#has_context_bundle"],
        create_missing_concepts=True,
    )

    assert result["success"] is True
    assert ("#V#has_context_bundle", "is_an_instance_of", "#V#predicate") in add_calls
    assert result["counts"]["repaired_parent_links"] == 1
