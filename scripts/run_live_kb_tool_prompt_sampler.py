"""Run one sampled KB+tool-sensitive prompt against a live Von server.

This script exercises the real `/von/generate` route in a fresh test
conversation, fetches persisted turn telemetry, and emits a conservative
verdict about whether the resulting answer would likely satisfy a user.

Use `--complexity-class` to constrain random selection to easier direct
questions, KB-grounded questions, or harder tool-augmented questions.

The prompt bank lives in `scripts/live_kb_tool_prompt_bank.json`. Runtime
selection loads that file so replay cases remain data artefacts rather than
task-specific Python policy.

Use this sampler together with
`docs/engineering/real_path_server_replay_and_telemetry_loop.md`.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import random
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.live_test_server_defaults import (
    DEFAULT_AGENT_TEST_BASE_URL,
    AGENT_TEST_BASE_URL_ENV_VAR,
    build_agent_test_server_requirement_error,
    get_default_agent_test_base_url,
    resolve_live_test_base_url,
)
from src.backend.services.model_registry_service import (
    DEFAULT_MINIMUM_REPLAY_CASES_FOR_CERTIFICATION,
    MODEL_STAGE_SUITABILITY_EVIDENCE_SCHEMA_VERSION,
    assess_model_stage_certification,
    build_model_stage_suitability_evidence,
    get_model_registry_snapshot,
)
from src.backend.services import (
    replay_arm_planning_service,
    replay_experiment_observation_service,
)

DEFAULT_BASE_URL = DEFAULT_AGENT_TEST_BASE_URL
DEFAULT_MODEL = "gemma4:31b"
DEFAULT_MINIMUM_REPLAY_SUCCESS_RATE = 0.95
DEFAULT_REPLAY_SET_ID = "JVNAUTOSCI-1894"
DEFAULT_USER_CONCEPT_ID = "#V#michael_witbrock"
DEFAULT_ORGANISATION_CONCEPT_ID = "university_of_auckland_strong_ai_lab"
DEFAULT_SESSION_NAME = "JVNAUTOSCI-1894 live prompt sample"
ACTIVE_AUTHENTICATED_MODEL_LABEL = "active_authenticated_model"
LOCAL_MODEL_PROVIDER_NAME = "ollama"
PREMIUM_MODEL_PROVIDER_NAMES = frozenset({"openai", "anthropic", "gemini", "azure_openai"})
KNOWN_MODEL_PROVIDER_NAMES = frozenset(
    {LOCAL_MODEL_PROVIDER_NAME, *PREMIUM_MODEL_PROVIDER_NAMES}
)
PREMIUM_MODEL_PREFIXES = (
    "gpt-",
    "gpt4",
    "gpt5",
    "o1",
    "o3",
    "o4",
    "claude",
    "gemini",
    "text-davinci",
)
MODEL_PORTFOLIO_REPLAY_REPORT_SCHEMA_VERSION = "model_portfolio_replay_report.v1"
LOCAL_OLLAMA_REPLAY_MODEL_CATALOGUE_SCHEMA_VERSION = (
    "local_ollama_replay_model_catalogue.v1"
)
LOCAL_OLLAMA_REPLAY_PROBE_SCHEMA_VERSION = "local_ollama_replay_probe.v1"
DEFAULT_LOCAL_MODEL_PROBE_CACHE_PATH = Path(
    "artifacts/local_ollama_replay_model_probe_cache.json"
)
CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST = "coding_agent_test"
CHAT_SESSION_CREATED_BY_ACTOR_CONCEPT_ID = "#V#von_system"
CHAT_SESSION_CREATED_BY_ACTOR_TYPE = "#V#coding_agent"
LIVE_PROMPT_SAMPLER_TEST_ARTIFACT_KIND = "live_kb_tool_prompt_sampler_chat_session"
PROMPT_BANK_PATH = Path(__file__).with_name("live_kb_tool_prompt_bank.json")
REAL_PATH_REPLAY_GUIDE = (
    "docs/engineering/real_path_server_replay_and_telemetry_loop.md"
)
REAL_PATH_REPLAY_GUIDE_NOTE = (
    "Use this random prompt sampler together with "
    f"`{REAL_PATH_REPLAY_GUIDE}`. Before judging the turn, follow that guide's "
    "expectation-first preflight, real-path replay, false-empty verification, "
    "telemetry review, and thinking-panel review steps."
)
SERVER_METADATA_TIMEOUT_SECONDS = 15.0
ACTIVE_LLM_INFO_TIMEOUT_SECONDS = 15.0
LOCAL_OLLAMA_REPLAY_MODEL_CATALOGUE: tuple[dict[str, Any], ...] = (
    {
        "model": "granite3.3:2b",
        "strength_rank": 10,
        "relative_cost_rank": 10,
        "notes": "Small local smoke-test candidate; often too weak for workflow selection.",
    },
    {
        "model": "llama3.2:latest",
        "strength_rank": 20,
        "relative_cost_rank": 15,
        "notes": "Small local general-purpose candidate.",
    },
    {
        "model": "llama3:latest",
        "strength_rank": 35,
        "relative_cost_rank": 25,
        "notes": "Mid-small local general-purpose candidate.",
    },
    {
        "model": "gemma4:e4b",
        "strength_rank": 50,
        "relative_cost_rank": 40,
        "notes": "Default replay-local candidate observed in prior Von replay work.",
    },
    {
        "model": "gpt-oss:20b",
        "strength_rank": 65,
        "relative_cost_rank": 55,
        "notes": "Local Ollama open-weight candidate; not an OpenAI API model.",
    },
    {
        "model": "gemma4:26b",
        "strength_rank": 75,
        "relative_cost_rank": 65,
        "notes": "Stronger local replay candidate; slower but useful for workflow reasoning.",
    },
    {
        "model": "gemma4:31b",
        "strength_rank": 78,
        "relative_cost_rank": 68,
        "notes": "Current default local replay candidate on origin/main.",
    },
    {
        "model": "qwen3.5:27b",
        "strength_rank": 80,
        "relative_cost_rank": 70,
        "notes": "Stronger local reasoning candidate when installed.",
    },
    {
        "model": "llama3.3:70b",
        "strength_rank": 95,
        "relative_cost_rank": 95,
        "notes": "High-cost local fallback; use only after cheaper installed candidates fail.",
    },
)
PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS: dict[str, str] = {
    "direct_context_or_background": (
        "Questions that a capable direct-response LLM should usually answer from "
        "turn context, runtime context, or background knowledge without needing "
        "KB grounding or extra tools."
    ),
    "vontology_grounded": (
        "Questions that should build on direct/context knowledge plus represented "
        "Vontology or KB content, but do not inherently need live external tool "
        "surfaces such as web, Jira, or arXiv."
    ),
    "vontology_plus_single_tool": (
        "Questions that build on represented Vontology or KB context and invite "
        "one additional live retrieval or operational tool surface such as "
        "arXiv, Jira, or RAG, without broader multi-tool orchestration."
    ),
    "tool_augmented": (
        "Questions that combine direct/context and KB reasoning with live tool "
        "use, especially web, Jira, arXiv, or other external/operational surfaces."
    ),
}
HARD_FAILURE_RESPONSE_MARKERS = (
    "i couldn't complete",
    "i could not complete",
    "i can't access",
    "i cannot access",
    "not authenticated",
    "workflow not runnable",
    "instance was not created",
    "authoritative conversation-turn workflow failed",
    "did not produce a user-visible response",
    "workflow_llm_step_timeout",
    "llm call timed out",
    "i am unable to retrieve",
    "i am unable to provide",
    "unable to retrieve the information",
    "unable to access the necessary data",
    "necessary tool was not permitted",
    "execution status: required grounded evidence was not retrieved",
)
SOFT_FAILURE_RESPONSE_MARKERS = (
    "i don't currently have",
    "i do not currently have",
    "i don't have enough information",
    "i do not have enough information",
    "i don't know",
    "i do not know",
)
GROUNDED_EMPTY_RESULT_MARKERS = (
    "no pending von tasks",
    "no pending tasks",
    "no unread von messages",
    "no unread messages",
    "no von messages",
    "no messages",
    "don't currently have any pending von tasks",
    "do not currently have any pending von tasks",
    "don't currently have any unread von messages",
    "do not currently have any unread von messages",
    "no matching tasks",
    "couldn't find any matching tasks",
    "could not find any matching tasks",
    "didn't find any matching tasks",
    "did not find any matching tasks",
)
INVENTORY_ONLY_TOOLS = frozenset({"list_papers"})
RELATIONSHIP_CLAIM_MARKERS = (
    "your papers",
    "papers of yours",
    "my papers",
    "our papers",
)
VONTOLOGY_CONCEPT_ID_RE = re.compile(r"#V#[-A-Za-z0-9_./:]+")
TERMINAL_CONCEPT_ID_PUNCTUATION = ".,;:!?"
CONSERVATIVE_CANONICAL_ID_NEAR_MISS_DISTANCE = 2
EXPLICIT_CANONICAL_CONCEPT_ID_KEYS = (
    "canonical_concept_id",
    "canonical_subject_concept_id",
    "expected_canonical_concept_id",
    "expected_subject_concept_id",
    "expected_concept_id",
)
EXPLICIT_CANONICAL_CONCEPT_IDS_KEYS = (
    "canonical_concept_ids",
    "canonical_subject_concept_ids",
    "expected_canonical_concept_ids",
    "expected_subject_concept_ids",
    "expected_concept_ids",
)
AUTHENTICATED_USER_CONCEPT_ID_KEYS = (
    "authenticated_user_concept_id",
    "server_effective_user_concept_id",
    "server_header_user_concept_id",
    "server_session_user_concept_id",
)
DIAGNOSTIC_EVIDENCE_EFFECT_TYPE = "diagnostic_evidence"
DIAGNOSTIC_EVIDENCE_KNOWLEDGE_SURFACES = frozenset(
    {
        "conversation_diagnostics",
        "conversation_history",
        "conversation_telemetry",
        "stored_chat_context",
        "turn_execution_diagnostics",
        "turn_telemetry",
    }
)
DIAGNOSTIC_EVIDENCE_TOOLS = frozenset(
    {
        "conversation_telemetry_get_locator",
        "chat_history_get_segments",
        "chat_history_get_debug_entry",
        "turn_execution_get_diagnostics",
        "workflow_get_execution_trace",
    }
)
PROMPT_BANK_PAYLOAD: dict[str, Any] = {
    "schema_version": "live_kb_tool_prompt_bank.v3",
    "description": (
        "Prompt bank for live /von/generate sampling of turns that range from "
        "easy direct-response questions through KB-grounded questions to "
        "tool-augmented prompts, including local operational prompts for Von "
        "task and message creation/manipulation."
    ),
    "complexity_classes": PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS,
    "prompts": [
        {
            "id": "who_am_i_in_this_conversation",
            "category": "identity_context",
            "complexity_class": "direct_context_or_background",
            "prompt": "Who am I in this conversation?",
            "knowledge_surfaces": ["turn_context"],
            "likely_tools": [],
        },
        {
            "id": "what_is_my_organisation_here",
            "category": "identity_context",
            "complexity_class": "direct_context_or_background",
            "prompt": "What organisation am I currently working in here?",
            "knowledge_surfaces": ["turn_context"],
            "likely_tools": [],
        },
        {
            "id": "what_time_is_it_here_right_now",
            "category": "runtime_context",
            "complexity_class": "direct_context_or_background",
            "prompt": "What time is it here right now?",
            "knowledge_surfaces": ["runtime_context"],
            "likely_tools": [],
        },
        {
            "id": "what_is_the_capital_of_france",
            "category": "general_knowledge",
            "complexity_class": "direct_context_or_background",
            "prompt": "What is the capital of France?",
            "knowledge_surfaces": ["background_knowledge"],
            "likely_tools": [],
        },
        {
            "id": "key_predicates_for_scientific_papers",
            "category": "ontology_predicate_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What are key predicates for scientific papers in Vontology?",
            "knowledge_surfaces": ["background_knowledge", "kb"],
            "likely_tools": ["search_concepts", "get_predicate_incidence"],
        },
        {
            "id": "key_predicates_for_sail_students",
            "category": "ontology_predicate_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": (
                "What are key predicates or represented relationships for SAIL "
                "students?"
            ),
            "knowledge_surfaces": ["background_knowledge", "kb"],
            "likely_tools": ["search_concepts", "get_predicate_incidence"],
        },
        {
            "id": "salient_predicates_for_sail_students",
            "category": "ontology_predicate_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What predicates are salient to SAIL students?",
            "knowledge_surfaces": ["background_knowledge", "kb"],
            "likely_tools": ["search_concepts", "get_predicate_incidence"],
        },
        {
            "id": "text_relations_for_michael_witbrock_concept",
            "category": "represented_relation_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What text relations are used with the concept for Michael Witbrock?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["get_text_relations_summary"],
            "requires_tool_use": True,
        },
        {
            "id": "list_my_papers",
            "category": "entity_relative_kb_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "List my papers.",
            "knowledge_surfaces": ["turn_context", "kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "list_publications_in_2026_from_people_in_sail",
            "category": "organisational_publication_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "List publications in 2026 from people in SAIL.",
            "knowledge_surfaces": ["background_knowledge", "kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "what_papers_of_mine_do_you_know_about",
            "category": "entity_relative_kb_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "What papers of mine do you know about?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "tell_me_who_i_am_and_list_my_papers",
            "category": "entity_relative_kb_lookup",
            "complexity_class": "vontology_grounded",
            "prompt": "Tell me who I am and list my papers.",
            "knowledge_surfaces": ["turn_context", "kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "research_interests_and_collaborators",
            "category": "profile_and_network_summary",
            "complexity_class": "vontology_grounded",
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
            "complexity_class": "vontology_grounded",
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
            "complexity_class": "vontology_grounded",
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
            "complexity_class": "vontology_grounded",
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
            "complexity_class": "vontology_grounded",
            "prompt": (
                "Tell me about myself as represented here, but separate "
                "established facts from likely inferences."
            ),
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
        },
        {
            "id": "one_recent_arxiv_paper_for_my_agent_memory_work",
            "category": "single_tool_arxiv_recommendation",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Given what is represented about my work, find one recent arXiv "
                "paper on agent memory that I should read next."
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_arxiv"],
            "requires_tool_use": True,
        },
        {
            "id": "one_recent_arxiv_paper_for_my_neurosymbolic_work",
            "category": "single_tool_arxiv_recommendation",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Given what is represented about my work, find one recent arXiv "
                "paper on neuro-symbolic agents that looks relevant."
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_arxiv"],
            "requires_tool_use": True,
        },
        {
            "id": "one_recent_arxiv_paper_for_sail_workflow_work",
            "category": "single_tool_arxiv_recommendation",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Using the represented SAIL context, find one recent arXiv paper "
                "on workflow orchestration that looks relevant to our work."
            ),
            "knowledge_surfaces": ["kb", "arxiv"],
            "likely_tools": ["search_arxiv"],
            "requires_tool_use": True,
        },
        {
            "id": "summarise_jvnautosci_1894_parent_replay_programme",
            "category": "single_tool_jira_summary",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Summarise JVNAUTOSCI-1894 as the parent replay programme for "
                "these tests."
            ),
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_get_issue"],
            "requires_tool_use": True,
        },
        {
            "id": "what_does_jvnautosci_1925_ask_to_record",
            "category": "single_tool_jira_summary",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "What does JVNAUTOSCI-1925 ask the replayer to record?",
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_get_issue"],
            "requires_tool_use": True,
        },
        {
            "id": "tell_me_about_jvnautosci_150_in_jira",
            "category": "single_tool_jira_summary",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "Tell me about JVNAUTOSCI-150 in JIRA",
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_get_issue"],
            "requires_tool_use": True,
        },
        {
            "id": "parent_and_subtasks_for_jvnautosci_150",
            "category": "single_tool_jira_summary",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "What parent and subtasks are recorded for JVNAUTOSCI-150?",
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_get_issue"],
            "requires_tool_use": True,
        },
        {
            "id": "summarise_jvnautosci_2097_tool_plan_repair_task",
            "category": "single_tool_jira_summary",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Summarise JVNAUTOSCI-2097 and explain the tool-calling failure "
                "it asks us to fix."
            ),
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_get_issue"],
            "requires_tool_use": True,
        },
        {
            "id": "which_open_jira_issue_is_the_parent_replay_programme",
            "category": "single_tool_jira_search",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Which open Jira issue is the parent replay programme for these "
                "KB and tool stability subtasks?"
            ),
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_search"],
            "requires_tool_use": True,
        },
        {
            "id": "which_jira_task_tracks_tool_call_repair_critic",
            "category": "single_tool_jira_search",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": (
                "Which Jira task tracks adding a one-shot tool-call repair critic "
                "for malformed required tool plans?"
            ),
            "knowledge_surfaces": ["turn_context", "jira"],
            "likely_tools": ["jira_search"],
            "requires_tool_use": True,
        },
        {
            "id": "list_last_ten_zhan_gmail_messages",
            "category": "single_tool_gmail_listing",
            "complexity_class": "tool_augmented",
            "prompt": (
                "List the last ten email messages received by "
                "zhanvonwitbrock@gmail.com the zhan-gmail identity"
            ),
            "knowledge_surfaces": ["turn_context", "gmail"],
            "likely_tools": ["gmail_list_messages", "gmail_get_message"],
            "requires_tool_use": True,
        },
        {
            "id": "what_research_interests_of_mine_are_explicitly_represented_here",
            "category": "single_tool_rag_lookup",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "What research interests of mine are explicitly represented here?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
            "requires_tool_use": True,
        },
        {
            "id": "what_collaborators_of_mine_are_explicitly_represented_here",
            "category": "single_tool_rag_lookup",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "What collaborators of mine are explicitly represented here?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
            "requires_tool_use": True,
        },
        {
            "id": "what_projects_of_mine_are_explicitly_represented_here",
            "category": "single_tool_rag_lookup",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "What projects of mine are explicitly represented here?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
            "requires_tool_use": True,
        },
        {
            "id": "what_sail_people_or_roles_are_explicitly_represented_here",
            "category": "single_tool_rag_lookup",
            "complexity_class": "vontology_plus_single_tool",
            "prompt": "What SAIL people or roles are explicitly represented here?",
            "knowledge_surfaces": ["kb"],
            "likely_tools": ["search_knowledge_base"],
            "requires_tool_use": True,
        },
        {
            "id": "create_von_task_for_replay_review",
            "category": "von_task_creation",
            "complexity_class": "tool_augmented",
            "prompt": (
                "Create a Von task for me titled 'Review replay results' with a "
                "short description saying it came from the JVNAUTOSCI-1894 "
                "replay programme."
            ),
            "knowledge_surfaces": ["turn_context", "von_tasks"],
            "likely_tools": ["task_create"],
            "requires_tool_use": True,
        },
        {
            "id": "list_my_pending_von_tasks",
            "category": "von_task_listing",
            "complexity_class": "tool_augmented",
            "prompt": "List my pending Von tasks.",
            "knowledge_surfaces": ["turn_context", "von_tasks"],
            "likely_tools": ["task_list"],
            "requires_tool_use": True,
            "allows_grounded_empty_result": True,
        },
        {
            "id": "search_my_von_tasks_for_replay_or_thinking_card_work",
            "category": "von_task_search",
            "complexity_class": "tool_augmented",
            "prompt": (
                "Search my Von tasks for anything about replay testing or the "
                "thinking card."
            ),
            "knowledge_surfaces": ["turn_context", "von_tasks"],
            "likely_tools": ["task_search"],
            "requires_tool_use": True,
            "allows_grounded_empty_result": True,
        },
        {
            "id": "mark_replay_related_von_task_in_progress",
            "category": "von_task_status_update",
            "complexity_class": "tool_augmented",
            "prompt": (
                "If I have a pending Von task about replay testing, mark it in "
                "progress and tell me which task you changed."
            ),
            "knowledge_surfaces": ["turn_context", "von_tasks"],
            "likely_tools": ["task_search", "task_update_status"],
            "requires_tool_use": True,
            "allows_grounded_empty_result": True,
        },
        {
            "id": "send_myself_a_von_message_about_replay_results",
            "category": "von_message_creation",
            "complexity_class": "tool_augmented",
            "prompt": (
                "Send me a Von message reminding me to review the latest replay "
                "results."
            ),
            "knowledge_surfaces": ["turn_context", "von_messages"],
            "likely_tools": ["message_create"],
            "requires_tool_use": True,
        },
        {
            "id": "count_my_unread_von_messages",
            "category": "von_message_listing",
            "complexity_class": "tool_augmented",
            "prompt": "How many unread Von messages do I have right now?",
            "knowledge_surfaces": ["turn_context", "von_messages"],
            "likely_tools": ["message_list"],
            "requires_tool_use": True,
            "allows_grounded_empty_result": True,
        },
        {
            "id": "closest_kb_papers_to_recent_arxiv_interests",
            "category": "personalised_arxiv_recommendation",
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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
            "complexity_class": "tool_augmented",
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

EMBEDDED_PROMPT_BANK_PAYLOAD = PROMPT_BANK_PAYLOAD


def _load_prompt_bank_payload_from_file() -> dict[str, Any]:
    payload = json.loads(PROMPT_BANK_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Prompt bank file must contain a JSON object.")
    return payload


PROMPT_BANK_PAYLOAD = _load_prompt_bank_payload_from_file()


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


def _dedupe_texts(values: Sequence[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _safe_text(value)
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(cleaned)
    return deduped


def _split_model_provider_prefix(value: Any) -> tuple[str | None, str]:
    cleaned = _safe_text(value)
    if not cleaned or ":" not in cleaned:
        return None, cleaned
    provider, remainder = cleaned.split(":", 1)
    provider_key = provider.strip().lower()
    if provider_key in KNOWN_MODEL_PROVIDER_NAMES:
        return provider_key, remainder.strip()
    return None, cleaned


def _infer_provider_from_model_identifier(value: Any) -> str | None:
    provider, bare_model = _split_model_provider_prefix(value)
    if provider:
        return provider
    lowered = bare_model.lower()
    if not lowered:
        return None
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith("gemini"):
        return "gemini"
    if any(lowered.startswith(prefix) for prefix in PREMIUM_MODEL_PREFIXES):
        return "openai"
    if ":" in bare_model and not lowered.startswith("ft:"):
        return LOCAL_MODEL_PROVIDER_NAME
    return None


def _model_identifier_looks_premium(value: Any) -> bool:
    provider, bare_model = _split_model_provider_prefix(value)
    if provider == LOCAL_MODEL_PROVIDER_NAME:
        return False
    if _provider_looks_premium(provider):
        return True
    lowered = bare_model.lower()
    return bool(lowered) and any(
        lowered.startswith(prefix) for prefix in PREMIUM_MODEL_PREFIXES
    )


def _provider_looks_premium(value: Any) -> bool:
    cleaned = _safe_text(value).lower()
    return cleaned in PREMIUM_MODEL_PROVIDER_NAMES


def _build_model_policy_report(
    *,
    requested_model_arms: Sequence[Mapping[str, Any]],
    run_environment: Mapping[str, Any],
    allow_premium_model: bool,
) -> dict[str, Any]:
    active_provider = _safe_text(
        run_environment.get("server_resolved_active_llm_provider")
    ) or None
    active_model = _safe_text(run_environment.get("server_resolved_active_llm_model")) or None
    active_lookup_error = _safe_text(
        run_environment.get("server_resolved_active_llm_lookup_error")
    ) or None
    arms: list[dict[str, Any]] = []
    for arm in requested_model_arms:
        requested_model = _safe_text(arm.get("requested_model")) or None
        requested_provider = _safe_text(arm.get("requested_provider")) or None
        if requested_model and not requested_provider:
            requested_provider = _infer_provider_from_model_identifier(requested_model)
        source = "explicit_model_override" if requested_model else "active_authenticated_model"
        effective_model = requested_model or active_model
        effective_provider = requested_provider if requested_model else active_provider
        premium = (
            _model_identifier_looks_premium(effective_model)
            or _provider_looks_premium(effective_provider)
        )
        unverifiable_active_model = (
            requested_model is None and not active_model and bool(active_lookup_error)
        )
        arms.append(
            {
                "arm_id": _safe_text(arm.get("arm_id")) or None,
                "label": _safe_text(arm.get("label")) or None,
                "source": source,
                "requested_model": requested_model,
                "requested_provider": requested_provider,
                "effective_model": effective_model,
                "effective_provider": effective_provider,
                "premium_model_deviation": premium,
                "active_model_unverified": unverifiable_active_model,
            }
        )
    premium_arm_count = sum(
        1 for arm in arms if bool(arm.get("premium_model_deviation"))
    )
    unverified_active_arm_count = sum(
        1 for arm in arms if bool(arm.get("active_model_unverified"))
    )
    return {
        "default_model": DEFAULT_MODEL,
        "local_only_default": True,
        "premium_model_allowed": bool(allow_premium_model),
        "premium_model_deviation": premium_arm_count > 0,
        "premium_model_deviation_count": premium_arm_count,
        "unverified_active_model_arm_count": unverified_active_arm_count,
        "arms": arms,
    }


def _enforce_model_policy(report: Mapping[str, Any]) -> None:
    if bool(report.get("premium_model_allowed")):
        return
    premium_arms = [
        arm
        for arm in _as_list(report.get("arms"))
        if isinstance(arm, Mapping)
        and (
            bool(arm.get("premium_model_deviation"))
            or bool(arm.get("active_model_unverified"))
        )
    ]
    if not premium_arms:
        return
    raise RuntimeError(
        "Replay sampler defaults to local-only model execution. Premium or "
        "unverified active-model arms require --allow-premium-model. "
        f"Model policy report: {json.dumps(dict(report), ensure_ascii=True, sort_keys=True)}"
    )


def _load_json_mapping_argument(value: str, *, argument_name: str) -> dict[str, Any]:
    cleaned = _safe_text(value)
    if not cleaned:
        return {}
    raw_text = cleaned
    if cleaned.startswith("@"):
        raw_text = Path(cleaned[1:]).read_text(encoding="utf-8")
    elif cleaned.startswith("{"):
        raw_text = cleaned
    else:
        candidate_path = Path(cleaned)
        try:
            path_exists = candidate_path.exists() and candidate_path.is_file()
        except OSError:
            path_exists = False
        if path_exists:
            raw_text = candidate_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{argument_name} must be JSON or @path to JSON: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"{argument_name} must decode to a JSON object.")
    return {str(key): item for key, item in payload.items() if isinstance(key, str)}


def _normalise_prompt_entry(
    *,
    prompt_text: str,
    replay_case_id: str,
    category: str,
    likely_tools: Sequence[Any] = (),
    knowledge_surfaces: Sequence[Any] = (),
    requires_tool_use: bool | None = None,
    source_kind: str | None = None,
    source_request_id: str | None = None,
    source_workflow_id: str | None = None,
) -> dict[str, Any]:
    cleaned_prompt = _safe_text(prompt_text)
    _assert(bool(cleaned_prompt), "Replay prompt text cannot be empty.")
    tool_names = _dedupe_texts(likely_tools)
    surfaces = _dedupe_texts(knowledge_surfaces)
    return {
        "id": _safe_text(replay_case_id) or f"ad_hoc_replay_{uuid.uuid4().hex[:12]}",
        "category": _safe_text(category) or "ad_hoc_replay",
        "complexity_class": "tool_augmented" if tool_names else "vontology_grounded",
        "prompt": cleaned_prompt,
        "knowledge_surfaces": surfaces or ["turn_context"],
        "likely_tools": tool_names,
        "requires_tool_use": bool(tool_names)
        if requires_tool_use is None
        else bool(requires_tool_use),
        "source_kind": _safe_text(source_kind) or None,
        "source_request_id": _safe_text(source_request_id) or None,
        "source_workflow_id": _safe_text(source_workflow_id) or None,
    }


def _build_prompt_entry_from_failure_case(
    failure_case: Mapping[str, Any],
    *,
    replay_case_id: str | None,
) -> dict[str, Any]:
    turn = _as_mapping(failure_case.get("turn"))
    prompt_payload = _as_mapping(turn.get("prompt"))
    prompt_text = _safe_text(prompt_payload.get("text"))
    workflow = _as_mapping(failure_case.get("workflow"))
    tool_ledger = _as_mapping(failure_case.get("tool_ledger"))
    by_tool = [
        _safe_text(_as_mapping(item).get("tool"))
        for item in _as_list(tool_ledger.get("by_tool"))
    ]
    required_tools = [
        _safe_text(item) for item in _as_list(tool_ledger.get("required_tools"))
    ]
    request_id = _safe_text(failure_case.get("request_id"))
    case_id = _safe_text(replay_case_id) or request_id or "failure_case_replay"
    return _normalise_prompt_entry(
        prompt_text=prompt_text,
        replay_case_id=case_id,
        category="failure_case_replay",
        likely_tools=[*by_tool, *required_tools],
        knowledge_surfaces=["conversation_history", "turn_execution_diagnostics"],
        requires_tool_use=bool(by_tool or required_tools),
        source_kind="failure_case_intake",
        source_request_id=request_id or None,
        source_workflow_id=_safe_text(workflow.get("selected_workflow_id")) or None,
    )


def _collect_failure_case_prompt_entry(
    *,
    conversation_ref: Mapping[str, Any] | None,
    chat_history_lookup: Mapping[str, Any] | None,
    request_id: str | None,
    session_id: str | None,
    namespace: str | None,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    target_model: str | None,
    comparator_model: str | None,
    workflow_id: str | None,
    stage_id: str | None,
    current_request_id: str | None,
    reference_mode: str | None,
    reference_phrase: str | None,
    include_legacy: bool | None,
    history_tail_limit: int | None,
    replay_case_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from src.backend.services.failure_case_intake_service import (
        collect_failure_case_intake,
    )

    failure_case = collect_failure_case_intake(
        conversation_ref=conversation_ref,
        chat_history_lookup=chat_history_lookup,
        request_id=request_id,
        session_id=session_id,
        namespace=namespace,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        target_model=target_model,
        comparator_model=comparator_model,
        workflow_id=workflow_id,
        stage_id=stage_id,
        current_request_id=current_request_id,
        reference_mode=reference_mode,
        reference_phrase=reference_phrase,
        include_legacy=include_legacy,
        history_tail_limit=history_tail_limit,
    )
    if failure_case.get("success") is False:
        raise RuntimeError(
            "Failure-case intake did not produce a replayable prompt: "
            f"{json.dumps(failure_case, ensure_ascii=True, sort_keys=True)[:1200]}"
        )
    return (
        _build_prompt_entry_from_failure_case(
            failure_case,
            replay_case_id=replay_case_id,
        ),
        dict(failure_case),
    )


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def _normalise_concept_id(value: Any) -> str | None:
    text = _safe_text(value)
    if not text:
        return None
    text = text.rstrip(TERMINAL_CONCEPT_ID_PUNCTUATION)
    if text.startswith("#v#"):
        text = f"#V#{text[3:]}"
    elif text.startswith("V#") or text.startswith("v#"):
        text = f"#V#{text[2:]}"
    if not text.startswith("#V#"):
        return None
    if VONTOLOGY_CONCEPT_ID_RE.fullmatch(text) is None:
        return None
    return text


def _append_unique_concept_id(target: list[str], value: Any) -> None:
    concept_id = _normalise_concept_id(value)
    if concept_id and concept_id not in target:
        target.append(concept_id)


def _collect_expected_canonical_concept_ids(
    *,
    prompt_entry: Mapping[str, Any],
    run_environment: Mapping[str, Any] | None,
    llm_debug_data: Mapping[str, Any],
) -> list[str]:
    expected_ids: list[str] = []

    for key in EXPLICIT_CANONICAL_CONCEPT_ID_KEYS:
        _append_unique_concept_id(expected_ids, prompt_entry.get(key))
    for key in EXPLICIT_CANONICAL_CONCEPT_IDS_KEYS:
        for value in _as_list(prompt_entry.get(key)):
            _append_unique_concept_id(expected_ids, value)

    environment = _as_mapping(run_environment)
    for key in AUTHENTICATED_USER_CONCEPT_ID_KEYS:
        _append_unique_concept_id(expected_ids, environment.get(key))

    namespace_context = _as_mapping(llm_debug_data.get("namespace_context"))
    _append_unique_concept_id(expected_ids, namespace_context.get("user_id"))

    for key in (
        "authenticated_user_concept_id",
        "effective_user_concept_id",
        "user_concept_id",
    ):
        _append_unique_concept_id(expected_ids, llm_debug_data.get(key))

    return expected_ids


def _extract_vontology_concept_ids(text: str) -> list[str]:
    concept_ids: list[str] = []
    for match in VONTOLOGY_CONCEPT_ID_RE.finditer(text or ""):
        concept_id = _normalise_concept_id(match.group(0))
        if concept_id and concept_id not in concept_ids:
            concept_ids.append(concept_id)
    return concept_ids


def _bounded_edit_distance(left: str, right: str, max_distance: int) -> int:
    if abs(len(left) - len(right)) > max_distance:
        return max_distance + 1
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_min = current[0]
        for right_index, right_char in enumerate(right, start=1):
            substitution_cost = 0 if left_char == right_char else 1
            value = min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1] + substitution_cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _concept_id_near_miss_distance(expected_id: str, observed_id: str) -> int | None:
    if expected_id == observed_id:
        return None
    expected_slug = expected_id[3:] if expected_id.startswith("#V#") else expected_id
    observed_slug = observed_id[3:] if observed_id.startswith("#V#") else observed_id
    distance = _bounded_edit_distance(
        expected_slug,
        observed_slug,
        CONSERVATIVE_CANONICAL_ID_NEAR_MISS_DISTANCE,
    )
    if 0 < distance <= CONSERVATIVE_CANONICAL_ID_NEAR_MISS_DISTANCE:
        return distance
    return None


def _evaluate_canonical_concept_id_fidelity(
    *,
    prompt_entry: Mapping[str, Any],
    response_text: str,
    run_environment: Mapping[str, Any] | None,
    llm_debug_data: Mapping[str, Any],
) -> dict[str, Any]:
    expected_ids = _collect_expected_canonical_concept_ids(
        prompt_entry=prompt_entry,
        run_environment=run_environment,
        llm_debug_data=llm_debug_data,
    )
    observed_ids = _extract_vontology_concept_ids(response_text)
    findings: list[dict[str, Any]] = []

    if expected_ids and observed_ids:
        for expected_id in expected_ids:
            if expected_id in observed_ids:
                continue
            for observed_id in observed_ids:
                distance = _concept_id_near_miss_distance(expected_id, observed_id)
                if distance is None:
                    continue
                findings.append(
                    {
                        "reason_code": "canonical_concept_id_mismatch",
                        "severity": "failed",
                        "expected_concept_id": expected_id,
                        "observed_concept_id": observed_id,
                        "edit_distance": distance,
                        "message": (
                            "Response displayed a near-miss Vontology concept ID "
                            f"{observed_id} where canonical ID {expected_id} was "
                            "the expected grounded subject."
                        ),
                    }
                )

    status = "failed" if findings else "passed" if expected_ids else "not_applicable"
    return {
        "status": status,
        "expected_concept_ids": expected_ids,
        "observed_concept_ids": observed_ids,
        "findings": findings,
    }


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class BackgroundGenerateTaskError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        task_id: str | None = None,
        status_payload: Mapping[str, Any] | None = None,
        cancellation_payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.status_payload = dict(status_payload or {})
        self.cancellation_payload = dict(cancellation_payload or {})

    def to_report(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status_payload": self.status_payload,
            "cancellation_payload": self.cancellation_payload,
        }


def _build_replay_session_creation_payload(session_name: str) -> dict[str, Any]:
    # Mirrors the create_chat_session provenance contract without importing backend services.
    return {
        "session_name": session_name,
        "origin_kind": CHAT_SESSION_ORIGIN_KIND_CODING_AGENT_TEST,
        "created_by_actor_concept_id": CHAT_SESSION_CREATED_BY_ACTOR_CONCEPT_ID,
        "created_by_actor_type": CHAT_SESSION_CREATED_BY_ACTOR_TYPE,
        "is_agent_created": True,
        "test_artifact_kind": LIVE_PROMPT_SAMPLER_TEST_ARTIFACT_KIND,
    }


def _emit_replay_guide_note() -> None:
    print(
        f"NOTE: {REAL_PATH_REPLAY_GUIDE_NOTE}",
        file=sys.stderr,
    )


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
    payload = _load_prompt_bank_payload_from_file()
    if payload != PROMPT_BANK_PAYLOAD:
        raise RuntimeError(
            "Prompt bank file changed after the sampler module was imported. "
            "Re-run the sampler so replay selection uses a single prompt-bank snapshot."
        )
    return json.loads(json.dumps(payload))


def _write_json_output(output_json: str, payload: Mapping[str, Any]) -> None:
    output_path = Path(output_json)
    if output_path.parent != Path("."):
        output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _normalise_ollama_model_name(model_name: str | None) -> str:
    provider, bare_model = _split_model_provider_prefix(model_name)
    if provider == LOCAL_MODEL_PROVIDER_NAME:
        return bare_model.lower()
    return _safe_text(model_name).lower()


def _build_local_ollama_generate_model_override(model_name: str | None) -> str:
    cleaned = _safe_text(model_name)
    if not cleaned:
        return ""
    provider, _bare_model = _split_model_provider_prefix(cleaned)
    if provider == LOCAL_MODEL_PROVIDER_NAME:
        return cleaned
    return f"{LOCAL_MODEL_PROVIDER_NAME}:{cleaned}"


def _parse_ollama_list_output(output: str) -> set[str]:
    installed: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("name"):
            continue
        model_name = line.split()[0].strip()
        if model_name:
            installed.add(_normalise_ollama_model_name(model_name))
    return installed


def _list_installed_ollama_models() -> set[str]:
    try:
        completed = subprocess.run(
            ["ollama", "list"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return _parse_ollama_list_output(completed.stdout)


def _pull_ollama_model(model_name: str) -> dict[str, Any]:
    started = time.time()
    try:
        completed = subprocess.run(
            ["ollama", "pull", model_name],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "model": model_name,
            "status": "error",
            "error": str(exc),
            "duration_seconds": round(time.time() - started, 3),
        }
    return {
        "model": model_name,
        "status": "ok" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-1000:],
        "stderr_tail": completed.stderr[-1000:],
        "duration_seconds": round(time.time() - started, 3),
    }


def _coerce_rank(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return fallback


def _registry_local_ollama_model_entries() -> list[dict[str, Any]]:
    try:
        snapshot = get_model_registry_snapshot()
    except Exception:
        return []
    snapshot_source = _safe_text(snapshot.get("source")) or "model_registry"
    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(_as_list(snapshot.get("models"))):
        if not isinstance(entry, Mapping):
            continue
        provider = _safe_text(entry.get("provider")).lower()
        locality = _safe_text(entry.get("locality")).lower()
        if provider != LOCAL_MODEL_PROVIDER_NAME and locality != "local":
            continue
        model = _safe_text(entry.get("model_id"))
        if not model:
            aliases = _as_list(entry.get("model_aliases"))
            model = _safe_text(aliases[0]) if aliases else ""
        if not model:
            continue
        entries.append(
            {
                "model": _normalise_ollama_model_name(model),
                "provider": LOCAL_MODEL_PROVIDER_NAME,
                "registry_source": snapshot_source,
                "registry_entry_id": _safe_text(entry.get("registry_entry_id")) or None,
                "concept_id": _safe_text(entry.get("concept_id")) or None,
                "strength_rank": _coerce_rank(
                    entry.get("strength_rank")
                    or entry.get("replay_strength_rank")
                    or entry.get("capability_rank"),
                    fallback=1000 + index,
                ),
                "relative_cost_rank": _coerce_rank(
                    entry.get("relative_cost_rank")
                    or entry.get("cost_rank")
                    or entry.get("replay_cost_rank"),
                    fallback=1000 + index,
                ),
                "notes": _safe_text(entry.get("notes")) or None,
            }
        )
    return entries


def _build_local_ollama_replay_model_candidates(
    *,
    requested_candidates: Sequence[str],
    installed_models: set[str] | None,
    pull_missing_models: bool,
) -> list[dict[str, Any]]:
    requested = [_safe_text(entry) for entry in requested_candidates]
    requested = [entry for entry in requested if entry]
    installed = installed_models if installed_models is not None else _list_installed_ollama_models()
    catalogue_by_model = {
        _normalise_ollama_model_name(entry.get("model")): dict(entry)
        for entry in LOCAL_OLLAMA_REPLAY_MODEL_CATALOGUE
    }
    registry_by_model = {
        _normalise_ollama_model_name(entry.get("model")): dict(entry)
        for entry in _registry_local_ollama_model_entries()
    }
    if requested:
        source_models = requested
    elif registry_by_model:
        source_models = list(registry_by_model)
    else:
        source_models = [entry["model"] for entry in LOCAL_OLLAMA_REPLAY_MODEL_CATALOGUE]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    pull_results: dict[str, Any] = {}
    for index, model_name in enumerate(source_models):
        cleaned_model = _safe_text(model_name)
        model_key = _normalise_ollama_model_name(cleaned_model)
        if not cleaned_model or model_key in seen:
            continue
        seen.add(model_key)
        registry_entry = registry_by_model.get(model_key, {})
        catalogue_entry = catalogue_by_model.get(model_key, {})
        installed_now = model_key in installed if installed else False
        if not installed_now and pull_missing_models:
            pull_result = _pull_ollama_model(cleaned_model)
            pull_results[model_key] = pull_result
            if pull_result.get("status") == "ok":
                installed.add(model_key)
                installed_now = True
        if not installed_now:
            continue
        strength_rank = registry_entry.get("strength_rank", catalogue_entry.get("strength_rank"))
        relative_cost_rank = registry_entry.get(
            "relative_cost_rank", catalogue_entry.get("relative_cost_rank")
        )
        candidates.append(
            {
                "schema_version": LOCAL_OLLAMA_REPLAY_MODEL_CATALOGUE_SCHEMA_VERSION,
                "model": model_key,
                "provider": LOCAL_MODEL_PROVIDER_NAME,
                "installed": installed_now,
                "strength_rank": _coerce_rank(strength_rank, fallback=1000 + index),
                "relative_cost_rank": _coerce_rank(relative_cost_rank, fallback=1000 + index),
                "catalogue_source": (
                    "vontology_model_registry" if registry_entry else "replay_local_catalogue"
                ),
                "registry_source": registry_entry.get("registry_source"),
                "registry_entry_id": registry_entry.get("registry_entry_id"),
                "concept_id": registry_entry.get("concept_id"),
                "notes": _safe_text(registry_entry.get("notes") or catalogue_entry.get("notes"))
                or None,
                "pull_result": pull_results.get(model_key),
            }
        )
    return sorted(
        candidates,
        key=lambda entry: (
            int(entry.get("strength_rank") or 10_000),
            int(entry.get("relative_cost_rank") or 10_000),
            _safe_text(entry.get("model")),
        ),
    )


def _resolve_scoped_active_llm_setting(
    *, user_concept_id: str | None, organisation_concept_id: str | None
) -> Mapping[str, Any] | None:
    from src.backend.services.settings_service import resolve_llm_setting

    resolved = resolve_llm_setting(
        user_concept_id=_safe_text(user_concept_id) or None,
        org_concept_id=_safe_text(organisation_concept_id) or None,
    )
    return dict(resolved) if isinstance(resolved, Mapping) else None


def _set_scoped_active_llm_setting(
    *,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    provider: str,
    model: str,
) -> bool:
    from src.backend.services.settings_service import (
        set_org_llm_setting,
        set_user_llm_setting,
    )

    user_id = _safe_text(user_concept_id) or None
    organisation_id = _safe_text(organisation_concept_id) or None
    if user_id:
        return bool(set_user_llm_setting(user_id, provider, model))
    if organisation_id:
        return bool(set_org_llm_setting(organisation_id, provider, model))
    raise RuntimeError("A user or organisation concept id is required to override scoped active LLM.")


@contextmanager
def _temporary_scoped_active_llm(
    *,
    user_concept_id: str | None,
    organisation_concept_id: str | None,
    provider: str,
    model: str,
) -> Any:
    previous = _resolve_scoped_active_llm_setting(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
    )
    if not previous:
        raise RuntimeError(
            "Cannot safely restore scoped active LLM because no previous setting resolved."
        )
    if not _set_scoped_active_llm_setting(
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        provider=provider,
        model=model,
    ):
        raise RuntimeError(f"Failed to set scoped active LLM to {provider}:{model}.")
    try:
        yield previous
    finally:
        previous_provider = _safe_text(previous.get("provider"))
        previous_model = _safe_text(previous.get("model"))
        if previous_provider and previous_model:
            _set_scoped_active_llm_setting(
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
                provider=previous_provider,
                model=previous_model,
            )


def _git_capture(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _collect_run_environment(
    *,
    base_url: str,
    requested_model: str | None,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
) -> dict[str, Any]:
    return {
        "base_url": base_url,
        "requested_model": _safe_text(requested_model) or None,
        "authenticated_user_concept_id": _safe_text(user_concept_id) or None,
        "authenticated_organisation_concept_id": (
            _safe_text(organisation_concept_id) or None
        ),
        "session_name": _safe_text(session_name) or None,
        "run_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "local_python_executable": sys.executable,
        "local_python_version": platform.python_version(),
        "local_platform": platform.platform(),
        "local_working_directory": str(Path.cwd()),
        "local_repo_git_branch": _git_capture("rev-parse", "--abbrev-ref", "HEAD"),
        "local_repo_git_head": _git_capture("rev-parse", "HEAD"),
    }


def _summarise_server_diag(diag_payload: Mapping[str, Any]) -> dict[str, Any]:
    version_details = _as_mapping(diag_payload.get("version_details"))
    durable_startup = _as_mapping(diag_payload.get("durable_workflow_startup"))
    durable_workflows = _as_mapping(diag_payload.get("durable_workflows"))
    return {
        "server_reported_version": _safe_text(diag_payload.get("version")) or None,
        "server_reported_python_version": (
            _safe_text(diag_payload.get("python_version")) or None
        ),
        "server_reported_git_branch": (
            _safe_text(version_details.get("git_branch")) or None
        ),
        "server_reported_git_commit": (
            _safe_text(version_details.get("git_commit")) or None
        ),
        "server_reported_git_short_commit": (
            _safe_text(version_details.get("git_short_commit")) or None
        ),
        "server_reported_git_dirty": version_details.get("git_dirty"),
        "server_effective_user_concept_id": (
            _safe_text(diag_payload.get("effective_user_concept_id")) or None
        ),
        "server_header_user_concept_id": (
            _safe_text(diag_payload.get("header_user_concept_id")) or None
        ),
        "server_session_user_concept_id": (
            _safe_text(diag_payload.get("session_user_concept_id")) or None
        ),
        "server_durable_workflow_ready": durable_startup.get("ready"),
        "server_worker_running": durable_workflows.get("worker_running"),
        "server_scheduler_running": durable_workflows.get("scheduler_running"),
        "server_uptime_sec": diag_payload.get("uptime_sec"),
        "server_agent_test_instance": diag_payload.get("agent_test_instance"),
    }


def _augment_run_environment_with_server_diag(
    *,
    session: requests.Session,
    base_url: str,
    run_environment: dict[str, Any],
) -> dict[str, Any]:
    augmented_environment = dict(run_environment)
    last_error: str | None = None
    for source, path in (("health", "/health"), ("diag", "/diag")):
        try:
            payload = _request_json(
                session,
                "GET",
                f"{base_url}{path}",
                timeout_seconds=SERVER_METADATA_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            last_error = f"{source}: {exc}"
            continue
        augmented_environment.update(_summarise_server_diag(payload))
        augmented_environment["server_metadata_source"] = source
        augmented_environment["server_metadata_error"] = None
        return augmented_environment
    augmented_environment["server_metadata_source"] = None
    augmented_environment["server_metadata_error"] = last_error
    return augmented_environment


def _summarise_active_llm_info(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "server_resolved_active_llm_provider": (
            _safe_text(payload.get("provider")) or None
        ),
        "server_resolved_active_llm_model": _safe_text(payload.get("model")) or None,
        "server_resolved_active_llm_status": _safe_text(payload.get("status")) or None,
        "server_resolved_active_llm_ping_ok": payload.get("ping_ok"),
        "server_resolved_active_llm_error": _safe_text(payload.get("error")) or None,
    }


def _augment_run_environment_with_active_llm_info(
    *,
    session: requests.Session,
    base_url: str,
    user_concept_id: str,
    organisation_concept_id: str,
    run_environment: dict[str, Any],
) -> dict[str, Any]:
    augmented_environment = dict(run_environment)
    try:
        llm_info_payload = _request_json(
            session,
            "GET",
            f"{base_url}/api/settings/llm/info",
            timeout_seconds=ACTIVE_LLM_INFO_TIMEOUT_SECONDS,
            params={
                "user_concept_id": user_concept_id,
                "organisation_concept_id": organisation_concept_id,
            },
        )
    except Exception as exc:
        augmented_environment["server_resolved_active_llm_lookup_error"] = str(exc)
        return augmented_environment
    augmented_environment.update(_summarise_active_llm_info(llm_info_payload))
    augmented_environment["server_resolved_active_llm_lookup_error"] = None
    return augmented_environment


def _choose_prompt(
    prompt_bank: Sequence[Mapping[str, Any]],
    *,
    seed: int | None,
    prompt_id: str | None,
    allowed_complexity_classes: frozenset[str],
) -> dict[str, Any]:
    prompts = [
        dict(entry)
        for entry in prompt_bank
        if isinstance(entry, Mapping)
        and (
            not allowed_complexity_classes
            or _safe_text(entry.get("complexity_class")) in allowed_complexity_classes
        )
    ]
    _assert(
        bool(prompts),
        "Prompt bank is empty for the requested complexity-class filter.",
    )
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
        json=_build_replay_session_creation_payload(session_name),
    )
    session_id = _safe_text(session_payload.get("session_id"))
    _assert(bool(session_id), "Session creation did not return a session_id.")
    _request_json(session, "POST", f"{base_url}/von/reset", json={})
    return session_id, window_session_id


def _request_task_cancellation(
    *,
    session: requests.Session,
    base_url: str,
    task_id: str,
    await_terminal_seconds: float = 10.0,
    poll_interval_seconds: float = 0.5,
) -> dict[str, Any]:
    try:
        cancellation_payload = _request_json(
            session,
            "POST",
            f"{base_url}/von/api/task/cancel/{task_id}",
            timeout_seconds=15.0,
        )
    except Exception as exc:
        return {
            "success": False,
            "task_id": task_id,
            "error": str(exc),
        }

    result = dict(cancellation_payload)
    deadline = time.monotonic() + max(float(await_terminal_seconds), 0.0)
    last_status_payload: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            status_payload = _request_json(
                session,
                "GET",
                f"{base_url}/von/api/task/status/{task_id}",
                timeout_seconds=15.0,
            )
        except Exception as exc:
            result["post_cancellation_status_error"] = str(exc)
            break
        last_status_payload = status_payload
        status = _safe_text(status_payload.get("status"))
        if status in {"completed", "failed", "cancelled"}:
            result["post_cancellation_terminal"] = True
            result["post_cancellation_status_payload"] = status_payload
            return result
        if await_terminal_seconds <= 0:
            break
        time.sleep(max(float(poll_interval_seconds), 0.2))

    if last_status_payload is not None:
        result["post_cancellation_terminal"] = False
        result["post_cancellation_status_payload"] = last_status_payload
    return result


def _run_generate_background(
    *,
    session: requests.Session,
    base_url: str,
    prompt: str,
    model: str | None,
    gmail_profile: str | None,
    presenter_mode: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> tuple[str, dict[str, Any]]:
    client_request_id = f"live-kb-prompt-{uuid.uuid4()}"
    request_payload: dict[str, Any] = {
        "prompt": prompt,
        "background": True,
        "client_request_id": client_request_id,
    }
    cleaned_model = _safe_text(model)
    if cleaned_model:
        request_payload["model"] = cleaned_model
    cleaned_gmail_profile = _safe_text(gmail_profile)
    if cleaned_gmail_profile:
        request_payload["gmail_profile"] = cleaned_gmail_profile
    if presenter_mode:
        request_payload["presenter_mode"] = True
    model_provider = _infer_provider_from_model_identifier(cleaned_model)
    if model_provider:
        request_payload["model_provider"] = model_provider
        request_payload["selected_model_provider"] = model_provider
    submission = _request_json(
        session,
        "POST",
        f"{base_url}/von/generate",
        expected_status=202,
        timeout_seconds=max(float(timeout_seconds), 30.0),
        json=request_payload,
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
            raise BackgroundGenerateTaskError(
                "Background generate task did not complete successfully: "
                f"{json.dumps(status_payload, ensure_ascii=True, sort_keys=True)}",
                task_id=task_id,
                status_payload=status_payload,
            )
        time.sleep(max(float(poll_interval_seconds), 0.2))

    if not (
        isinstance(status_payload, dict)
        and _safe_text(status_payload.get("status")) == "completed"
    ):
        cancellation_payload = _request_task_cancellation(
            session=session,
            base_url=base_url,
            task_id=task_id,
            poll_interval_seconds=poll_interval_seconds,
        )
        raise BackgroundGenerateTaskError(
            "Background generate task did not complete before timeout: "
            f"{json.dumps(status_payload or {}, ensure_ascii=True, sort_keys=True)}",
            task_id=task_id,
            status_payload=status_payload or {},
            cancellation_payload=cancellation_payload,
        )
    task_result_payload = _request_json(
        session,
        "GET",
        f"{base_url}/von/api/task/result/{task_id}",
    )
    generate_payload = _as_mapping(task_result_payload.get("result"))
    if not generate_payload:
        raise BackgroundGenerateTaskError(
            f"Background task result was empty: {task_result_payload!r}",
            task_id=task_id,
            status_payload=status_payload or {},
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


def _required_workflow_tool_groups(
    llm_debug_data: Mapping[str, Any],
) -> list[dict[str, Any]]:
    turn_record = _as_mapping(llm_debug_data.get("turn_execution_record"))
    execution = _as_mapping(turn_record.get("execution"))
    workflow_contract = _as_mapping(execution.get("workflow_required_effects_contract"))
    required_effects = _as_list(workflow_contract.get("required_effects"))
    groups: list[dict[str, Any]] = []
    for effect in required_effects:
        if not isinstance(effect, Mapping):
            continue
        required_tools = [
            _safe_text(tool_name)
            for tool_name in _as_list(effect.get("required_tools"))
            if _safe_text(tool_name)
        ]
        if not required_tools:
            continue
        explicit_answer_required: bool | None = None
        for key in (
            "user_answer_required",
            "answer_required",
            "required_for_user_answer",
        ):
            if key in effect:
                explicit_answer_required = _optional_bool(effect.get(key))
                break
        groups.append(
            {
                "effect_id": _safe_text(effect.get("effect_id")),
                "effect_type": _safe_text(effect.get("effect_type")),
                "required_tools": required_tools,
                "match": (_safe_text(effect.get("required_tools_match")) or "any")
                .strip()
                .lower(),
                "requirement_source": "workflow_required_effects_contract",
                "explicit_user_answer_required": explicit_answer_required,
            }
        )
    return groups


def _dispatch_missing_required_tool_group(
    dispatch: Mapping[str, Any],
) -> dict[str, Any] | None:
    missing_tools = [
        _safe_text(tool_name)
        for tool_name in _as_list(
            dispatch.get("required_effects_missing_required_tools")
        )
        if _safe_text(tool_name)
    ]
    if not missing_tools:
        return None
    required_tools = [
        _safe_text(tool_name)
        for tool_name in _as_list(dispatch.get("required_effects_required_tools"))
        if _safe_text(tool_name)
    ]
    unresolved_effect_ids = [
        _safe_text(effect_id)
        for effect_id in _as_list(
            dispatch.get("required_effects_unresolved_effect_ids")
        )
        if _safe_text(effect_id)
    ]
    unresolved_effect_types = [
        _safe_text(effect_type)
        for effect_type in _as_list(
            dispatch.get("required_effects_unresolved_effect_types")
        )
        if _safe_text(effect_type)
    ]
    return {
        "effect_id": (
            ", ".join(unresolved_effect_ids) or "workflow_dispatch_required_effects"
        ),
        "effect_type": (", ".join(unresolved_effect_types) or "required_evidence"),
        "required_tools": required_tools or missing_tools,
        "known_missing_tools": missing_tools,
        "match": "all",
        "requirement_source": "workflow_dispatch_required_effects",
        "explicit_user_answer_required": True,
    }


def _prompt_requires_diagnostic_evidence(prompt_entry: Mapping[str, Any]) -> bool:
    explicit_requirement = _optional_bool(
        prompt_entry.get("requires_diagnostic_evidence")
    )
    if explicit_requirement is not None:
        return explicit_requirement
    knowledge_surfaces = {
        _safe_text(surface).lower()
        for surface in _as_list(prompt_entry.get("knowledge_surfaces"))
        if _safe_text(surface)
    }
    if knowledge_surfaces & DIAGNOSTIC_EVIDENCE_KNOWLEDGE_SURFACES:
        return True
    likely_tools = {
        _safe_text(tool_name).lower()
        for tool_name in _as_list(prompt_entry.get("likely_tools"))
        if _safe_text(tool_name)
    }
    return bool(likely_tools & DIAGNOSTIC_EVIDENCE_TOOLS)


def _workflow_tool_group_is_user_answer_required(
    group: Mapping[str, Any],
    *,
    prompt_requires_diagnostic_evidence: bool,
) -> bool:
    explicit_requirement = group.get("explicit_user_answer_required")
    if isinstance(explicit_requirement, bool):
        return explicit_requirement
    effect_type = _safe_text(group.get("effect_type")).lower()
    if effect_type == DIAGNOSTIC_EVIDENCE_EFFECT_TYPE:
        return prompt_requires_diagnostic_evidence
    return True


def _evaluate_user_happiness(
    *,
    prompt_entry: Mapping[str, Any],
    generate_payload: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
    run_environment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    diagnostic_evidence_reasons: list[str] = []
    missing_evidence: list[dict[str, Any]] = []
    complexity_class = _safe_text(prompt_entry.get("complexity_class"))
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
    if completion_gate_status in {
        "fail",
        "failed",
        "blocked",
        "deny",
        "denied",
        "escalation_required",
        "follow_up_required",
    }:
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
    tool_names = _collect_tool_names(diagnostics, llm_debug_data)
    requires_tool_use = bool(prompt_entry.get("requires_tool_use"))
    allows_grounded_empty_result = bool(
        prompt_entry.get("allows_grounded_empty_result")
    )
    if any(marker in lowered_response for marker in HARD_FAILURE_RESPONSE_MARKERS):
        reasons.append("Response contains a concrete failure or access marker.")
    elif any(marker in lowered_response for marker in SOFT_FAILURE_RESPONSE_MARKERS):
        grounded_empty_result = (
            allows_grounded_empty_result
            and bool(tool_names)
            and any(
                marker in lowered_response for marker in GROUNDED_EMPTY_RESULT_MARKERS
            )
        )
        if not grounded_empty_result:
            reasons.append("Response contains a generic limitation or failure marker.")

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
    if requires_tool_use and not tool_names:
        reasons.append(
            "Prompt required operational tool use but no tool usage was recorded."
        )
    observed_tool_lookup = {tool_name.lower() for tool_name in tool_names}
    prompt_requires_diagnostic_evidence = _prompt_requires_diagnostic_evidence(
        prompt_entry
    )
    required_tool_groups = _required_workflow_tool_groups(llm_debug_data)
    dispatch_required_tool_group = _dispatch_missing_required_tool_group(dispatch)
    if dispatch_required_tool_group is not None:
        required_tool_groups.append(dispatch_required_tool_group)
    for group in required_tool_groups:
        required_tools = [
            tool_name
            for tool_name in _as_list(group.get("required_tools"))
            if isinstance(tool_name, str) and tool_name.strip()
        ]
        known_missing_tools = [
            tool_name
            for tool_name in _as_list(group.get("known_missing_tools"))
            if isinstance(tool_name, str) and tool_name.strip()
        ]
        missing_tools = known_missing_tools or [
            tool_name
            for tool_name in required_tools
            if tool_name.lower() not in observed_tool_lookup
        ]
        match_mode = _safe_text(group.get("match")).lower() or "any"
        requirement_satisfied = (
            not missing_tools
            if match_mode == "all"
            else len(missing_tools) < len(required_tools)
        )
        if requirement_satisfied:
            continue
        effect_id = _safe_text(group.get("effect_id")) or "workflow required effect"
        effect_type = _safe_text(group.get("effect_type")) or None
        reason = (
            "Workflow-authored required evidence was not retrieved for "
            f"{effect_id}; missing tools: {', '.join(missing_tools)}."
        )
        user_answer_required = _workflow_tool_group_is_user_answer_required(
            group,
            prompt_requires_diagnostic_evidence=prompt_requires_diagnostic_evidence,
        )
        evidence_entry = {
            "effect_id": effect_id,
            "effect_type": effect_type,
            "required_tools": required_tools,
            "missing_tools": missing_tools,
            "match": match_mode,
            "requirement_source": (
                _safe_text(group.get("requirement_source"))
                or "workflow_required_effects_contract"
            ),
            "user_answer_required": user_answer_required,
            "reason": reason,
        }
        missing_evidence.append(evidence_entry)
        if (effect_type or "").lower() == DIAGNOSTIC_EVIDENCE_EFFECT_TYPE:
            diagnostic_evidence_reasons.append(reason)
        if user_answer_required:
            reasons.append(reason)

    minimum_response_length = (
        4
        if complexity_class == "direct_context_or_background"
        else 20 if requires_tool_use or allows_grounded_empty_result else 40
    )
    if len(response_text) < minimum_response_length:
        reasons.append("Response was too short to plausibly satisfy the prompt.")

    if not reasons and not tool_names and not selected_execution_mode:
        reasons.append(
            "No positive evidence of grounded execution was recorded for this turn."
        )

    inventory_only_tools = [name for name in tool_names if name in INVENTORY_ONLY_TOOLS]
    if (
        inventory_only_tools
        and len(inventory_only_tools) == len(tool_names)
        and any(marker in lowered_response for marker in RELATIONSHIP_CLAIM_MARKERS)
    ):
        reasons.append(
            "Response made a relationship or ownership claim using inventory-only tool evidence."
        )

    canonical_concept_id_fidelity = _evaluate_canonical_concept_id_fidelity(
        prompt_entry=prompt_entry,
        response_text=response_text,
        run_environment=run_environment,
        llm_debug_data=llm_debug_data,
    )
    for finding in _as_list(canonical_concept_id_fidelity.get("findings")):
        if not isinstance(finding, Mapping):
            continue
        expected_id = _safe_text(finding.get("expected_concept_id"))
        observed_id = _safe_text(finding.get("observed_concept_id"))
        if expected_id and observed_id:
            reasons.append(
                "Canonical concept ID mismatch: expected "
                f"{expected_id} but response displayed near-miss {observed_id}."
            )

    should_user_be_happy = not reasons
    verdict = "happy" if should_user_be_happy else "unhappy"
    missing_answer_evidence = [
        entry for entry in missing_evidence if entry.get("user_answer_required") is True
    ]
    missing_diagnostic_evidence = [
        entry
        for entry in missing_evidence
        if _safe_text(entry.get("effect_type")).lower()
        == DIAGNOSTIC_EVIDENCE_EFFECT_TYPE
    ]
    diagnostic_evidence_complete = not missing_diagnostic_evidence
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
        "evaluation_authority": {
            "authoritative": False,
            "source": "live_prompt_sampler_local_smoke_check",
            "reason": (
                "This script-level check is diagnostic replay evidence only; "
                "durable experiment verdicts require represented replay "
                "evaluation authority."
            ),
        },
        "reasons": reasons,
        "diagnostic_evidence_complete": diagnostic_evidence_complete,
        "diagnostic_evidence_reasons": diagnostic_evidence_reasons,
        "missing_evidence": missing_evidence,
        "missing_answer_evidence": missing_answer_evidence,
        "missing_diagnostic_evidence": missing_diagnostic_evidence,
        "canonical_concept_id_fidelity": canonical_concept_id_fidelity,
        "positive_evidence": positive_evidence,
        "response_length": len(response_text),
        "response_preview": response_text[:400],
        "selected_workflow_id": selected_workflow_id or None,
        "selected_execution_mode": selected_execution_mode or None,
        "observed_tools": tool_names,
    }


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _selector_structured_output_valid(selector: Mapping[str, Any]) -> bool | None:
    selection_metadata = _as_mapping(selector.get("selection_metadata"))
    structured_detected = _optional_bool(
        selection_metadata.get("structured_selection_detected")
    )
    if structured_detected is not None:
        return structured_detected
    raw_response_format = _safe_text(selector.get("raw_response_format")).lower()
    if raw_response_format:
        return raw_response_format in {"json", "structured_json"}
    return None


def _extract_selector_model_policy(routing: Mapping[str, Any]) -> dict[str, Any]:
    selector = _as_mapping(routing.get("selector"))
    selected_model_candidate = _as_mapping(selector.get("selected_model_candidate"))
    return {
        "selected_model_candidate": selected_model_candidate or None,
        "fallback_used": bool(selector.get("fallback_used")),
        "fallback_attempt_count": selector.get("fallback_attempt_count"),
        "model_failure_count": selector.get("model_failure_count"),
        "primary_fallback_failure_kind": (
            _safe_text(selector.get("primary_fallback_failure_kind")) or None
        ),
        "model_errors": _as_list(selector.get("model_errors")),
    }


def _text_capture_preview(capture: Mapping[str, Any]) -> dict[str, Any]:
    char_count = _safe_int(capture.get("char_count"))
    text = _safe_text(capture.get("text"))
    return {
        "char_count": char_count if char_count is not None else len(text),
        "preview": text[:400] if text else None,
        "truncated": capture.get("truncated") if "truncated" in capture else None,
    }


def _extract_selector_evidence(
    routing: Mapping[str, Any],
) -> dict[str, Any]:
    selector = _as_mapping(routing.get("selector"))
    response_capture = _as_mapping(selector.get("response"))
    selected_candidate = _as_mapping(selector.get("selected_candidate"))
    return {
        "prompt_id": _safe_text(selector.get("prompt_id")) or None,
        "model_name": _safe_text(selector.get("model_name")) or None,
        "confidence_score": selector.get("confidence_score"),
        "reasoning": _safe_text(selector.get("reasoning")) or None,
        "selected_workflow_id": (
            _safe_text(routing.get("selected_workflow_id"))
            or _safe_text(selected_candidate.get("concept_id"))
            or None
        ),
        "selection_resolution": (
            _safe_text(selector.get("selection_resolution")) or None
        ),
        "raw_candidate_label": _safe_text(selector.get("raw_candidate_label"))
        or None,
        "raw_response_format": _safe_text(selector.get("raw_response_format"))
        or None,
        "structured_output_valid": _selector_structured_output_valid(selector),
        "raw_response": (
            _text_capture_preview(response_capture) if response_capture else None
        ),
        "model_policy": _extract_selector_model_policy(routing),
    }


def _selector_telemetry_completeness(routing: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _extract_selector_evidence(routing)
    missing_fields = [
        field
        for field in ("selected_workflow_id", "model_name", "raw_response")
        if not evidence.get(field)
    ]
    return {
        "complete": not missing_fields,
        "missing_fields": missing_fields,
        "selector_prompt_id": evidence.get("prompt_id"),
        "selector_model_name": evidence.get("model_name"),
        "structured_output_valid": evidence.get("structured_output_valid"),
    }


def _iter_llm_exchange_summaries(llm_debug_data: Mapping[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for stage in _as_list(llm_debug_data.get("stage_diagnostics")):
        if not isinstance(stage, Mapping):
            continue
        stage_id = _safe_text(stage.get("stage_id"))
        latest_exchange = _as_mapping(stage.get("latest_llm_exchange"))
        if latest_exchange:
            summaries.append(
                {
                    "stage_id": stage_id or None,
                    "stage_label": _safe_text(stage.get("stage_label")) or None,
                    "latest_status": _safe_text(stage.get("latest_status")) or None,
                    "exchange": latest_exchange,
                }
            )
        for exchange in _as_list(stage.get("llm_exchange_summaries")):
            if not isinstance(exchange, Mapping):
                continue
            summaries.append(
                {
                    "stage_id": stage_id or None,
                    "stage_label": _safe_text(stage.get("stage_label")) or None,
                    "latest_status": _safe_text(stage.get("latest_status")) or None,
                    "exchange": dict(exchange),
                }
            )
    return summaries


def _collect_empty_success_llm_suspects(
    llm_debug_data: Mapping[str, Any],
    *,
    final_response_text: str,
) -> list[dict[str, Any]]:
    suspects: list[dict[str, Any]] = []
    for index, entry in enumerate(_iter_llm_exchange_summaries(llm_debug_data)):
        exchange = _as_mapping(entry.get("exchange"))
        response_preview = _as_mapping(exchange.get("response_preview"))
        char_count = _safe_int(response_preview.get("char_count"))
        if char_count is None:
            response_text = _safe_text(response_preview.get("text"))
            char_count = len(response_text) if response_text else None
        request_state = _safe_text(exchange.get("llm_request_state")).lower()
        if char_count == 0 and request_state in {"completed", "success"}:
            suspects.append(
                {
                    "source": "stage_diagnostics",
                    "exchange_index": index,
                    "stage_id": entry.get("stage_id"),
                    "stage_label": entry.get("stage_label"),
                    "latest_status": entry.get("latest_status"),
                    "llm_request_state": request_state,
                    "model": _safe_text(exchange.get("selected_model")) or None,
                    "response_char_count": 0,
                }
            )
    if not _safe_text(final_response_text):
        suspects.append(
            {
                "source": "final_response",
                "stage_id": "turn_answer",
                "response_char_count": 0,
            }
        )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for suspect in suspects:
        key = (
            suspect.get("source"),
            suspect.get("stage_id"),
            suspect.get("exchange_index"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(suspect)
    return deduped


def _extract_timing_metrics(diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    timing = _as_mapping(diagnostics.get("timing_breakdown"))
    totals = _as_mapping(timing.get("totals"))
    return {
        "elapsed_ms": totals.get("elapsed_ms"),
        "llm_elapsed_ms": totals.get("llm_elapsed_ms"),
        "llm_call_count": totals.get("llm_call_count"),
        "llm_calls_by_stage_model": _as_list(
            timing.get("llm_calls_by_stage_model")
        ),
    }


def _build_model_portfolio_arm_evaluation(
    *,
    summary: Mapping[str, Any],
    llm_debug_data: Mapping[str, Any],
    prompt_entry: Mapping[str, Any],
    requested_model: str | None,
) -> dict[str, Any]:
    telemetry = _as_mapping(summary.get("telemetry"))
    evaluation = _as_mapping(summary.get("evaluation"))
    response = _as_mapping(summary.get("response"))
    conversation = _as_mapping(summary.get("conversation"))
    diagnostics = _as_mapping(llm_debug_data.get("turn_execution_diagnostics"))
    routing = _as_mapping(diagnostics.get("workflow_routing_diagnostics"))
    selector_evidence = _extract_selector_evidence(routing)
    completion_gate = _as_mapping(llm_debug_data.get("completion_gate_verdict"))
    critic_verdict = _as_mapping(llm_debug_data.get("critic_verdict"))
    response_text = _safe_text(response.get("text"))
    empty_success_suspects = _collect_empty_success_llm_suspects(
        llm_debug_data,
        final_response_text=response_text,
    )
    canonical_concept_id_fidelity = _as_mapping(
        evaluation.get("canonical_concept_id_fidelity")
    )
    canonical_concept_id_findings = _as_list(
        canonical_concept_id_fidelity.get("findings")
    )
    missing_answer_evidence = _as_list(evaluation.get("missing_answer_evidence"))
    missing_evidence = _as_list(evaluation.get("missing_evidence"))
    tool_history = _as_list(telemetry.get("tool_history"))
    prompt_variant_evaluation = _as_mapping(summary.get("prompt_variant_evaluation"))
    replay_scoring_consistency = _as_mapping(
        summary.get("replay_scoring_consistency")
    )
    structured_output_valid = selector_evidence.get("structured_output_valid")
    should_user_be_happy = bool(evaluation.get("should_user_be_happy"))
    selector_metrics = {
        "selected_workflow_id": selector_evidence.get("selected_workflow_id"),
        "selector_confidence_score": selector_evidence.get("confidence_score"),
        "structured_output_valid": structured_output_valid,
        "raw_response_format": selector_evidence.get("raw_response_format"),
        "selection_resolution": selector_evidence.get("selection_resolution"),
        "selected_model_candidate": selector_evidence["model_policy"].get(
            "selected_model_candidate"
        ),
    }
    answer_metrics = {
        "final_response_length": len(response_text),
        "final_answer_useful": should_user_be_happy,
        "tool_count": len(tool_history),
        "missing_evidence_count": len(missing_evidence),
        "missing_answer_evidence_count": len(missing_answer_evidence),
        "empty_success_suspect_count": len(empty_success_suspects),
        "canonical_concept_id_fidelity_status": (
            _safe_text(canonical_concept_id_fidelity.get("status"))
            or "not_applicable"
        ),
        "canonical_concept_id_mismatch_count": len(canonical_concept_id_findings),
        "canonical_expected_concept_id_count": len(
            _as_list(canonical_concept_id_fidelity.get("expected_concept_ids"))
        ),
        "canonical_observed_concept_id_count": len(
            _as_list(canonical_concept_id_fidelity.get("observed_concept_ids"))
        ),
        "completion_gate_status": (
            _safe_text(
                completion_gate.get("status")
                or completion_gate.get("verdict")
                or completion_gate.get("decision")
            )
            or None
        ),
        "critic_verdict_status": (
            _safe_text(
                critic_verdict.get("status")
                or critic_verdict.get("verdict")
                or critic_verdict.get("decision")
            )
            or None
        ),
        **_extract_timing_metrics(diagnostics),
    }
    promotion_blockers = _dedupe_texts(
        [
            "single_prompt_replay_evidence_only",
            *_as_list(prompt_variant_evaluation.get("promotion_blockers")),
            *_as_list(replay_scoring_consistency.get("promotion_blockers")),
        ]
    )
    selector_verdict = (
        "passed"
        if selector_metrics.get("selected_workflow_id") and structured_output_valid is not False
        else "suspect"
    )
    if not should_user_be_happy or empty_success_suspects:
        answer_verdict = "failed"
    else:
        answer_verdict = "passed"
    observed_model = _safe_text(telemetry.get("model")) or _safe_text(requested_model)
    replay_case_id = _safe_text(prompt_entry.get("id"))
    replay_set_id = DEFAULT_REPLAY_SET_ID
    answer_prompt_id = (
        _safe_text(prompt_variant_evaluation.get("base_prompt_id"))
        or _safe_text(prompt_entry.get("id"))
        or None
    )
    answer_prompt_variant_id = (
        _safe_text(prompt_variant_evaluation.get("selected_prompt_id"))
        or _safe_text(prompt_variant_evaluation.get("candidate_prompt_variant_id"))
        or None
    )
    if answer_prompt_variant_id and answer_prompt_variant_id == answer_prompt_id:
        answer_prompt_variant_id = None
    selector_evidence_payload = build_model_stage_suitability_evidence(
        model=observed_model,
        stage="workflow_selector",
        workflow_id=_safe_text(telemetry.get("selected_workflow_id")) or None,
        prompt_id=selector_evidence.get("prompt_id"),
        replay_set_id=replay_set_id,
        replay_case_id=replay_case_id,
        request_id=_safe_text(conversation.get("request_id")) or None,
        verdict=selector_verdict,
        metrics=selector_metrics,
        rationale=selector_evidence.get("reasoning"),
        evidence_artifact={
            "conversation": conversation,
            "selector": selector_evidence,
        },
        promotion_blockers=promotion_blockers,
    )
    answer_evidence_payload = build_model_stage_suitability_evidence(
        model=observed_model,
        stage="turn_answer",
        workflow_id=_safe_text(telemetry.get("selected_workflow_id")) or None,
        prompt_id=answer_prompt_id,
        prompt_variant_id=answer_prompt_variant_id,
        replay_set_id=replay_set_id,
        replay_case_id=replay_case_id,
        request_id=_safe_text(conversation.get("request_id")) or None,
        verdict=answer_verdict,
        metrics=answer_metrics,
        rationale="; ".join(_as_list(evaluation.get("reasons"))) or None,
        evidence_artifact={
            "conversation": conversation,
            "response_preview": response_text[:400],
            "empty_success_suspects": empty_success_suspects,
            "canonical_concept_id_fidelity": canonical_concept_id_fidelity,
            "prompt_variant_evaluation": prompt_variant_evaluation,
            "replay_scoring_consistency": replay_scoring_consistency,
        },
        promotion_blockers=promotion_blockers,
    )
    stage_evidence = [selector_evidence_payload, answer_evidence_payload]
    return {
        "schema_version": MODEL_PORTFOLIO_REPLAY_REPORT_SCHEMA_VERSION,
        "replay_set_id": replay_set_id,
        "replay_case_id": replay_case_id,
        "requested_model": _safe_text(requested_model) or None,
        "observed_model": observed_model or None,
        "selector": selector_evidence,
        "empty_success_suspects": empty_success_suspects,
        "stage_evidence": stage_evidence,
        "stage_evidence_schema_version": MODEL_STAGE_SUITABILITY_EVIDENCE_SCHEMA_VERSION,
        "certification_decision": assess_model_stage_certification(
            stage_evidence,
            minimum_replay_cases=DEFAULT_MINIMUM_REPLAY_CASES_FOR_CERTIFICATION,
        ),
        "policy_update": {
            "authorised": False,
            "reason": (
                "Replay evidence is emitted for represented policy review; "
                "this runner does not mutate workflow/model policy."
            ),
        },
    }


def _build_model_portfolio_comparison_report(
    *,
    arm_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stage_evidence: list[Mapping[str, Any]] = []
    arm_reports: list[dict[str, Any]] = []
    for arm_summary in arm_summaries:
        if not isinstance(arm_summary, Mapping):
            continue
        arm = _as_mapping(arm_summary.get("arm"))
        arm_label = _safe_text(arm.get("label")) or _safe_text(arm.get("arm_id"))
        report = _as_mapping(arm_summary.get("model_portfolio_evaluation"))
        if not report:
            continue
        prompt_variant_evaluation = _as_mapping(
            arm_summary.get("prompt_variant_evaluation")
        )
        scoring_consistency = _as_mapping(
            arm_summary.get("replay_scoring_consistency")
        )
        arm_reports.append(
            {
                "arm_id": _safe_text(arm.get("arm_id")) or None,
                "label": arm_label or None,
                "requested_model": _safe_text(arm.get("requested_model")) or None,
                "observed_model": _safe_text(report.get("observed_model")) or None,
                "replay_case_id": _safe_text(report.get("replay_case_id")) or None,
                "base_prompt_id": _safe_text(
                    prompt_variant_evaluation.get("base_prompt_id")
                )
                or None,
                "candidate_prompt_variant_id": _safe_text(
                    prompt_variant_evaluation.get("candidate_prompt_variant_id")
                )
                or None,
                "selected_prompt_id": _safe_text(
                    prompt_variant_evaluation.get("selected_prompt_id")
                )
                or None,
                "candidate_prompt_variant_selected": prompt_variant_evaluation.get(
                    "candidate_prompt_variant_selected"
                ),
                "telemetry_consistency_non_promotable": bool(
                    scoring_consistency.get("non_promotable")
                ),
                "empty_success_suspect_count": len(
                    _as_list(report.get("empty_success_suspects"))
                ),
                "certification_decision": _as_mapping(
                    report.get("certification_decision")
                ),
            }
        )
        for entry in _as_list(report.get("stage_evidence")):
            if isinstance(entry, Mapping):
                stage_evidence.append(entry)

    aggregate_decision = assess_model_stage_certification(
        stage_evidence,
        minimum_replay_cases=DEFAULT_MINIMUM_REPLAY_CASES_FOR_CERTIFICATION,
    )
    return {
        "schema_version": MODEL_PORTFOLIO_REPLAY_REPORT_SCHEMA_VERSION,
        "replay_set_id": DEFAULT_REPLAY_SET_ID,
        "arm_count": len(arm_reports),
        "stage_evidence_count": len(stage_evidence),
        "arm_reports": arm_reports,
        "aggregate_certification_decision": aggregate_decision,
        "policy_update": {
            "authorised": False,
            "reason": (
                "Comparison evidence is a replay artifact for represented "
                "policy review. Model-stage certification requires the "
                "promotion gate to pass and a separate Vontology/model-policy "
                "mutation path."
            ),
        },
    }


def _build_experiment_observation_from_arm_summary(
    summary: Mapping[str, Any],
) -> dict[str, Any]:
    return replay_experiment_observation_service.build_experiment_observation_from_arm_summary(
        summary,
        default_replay_set_id=DEFAULT_REPLAY_SET_ID,
    )


def _record_experiment_observations(
    *,
    run_id: str,
    arm_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return replay_experiment_observation_service.record_experiment_observations(
        run_id=run_id,
        arm_summaries=arm_summaries,
        default_replay_set_id=DEFAULT_REPLAY_SET_ID,
    )


def _build_prompt_summary(prompt_entry: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        "id": _safe_text(prompt_entry.get("id")),
        "category": _safe_text(prompt_entry.get("category")),
        "complexity_class": _safe_text(prompt_entry.get("complexity_class")),
        "text": _safe_text(prompt_entry.get("prompt")),
        "knowledge_surfaces": _as_list(prompt_entry.get("knowledge_surfaces")),
        "likely_tools": _as_list(prompt_entry.get("likely_tools")),
        "requires_tool_use": bool(prompt_entry.get("requires_tool_use")),
        "allows_grounded_empty_result": bool(
            prompt_entry.get("allows_grounded_empty_result")
        ),
    }
    for source_key in ("source_kind", "source_request_id", "source_workflow_id"):
        source_value = _safe_text(prompt_entry.get(source_key))
        if source_value:
            summary[source_key] = source_value
    return summary


def _build_selection_summary(
    *,
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    selection = {
        "prompt_bank_schema_version": prompt_bank_schema_version,
        "requested_complexity_classes": list(requested_complexity_classes),
        "seed": seed,
        "requested_model": _safe_text(requested_model) or None,
    }
    if requested_model_arms:
        selection["requested_model_arms"] = [
            {
                "arm_id": _safe_text(entry.get("arm_id")) or None,
                "label": _safe_text(entry.get("label")) or None,
                "requested_model": _safe_text(entry.get("requested_model")) or None,
                "requested_provider": (
                    _safe_text(entry.get("requested_provider")) or None
                ),
                **{
                    optional_key: optional_value
                    for optional_key in (
                        "model_arm_id",
                        "base_prompt_id",
                        "candidate_prompt_variant_id",
                        "workflow_stage_id",
                        "target_workflow_id",
                        "replay_set_id",
                        "replay_case_id",
                    )
                    if (optional_value := _safe_text(entry.get(optional_key)))
                },
            }
            for entry in requested_model_arms
            if isinstance(entry, Mapping)
        ]
    return selection


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
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    run_environment: Mapping[str, Any],
    arm_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostics = _as_mapping(llm_debug_data.get("turn_execution_diagnostics"))
    routing = _as_mapping(diagnostics.get("workflow_routing_diagnostics"))
    dispatch = _as_mapping(routing.get("dispatch"))
    tool_history = _as_list(diagnostics.get("tool_history"))
    response_text = (
        _safe_text(generate_payload.get("response"))
        or _safe_text(generate_payload.get("response_text"))
        or _safe_text(llm_debug_data.get("response"))
    )
    prompt_variant_evaluation = (
        replay_experiment_observation_service.build_prompt_variant_evaluation(
            llm_debug_data=llm_debug_data,
            arm_metadata=arm_metadata,
            requested_model=requested_model,
        )
    )
    replay_scoring_consistency = (
        replay_experiment_observation_service.build_replay_scoring_consistency(
            llm_debug_data=llm_debug_data,
            evaluation=evaluation,
            response_text=response_text,
        )
    )
    summary = {
        "status": "ok",
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=requested_model,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "conversation": {
            "session_id": session_id,
            "request_id": request_id,
            "background_task_id": task_id,
            "history_location": dict(history_location),
        },
        "response": {
            "text": response_text,
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
            "selector_telemetry_completeness": _selector_telemetry_completeness(
                routing
            ),
        },
        "evaluation": dict(evaluation),
        "prompt_variant_evaluation": prompt_variant_evaluation,
        "replay_scoring_consistency": replay_scoring_consistency,
    }
    if arm_metadata:
        summary["arm"] = {
            "arm_id": _safe_text(arm_metadata.get("arm_id")) or None,
            "label": _safe_text(arm_metadata.get("label")) or None,
            "requested_model": _safe_text(arm_metadata.get("requested_model")) or None,
            "requested_provider": (
                _safe_text(arm_metadata.get("requested_provider")) or None
            ),
        }
        for optional_key in (
            "model_arm_id",
            "base_prompt_id",
            "candidate_prompt_variant_id",
            "workflow_stage_id",
            "target_workflow_id",
            "replay_set_id",
            "replay_case_id",
        ):
            optional_value = _safe_text(arm_metadata.get(optional_key))
            if optional_value:
                summary["arm"][optional_key] = optional_value
    summary["model_portfolio_evaluation"] = _build_model_portfolio_arm_evaluation(
        summary=summary,
        llm_debug_data=llm_debug_data,
        prompt_entry=prompt_entry,
        requested_model=requested_model,
    )
    return summary


def _build_model_arm_plan(
    *,
    requested_model: str | None,
    compare_models: Sequence[str],
    include_active_model_arm: bool,
) -> list[dict[str, Any]]:
    planned_arms: list[dict[str, Any]] = []
    seen_models: set[str] = set()

    def _append_arm(model_name: str | None) -> None:
        cleaned_model = _safe_text(model_name) or None
        dedupe_key = cleaned_model or "__active_authenticated_model__"
        if dedupe_key in seen_models:
            return
        seen_models.add(dedupe_key)
        planned_arms.append(
            {
                "arm_id": f"arm_{len(planned_arms) + 1}",
                "label": cleaned_model or ACTIVE_AUTHENTICATED_MODEL_LABEL,
                "requested_model": cleaned_model,
                "requested_provider": (
                    _infer_provider_from_model_identifier(cleaned_model)
                    if cleaned_model
                    else None
                ),
            }
        )

    if include_active_model_arm:
        _append_arm(None)
    _append_arm(requested_model)
    for entry in compare_models:
        cleaned_entry = _safe_text(entry)
        if cleaned_entry:
            _append_arm(cleaned_entry)
    if not planned_arms:
        _append_arm(None)
    return planned_arms


def _build_replay_arm_plan(
    *,
    model_arms: Sequence[Mapping[str, Any]],
    base_prompt_id: str | None,
    prompt_variant_ids: Sequence[Any],
    workflow_stage_id: str | None,
    target_workflow_id: str | None,
    replay_set_id: str | None,
    replay_case_id: str | None,
) -> list[dict[str, Any]]:
    return replay_arm_planning_service.build_replay_arm_plan(
        model_arms=model_arms,
        base_prompt_id=base_prompt_id,
        prompt_variant_ids=prompt_variant_ids,
        workflow_stage_id=workflow_stage_id,
        target_workflow_id=target_workflow_id,
        replay_set_id=replay_set_id,
        replay_case_id=replay_case_id,
        default_replay_set_id=DEFAULT_REPLAY_SET_ID,
    )


def _replay_arms_require_active_llm_info(
    replay_arms: Sequence[Mapping[str, Any]],
) -> bool:
    return any(not _safe_text(arm.get("requested_model")) for arm in replay_arms)


def _build_arm_session_name(
    *, base_session_name: str, arm_metadata: Mapping[str, Any] | None
) -> str:
    return replay_arm_planning_service.build_arm_session_name(
        base_session_name=base_session_name,
        arm_metadata=arm_metadata,
        default_session_name=DEFAULT_SESSION_NAME,
    )


def _build_arm_run_environment(
    *,
    shared_run_environment: Mapping[str, Any],
    requested_model: str | None,
    session_name: str,
    arm_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return replay_arm_planning_service.build_arm_run_environment(
        shared_run_environment=shared_run_environment,
        requested_model=requested_model,
        session_name=session_name,
        arm_metadata=arm_metadata,
    )


def _run_prompt_replay_arm(
    *,
    prompt_entry: Mapping[str, Any],
    base_url: str,
    requested_model: str | None,
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    base_session_name: str,
    shared_run_environment: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    arm_metadata: Mapping[str, Any] | None,
    presenter_mode: bool,
    gmail_profile: str | None,
) -> dict[str, Any]:
    session = requests.Session()
    session_name = _build_arm_session_name(
        base_session_name=base_session_name,
        arm_metadata=arm_metadata,
    )
    default_session_id, _window_session_id = _establish_authenticated_session(
        session=session,
        base_url=base_url,
        user_concept_id=user_concept_id,
        organisation_concept_id=organisation_concept_id,
        session_name=session_name,
    )
    run_environment = _build_arm_run_environment(
        shared_run_environment=shared_run_environment,
        requested_model=requested_model,
        session_name=session_name,
        arm_metadata=arm_metadata,
    )
    task_id, generate_payload = _run_generate_background(
        session=session,
        base_url=base_url,
        prompt=_safe_text(prompt_entry.get("prompt")),
        model=requested_model,
        gmail_profile=gmail_profile,
        presenter_mode=presenter_mode,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    request_id, session_id = _extract_request_and_session_ids(
        task_id=task_id,
        generate_payload=generate_payload,
        default_session_id=default_session_id,
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
    llm_debug_data = _fetch_turn_debug(
        session=session,
        base_url=base_url,
        session_id=session_id,
        history_index=history_index_raw,
    )
    evaluation = _evaluate_user_happiness(
        prompt_entry=prompt_entry,
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
        run_environment=run_environment,
    )
    return _build_summary(
        prompt_entry=prompt_entry,
        task_id=task_id,
        session_id=session_id,
        request_id=request_id,
        history_location=history_location,
        generate_payload=generate_payload,
        llm_debug_data=llm_debug_data,
        evaluation=evaluation,
        prompt_bank_schema_version=prompt_bank_schema_version,
        requested_complexity_classes=requested_complexity_classes,
        seed=seed,
        requested_model=requested_model,
        run_environment=run_environment,
        arm_metadata=arm_metadata,
    )


def _build_multi_arm_summary(
    *,
    prompt_entry: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]],
    run_environment: Mapping[str, Any],
    arm_summaries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    happy_arm_labels: list[str] = []
    unhappy_arm_labels: list[str] = []
    telemetry_models: list[str] = []
    selected_workflow_ids: list[str] = []
    selected_execution_modes: list[str] = []
    for arm_summary in arm_summaries:
        if not isinstance(arm_summary, Mapping):
            continue
        arm = _as_mapping(arm_summary.get("arm"))
        label = _safe_text(arm.get("label")) or _safe_text(arm.get("arm_id"))
        if bool(_as_mapping(arm_summary.get("evaluation")).get("should_user_be_happy")):
            if label:
                happy_arm_labels.append(label)
        elif label:
            unhappy_arm_labels.append(label)
        telemetry_model = _safe_text(
            _as_mapping(arm_summary.get("telemetry")).get("model")
        )
        if telemetry_model and telemetry_model not in telemetry_models:
            telemetry_models.append(telemetry_model)
        workflow_id = _safe_text(
            _as_mapping(arm_summary.get("telemetry")).get("selected_workflow_id")
        )
        if workflow_id and workflow_id not in selected_workflow_ids:
            selected_workflow_ids.append(workflow_id)
        execution_mode = _safe_text(
            _as_mapping(arm_summary.get("telemetry")).get("selected_execution_mode")
        )
        if execution_mode and execution_mode not in selected_execution_modes:
            selected_execution_modes.append(execution_mode)
    summary = {
        "status": "ok",
        "mode": "multi_arm_comparison",
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=requested_model,
            requested_model_arms=requested_model_arms,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "comparison": {
            "arm_count": len(arm_summaries),
            "happy_arm_count": len(happy_arm_labels),
            "unhappy_arm_count": len(unhappy_arm_labels),
            "all_should_user_be_happy": len(unhappy_arm_labels) == 0,
            "happy_arm_labels": happy_arm_labels,
            "unhappy_arm_labels": unhappy_arm_labels,
            "telemetry_models": telemetry_models,
            "selected_workflow_ids": selected_workflow_ids,
            "selected_execution_modes": selected_execution_modes,
        },
        "arms": [dict(entry) for entry in arm_summaries if isinstance(entry, Mapping)],
    }
    summary["model_portfolio_report"] = _build_model_portfolio_comparison_report(
        arm_summaries=arm_summaries
    )
    return summary


def _run_replay_plan(
    *,
    prompt_entry: Mapping[str, Any],
    base_url: str,
    requested_model: str | None,
    replay_arms: Sequence[Mapping[str, Any]],
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
    run_environment: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    base_prompt_id: str | None,
    prompt_variant_ids: Sequence[Any],
    presenter_mode: bool,
    gmail_profile: str | None,
) -> tuple[dict[str, Any], bool]:
    if len(replay_arms) == 1:
        summary = _run_prompt_replay_arm(
            prompt_entry=prompt_entry,
            base_url=base_url,
            requested_model=requested_model,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            base_session_name=session_name,
            shared_run_environment=run_environment,
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            arm_metadata=replay_arms[0] if (base_prompt_id or prompt_variant_ids) else None,
            presenter_mode=presenter_mode,
            gmail_profile=gmail_profile,
        )
        should_user_be_happy = bool(
            _as_mapping(summary.get("evaluation")).get("should_user_be_happy")
        )
        return summary, should_user_be_happy

    arm_summaries = [
        _run_prompt_replay_arm(
            prompt_entry=prompt_entry,
            base_url=base_url,
            requested_model=_safe_text(arm.get("requested_model")) or None,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            user_concept_id=user_concept_id,
            organisation_concept_id=organisation_concept_id,
            base_session_name=session_name,
            shared_run_environment=run_environment,
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            arm_metadata=arm,
            presenter_mode=presenter_mode,
            gmail_profile=gmail_profile,
        )
        for arm in replay_arms
    ]
    summary = _build_multi_arm_summary(
        prompt_entry=prompt_entry,
        prompt_bank_schema_version=prompt_bank_schema_version,
        requested_complexity_classes=requested_complexity_classes,
        seed=seed,
        requested_model=requested_model,
        requested_model_arms=replay_arms,
        run_environment=run_environment,
        arm_summaries=arm_summaries,
    )
    should_user_be_happy = bool(
        _as_mapping(summary.get("comparison")).get("all_should_user_be_happy")
    )
    return summary, should_user_be_happy


def _build_failed_replay_attempt_summary(
    *,
    exc: BaseException,
    attempt_index: int,
    prompt_entry: Mapping[str, Any],
    run_environment: Mapping[str, Any],
    requested_model: str | None,
) -> dict[str, Any]:
    failure: dict[str, Any] = {
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, BackgroundGenerateTaskError):
        failure["background_task"] = exc.to_report()
    return {
        "status": "error",
        "attempt": {"attempt_index": attempt_index},
        "environment": dict(run_environment),
        "selection": {
            "requested_model": requested_model,
            "requested_provider": _infer_provider_from_model_identifier(
                requested_model
            ),
        },
        "prompt": _build_prompt_summary(prompt_entry),
        "conversation": {
            "background_task_id": _as_mapping(failure.get("background_task")).get(
                "task_id"
            )
        },
        "response": {"text": "", "failure": failure},
        "telemetry": {
            "selected_workflow_id": None,
            "selected_execution_mode": None,
            "selector_telemetry_completeness": {
                "complete": False,
                "missing_fields": ["turn_execution_diagnostics"],
            },
        },
        "evaluation": {
            "verdict": "error",
            "should_user_be_happy": False,
            "reasons": [str(exc)],
        },
    }


def _build_repeated_replay_summary(
    *,
    prompt_entry: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    requested_model: str | None,
    requested_model_arms: Sequence[Mapping[str, Any]],
    run_environment: Mapping[str, Any],
    attempt_summaries: Sequence[Mapping[str, Any]],
    success_count: int,
    minimum_success_rate: float,
) -> dict[str, Any]:
    attempt_count = len(attempt_summaries)
    success_rate = (float(success_count) / float(attempt_count)) if attempt_count else 0.0
    return {
        "status": "ok" if success_rate >= minimum_success_rate else "failed",
        "mode": "repeated_replay_suite",
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=requested_model,
            requested_model_arms=requested_model_arms,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "repeat": {
            "attempt_count": attempt_count,
            "successful_attempt_count": success_count,
            "failed_attempt_count": attempt_count - success_count,
            "success_rate": success_rate,
            "minimum_success_rate": minimum_success_rate,
            "meets_minimum_success_rate": success_rate >= minimum_success_rate,
        },
        "attempts": [
            dict(attempt) for attempt in attempt_summaries if isinstance(attempt, Mapping)
        ],
    }


def _extract_trailing_json_mapping(text: str) -> dict[str, Any] | None:
    if not isinstance(text, str) or "{" not in text:
        return None
    for index in range(len(text) - 1, -1, -1):
        if text[index] != "{":
            continue
        try:
            parsed = json.loads(text[index:])
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if platform.system().lower().startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    process.kill()


def _summary_meets_success_threshold(summary: Mapping[str, Any]) -> bool:
    repeat = _as_mapping(summary.get("repeat"))
    if repeat:
        return bool(repeat.get("meets_minimum_success_rate"))
    if _safe_text(summary.get("status")) != "ok":
        return False
    return bool(_as_mapping(summary.get("evaluation")).get("should_user_be_happy"))


def _run_sampler_subprocess_replay_suite(
    *,
    prompt_entry: Mapping[str, Any],
    base_url: str,
    requested_model: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
    presenter_mode: bool,
    allow_non_agent_test_server: bool,
    repeat_count: int,
    minimum_success_rate: float,
    process_timeout_seconds: float,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    prompt_text = _safe_text(prompt_entry.get("prompt"))
    replay_case_id = _safe_text(prompt_entry.get("id"))
    with tempfile.TemporaryDirectory(prefix="von_replay_probe_") as temp_dir:
        output_path = Path(temp_dir) / "attempt.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--base-url",
            base_url,
            "--prompt-text",
            prompt_text,
            "--timeout-seconds",
            str(timeout_seconds),
            "--poll-interval-seconds",
            str(poll_interval_seconds),
            "--user-concept-id",
            user_concept_id,
            "--organisation-concept-id",
            organisation_concept_id,
            "--session-name",
            session_name,
            "--model",
            requested_model,
            "--repeat-count",
            str(max(int(repeat_count), 1)),
            "--minimum-success-rate",
            str(min(max(float(minimum_success_rate), 0.0), 1.0)),
            "--output-json",
            str(output_path),
        ]
        if replay_case_id:
            command.extend(["--replay-case-id", replay_case_id])
        if presenter_mode:
            command.append("--presenter-mode")
        if allow_non_agent_test_server:
            command.append("--allow-non-agent-test-server")
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            stdout, stderr = process.communicate(
                timeout=max(float(process_timeout_seconds), 1.0)
            )
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            stdout, stderr = process.communicate()
            return {
                "status": "error",
                "started_at_utc": started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "error": "replay_attempt_process_timeout",
                "timeout_seconds": process_timeout_seconds,
                "stdout_tail": stdout[-2000:],
                "stderr_tail": stderr[-2000:],
            }
        summary: dict[str, Any]
        if output_path.exists():
            try:
                parsed_output = json.loads(output_path.read_text(encoding="utf-8"))
                summary = dict(parsed_output) if isinstance(parsed_output, Mapping) else {}
            except Exception as exc:
                summary = {
                    "status": "error",
                    "error": f"invalid_attempt_output_json: {exc}",
                }
        else:
            recovered_summary = (
                _extract_trailing_json_mapping(stderr)
                or _extract_trailing_json_mapping(stdout)
                or {"status": "error", "error": "attempt_output_json_missing"}
            )
            summary = dict(recovered_summary)
        if not summary:
            summary = {"status": "error", "error": "attempt_output_json_not_object"}
        summary["subprocess"] = {
            "exit_code": process.returncode,
            "stdout_tail": stdout[-2000:],
            "stderr_tail": stderr[-2000:],
            "process_timeout_seconds": process_timeout_seconds,
        }
        return summary


def _build_local_model_probe_summary(
    *,
    prompt_entry: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    run_environment: Mapping[str, Any],
    candidate_results: Sequence[Mapping[str, Any]],
    selected_candidate: Mapping[str, Any] | None,
    minimum_success_rate: float,
) -> dict[str, Any]:
    selected_model = (
        _safe_text(selected_candidate.get("model"))
        if isinstance(selected_candidate, Mapping)
        else None
    )
    return {
        "status": "ok" if selected_model else "failed",
        "mode": "local_ollama_replay_model_probe",
        "schema_version": LOCAL_OLLAMA_REPLAY_PROBE_SCHEMA_VERSION,
        "guidance": {
            "replay_guide_path": REAL_PATH_REPLAY_GUIDE,
            "replay_guide_note": REAL_PATH_REPLAY_GUIDE_NOTE,
        },
        "environment": dict(run_environment),
        "selection": _build_selection_summary(
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=seed,
            requested_model=selected_model,
        ),
        "prompt": _build_prompt_summary(prompt_entry),
        "local_model_probe": {
            "minimum_success_rate": minimum_success_rate,
            "selected_model": selected_model,
            "candidate_count": len(candidate_results),
            "candidates": [dict(entry) for entry in candidate_results],
            "no_local_model_succeeded": selected_model is None,
        },
    }


def _write_local_model_probe_cache(
    *, cache_path: Path, probe_summary: Mapping[str, Any]
) -> None:
    payload: dict[str, Any] = {}
    if cache_path.exists():
        try:
            parsed = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            parsed = {}
        if isinstance(parsed, Mapping):
            payload = dict(parsed)
    payload["schema_version"] = LOCAL_OLLAMA_REPLAY_PROBE_SCHEMA_VERSION
    payload["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    payload.setdefault("runs", [])
    runs = payload.get("runs")
    if not isinstance(runs, list):
        runs = []
        payload["runs"] = runs
    run_record = {
        "prompt_id": _safe_text(_as_mapping(probe_summary.get("prompt")).get("id"))
        or None,
        "selected_model": _safe_text(
            _as_mapping(probe_summary.get("local_model_probe")).get("selected_model")
        )
        or None,
        "status": _safe_text(probe_summary.get("status")) or None,
        "recorded_at_utc": payload["updated_at_utc"],
    }
    runs.append(run_record)
    payload["last_run"] = run_record
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _run_local_ollama_model_probe(
    *,
    prompt_entry: Mapping[str, Any],
    base_url: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    user_concept_id: str,
    organisation_concept_id: str,
    session_name: str,
    run_environment: Mapping[str, Any],
    prompt_bank_schema_version: str,
    requested_complexity_classes: Sequence[str],
    seed: int | None,
    presenter_mode: bool,
    allow_non_agent_test_server: bool,
    repeat_count: int,
    screen_repeat_count: int,
    attempt_process_timeout_seconds: float,
    minimum_success_rate: float,
    requested_candidates: Sequence[str],
    pull_missing_models: bool,
    cache_path: Path | None,
) -> dict[str, Any]:
    installed_models = _list_installed_ollama_models()
    candidates = _build_local_ollama_replay_model_candidates(
        requested_candidates=requested_candidates,
        installed_models=installed_models,
        pull_missing_models=pull_missing_models,
    )
    candidate_results: list[dict[str, Any]] = []
    selected_candidate: Mapping[str, Any] | None = None
    probe_environment = {
        **run_environment,
        "local_model_probe_enabled": True,
        "local_model_probe_schema_version": LOCAL_OLLAMA_REPLAY_PROBE_SCHEMA_VERSION,
        "local_model_probe_installed_models": sorted(installed_models),
        "local_model_probe_candidate_order": [
            _safe_text(candidate.get("model")) for candidate in candidates
        ],
    }
    for candidate in candidates:
        model_name = _safe_text(candidate.get("model"))
        if not model_name:
            continue
        requested_model = _build_local_ollama_generate_model_override(model_name)
        candidate_environment = {
            **probe_environment,
            "requested_model": model_name,
            "requested_generate_model_override": requested_model,
            "scoped_active_llm_temporarily_overridden": True,
            "scoped_active_llm_temporary_provider": LOCAL_MODEL_PROVIDER_NAME,
            "scoped_active_llm_temporary_model": model_name,
            "local_model_probe_candidate": dict(candidate),
        }
        try:
            with _temporary_scoped_active_llm(
                user_concept_id=user_concept_id,
                organisation_concept_id=organisation_concept_id,
                provider=LOCAL_MODEL_PROVIDER_NAME,
                model=model_name,
            ) as previous_active_llm:
                candidate_environment["scoped_active_llm_previous"] = dict(
                    previous_active_llm
                )
                summary = _run_sampler_subprocess_replay_suite(
                    prompt_entry=prompt_entry,
                    base_url=base_url,
                    requested_model=requested_model,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    user_concept_id=user_concept_id,
                    organisation_concept_id=organisation_concept_id,
                    session_name=f"{session_name} [{model_name}] screening",
                    presenter_mode=presenter_mode,
                    allow_non_agent_test_server=allow_non_agent_test_server,
                    repeat_count=max(int(screen_repeat_count), 1),
                    minimum_success_rate=minimum_success_rate,
                    process_timeout_seconds=attempt_process_timeout_seconds,
                )
                if _summary_meets_success_threshold(summary) and max(int(repeat_count), 1) > max(
                    int(screen_repeat_count), 1
                ):
                    screen_summary = summary
                    summary = _run_sampler_subprocess_replay_suite(
                        prompt_entry=prompt_entry,
                        base_url=base_url,
                        requested_model=requested_model,
                        timeout_seconds=timeout_seconds,
                        poll_interval_seconds=poll_interval_seconds,
                        user_concept_id=user_concept_id,
                        organisation_concept_id=organisation_concept_id,
                        session_name=f"{session_name} [{model_name}] confirmation",
                        presenter_mode=presenter_mode,
                        allow_non_agent_test_server=allow_non_agent_test_server,
                        repeat_count=max(int(repeat_count), 1),
                        minimum_success_rate=minimum_success_rate,
                        process_timeout_seconds=attempt_process_timeout_seconds,
                    )
                    summary["screening"] = screen_summary
        except Exception as exc:
            summary = {
                "status": "error",
                "requested_model": model_name,
                "error": str(exc),
            }
        threshold_met = _summary_meets_success_threshold(summary)
        candidate_result = {
            "candidate": dict(candidate),
            "model": model_name,
            "status": _safe_text(summary.get("status")) or "unknown",
            "threshold_met": threshold_met,
            "suite": summary,
        }
        candidate_results.append(candidate_result)
        if threshold_met:
            selected_candidate = candidate
            break
    probe_summary = _build_local_model_probe_summary(
        prompt_entry=prompt_entry,
        prompt_bank_schema_version=prompt_bank_schema_version,
        requested_complexity_classes=requested_complexity_classes,
        seed=seed,
        run_environment=probe_environment,
        candidate_results=candidate_results,
        selected_candidate=selected_candidate,
        minimum_success_rate=minimum_success_rate,
    )
    if cache_path is not None:
        _write_local_model_probe_cache(cache_path=cache_path, probe_summary=probe_summary)
    return probe_summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one sampled KB+tool-sensitive prompt against a live Von server "
            "and judge whether the user should be happy with the response. "
            f"Use in conjunction with {REAL_PATH_REPLAY_GUIDE}. "
            "Use --complexity-class to work up from easier direct prompts to "
            "KB-grounded and then tool-augmented prompts."
        )
    )
    parser.add_argument(
        "--base-url",
        default=get_default_agent_test_base_url(),
        help=(
            "Live Von server base URL. Defaults to the JVNAUTOSCI-2070 "
            f"isolated agent-test server ({DEFAULT_AGENT_TEST_BASE_URL}); set "
            f"{AGENT_TEST_BASE_URL_ENV_VAR} or pass this flag when using a "
            "different -AgentTest -Port value."
        ),
    )
    parser.add_argument(
        "--allow-non-agent-test-server",
        action="store_true",
        help=(
            "Allow the replay to target a server whose /health response does "
            "not report agent_test_instance=true. Use only when deliberately "
            "testing the interactive/user-facing server."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "Explicit model override for the replay. Defaults to Ollama "
            "`gemma4:31b` for the JVNAUTOSCI-1894 replay programme. Pass an "
            "empty string to omit the override and let /von/generate use the "
            "active user-facing model for the authenticated session."
        ),
    )
    parser.add_argument(
        "--compare-model",
        dest="compare_models",
        action="append",
        default=[],
        help=(
            "Additional model override to run as a comparison arm. Repeat the "
            "flag to compare the same prompt and authenticated context across "
            "multiple requested models."
        ),
    )
    parser.add_argument(
        "--include-active-model-arm",
        action="store_true",
        help=(
            "When running comparison arms, also replay one arm without any model "
            "override so the authenticated session's active user-facing model is "
            "measured alongside explicit requested-model arms."
        ),
    )
    parser.add_argument(
        "--allow-premium-model",
        action="store_true",
        help=(
            "Permit premium or unverified active-model arms. By default this "
            "sampler is local-only and rejects OpenAI/Gemini/Anthropic-looking "
            "models or active-model arms whose provider cannot be verified local."
        ),
    )
    parser.add_argument(
        "--repeat-count",
        type=int,
        default=1,
        help="Run the same selected prompt/arm plan repeatedly and report a success rate.",
    )
    parser.add_argument(
        "--minimum-success-rate",
        "--success-threshold",
        dest="minimum_success_rate",
        type=float,
        default=DEFAULT_MINIMUM_REPLAY_SUCCESS_RATE,
        help="Required repeated-suite or probe success rate; default 0.95.",
    )
    parser.add_argument(
        "--probe-local-models",
        action="store_true",
        help=(
            "Search installed local Ollama models from weakest/cheapest to "
            "strongest and report the first model that meets the success rate."
        ),
    )
    parser.add_argument(
        "--local-model-candidate",
        dest="local_model_candidates",
        action="append",
        default=[],
        help=(
            "Restrict --probe-local-models to one local Ollama model candidate. "
            "Repeat to provide an ordered candidate set."
        ),
    )
    parser.add_argument(
        "--model-probe-screen-repeat-count",
        type=int,
        default=1,
        help="Number of cheap screening repeats per candidate before confirmation.",
    )
    parser.add_argument(
        "--model-probe-attempt-timeout-seconds",
        type=float,
        default=900.0,
        help="Wall-clock timeout for each subprocess-isolated candidate replay suite.",
    )
    parser.add_argument(
        "--pull-missing-local-models",
        action="store_true",
        help="Allow the probe to run `ollama pull` for missing local candidates.",
    )
    parser.add_argument(
        "--local-model-probe-cache-json",
        default=str(DEFAULT_LOCAL_MODEL_PROBE_CACHE_PATH),
        help="Path for appending local model probe cache metadata. Pass an empty string to disable.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--user-concept-id", default=DEFAULT_USER_CONCEPT_ID)
    parser.add_argument(
        "--organisation-concept-id", default=DEFAULT_ORGANISATION_CONCEPT_ID
    )
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--prompt-id", default="")
    parser.add_argument(
        "--prompt-text",
        default="",
        help=(
            "Use an explicit prompt text instead of sampling the prompt bank. "
            "Useful for fixed failure-case replays after a workflow has already "
            "resolved the replay case."
        ),
    )
    parser.add_argument(
        "--replay-case-id",
        default="",
        help="Stable replay case id to attach to reports for explicit prompt/failure-case runs.",
    )
    parser.add_argument(
        "--replay-set-id",
        default="",
        help="Replay set id to attach to prompt-variant arm metadata.",
    )
    parser.add_argument(
        "--workflow-id",
        default="",
        help="Expected or target workflow id for replay-case provenance.",
    )
    parser.add_argument(
        "--workflow-stage-id",
        default="",
        help="Workflow stage id whose prompt variants are being evaluated.",
    )
    parser.add_argument(
        "--base-prompt-id",
        default="",
        help="Base Vontology prompt concept id for prompt-variant replay arms.",
    )
    parser.add_argument(
        "--prompt-variant-id",
        dest="prompt_variant_ids",
        action="append",
        default=[],
        help=(
            "Candidate represented prompt variant concept id. Repeat to create "
            "candidate arms. The runner records whether normal runtime prompt "
            "variant resolution selected this id; it does not inject raw prompt text."
        ),
    )
    parser.add_argument(
        "--experiment-run-id",
        default="",
        help=(
            "Existing experiment_run concept id. When supplied, replay arm "
            "observations are appended through the internal MCP experiment surface."
        ),
    )
    parser.add_argument(
        "--failure-conversation-ref-json",
        default="",
        help=(
            "JSON object, path, or @path for a conversation_ref accepted by "
            "failure-case intake."
        ),
    )
    parser.add_argument(
        "--failure-chat-history-lookup-json",
        default="",
        help="JSON object, path, or @path for optional chat_history_lookup context.",
    )
    parser.add_argument(
        "--failure-request-id",
        default="",
        help="Existing failed turn request_id to resolve into a replay prompt.",
    )
    parser.add_argument(
        "--failure-current-request-id",
        default="",
        help="Current request id to exclude when resolving same-conversation failure references.",
    )
    parser.add_argument(
        "--failure-reference-mode",
        default="",
        help=(
            "Failure reference mode, for example latest_prior_failure, when no "
            "exact failure request id is supplied."
        ),
    )
    parser.add_argument(
        "--failure-reference-phrase",
        default="",
        help="User phrase that triggered same-conversation failure-case resolution.",
    )
    parser.add_argument(
        "--namespace",
        default="",
        help="Namespace to use for failure-case intake and experiment provenance.",
    )
    parser.add_argument(
        "--include-legacy-history",
        action="store_true",
        help="Allow failure-case intake to include legacy chat-history records.",
    )
    parser.add_argument(
        "--history-tail-limit",
        type=int,
        default=None,
        help="Optional history tail limit for failure-case intake.",
    )
    parser.add_argument(
        "--complexity-class",
        dest="complexity_classes",
        action="append",
        choices=tuple(PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS),
        help=(
            "Restrict random selection to one prompt complexity class. "
            "Repeat the flag to allow more than one class."
        ),
    )
    parser.add_argument("--output-json", default="")
    parser.add_argument(
        "--presenter-mode",
        action="store_true",
        help="Send presenter_mode=true to /von/generate to match the browser chat path.",
    )
    parser.add_argument(
        "--gmail-profile",
        default="",
        help=(
            "Optional configured Gmail profile alias to send to /von/generate "
            "for Gmail-backed replay prompts."
        ),
    )
    parser.add_argument("--list-prompts", action="store_true")
    parser.add_argument("--list-complexity-classes", action="store_true")
    args = parser.parse_args(argv)

    _emit_replay_guide_note()

    prompt_bank_payload = _load_prompt_bank()
    prompt_bank_schema_version = _safe_text(prompt_bank_payload.get("schema_version"))
    prompt_bank = _as_list(prompt_bank_payload.get("prompts"))
    requested_complexity_classes = [
        name
        for name in _as_list(args.complexity_classes)
        if isinstance(name, str) and name in PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS
    ]
    allowed_complexity_classes = frozenset(requested_complexity_classes)
    if args.list_complexity_classes:
        print(
            json.dumps(
                PROMPT_COMPLEXITY_CLASS_DESCRIPTIONS,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if args.list_prompts:
        print(
            json.dumps(
                [
                    {
                        "id": _safe_text(entry.get("id")),
                        "category": _safe_text(entry.get("category")),
                        "complexity_class": _safe_text(entry.get("complexity_class")),
                        "prompt": _safe_text(entry.get("prompt")),
                        "requires_tool_use": bool(entry.get("requires_tool_use")),
                        "allows_grounded_empty_result": bool(
                            entry.get("allows_grounded_empty_result")
                        ),
                    }
                    for entry in prompt_bank
                    if isinstance(entry, Mapping)
                    and (
                        not allowed_complexity_classes
                        or _safe_text(entry.get("complexity_class"))
                        in allowed_complexity_classes
                    )
                ],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    base_url = resolve_live_test_base_url(args.base_url)
    requested_model = _safe_text(args.model) or None
    requested_gmail_profile = _safe_text(args.gmail_profile) or None
    compare_models = [
        cleaned
        for entry in _as_list(args.compare_models)
        if isinstance(entry, str) and (cleaned := _safe_text(entry))
    ]
    model_arms = _build_model_arm_plan(
        requested_model=requested_model,
        compare_models=compare_models,
        include_active_model_arm=bool(args.include_active_model_arm),
    )
    replay_case_id = _safe_text(args.replay_case_id) or None
    replay_set_id = _safe_text(args.replay_set_id) or None
    target_workflow_id = _safe_text(args.workflow_id) or None
    workflow_stage_id = _safe_text(args.workflow_stage_id) or None
    base_prompt_id = _safe_text(args.base_prompt_id) or None
    prompt_variant_ids = _dedupe_texts(_as_list(args.prompt_variant_ids))
    failure_case_intake: dict[str, Any] | None = None
    explicit_prompt_text = _safe_text(args.prompt_text)
    failure_conversation_ref = _load_json_mapping_argument(
        args.failure_conversation_ref_json,
        argument_name="--failure-conversation-ref-json",
    )
    failure_chat_history_lookup = _load_json_mapping_argument(
        args.failure_chat_history_lookup_json,
        argument_name="--failure-chat-history-lookup-json",
    )
    failure_request_id = _safe_text(args.failure_request_id) or None
    failure_reference_mode = _safe_text(args.failure_reference_mode) or None
    failure_reference_phrase = _safe_text(args.failure_reference_phrase) or None
    if (
        failure_conversation_ref
        or failure_chat_history_lookup
        or failure_request_id
        or failure_reference_mode
    ):
        prompt_entry, failure_case_intake = _collect_failure_case_prompt_entry(
            conversation_ref=failure_conversation_ref or None,
            chat_history_lookup=failure_chat_history_lookup or None,
            request_id=failure_request_id,
            session_id=None,
            namespace=_safe_text(args.namespace) or None,
            user_concept_id=_safe_text(args.user_concept_id) or None,
            organisation_concept_id=_safe_text(args.organisation_concept_id) or None,
            target_model=requested_model,
            comparator_model=compare_models[0] if compare_models else None,
            workflow_id=target_workflow_id,
            stage_id=workflow_stage_id,
            current_request_id=_safe_text(args.failure_current_request_id) or None,
            reference_mode=failure_reference_mode,
            reference_phrase=failure_reference_phrase,
            include_legacy=bool(args.include_legacy_history),
            history_tail_limit=args.history_tail_limit,
            replay_case_id=replay_case_id,
        )
        replay_case_id = _safe_text(prompt_entry.get("id")) or replay_case_id
        target_workflow_id = (
            target_workflow_id
            or _safe_text(prompt_entry.get("source_workflow_id"))
            or None
        )
    elif explicit_prompt_text:
        prompt_entry = _normalise_prompt_entry(
            prompt_text=explicit_prompt_text,
            replay_case_id=replay_case_id or f"ad_hoc_replay_{uuid.uuid4().hex[:12]}",
            category="ad_hoc_replay",
            likely_tools=[],
            knowledge_surfaces=["turn_context"],
            requires_tool_use=False,
            source_kind="explicit_prompt_text",
        )
        replay_case_id = _safe_text(prompt_entry.get("id")) or replay_case_id
    else:
        prompt_entry = _choose_prompt(
            prompt_bank,
            seed=args.seed,
            prompt_id=_safe_text(args.prompt_id) or None,
            allowed_complexity_classes=allowed_complexity_classes,
        )
    replay_arms = _build_replay_arm_plan(
        model_arms=model_arms,
        base_prompt_id=base_prompt_id,
        prompt_variant_ids=prompt_variant_ids,
        workflow_stage_id=workflow_stage_id,
        target_workflow_id=target_workflow_id,
        replay_set_id=replay_set_id,
        replay_case_id=replay_case_id,
    )
    authenticated_user_concept_id = (
        _safe_text(args.user_concept_id) or DEFAULT_USER_CONCEPT_ID
    )
    authenticated_organisation_concept_id = (
        _safe_text(args.organisation_concept_id) or DEFAULT_ORGANISATION_CONCEPT_ID
    )
    session_name = _safe_text(args.session_name) or DEFAULT_SESSION_NAME
    run_environment = _collect_run_environment(
        base_url=base_url,
        requested_model=requested_model if len(model_arms) == 1 else None,
        user_concept_id=authenticated_user_concept_id,
        organisation_concept_id=authenticated_organisation_concept_id,
        session_name=session_name,
    )
    metadata_session = requests.Session()
    run_environment = _augment_run_environment_with_server_diag(
        session=metadata_session,
        base_url=base_url,
        run_environment=run_environment,
    )
    if not bool(args.allow_non_agent_test_server):
        agent_test_error = build_agent_test_server_requirement_error(
            run_environment,
            base_url=base_url,
        )
        if agent_test_error:
            raise RuntimeError(agent_test_error)
    if _replay_arms_require_active_llm_info(replay_arms):
        run_environment = _augment_run_environment_with_active_llm_info(
            session=metadata_session,
            base_url=base_url,
            user_concept_id=authenticated_user_concept_id,
            organisation_concept_id=authenticated_organisation_concept_id,
            run_environment=run_environment,
        )
    else:
        run_environment = {
            **run_environment,
            "server_resolved_active_llm_lookup_skipped": True,
            "server_resolved_active_llm_lookup_skip_reason": (
                "explicit_model_override_arms_only"
            ),
        }
    model_policy_report = _build_model_policy_report(
        requested_model_arms=replay_arms,
        run_environment=run_environment,
        allow_premium_model=bool(args.allow_premium_model),
    )
    _enforce_model_policy(model_policy_report)
    run_environment = {
        **run_environment,
        "model_policy": model_policy_report,
    }
    repeat_count = max(int(args.repeat_count or 1), 1)
    minimum_success_rate = min(max(float(args.minimum_success_rate), 0.0), 1.0)
    if bool(args.probe_local_models):
        probe_cache_raw = _safe_text(args.local_model_probe_cache_json)
        summary = _run_local_ollama_model_probe(
            prompt_entry=prompt_entry,
            base_url=base_url,
            timeout_seconds=float(args.timeout_seconds),
            poll_interval_seconds=float(args.poll_interval_seconds),
            user_concept_id=authenticated_user_concept_id,
            organisation_concept_id=authenticated_organisation_concept_id,
            session_name=session_name,
            run_environment=run_environment,
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=args.seed,
            presenter_mode=bool(args.presenter_mode),
            allow_non_agent_test_server=bool(args.allow_non_agent_test_server),
            repeat_count=repeat_count,
            screen_repeat_count=max(int(args.model_probe_screen_repeat_count or 1), 1),
            attempt_process_timeout_seconds=float(args.model_probe_attempt_timeout_seconds),
            minimum_success_rate=minimum_success_rate,
            requested_candidates=[
                entry
                for entry in _as_list(args.local_model_candidates)
                if isinstance(entry, str)
            ],
            pull_missing_models=bool(args.pull_missing_local_models),
            cache_path=Path(probe_cache_raw) if probe_cache_raw else None,
        )
        if failure_case_intake is not None:
            summary["failure_case_intake"] = failure_case_intake
        output_json = _safe_text(args.output_json)
        if output_json:
            _write_json_output(output_json, summary)
        print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if _safe_text(summary.get("status")) == "ok" else 1
    if repeat_count == 1:
        try:
            summary, should_user_be_happy = _run_replay_plan(
                prompt_entry=prompt_entry,
                base_url=base_url,
                requested_model=requested_model,
                replay_arms=replay_arms,
                timeout_seconds=float(args.timeout_seconds),
                poll_interval_seconds=float(args.poll_interval_seconds),
                user_concept_id=authenticated_user_concept_id,
                organisation_concept_id=authenticated_organisation_concept_id,
                session_name=session_name,
                run_environment=run_environment,
                prompt_bank_schema_version=prompt_bank_schema_version,
                requested_complexity_classes=requested_complexity_classes,
                seed=args.seed,
                base_prompt_id=base_prompt_id,
                prompt_variant_ids=prompt_variant_ids,
                presenter_mode=bool(args.presenter_mode),
                gmail_profile=requested_gmail_profile,
            )
        except Exception as exc:
            summary = _build_failed_replay_attempt_summary(
                exc=exc,
                attempt_index=1,
                prompt_entry=prompt_entry,
                run_environment=run_environment,
                requested_model=requested_model,
            )
            should_user_be_happy = False
    else:
        attempt_summaries: list[dict[str, Any]] = []
        successful_attempt_count = 0
        for attempt_index in range(1, repeat_count + 1):
            try:
                attempt_summary, attempt_success = _run_replay_plan(
                    prompt_entry=prompt_entry,
                    base_url=base_url,
                    requested_model=requested_model,
                    replay_arms=replay_arms,
                    timeout_seconds=float(args.timeout_seconds),
                    poll_interval_seconds=float(args.poll_interval_seconds),
                    user_concept_id=authenticated_user_concept_id,
                    organisation_concept_id=authenticated_organisation_concept_id,
                    session_name=session_name,
                    run_environment=run_environment,
                    prompt_bank_schema_version=prompt_bank_schema_version,
                    requested_complexity_classes=requested_complexity_classes,
                    seed=args.seed,
                    base_prompt_id=base_prompt_id,
                    prompt_variant_ids=prompt_variant_ids,
                    presenter_mode=bool(args.presenter_mode),
                    gmail_profile=requested_gmail_profile,
                )
                attempt_summary = {
                    **attempt_summary,
                    "attempt": {"attempt_index": attempt_index},
                }
                successful_attempt_count += 1 if attempt_success else 0
                attempt_summaries.append(attempt_summary)
            except Exception as exc:
                attempt_summaries.append(
                    _build_failed_replay_attempt_summary(
                        exc=exc,
                        attempt_index=attempt_index,
                        prompt_entry=prompt_entry,
                        run_environment=run_environment,
                        requested_model=requested_model,
                    )
                )
        summary = _build_repeated_replay_summary(
            prompt_entry=prompt_entry,
            prompt_bank_schema_version=prompt_bank_schema_version,
            requested_complexity_classes=requested_complexity_classes,
            seed=args.seed,
            requested_model=requested_model,
            requested_model_arms=replay_arms,
            run_environment=run_environment,
            attempt_summaries=attempt_summaries,
            success_count=successful_attempt_count,
            minimum_success_rate=minimum_success_rate,
        )
        should_user_be_happy = bool(
            _as_mapping(summary.get("repeat")).get("meets_minimum_success_rate")
        )
    if failure_case_intake is not None:
        summary["failure_case_intake"] = failure_case_intake
    experiment_run_id = _safe_text(args.experiment_run_id)
    if experiment_run_id:
        if repeat_count > 1:
            arm_summaries_for_recording = []
            for attempt in _as_list(summary.get("attempts")):
                if not isinstance(attempt, Mapping) or attempt.get("status") == "error":
                    continue
                if isinstance(attempt.get("arms"), Sequence):
                    arm_summaries_for_recording.extend(_as_list(attempt.get("arms")))
                else:
                    arm_summaries_for_recording.append(attempt)
        else:
            arm_summaries_for_recording = (
                _as_list(summary.get("arms"))
                if isinstance(summary.get("arms"), Sequence)
                else [summary]
            )
        experiment_recording = _record_experiment_observations(
            run_id=experiment_run_id,
            arm_summaries=[
                entry for entry in arm_summaries_for_recording if isinstance(entry, Mapping)
            ],
        )
        summary["experiment_recording"] = experiment_recording
        if not bool(experiment_recording.get("success")):
            raise RuntimeError(
                "Experiment observation recording failed: "
                f"{json.dumps(experiment_recording, ensure_ascii=True, sort_keys=True)}"
            )
    output_json = _safe_text(args.output_json)
    if output_json:
        _write_json_output(output_json, summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if should_user_be_happy else 1


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
