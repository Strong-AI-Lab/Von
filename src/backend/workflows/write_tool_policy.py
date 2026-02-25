"""Write-tool policy helpers.

This module centralises the policy for when a user prompt should allow invoking
write-category tools.

The policy is intentionally conservative and aims to prevent accidental writes
caused by the model emitting an unrelated tool call.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class WriteToolPolicyDecision:
    allowed_tools: frozenset[str]
    reason: str


# High-impact KB writes mutate ontology structure and should be review-gated when
# the admin toggle is enabled (JVNAUTOSCI-925).
HIGH_IMPACT_VONTOLOGY_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_concepts",
        "add_relationship",
        "remove_relationship",
        "remove_relationships_bulk",
        "undo_relationship_removal",
        "merge_concepts",
        "delete_concept",
        "update_concept",
    }
)


def compute_allowed_write_tools(
    *,
    prompt: str,
    requested_tools: list[str],
    recent_user_prompts: list[str] | None = None,
) -> WriteToolPolicyDecision:
    """Compute which write tools are allowed for this user prompt.

    Currently heuristic-based and workflow-friendly (deterministic), with
    conservative defaults.
    """

    allowed: set[str] = set()
    requested = [tool for tool in requested_tools if isinstance(tool, str) and tool]
    recent_prompts = [
        str(item).strip()
        for item in (recent_user_prompts or [])
        if isinstance(item, str) and str(item).strip()
    ]

    explicit_vontology_intent = _prompt_allows_vontology_mutation(prompt)
    explicit_artefact_intent = _prompt_allows_artefact_download(prompt)
    recent_vontology_intent = any(
        _prompt_allows_vontology_mutation(item) for item in recent_prompts
    )
    recent_artefact_intent = any(
        _prompt_allows_artefact_download(item) for item in recent_prompts
    )

    if explicit_vontology_intent or recent_vontology_intent:
        allowed.update(requested)
        return WriteToolPolicyDecision(
            allowed_tools=frozenset(allowed),
            reason=(
                "explicit_vontology_mutation_request"
                if explicit_vontology_intent
                else "recent_vontology_mutation_request"
            ),
        )

    # Side-effect writes (artefact storage) are allowed when explicitly requested.
    artefact_tools = {"download_paper", "finalise_cached_paper"}
    requested_artefact_tools = artefact_tools.intersection(set(requested))
    if requested_artefact_tools and (
        explicit_artefact_intent or recent_artefact_intent
    ):
        allowed.update(requested_artefact_tools)
        return WriteToolPolicyDecision(
            allowed_tools=frozenset(allowed),
            reason=(
                "explicit_artefact_download_request"
                if explicit_artefact_intent
                else "recent_artefact_download_request"
            ),
        )

    return WriteToolPolicyDecision(
        allowed_tools=frozenset(),
        reason="no_explicit_write_intent_detected",
    )


def is_high_impact_vontology_write_tool(tool_name: str) -> bool:
    """Return whether a tool is considered high-impact for ontology writes."""

    if not isinstance(tool_name, str):
        return False
    return tool_name.strip().lower() in HIGH_IMPACT_VONTOLOGY_WRITE_TOOLS


def prompt_grants_high_impact_kb_write_approval(
    *,
    prompt: str,
    recent_user_prompts: list[str] | None = None,
) -> bool:
    """Detect explicit human approval phrasing for high-impact KB writes.

    This is intentionally strict: simple write intent ("add", "create") is not
    treated as review approval. We require explicit approval language.
    """

    candidates: list[str] = []
    if isinstance(prompt, str) and prompt.strip():
        candidates.append(prompt.strip())
    for item in recent_user_prompts or []:
        if isinstance(item, str) and item.strip():
            candidates.append(item.strip())

    if not candidates:
        return False

    approval_pattern = re.compile(
        r"\b("
        r"approved|approve this|i approve|explicitly approved|"
        r"go ahead|proceed now|proceed with write|authori[sz]e(?:d)?|"
        r"you may write|human review complete|reviewed and approved|"
        r"confirmed(?: for write)?|permission granted"
        r")\b",
        flags=re.IGNORECASE,
    )
    kb_context_pattern = re.compile(
        r"\b(vontology|ontology|knowledge base|kb|concept|relationship|predicate)\b",
        flags=re.IGNORECASE,
    )

    for text in candidates:
        if approval_pattern.search(text) and kb_context_pattern.search(text):
            return True
    return False


def prompt_explicitly_denies_write(prompt: str) -> bool:
    """Return True when the user explicitly forbids write-side effects."""

    if not isinstance(prompt, str):
        return False

    lowered = prompt.lower()

    verbs = (
        "create",
        "add",
        "insert",
        "upsert",
        "update",
        "edit",
        "change",
        "delete",
        "remove",
        "rename",
        "merge",
        "link",
        "unlink",
        "set",
        "download",
        "fetch",
        "save",
        "store",
        "cache",
        "archive",
        "persist",
        "upload",
        "finalise",
        "finalize",
    )

    return any(
        re.search(
            rf"\b(?:do not|don't|dont|never)\s+{re.escape(verb)}\b",
            lowered,
        )
        for verb in verbs
    )


def _prompt_allows_vontology_mutation(prompt: str) -> bool:
    """Return True when the user explicitly requests a Vontology mutation."""

    if not isinstance(prompt, str):
        return False

    lowered = prompt.lower()

    mentions_vontology = any(
        token in lowered
        for token in (
            "#v#",
            "vontology",
            "ontology",
            "concept",
            "relationship",
            "predicate",
            "text relation",
            "note",
            "notes",
            "description",
            "summary",
            "annotation",
            "metadata",
        )
    )
    if not mentions_vontology:
        return False

    verbs = (
        "create",
        "add",
        "insert",
        "upsert",
        "update",
        "edit",
        "change",
        "delete",
        "remove",
        "rename",
        "merge",
        "link",
        "unlink",
        "set",
    )

    negated = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:do not|don't|dont|never)\s+(create|add|insert|upsert|update|edit|change|delete|remove|rename|merge|link|unlink|set)\b",
            lowered,
        )
    }

    for verb in verbs:
        if re.search(rf"\b{re.escape(verb)}\b", lowered) and verb not in negated:
            return True

    return False


def _prompt_allows_artefact_download(prompt: str) -> bool:
    """Detect explicit requests to download/store an artefact (e.g., arXiv PDF)."""

    if not isinstance(prompt, str):
        return False

    lowered = prompt.lower()

    action_verbs = (
        "get",
        "download",
        "fetch",
        "retrieve",
        "save",
        "store",
        "cache",
        "archive",
        "persist",
        "upload",
        "finalise",
        "finalize",
    )
    artefact_terms = (
        "arxiv",
        "paper",
        "pdf",
        "artefact",
        "artifact",
        "blob",
        "blob store",
        "artefact store",
        "artifact store",
        "swift",
        "object store",
        "storage",
        "container",
    )

    mentions_artefact = any(token in lowered for token in artefact_terms)
    mentions_arxiv_id = bool(re.search(r"\b\d{4}\.\d{4,5}\b", lowered))
    if not (mentions_artefact or mentions_arxiv_id):
        return False

    negated = {
        match.group(1)
        for match in re.finditer(
            r"\b(?:do not|don't|dont|never)\s+(download|fetch|save|store|cache|archive|persist|upload|finalise|finalize)\b",
            lowered,
        )
    }

    for verb in action_verbs:
        if re.search(rf"\b{re.escape(verb)}\b", lowered) and verb not in negated:
            return True

    return False
