from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from flask import Flask

from representation_intent_regression_helpers import (
    patch_representation_profile_loader,
)
from src.backend.integrations.internal_mcp.catalogue import (
    _download_paper,
    _download_paper_input_schema,
    _download_paper_output_schema,
)
from src.backend.integrations.internal_mcp.gateway import (
    InternalMCPGateway,
    MethodCatalogue,
    MethodDefinition,
)
from src.backend.integrations.internal_mcp.transport import InternalMCPTransport
from src.backend.workflows.definitions import (
    ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
    CHAT_ASSISTANT_WORKFLOW_ID,
    TOOL_CALLING_WORKFLOW_ID,
)
from orchestrator_test_harness import (
    _stub_stage_model_snapshot,
    _stub_stage_path,
    build_db_independent_orchestrator,
)


class _LLMSequence:
    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def generate(self, prompt, context, model):
        self.calls.append({"prompt": prompt, "context": list(context), "model": model})
        if not self._responses:
            raise AssertionError("No stubbed LLM responses remaining")
        return self._responses.pop(0)


class _ArxivProxyStub:
    async def download_paper(self, *, arxiv_id: str, filename=None):
        return {
            "success": True,
            "file_path": f"data/arxiv_cache/{arxiv_id}.pdf",
            "arxiv_id": arxiv_id,
            "version": 1,
            "size_bytes": 12345,
            "sha256": "abc123",
            "storage": {
                "backend": "swift",
                "key": f"arxiv/{arxiv_id}.pdf",
                "uri": f"swift://von-artifacts/arxiv/{arxiv_id}.pdf",
            },
        }


class _ArxivProxyFailureStub:
    async def download_paper(self, *, arxiv_id: str, filename=None):
        return {
            "success": False,
            "error": "arXiv proxy timed out while downloading the PDF.",
            "error_code": "arxiv_proxy_error",
            "error_details": {
                "arxiv_id": arxiv_id,
                "exception_type": "ArxivProxyError",
            },
        }


@dataclass(frozen=True)
class _FileCopyRecord:
    concept_id: str
    uploaded_at: str


def _build_gateway(
    monkeypatch,
    *,
    proxy_factory=None,
) -> InternalMCPGateway:
    proxy_factory = proxy_factory or _ArxivProxyStub

    async def _get_proxy():
        return proxy_factory()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp._find_cached_pdf_for_arxiv_id",
        lambda *_a, **_kw: None,
    )
    # Keep these route-level tests deterministic by forcing the live proxy/stub
    # path rather than rehydrating a previously persisted durable blob.
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._rehydrate_cached_arxiv_pdf_from_durable_blob",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp.get_arxiv_proxy",
        _get_proxy,
    )
    monkeypatch.setattr(
        "src.backend.services.computer_file_copy_service.create_computer_file_copy_instance",
        lambda **_kwargs: _FileCopyRecord(
            concept_id="#V#uploaded_file_copy_2510_06248",
            uploaded_at="2026-03-08T02:00:00Z",
        ),
    )
    monkeypatch.setattr(
        "src.backend.services.arxiv_paper_link_service.link_file_copy_to_arxiv_paper",
        lambda **_kwargs: {"paper_concept_id": "#V#paper_on_arxiv_2510_06248"},
    )
    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.catalogue._materialise_arxiv_file_copy_representation",
        lambda **_kwargs: {
            "attempted": True,
            "verified": True,
            "paper_concept_id": "#V#paper_on_arxiv_2510_06248",
            "author_concept_ids": [
                "#V#person_author_alpha",
                "#V#person_author_beta",
            ],
        },
    )

    catalogue = MethodCatalogue()
    catalogue.register(
        MethodDefinition(
            name="download_paper",
            handler=_download_paper,
            input_schema=_download_paper_input_schema(),
            output_schema=_download_paper_output_schema(),
            category="write",
            description="Download and persist an arXiv paper.",
        )
    )
    gateway = InternalMCPGateway(
        catalogue=catalogue,
        transport=InternalMCPTransport(read_timeout_sec=2.0, write_timeout_sec=2.0),
        enabled=True,
    )
    return gateway


def _make_app(
    monkeypatch,
    *,
    llm: _LLMSequence,
    selector_enabled: bool = True,
    proxy_factory=None,
    workflow_discovery_result=None,
) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv(
        "VON_WORKFLOW_DISCOVERY_ENABLE",
        "1" if workflow_discovery_result is not None else "0",
    )
    monkeypatch.setenv("VON_DB_NAME", "test_von_db")
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_model_snapshot",
        _stub_stage_model_snapshot,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.build_conversation_turn_stage_path",
        _stub_stage_path,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_llm_client",
        lambda **_kwargs: llm,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.get_active_model_name",
        lambda *args, **kwargs: "test-model",
    )
    monkeypatch.setattr(
        "src.backend.security.access_control.get_effective_user_concept_id",
        lambda: "#V#test_user",
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.add_message_to_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.server.routes.von_routes.chat_history_service.get_chat_history",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        "src.backend.services.chat_auxiliary_prompt_service.get_user_specific_prompt_fragments",
        lambda _user_id, **_kwargs: [],
    )
    if callable(workflow_discovery_result):
        discovery_handler = workflow_discovery_result
    else:
        def discovery_handler(*_args, **_kwargs):
            return workflow_discovery_result

    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        discovery_handler,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: None,
    )
    patch_representation_profile_loader(monkeypatch)

    gateway = _build_gateway(monkeypatch, proxy_factory=proxy_factory)
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=gateway,
        selector_enabled=selector_enabled,
        max_tool_invocations=1,
    )

    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(von_bp, url_prefix="/von")
    app.config["CONTEXT"] = []
    app.config["INTERNAL_MCP_ORCHESTRATOR"] = orchestrator
    app.config["INTERNAL_MCP_GATEWAY"] = gateway
    app.config["_test_llm"] = llm
    return app


def test_generate_bare_arxiv_url_routes_to_specialised_workflow_and_surfaces_created_concepts(
    monkeypatch,
):
    llm = _LLMSequence(
        [
            (
                '{"workflow_id":"#V#arxiv_paper_representation_workflow",'
                '"confidence":0.99,'
                '"reasoning":"Bare arXiv URL should use the specialised arXiv '
                'paper representation workflow."}'
            ),
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
        ]
    )
    discovery_result = {
        "query": "https://arxiv.org/abs/2510.06248",
        "requested_query": "https://arxiv.org/abs/2510.06248",
        "search_sources": ["capability_index"],
        "candidate_count": 1,
        "match_count": 1,
        "matches": [
            {
                "concept_id": ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                "name": "Arxiv Paper Representation Workflow",
                "description": "Represent an arXiv paper from a raw URL or arXiv identifier.",
                "is_executable": True,
                "executability_reason": "executable_now",
                "is_policy_safe": True,
                "routing_eligible": True,
                "candidate_source": "capability_index",
            }
        ],
        "candidates": [
            {
                "concept_id": ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                "name": "Arxiv Paper Representation Workflow",
                "description": "Represent an arXiv paper from a raw URL or arXiv identifier.",
                "is_executable": True,
                "executability_reason": "executable_now",
                "is_policy_safe": True,
                "routing_eligible": True,
                "candidate_source": "capability_index",
            }
        ],
    }
    app = _make_app(
        monkeypatch,
        llm=llm,
        workflow_discovery_result=discovery_result,
    )

    orchestrator = app.config["INTERNAL_MCP_ORCHESTRATOR"]
    original_execute_workflow = orchestrator.execute_workflow

    def _execute_workflow(workflow_id: str, **kwargs):
        if workflow_id != ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID:
            return original_execute_workflow(workflow_id, **kwargs)
        return SimpleNamespace(
            completed=True,
            final_state="verify_arxiv_path",
            error=None,
            data={
                "response_text": "Downloaded and represented the paper.",
                "final_response": "Downloaded and represented the paper.",
                "paper_concept_id": "#V#paper_on_arxiv_2510_06248",
                "file_copy_concept_id": "#V#uploaded_file_copy_2510_06248",
                "workflow_execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                    "completed": True,
                    "final_state": "verify_arxiv_path",
                    "durable_side_effect_count": 2,
                    "durable_side_effects": [
                        {
                            "mutation_kind": "created",
                            "artefact_type": "paper_concept",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#paper_on_arxiv_2510_06248"],
                        },
                        {
                            "mutation_kind": "created",
                            "artefact_type": "file_copy",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#uploaded_file_copy_2510_06248"],
                        },
                    ],
                },
            },
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "https://arxiv.org/abs/2510.06248"},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    text = body.get("response") or ""
    assert "Created paper concept: #V#paper_on_arxiv_2510_06248." in text
    assert "Linked file copy: #V#uploaded_file_copy_2510_06248." in text
    assert "Downloaded and represented the paper." in text

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    assert workflow_routing.get("source") == "selector"

    tool_invocations = llm_debug.get("tool_invocations") or []
    assert tool_invocations == []

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert completion_report.get("paper_concept_id") == "#V#paper_on_arxiv_2510_06248"
    assert completion_report.get("file_copy_concept_id") == (
        "#V#uploaded_file_copy_2510_06248"
    )
    assert "Created paper concept: #V#paper_on_arxiv_2510_06248." in (
        completion_report.get("response_text") or ""
    )


def test_generate_bare_arxiv_url_recovers_from_selector_clarification_to_specialised_workflow(
    monkeypatch,
):
    llm = _LLMSequence(
        [
            (
                "I'm not sure which workflow you'd like me to select from the "
                "provided candidates. If you want me to download or represent "
                "the paper, please say so explicitly."
            ),
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
        ]
    )
    discovery_result = {
        "query": "https://arxiv.org/abs/2510.06248",
        "requested_query": "https://arxiv.org/abs/2510.06248",
        "search_sources": ["capability_index"],
        "candidate_count": 1,
        "match_count": 1,
        "matches": [
            {
                "concept_id": ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                "name": "Arxiv Paper Representation Workflow",
                "description": "Represent an arXiv paper from a raw URL or arXiv identifier.",
                "is_executable": True,
                "executability_reason": "executable_now",
                "is_policy_safe": True,
                "routing_eligible": True,
                "candidate_source": "capability_index",
                "routing_profile": {"role": "execution"},
            }
        ],
        "candidates": [
            {
                "concept_id": ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                "name": "Arxiv Paper Representation Workflow",
                "description": "Represent an arXiv paper from a raw URL or arXiv identifier.",
                "is_executable": True,
                "executability_reason": "executable_now",
                "is_policy_safe": True,
                "routing_eligible": True,
                "candidate_source": "capability_index",
                "routing_profile": {"role": "execution"},
            }
        ],
    }
    app = _make_app(
        monkeypatch,
        llm=llm,
        workflow_discovery_result=discovery_result,
    )

    orchestrator = app.config["INTERNAL_MCP_ORCHESTRATOR"]
    original_execute_workflow = orchestrator.execute_workflow

    def _execute_workflow(workflow_id: str, **kwargs):
        if workflow_id != ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID:
            return original_execute_workflow(workflow_id, **kwargs)
        return SimpleNamespace(
            completed=True,
            final_state="verify_arxiv_path",
            error=None,
            data={
                "response_text": "Downloaded and represented the paper.",
                "final_response": "Downloaded and represented the paper.",
                "paper_concept_id": "#V#paper_on_arxiv_2510_06248",
                "file_copy_concept_id": "#V#uploaded_file_copy_2510_06248",
                "workflow_execution_summary": {
                    "schema_version": "workflow_execution_summary.v1",
                    "workflow_id": ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID,
                    "completed": True,
                    "final_state": "verify_arxiv_path",
                    "durable_side_effect_count": 2,
                    "durable_side_effects": [
                        {
                            "mutation_kind": "created",
                            "artefact_type": "paper_concept",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#paper_on_arxiv_2510_06248"],
                        },
                        {
                            "mutation_kind": "created",
                            "artefact_type": "computer_file_copy",
                            "artefact_count": 1,
                            "artefact_ids": ["#V#uploaded_file_copy_2510_06248"],
                        },
                    ],
                },
            },
        )

    monkeypatch.setattr(orchestrator, "execute_workflow", _execute_workflow)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "https://arxiv.org/abs/2510.06248"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    assert "Created paper concept: #V#paper_on_arxiv_2510_06248." in (
        body.get("response") or ""
    )

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    assert workflow_routing.get("verdict") == "rag_selected"
    assert workflow_routing.get("source") == "selector_override"

    selected_workflow_trace = llm_debug.get("selected_workflow_trace") or {}
    selector_override = selected_workflow_trace.get("selector_override") or {}
    assert (
        selector_override.get("reason")
        == "selector_default_recovered_to_single_discovered_execution_workflow"
    )
    assert selector_override.get("recovered_candidate_workflow_id") == (
        ARXIV_PAPER_REPRESENTATION_WORKFLOW_ID
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_report = turn_record.get("completion_report") or {}
    assert completion_report.get("paper_concept_id") == "#V#paper_on_arxiv_2510_06248"
    assert completion_report.get("file_copy_concept_id") == (
        "#V#uploaded_file_copy_2510_06248"
    )


def test_generate_bare_arxiv_url_falls_back_to_tool_pipeline_when_specialised_route_is_unavailable(
    monkeypatch,
):
    llm = _LLMSequence(
        [
            (
                '{"workflow_id":"#V#tool_calling_workflow","confidence":0.98,'
                '"reasoning":"Bare arXiv URL should use tool workflow."}'
            ),
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
        ]
    )
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "https://arxiv.org/abs/2510.06248"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    llm_debug = body.get("llm_debug") or {}

    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert workflow_routing.get("verdict") == "rag_selected"
    assert workflow_routing.get("source") == "selector"

    tool_invocations = llm_debug.get("tool_invocations") or []
    download_records = [
        record
        for record in tool_invocations
        if isinstance(record, dict)
        and (record.get("tool") or record.get("method")) == "download_paper"
    ]
    assert download_records
    assert all(not record.get("blocked") for record in download_records)
    assert "Execution status: mutation may have run but verification is inconclusive." in (
        body.get("response") or ""
    )

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    tool_history = diagnostics.get("tool_history") or []
    assert any(
        isinstance(entry, dict)
        and entry.get("tool") == "download_paper"
        and entry.get("success") is True
        for entry in tool_history
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    required_effects = turn_record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    assert effect.get("required_tools") == ["download_paper"]
    assert effect.get("status") == "satisfied"

    completion_gate = turn_record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "partial"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert "postcondition_inconclusive" in list(
        completion_gate.get("blocking_failure_codes") or []
    )
    assert len(llm.calls) == 4


def test_generate_bare_arxiv_url_recovers_from_noisy_initial_tool_plan_output(
    monkeypatch,
):
    llm = _LLMSequence(
        [
            (
                '{"workflow_id":"#V#tool_calling_workflow","confidence":0.98,'
                '"reasoning":"Bare arXiv URL should use tool workflow."}'
            ),
            "I would route this as plain_response.",
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
        ]
    )
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "https://arxiv.org/abs/2510.06248"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    llm_debug = body.get("llm_debug") or {}

    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert workflow_routing.get("verdict") == "rag_selected"
    assert workflow_routing.get("source") == "selector"

    tool_invocations = llm_debug.get("tool_invocations") or []
    download_records = [
        record
        for record in tool_invocations
        if isinstance(record, dict)
        and (record.get("tool") or record.get("method")) == "download_paper"
    ]
    assert len(download_records) == 1
    assert "Execution status: mutation may have run but verification is inconclusive." in (
        body.get("response") or ""
    )

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    assert int(diagnostics.get("tool_call_count") or 0) >= 1
    assert int(diagnostics.get("tool_success_count") or 0) >= 1
    assert diagnostics.get("tool_failure_count") == 0
    assert int(diagnostics.get("tool_pending_count") or 0) <= 1

    tool_history = diagnostics.get("tool_history") or []
    assert tool_history
    assert all(entry.get("tool") == "download_paper" for entry in tool_history)

    workflow_stage_path = diagnostics.get("workflow_stage_path") or {}
    assert workflow_stage_path.get("workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert TOOL_CALLING_WORKFLOW_ID in list(
        workflow_stage_path.get("observed_workflow_ids") or []
    )
    path = workflow_stage_path.get("path") or []
    tool_execute_entry = next(
        entry
        for entry in path
        if isinstance(entry, dict) and entry.get("stage_id") == "tool_execute"
    )
    assert tool_execute_entry.get("workflow_id") == TOOL_CALLING_WORKFLOW_ID

    stage_diagnostics = diagnostics.get("stage_diagnostics") or []
    tool_stage = next(
        entry
        for entry in stage_diagnostics
        if isinstance(entry, dict) and entry.get("stage_id") == "tool_execute"
    )
    assert int(tool_stage.get("tool_call_count") or 0) >= 1
    assert int(tool_stage.get("tool_success_count") or 0) >= 1
    assert tool_stage.get("tool_failure_count") == 0
    assert tool_stage.get("tool_pending_count") == 0
    assert len(llm.calls) == 5


def test_generate_bare_arxiv_url_without_selector_still_forces_tool_pipeline_routing(
    monkeypatch,
):
    llm = _LLMSequence(
        [
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
            "Downloaded and represented the paper.",
            "Downloaded and represented the paper.",
        ]
    )
    app = _make_app(monkeypatch, llm=llm, selector_enabled=False)

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "https://arxiv.org/abs/2510.06248"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    llm_debug = body.get("llm_debug") or {}

    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("verdict") == "selector_disabled"
    assert workflow_routing.get("source") == "default"

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    assert diagnostics.get("tool_call_count") == 1
    assert diagnostics.get("tool_success_count") == 1
    assert diagnostics.get("tool_pending_count") == 0
    assert "Execution status: mutation may have run but verification is inconclusive." in (
        body.get("response") or ""
    )
    turn_record = llm_debug.get("turn_execution_record") or {}
    completion_gate = turn_record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "partial"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert len(llm.calls) == 3


def test_generate_bare_arxiv_url_fails_closed_when_download_tool_returns_error(
    monkeypatch,
):
    llm = _LLMSequence(
        [
            (
                '{"workflow_id":"#V#tool_calling_workflow","confidence":0.98,'
                '"reasoning":"Bare arXiv URL should use tool workflow."}'
            ),
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2602.20478"}}',
            "Download failed, follow-up required.",
            "Download failed, follow-up required.",
        ]
    )
    app = _make_app(
        monkeypatch,
        llm=llm,
        proxy_factory=_ArxivProxyFailureStub,
    )

    client = app.test_client()
    response = client.post("/von/generate", json={"prompt": "https://arxiv.org/abs/2602.20478"})
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    text = body.get("response") or ""
    assert "Execution status: requested mutation failed or was blocked." in text
    assert "kb_mutation_download_paper_failed" in text

    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == TOOL_CALLING_WORKFLOW_ID
    assert workflow_routing.get("verdict") == "rag_selected"
    assert workflow_routing.get("source") == "selector"

    tool_invocations = llm_debug.get("tool_invocations") or []
    download_record = next(
        record
        for record in tool_invocations
        if isinstance(record, dict)
        and (record.get("tool") or record.get("method")) == "download_paper"
    )
    assert download_record.get("arguments") == {
        "arxiv_id": "2602.20478",
        "namespace": "#V#test_user",
    }
    assert download_record.get("error") == "arXiv proxy timed out while downloading the PDF."

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    tool_history = diagnostics.get("tool_history") or []
    failed_download = next(
        entry
        for entry in tool_history
        if isinstance(entry, dict) and entry.get("tool") == "download_paper"
    )
    assert failed_download.get("success") is False
    assert failed_download.get("resultSummary") == (
        "Error: arxiv_proxy_error — arXiv proxy timed out while downloading the PDF."
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    required_effects = turn_record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    # Tool-authored effects reference the specific tool that failed.
    assert effect.get("required_tools") == ["download_paper"]
    assert effect.get("intent_origin") == "tool_authored"
    assert effect.get("status") == "not_satisfied"
    assert effect.get("failure_code") == "kb_mutation_download_paper_failed"
    assert "download_paper" in (effect.get("status_reason") or "")

    completion_gate = turn_record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "failed"
    assert completion_gate.get("safe_to_claim_completion") is False
    assert completion_gate.get("requires_follow_up") is True
    assert "kb_mutation_download_paper_failed" in list(
        completion_gate.get("blocking_failure_codes") or []
    )
    assert len(llm.calls) == 4


def test_generate_bare_arxiv_url_with_explicit_denial_stays_non_mutating(monkeypatch):
    llm = _LLMSequence(
        [
            (
                '{"workflow_id":"#V#chat_assistant_workflow","confidence":0.9,'
                '"reasoning":"Explicit denial keeps this read-only."}'
            ),
            "Read-only response.",
            "Read-only response.",
        ]
    )
    app = _make_app(monkeypatch, llm=llm)

    client = app.test_client()
    response = client.post(
        "/von/generate",
        json={"prompt": "Do not download or store this: https://arxiv.org/abs/2510.06248"},
    )
    assert response.status_code == 200

    body = response.get_json()
    assert isinstance(body, dict)
    llm_debug = body.get("llm_debug") or {}
    workflow_routing = llm_debug.get("workflow_routing") or {}
    assert workflow_routing.get("workflow_id") == CHAT_ASSISTANT_WORKFLOW_ID
    assert workflow_routing.get("verdict") == "rag_selected"
    assert workflow_routing.get("source") == "selector"

    tool_invocations = llm_debug.get("tool_invocations") or []
    assert not any(
        isinstance(record, dict)
        and (record.get("tool") or record.get("method")) == "download_paper"
        for record in tool_invocations
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    required_effects = turn_record.get("required_effects") or []
    assert not any(
        isinstance(effect, dict)
        and list(effect.get("required_tools") or []) == ["download_paper"]
        for effect in required_effects
    )
