"""Run one sampled KB+tool-sensitive prompt against a live Von server.

This script exercises the real `/von/generate` route in a fresh test
conversation, fetches persisted turn telemetry, and emits a conservative
verdict about whether the resulting answer would likely satisfy a user.

The prompt bank is intentionally embedded here and also checked into
`scripts/live_kb_tool_prompt_bank.json`. The script fails closed if the two
copies drift.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:5000"
DEFAULT_MODEL = "gpt-5.4-nano"
DEFAULT_USER_CONCEPT_ID = "#V#michael_witbrock"
DEFAULT_ORGANISATION_CONCEPT_ID = "university_of_auckland_strong_ai_lab"
DEFAULT_SESSION_NAME = "JVNAUTOSCI-1892 live KB prompt sample"
PROMPT_BANK_PATH = Path(__file__).with_name("live_kb_tool_prompt_bank.json")
NEGATIVE_RESPONSE_MARKERS = (
    "i don't currently have",
    "i do not currently have",
    "i don't have enough information",
    "i do not have enough information",
    "i can't access",
    "i cannot access",
    "not authenticated",
    "workflow not runnable",
    "instance was not created",
    "i don't know",
    "i do not know",
)

PROMPT_BANK_PAYLOAD: dict[str, Any] = {
    "schema_version": "live_kb_tool_prompt_bank.v1",
    "description": (
        "Prompt bank for live /von/generate sampling of turns that should draw "
        "on LLM knowledge, KB knowledge, and optional Jira/web/arXiv tools."
    ),
    "prompts": [
        {
            "id": "what_papers_of_mine_do_you_know_about",
            "category": "entity_relative_kb_lookup",
            "prompt": "What papers of mine do you know about?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "research_interests_and_collaborators",
            "category": "profile_and_network_summary",
            "prompt": (
                "What do you know about my current research interests, and how do "
                "they connect to my collaborators?"
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "most_relevant_people_for_my_neurosymbolic_work",
            "category": "network_ranking",
            "prompt": (
                "Who in my network seems most relevant to my work on "
                "neuro-symbolic agents, and why?"
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "students_or_collaborators_and_relationships",
            "category": "relation_lookup",
            "prompt": (
                "What students or collaborators of mine are represented in the KB, "
                "and what is my relationship to each?"
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "papers_talks_projects_clustered_by_theme",
            "category": "theme_synthesis",
            "prompt": (
                "What talks, papers, and projects of mine seem to cluster around "
                "the same theme?"
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "represented_self_facts_vs_inferences",
            "category": "epistemic_summary",
            "prompt": (
                "Tell me about myself as represented here, but separate "
                "established facts from likely inferences."
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "closest_kb_papers_to_recent_arxiv_interests",
            "category": "personalised_arxiv_recommendation",
            "prompt": (
                "Which papers in my KB are closest to my recent arXiv interests, "
                "and what newer arXiv papers should I read next?"
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_knowledge_base", "search_arxiv"],
        },
        {
            "id": "compare_my_papers_with_latest_arxiv_reasoning_work",
            "category": "external_comparison",
            "prompt": (
                "Compare the papers of mine you know about with the latest arXiv "
                "work on neuro-symbolic reasoning."
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_knowledge_base", "search_arxiv"],
        },
        {
            "id": "recent_arxiv_like_my_continual_learning_and_symbolic_memory_interests",
            "category": "personalised_arxiv_recommendation",
            "prompt": (
                "I’m interested in papers like mine on continual learning and "
                "symbolic memory. Find recent arXiv papers and explain the overlap."
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_knowledge_base", "search_arxiv"],
        },
        {
            "id": "which_arxiv_papers_should_i_ingest_into_the_kb",
            "category": "kb_growth_recommendation",
            "prompt": (
                "What arXiv papers should I ingest into the KB because they are "
                "especially relevant to my existing work?"
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_knowledge_base", "search_arxiv"],
        },
        {
            "id": "my_papers_with_follow_on_work_this_year",
            "category": "external_follow_on_search",
            "prompt": (
                "Do you know about any of my papers that have likely follow-on work "
                "on arXiv this year?"
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_knowledge_base", "search_arxiv"],
        },
        {
            "id": "recent_developments_i_should_care_about",
            "category": "personalised_web_relevance",
            "prompt": (
                "Given what you know about my work, what are the most relevant "
                "developments this month in agent memory and workflow orchestration?"
            ),
            "knowledge_surfaces": ["kb", "web"],
            "likely_tools": ["search_knowledge_base", "search_web"],
        },
        {
            "id": "companies_labs_projects_outside_kb_closest_to_ours",
            "category": "external_landscape_scan",
            "prompt": (
                "What companies, labs, or projects outside our KB are working on "
                "ideas closest to ours?"
            ),
            "knowledge_surfaces": ["kb", "web"],
            "likely_tools": ["search_knowledge_base", "search_web"],
        },
        {
            "id": "recent_news_or_releases_i_should_care_about",
            "category": "personalised_web_relevance",
            "prompt": (
                "Based on my represented interests, what recent news or releases "
                "should I probably care about?"
            ),
            "knowledge_surfaces": ["kb", "web"],
            "likely_tools": ["search_knowledge_base", "search_web"],
        },
        {
            "id": "open_source_projects_relevant_to_my_represented_themes",
            "category": "external_landscape_scan",
            "prompt": (
                "What open-source projects released recently look most aligned "
                "with the research themes already in my KB?"
            ),
            "knowledge_surfaces": ["kb", "web"],
            "likely_tools": ["search_knowledge_base", "search_web"],
        },
        {
            "id": "which_open_jira_tasks_connect_to_my_papers_and_projects",
            "category": "kb_jira_cross_reference",
            "prompt": (
                "Which of my open Jira tasks seem most closely connected to the "
                "papers and projects you know about me?"
            ),
            "knowledge_surfaces": ["kb", "jira"],
            "likely_tools": [
                "search_knowledge_base",
                "jira_search",
                "jira_get_issue",
            ],
        },
        {
            "id": "summarise_jvnautosci_1891_in_context_of_my_work",
            "category": "jira_contextual_summary",
            "prompt": (
                "Summarise JVNAUTOSCI-1891 in the context of my existing papers, "
                "workflows, and research goals."
            ),
            "knowledge_surfaces": ["kb", "jira"],
            "likely_tools": ["search_knowledge_base", "jira_get_issue"],
        },
        {
            "id": "open_jira_issues_relevant_to_paper_ingestion_retrieval_or_recommendation",
            "category": "jira_contextual_summary",
            "prompt": (
                "What open Jira issues are most relevant to my represented work on "
                "paper ingestion, retrieval, or recommendation?"
            ),
            "knowledge_surfaces": ["kb", "jira"],
            "likely_tools": ["search_knowledge_base", "jira_search"],
        },
        {
            "id": "research_briefing_my_papers_recent_arxiv_and_jira",
            "category": "multi_surface_briefing",
            "prompt": (
                "Prepare a short research briefing for me: my represented papers, "
                "relevant recent arXiv work, and any linked Jira tasks."
            ),
            "knowledge_surfaces": ["kb", "arxiv", "jira"],
            "likely_tools": ["search_knowledge_base", "search_arxiv", "jira_search"],
        },
        {
            "id": "next_three_research_actions_using_kb_jira_and_recent_literature",
            "category": "multi_surface_planning",
            "prompt": (
                "Given what you know about me, what should be my next three "
                "research actions, supported by KB evidence, Jira state, and "
                "recent external literature?"
            ),
            "knowledge_surfaces": ["kb", "jira", "arxiv", "web"],
            "likely_tools": [
                "search_knowledge_base",
                "jira_search",
                "search_arxiv",
                "search_web",
            ],
        },
    ],
}


def _safe_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    return str(value).strip()


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    expected_status: int = 200,
    timeout_seconds: float = 120.0,
    **kwargs: Any,
) -> dict[str, Any]:
    response = session.request(
        str(method or "GET").upper(),
        url,
        timeout=max(float(timeout_seconds), 1.0),
        **kwargs,
    )
    try:
        payload = response.json()
    except ValueError as exc:  # pragma: no cover - defensive
        raise RuntimeError(
            f"{method} {url} returned non-JSON payload (status={response.status_code}): "
            f"{response.text[:500]}"
        ) from exc
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{method} {url} failed with status {response.status_code}: "
            f"{json.dumps(payload, ensure_ascii=True, sort_keys=True)}"
        )
    return payload


def _load_prompt_bank() -> dict[str, Any]:
    payload = json.loads(PROMPT_BANK_PATH.read_text(encoding="utf-8"))
    if payload != PROMPT_BANK_PAYLOAD:
        raise RuntimeError(
            "Prompt bank file is out of sync with the embedded prompt bank. "
            "Update both copies together."
        )
    return json.loads(json.dumps(PROMPT_BANK_PAYLOAD))


def _choose_prompt(
    prompt_bank: Sequence[Mapping[str, Any]],
    *,
    seed: int | None,
    prompt_id: str | None,
) -> dict[str, Any]:
    prompts = [dict(entry) for entry in prompt_bank if isinstance(entry, Mapping)]
    _assert(bool(prompts), "Prompt bank is empty.")
    if isinstance(prompt_id, str) and prompt_id.strip():
        wanted = prompt_id.strip()
        for entry in prompts:
            if _safe_text(entry.get("id")) == wanted:
                return entry
        raise RuntimeError(f"Unknown prompt id: {wanted}")
    rng = random.Random(seed)
    return dict(rng.choice(prompts))


def _establish_authenticated_session(
    *,
    session: requests.Session,
    base_url: str,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
) -> tuple[str, str]:
    window_session_id = str(uuid.uuid4())
    session.headers.update(
        {
            "X-User-Concept-ID": user_concept_id,
            "X-Von-Window-Session": window_session_id,
        }
    )
    session_context_established = False
    try:
        _request_json(
            session,
            "POST",
            f"{base_url}/von/api/session/set_user_concept",
            json={"user_concept_id": user_concept_id},
        )
        session_context_established = True
    except RuntimeError:
        diag_payload = _request_json(session, "GET", f"{base_url}/diag")
        _assert(
            _safe_text(diag_payload.get("effective_user_concept_id"))
            == user_concept_id,
            f"Header-authenticated identity was not accepted: {diag_payload!r}",
        )

    if session_context_established and organisation_concept_id:
        _request_json(
            session,
            "POST",
            f"{base_url}/von/api/session/set_organisation",
            json={"organisation_concept_id": organisation_concept_id},
        )
    if session_context_established:
        context_payload = _request_json(
            session, "GET", f"{base_url}/von/api/session/context"
        )
        _assert(
            _safe_text(context_payload.get("user_id")) == user_concept_id,
            f"Session context user mismatch: {context_payload!r}",
        )

    session_payload = _request_json(
        session,
        "POST",
        f"{base_url}/von/api/session/create_chat_session",
        json={"session_name": session_name},
    )
    session_id = _safe_text(session_payload.get("session_id"))
    _assert(bool(session_id), "Session creation did not return a session_id.")
    _request_json(session, "POST", f"{base_url}/von/reset", json={})
    return session_id, window_session_id


def _run_generate_background(
    *,
    session: requests.Session,
    base_url: str,
    prompt: str,
    model: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[str, dict[str, Any]]:
    client_request_id = f"live-kb-prompt-{uuid.uuid4()}"
    submission = _request_json(
        session,
        "POST",
        f"{base_url}/von/generate",
        expected_status=202,
        timeout_seconds=max(float(timeout_seconds), 30.0),
        json={
            "prompt": prompt,
            "model": model,
            "background": True,
            "client_request_id": client_request_id,
        },
    )
    task_id = _safe_text(submission.get("task_id"))
    _assert(
        bool(task_id), f"Background submission did not return task_id: {submission!r}"
    )

    deadline = time.time() + max(float(timeout_seconds), 30.0)
    status_payload: dict[str, Any] | None = None
    while time.time() < deadline:
        status_payload = _request_json(
            session,
            "GET",
            f"{base_url}/von/api/task/status/{task_id}",
        )
        status = _safe_text(status_payload.get("status"))
        if status == "completed":
            break
        if status in {"failed", "cancelled"}:
            raise RuntimeError(
                "Background generate task did not complete successfully: "
                f"{json.dumps(status_payload, ensure_ascii=True, sort_keys=True)}"
            )
        time.sleep(max(float(poll_interval_seconds), 0.2))

    _assert(
        isinstance(status_payload, dict)
        and _safe_text(status_payload.get("status")) == "completed",
        f"Background generate task did not complete before timeout: {status_payload!r}",
    )
    task_result_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/api/task/result/{task_id}",
    )
    generate_payload = _as_mapping(task_result_payload.get("result"))
    _assert(
        bool(generate_payload),
        f"Background task result was empty: {task_result_payload!r}",
    )
    return task_id, generate_payload


def _extract_request_and_session_ids(
    *,
    task_id: str,
    generate_payload: Mapping[str, Any],
    default_session_id: str,
) -> tuple[str, str]:
    request_id = (
        _safe_text(generate_payload.get("request_id"))
        or _safe_text(_as_mapping(generate_payload.get("llm_debug")).get("request_id"))
        or task_id
    )
    session_id = (
        _safe_text(generate_payload.get("conversation_session_id"))
        or _safe_text(generate_payload.get("session_id"))
        or _safe_text(_as_mapping(generate_payload.get("llm_debug")).get("session_id"))
        or default_session_id
    )
    _assert(
        bool(session_id),
        f"Could not determine conversation session id: {generate_payload!r}",
    )
    return request_id, session_id


def _find_assistant_turn_history_location(
    *,
    session: requests.Session,
    base_url: str,
    session_id: str,
    request_id: str,
    response_text: str,
) -> dict[str, Any]:
    history_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/history",
        params={"session_id": session_id, "tail_limit": 8},
    )
    history = _as_list(history_payload.get("history"))
    target_response = _safe_text(response_text)
    for entry in reversed(history):
        if not isinstance(entry, Mapping):
            continue
        if _safe_text(entry.get("role")) != "assistant":
            continue
        debug_data = _as_mapping(entry.get("llm_debug_data"))
        debug_request_id = _safe_text(debug_data.get("request_id"))
        location = _as_mapping(entry.get("history_location"))
        if debug_request_id and debug_request_id == request_id and location:
            return location
        if (
            target_response
            and _safe_text(entry.get("content")) == target_response
            and location
        ):
            return location
    raise RuntimeError(
        f"Could not resolve assistant history location for request_id={request_id} "
        f"in session_id={session_id}."
    )


def _fetch_turn_debug(
    *,
    session: requests.Session,
    base_url: str,
    session_id: str,
    history_index: int,
) -> dict[str, Any]:
    debug_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/history/debug",
        params={"session_id": session_id, "history_index": history_index},
    )
    _assert(
        bool(debug_payload.get("success")),
        f"History debug lookup failed: {debug_payload!r}",
    )
    return _as_mapping(debug_payload.get("llm_debug_data"))


def _collect_tool_names(
    diagnostics: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
) -> list[str]:
    tool_names: list[str] = []
    for entry in _as_list(diagnostics.get("tool_history")):
        if not isinstance(entry, Mapping):
            continue
        tool_name = _safe_text(entry.get("tool") or entry.get("method"))
        if tool_name and tool_name not in tool_names:
            tool_names.append(tool_name)
    for entry in _as_list(llm_debug_data.get("tool_invocations")):
        if not isinstance(entry, Mapping):
            continue
        tool_name = _safe_text(entry.get("tool") or entry.get("method"))
        if tool_name and tool_name not in tool_names:
            tool_names.append(tool_name)
    return tool_names


def _evaluate_user_happiness(
    *,
    prompt_entry: Mapping[str, Any],
    generate_payload: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    response_text = (
        _safe_text(generate_payload.get("response"))
        or _safe_text(generate_payload.get("response_text"))
        or _safe_text(llm_debug_data.get("response"))
    )
    if not response_text:
        reasons.append("No assistant response text was returned.")

    diagnostics = _as_mapping(llm_debug_data.get("turn_execution_diagnostics"))
    routing = _as_mapping(diagnostics.get("workflow_routing_diagnostics"))
    dispatch = _as_mapping(routing.get("dispatch"))
    selected_execution_mode = _safe_text(dispatch.get("selected_execution_mode"))
    selected_workflow_id = _safe_text(
        dispatch.get("dispatch_workflow_id") or routing.get("selected_workflow_id")
    )
    dispatch_failure_reason = _safe_text(
        dispatch.get("dispatch_terminal_failure_reason")
    )
    if dispatch_failure_reason:
        reasons.append(f"Dispatch failed: {dispatch_failure_reason}.")
    dispatch_failure_detail = _safe_text(
        dispatch.get("dispatch_terminal_failure_detail")
    )
    if dispatch_failure_detail:
        reasons.append(f"Dispatch failure detail: {dispatch_failure_detail}.")

    completion_gate = _as_mapping(llm_debug_data.get("completion_gate_verdict"))
    completion_gate_status = _safe_text(
        completion_gate.get("status")
        or completion_gate.get("verdict")
        or completion_gate.get("decision")
    ).lower()
    if completion_gate_status in {"fail", "failed", "blocked", "deny", "denied"}:
        reasons.append(f"Completion gate reported {completion_gate_status}.")

    critic_verdict = _as_mapping(llm_debug_data.get("critic_verdict"))
    critic_status = _safe_text(
        critic_verdict.get("status")
        or critic_verdict.get("verdict")
        or critic_verdict.get("decision")
    ).lower()
    if critic_status in {"fail", "failed", "blocked", "deny", "denied"}:
        reasons.append(f"Critic verdict reported {critic_status}.")

    lowered_response = response_text.lower()
    if any(marker in lowered_response for marker in NEGATIVE_RESPONSE_MARKERS):
        reasons.append("Response contains a generic limitation or failure marker.")

    tool_names = _collect_tool_names(diagnostics, llm_debug_data)
    expected_tools = [
        _safe_text(name)
        for name in _as_list(prompt_entry.get("likely_tools"))
        if _safe_text(name)
    ]
    external_surfaces = {
        surface
        for surface in _as_list(prompt_entry.get("knowledge_surfaces"))
        if isinstance(surface, str) and surface in {"jira", "web", "arxiv"}
    }
    if external_surfaces and not tool_names:
        reasons.append(
            "Prompt expected external knowledge surfaces but no tool usage was recorded."
        )

    if len(response_text) < 40:
        reasons.append("Response was too short to plausibly satisfy the prompt.")

    if not reasons and not tool_names and selected_execution_mode != "direct_response":
        reasons.append(
            "No positive evidence of grounded execution was recorded for this turn."
        )

    should_user_be_happy = not reasons
    verdict = "happy" if should_user_be_happy else "unhappy"
    positive_evidence: list[str] = []
    if selected_workflow_id:
        positive_evidence.append(f"selected_workflow_id={selected_workflow_id}")
    if selected_execution_mode:
        positive_evidence.append(f"selected_execution_mode={selected_execution_mode}")
    if tool_names:
        positive_evidence.append(f"tools={','.join(tool_names)}")
    if expected_tools:
        positive_evidence.append(f"expected_tools={','.join(expected_tools)}")

    return {
        "verdict": verdict,
        "should_user_be_happy": should_user_be_happy,
        "reasons": reasons,
        "positive_evidence": positive_evidence,
        "response_length": len(response_text),
        "response_preview": response_text[:400],
        "selected_workflow_id": selected_workflow_id or None,
        "selected_execution_mode": selected_execution_mode or None,
        "observed_tools": tool_names,
    }


def _build_summary(
    *,
    prompt_entry: Mapping[str, Any],
    task_id: str,
    session_id: str,
    request_id: str,
    history_location: Mapping[str, Any],
    generate_payload: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    diagnostics = _as_mapping(llm_debug_data.get("turn_execution_diagnostics"))
    routing = _as_mapping(diagnostics.get("workflow_routing_diagnostics"))
    dispatch = _as_mapping(routing.get("dispatch"))
    tool_history = _as_list(diagnostics.get("tool_history"))
    return {
        "status": "ok",
        "prompt": {
            "id": _safe_text(prompt_entry.get("id")),
            "category": _safe_text(prompt_entry.get("category")),
            "text": _safe_text(prompt_entry.get("prompt")),
            "knowledge_surfaces": _as_list(prompt_entry.get("knowledge_surfaces")),
            "likely_tools": _as_list(prompt_entry.get("likely_tools")),
        },
        "conversation": {
            "session_id": session_id,
            "request_id": request_id,
            "background_task_id": task_id,
            "history_location": dict(history_location),
        },
        "response": {
            "text": (
                _safe_text(generate_payload.get("response"))
                or _safe_text(generate_payload.get("response_text"))
                or _safe_text(llm_debug_data.get("response"))
            ),
        },
        "telemetry": {
            "model": _safe_text(llm_debug_data.get("model")),
            "selected_workflow_id": _safe_text(
                dispatch.get("dispatch_workflow_id")
                or routing.get("selected_workflow_id")
                or _as_mapping(llm_debug_data.get("workflow_routing")).get(
                    "workflow_id"
                )
            ),
            "selected_execution_mode": _safe_text(
                dispatch.get("selected_execution_mode")
            ),
            "dispatch_terminal_failure_reason": _safe_text(
                dispatch.get("dispatch_terminal_failure_reason")
            )
            or None,
            "dispatch_terminal_failure_detail": _safe_text(
                dispatch.get("dispatch_terminal_failure_detail")
            )
            or None,
            "tool_history": tool_history,
            "tool_count": len(tool_history),
            "workflow_routing_diagnostics": routing,
        },
        "evaluation": dict(evaluation),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one sampled KB+tool-sensitive prompt against a live Von server "
            "and judge whether the user should be happy with the response."
        )
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--user-concept-id", default=DEFAULT_USER_CONCEPT_ID)
    parser.add_argument(
        "--organisation-concept-id", default=DEFAULT_ORGANISATION_CONCEPT_ID
    )
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--prompt-id", default="")
    parser.add_argument("--output-json", default="")
    parser.add_argument("--list-prompts", action="store_true")
    args = parser.parse_args(argv)

    prompt_bank_payload = _load_prompt_bank()
    prompt_bank = _as_list(prompt_bank_payload.get("prompts"))
    if args.list_prompts:
        print(
            json.dumps(
                [
                    {
                        "id": _safe_text(entry.get("id")),
                        "category": _safe_text(entry.get("category")),
                        "prompt": _safe_text(entry.get("prompt")),
                    }
                    for entry in prompt_bank
                    if isinstance(entry, Mapping)
                ],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    prompt_entry = _choose_prompt(
        prompt_bank,
        seed=args.seed,
        prompt_id=_safe_text(args.prompt_id) or None,
    )

    base_url = _safe_text(args.base_url).rstrip("/") or DEFAULT_BASE_URL
    session = requests.Session()
    session_id, _window_session_id = _establish_authenticated_session(
        session=session,
        base_url=base_url,
        user_concept_id=_safe_text(args.user_concept_id) or DEFAULT_USER_CONCEPT_ID,
        organisation_concept_id=(
            _safe_text(args.organisation_concept_id) or DEFAULT_ORGANISATION_CONCEPT_ID
        ),
        session_name=_safe_text(args.session_name) or DEFAULT_SESSION_NAME,
    )
    task_id, generate_payload = _run_generate_background(
        session=session,
        base_url=base_url,
        prompt=_safe_text(prompt_entry.get("prompt")),
        model=_safe_text(args.model) or DEFAULT_MODEL,
        timeout_seconds=float(args.timeout_seconds),
        poll_interval_seconds=float(args.poll_interval_seconds),
    )
    request_id, session_id = _extract_request_and_session_ids(
        task_id=task_id,
        generate_payload=generate_payload,
        default_session_id=session_id,
    )
    response_text = (
        _safe_text(generate_payload.get("response"))
        or _safe_text(generate_payload.get("response_text"))
        or _safe_text(_as_mapping(generate_payload.get("llm_debug")).get("response"))
    )
    history_location = _find_assistant_turn_history_location(
        session=session,
        base_url=base_url,
        session_id=session_id,
        request_id=request_id,
        response_text=response_text,
    )
    history_index_raw = history_location.get("history_index")
    if not isinstance(history_index_raw, int):
        raise RuntimeError(
            "History location did not include an integer history_index: "
            f"{history_location!r}"
        )
    history_index = history_index_raw
    llm_debug_data = _fetch_turn_debug(
        session=session,
        base_url=base_url,
        session_id=session_id,
        history_index=history_index,
    )
    evaluation = _evaluate_user_happiness(
        prompt_entry=prompt_entry,
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
    )
    summary = _build_summary(
        prompt_entry=prompt_entry,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        history_location=history_location,
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
        evaluation=evaluation,
    )
    output_json = _safe_text(args.output_json)
    if output_json:
        Path(output_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if bool(evaluation.get("should_user_be_happy")) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - CLI surface
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                },
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
