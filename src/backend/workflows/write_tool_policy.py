"""Write-tool policy helpers.

This module centralises the policy for when a user prompt should allow invoking
write-category tools.

The policy is intentionally conservative and aims to prevent accidental writes
caused by the model emitting an unrelated tool call.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WriteToolPolicyDecision:
    allowed_tools: frozenset[str]
    reason: str


def compute_allowed_write_tools(
    *, prompt: str, requested_tools: list[str], recent_user_prompts: list[str] | None = None
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
    if "download_paper" in requested and (
        explicit_artefact_intent or recent_artefact_intent
    ):
        allowed.add("download_paper")
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
        )
    )
    if not mentions_vontology:
        return False

    import re

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

    import re

    action_verbs = (
        "download",
        "fetch",
        "save",
        "store",
        "cache",
        "archive",
        "persist",
        "upload",
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
            r"\b(?:do not|don't|dont|never)\s+(download|fetch|save|store|cache|archive|persist|upload)\b",
            lowered,
        )
    }

    for verb in action_verbs:
        if re.search(rf"\b{re.escape(verb)}\b", lowered) and verb not in negated:
            return True

    return False
