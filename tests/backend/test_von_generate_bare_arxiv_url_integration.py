from __future__ import annotations

from dataclasses import dataclass

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


@dataclass(frozen=True)
class _FileCopyRecord:
    concept_id: str
    uploaded_at: str


def _build_gateway(monkeypatch) -> InternalMCPGateway:
    async def _get_proxy():
        return _ArxivProxyStub()

    monkeypatch.setattr(
        "src.backend.integrations.internal_mcp.arxiv_proxy_mcp._find_cached_pdf_for_arxiv_id",
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


def _make_app(monkeypatch, *, llm: _LLMSequence) -> Flask:
    from src.backend.server.routes.von_routes import von_bp

    monkeypatch.setenv("VON_WORKFLOW_DISCOVERY_ENABLE", "0")
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
        lambda: "test-model",
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
    monkeypatch.setattr(
        "src.backend.services.workflow_discovery_service.discover_workflows_for_turn",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "src.backend.services.workflow_continuation_service.get_session_workflow_continuation_context",
        lambda **_kwargs: None,
    )
    patch_representation_profile_loader(monkeypatch)

    gateway = _build_gateway(monkeypatch)
    orchestrator = build_db_independent_orchestrator(
        monkeypatch,
        gateway=gateway,
        selector_enabled=True,
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


def test_generate_bare_arxiv_url_auto_represents_paper(monkeypatch):
    llm = _LLMSequence(
        [
            "plain_response",
            '{"action":"call_tool","tool":"download_paper","payload":{"arxiv_id":"2510.06248"}}',
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
    assert workflow_routing.get("workflow_id") == "#V#tool_calling_workflow"
    assert workflow_routing.get("verdict") == "tool_seeking"

    tool_invocations = llm_debug.get("tool_invocations") or []
    download_records = [
        record
        for record in tool_invocations
        if isinstance(record, dict)
        and (record.get("tool") or record.get("method")) == "download_paper"
    ]
    assert download_records
    assert all(not record.get("blocked") for record in download_records)

    diagnostics = llm_debug.get("turn_execution_diagnostics") or {}
    tool_history = diagnostics.get("tool_history") or []
    assert any(
        isinstance(entry, dict)
        and entry.get("tool") == "download_paper"
        and entry.get("success") is True
        for entry in tool_history
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    execution = turn_record.get("execution") or {}
    contract = execution.get("required_effects_contract") or {}
    assert contract.get("domain_profile_id") == "paper"
    assert contract.get("artefact_source") == "url"

    required_effects = turn_record.get("required_effects") or []
    assert required_effects
    effect = required_effects[0]
    assert effect.get("required_tools") == ["download_paper"]
    assert effect.get("status") == "satisfied"

    completion_gate = turn_record.get("completion_gate") or {}
    assert completion_gate.get("decision") == "completed"
    assert completion_gate.get("safe_to_claim_completion") is True


def test_generate_bare_arxiv_url_with_explicit_denial_stays_non_mutating(monkeypatch):
    llm = _LLMSequence(
        [
            "plain_response",
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

    tool_invocations = llm_debug.get("tool_invocations") or []
    assert not any(
        isinstance(record, dict)
        and (record.get("tool") or record.get("method")) == "download_paper"
        for record in tool_invocations
    )

    turn_record = llm_debug.get("turn_execution_record") or {}
    required_effects = turn_record.get("required_effects") or []
    assert not required_effects
