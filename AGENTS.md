# AI Agent Documentation Guide

This document provides an overview of key files within the `docs` directory that are primarily intended for use by AI systems (like GitHub Copilot or other automated agents) working on the Von-Private project. Understanding the purpose of these files will help AI agents contribute more effectively and maintain project context.

**All AI agents must read this file and the `docs/engineering/security_considerations.md` file before starting any work.**

> **TL;DR FOR AGENTS (READ FIRST)**
> 1. Use **New Zealand English** spelling always (behaviour, colour, organisation, realise).
> 2. **PowerShell is the default shell**. Do **NOT** emit Bash heredocs (`<<EOF`), `export VAR=`, `$(cmd)` substitution, or `source venv/bin/activate` unless the user explicitly asks for a Bash variant. Provide `$env:VAR = 'value'`, here-strings, or `pdm run` patterns.
- **Pre-commit guardrails**: enable hooks with `git config core.hooksPath .githooks` to block committing runtime data (e.g., `data/rag_storage`, `data/raw`, `logs`).
> 3. Never auto-start the server; wait for explicit user instruction.
> 4. Prefer clarity over clever chaining: separate lines instead of `&&` unless failure short‑circuit is required.
> 5. Large multi-line Python → use a here-string variable then `python -c $code` (PowerShell) or propose a committed script.
> 5.1. When providing paste-ready text (e.g., `.env` snippets, JSON tool calls, commands), wrap it in fenced code blocks so Markdown does not reformat or linkify it.
> 6. If the user pastes Bash that fails, convert it to valid PowerShell rather than trying to “fix” heredocs.
> 7. Keep changes minimal, well‑scoped, and update related tests/documentation.
> 8. When uncertain, ask succinctly—do not guess or fabricate behaviour.
> 9. Always check VS Code’s Problems panel (or run `get_errors`) after edits and whenever the user reports an error.
> 10. Avoid writing monolithic code to solve the immediate problem at hand. Wherever possible, factor work into small, composable functions with clear responsibilities. Prefer local, purpose-built helpers over a single global “do everything” helper or a new mini‑framework. Choose abstractions that are just deep enough to support likely near‑term change.
>
> For high‑risk state changes (database writes, Vontology/concept mutations, user identity/auth/org handling), prefer a single, well-defined authoritative pathway (service/tool/API) and reuse it consistently. Treat “another way of doing the same mutation” as a design smell and apply extra care: trace the call path, add/update tests, and avoid bypassing logging/validation.
> 11. When starting a new task or implementing a new function, build enough context to act confidently: confirm the purpose, identify the relevant existing code paths (especially the authoritative mutation pathway), and note key constraints. For small fixes, timebox this investigation and start with the smallest safe change.
> 12. Remember that this work is for an AI system that richly integrates knowledge management, representation and acquisition, with LLM capabilities and reasoning. Reasoning and representation can be done by logic, logical form, and text-based methods. There is a core "vontology" with a current underlying representation, but the possibility of other implementations should be acknowledged.
> 13. Use MCP tools (Vontology, JIRA, MongoDB) by default over writing ad-hoc scripts or direct database code. If MCP does not support your case, explain why and use the smallest safe alternative.

## 🇳🇿 CRITICAL: New Zealand English Only

**ALL code, documentation, comments, commit messages, and user-facing text MUST be written in New Zealand English.**

Key differences to remember:

**This is non-negotiable. Always check your output before submitting.**

## 🚨 CRITICAL SERVER WARNING

- ❌ **DO NOT** use `run_in_terminal` with server startup commands unless explicitly requested by the user
- ❌ **DO NOT** assume the server needs to be running to verify code changes

**Why:** Starting the server automatically can interrupt active development sessions, kill existing server instances, and disrupt ongoing work.
- ✅ Wait for user to explicitly request server startup
## Proactive Automation Protocol ("ACTUALLY TRY") – 2025-09-07

Purpose: Eliminate unnecessary confirmation prompts for standard, codified workflows. When conventions + tooling are present, the agent MUST act directly (attempt → retry on transient failure → report) rather than asking the user to approve each routine step.

When To Act Automatically:
- After implementing a fix & tests pass: Post JIRA comment (summary, root cause, fix, tests, risk, follow-ups) then transition issue to the correct state (usually Done) without asking.
- When creating a new JIRA issue: assign it to the current user by default (unless the user explicitly names a different assignee).
- Retry transient Atlassian 5xx or rate limit errors up to 3 times with exponential backoff (~30s total) before surfacing failure.
- When a somewhat complex operation already has a lightweight LLM heuristic path (e.g. missing tool-use detection), prefer improving the heuristic behaviour (prompt/context/inputs) over adding brittle, case-specific string matching that will break as natural language phrasing evolves.
- Preserve raw user-authored text in UI workflows (store original in dataset/raw attribute) whenever rendered/HTML transformations occur — pattern originates from description markdown preservation.
- Consolidate duplicated DOM update logic into a single helper before expanding features (prevents regression drift).
- Add a regression test for every state/format loss bug fix (load → edit → save → re-edit cycle) before declaring completion.

JIRA assignee updates (do not misinterpret tooling):
- If an issue is created without an assignee (or with the wrong one), it can usually be fixed retrospectively using the Atlassian MCP edit tool.
- Preferred pattern (assignee by accountId):
  ```python
  mcp_atlassian_editJiraIssue(
      cloudId="...",
      issueIdOrKey="JVNAUTOSCI-XXX",
      fields={"assignee": {"accountId": "<account_id>"}}
  )
  ```
- Do not create a duplicate issue just to correct assignee unless the edit call fails due to permissions or workflow restrictions.

When To Ask (Legitimate Blockers Only):
- Ambiguous or missing JIRA issue key (multiple candidates, unparsable branch name).
- Atlassian cloudId lookup fails after all retries.
- Required tool category (e.g., repository management) not enabled — request enabling it by exact name.
- Tests failing; intent of expected behaviour unclear from context.
- Operation would delete or mutate data outside documented safe patterns.

Operational Checklist (Mental Pass Before Finishing a Task):
1. Tests (scoped to change) executed and green.
2. Commit message includes JIRA key + concise description.
3. Regression test added (if bug fix) and passing.
4. JIRA comment posted (complete template fields).
5. Issue transitioned (status verified).
6. Optional: Merge / branch cleanup if within remit and tools available.

Failure Handling Patterns:
- Transient API errors: Retry (exponential). Final failure → show status + truncated body (<300 chars) + next recommended action.
- Tool not available: Request enabling precise tool group; do NOT fabricate manual raw HTTP calls.
- Flaky MCP providers (especially Atlassian/JIRA): If tool calls fail with timeouts, connection errors, or intermittent 401/403/5xx responses despite correct inputs, treat it as a provider/session issue.
  - Before asking the user to restart anything, do a bounded self-heal retry: re-run the same tool call once (or up to twice total) with short backoff (e.g., ~1s then ~3s). This catches common “token just refreshed / provider waking up” cases.
  - If it still fails, re-check the session by calling the accessible-resources tool once; if that also fails, then ask the user to use the provider’s **Restart Server** action (extension-managed MCP providers have a restart command in the MCP Servers UI).
  - If **Restart Server** does not resolve it, ask the user to restart VS Code’s extension host (or run **Developer: Reload Window**) and then retry.
  - Do not loop indefinitely: cap total retries per operation (e.g., 2) and surface the last error with a clear recommended next action.
  - Do **NOT** “hack around” Atlassian MCP failures by creating ad-hoc scripts (e.g. temporary Python) or by calling Jira REST directly. Fix the MCP session instead (bounded retry → accessible-resources check → MCP server restart → Reload Window). If the cloudId is the blocker, use the documented tenant-info browser fallback.
- Cache/Data Structure Sensitivity: Never reorder or shrink tuple/dict cache structures relied upon by diagnostics (append only; update summariser accordingly).

Language & Shell Consistency:
- Enforce New Zealand English (behaviour, colour, organisation, initialise, analyse).
- PowerShell first: Avoid Bash heredocs, export, $( ). Translate user-provided failing Bash snippets into valid PowerShell.

Tags: #AUTO_ACTION_PROTOCOL #ACTUALLY_TRY #NO_REDUNDANT_PERMISSION #RAW_TEXT_PRESERVATION

### Tool Invocation Behaviour (Anti-Pattern: JSON Action Output)

**Problem (JVNAUTOSCI-698, October 2025):** Some LLMs output `{"action":"call_tool",...}` as *text* instead of triggering tool execution. This occurs when the LLM interprets instructions as "describe what you want to do" rather than "do it now".

**Prevention Mechanisms:**
- **Instruction message** emphasises **immediate invocation behaviour** over JSON format
- **Response detector** identifies when LLM outputs tool call JSON as text
- **Auto-recovery** mechanism extracts and executes tools from JSON responses
- **Logging** tracks frequency to identify models needing prompt refinement

**For AI Agents:** If you find yourself outputting JSON that looks like:
```json
{"action": "call_tool", "tool": "some_tool", "payload": {...}}
```
...you are experiencing the failure mode. Instead:
- **Just invoke the tool directly** (your framework handles formatting)
- **Don't describe what you're going to do** - do it
- **JSON output should only happen when you want immediate execution**
- If the user has to type "continue" to nudge you forward, you failed

**Detection:** System logs `[mcp_orchestrator] Detected JSON action output (LLM failure mode)` when this pattern occurs.

- ✅ Let the user control when and how the server runs
### Missing Tool Call Detector (Manual Use Only)

**Note:** Von's chat orchestrator does NOT currently invoke this detector automatically. It exists in the Vontology for potential future integration or manual testing.

**How to invoke manually:**
```python
# Load detector action from Vontology
detector = fetch_concept("#V#detect_missing_tool_call_action")
prompt_concept = detector.relationships["#V#uses_prompt"][0]  # #V#missing_tool_call_detection_prompt
prompt_text = get_text_relations(prompt_concept, predicate="hasContent")[0]["text"]
model = detector.relationships["#V#uses_llm_model"][0]  # #V#gpt-4o-mini

# Invoke with assistant message
result = llm_service.invoke(model, prompt_text, input_message=assistant_draft)
# Returns: "yes" (tool promised but not invoked) or "no" (OK)
```

**What it detects:** Promises like "I'll fetch JVNAUTOSCI-803" without actual tool call JSON. See `#V#missing_tool_call_detection_prompt` for exact classification logic.


### Automatic External Tool Activation (JIRA / GitHub / Mongo / Others)

Agents MUST proactively activate required external tool categories (e.g. Atlassian/JIRA, GitHub repos, GitHub PRs, MongoDB, Confluence) **without asking for permission first** when a user request clearly implies their use. This further removes unnecessary round‑trips and complements the Proactive Automation Protocol.

Activation / Usage Rules:
- Treat tool activation like importing a library: silent and routine, not a user decision.
- Activate only what is plausibly needed for the immediate task (minimal surface, avoid bulk enabling unrelated categories).
- If a tool category is already active, proceed directly—do NOT restate its availability.
- After activation, do the work (e.g. fetch issue, post comment, transition status) in the same flow unless blocked.
- Retry transient 5xx / rate limit responses (backoff, up to 3 attempts) exactly as in broader protocol.

When To Ask (Exception Cases Only):
- Ambiguous target (e.g. multiple possible JIRA keys / repository names) and inference would be speculative.
- Required capability genuinely absent (category not enabled) after a single activation attempt; then request enabling that specific category by name.
- Operation would be destructive outside documented safe patterns (mass deletion, force-push, dropping DBs).

Logging & Messaging:
- Keep user-facing narration minimal: a short preface when chaining multiple automated actions is acceptable; avoid verbose justification of activation.
- Do not expose internal tool identifiers—describe actions functionally ("Retrieving issue details", "Posting regression summary").

Failure Surfacing:
- On persistent failure after retries: report HTTP status (or tool error summary), truncated body (<300 chars), and the next recommended action.
- Never fabricate outcomes—if transition/comment creation uncertain, explicitly state uncertainty.

Security / Scope:
- Never perform cross‑project actions unless the context contains an explicit reference (e.g. switching from JVNAUTOSCI to KKAT).
- Do not cache credentials or tokens in documentation or code; rely solely on provided tool interfaces.

This section operationalises the expectation that well-understood, low‑risk integrations proceed automatically, further reducing user cognitive load while preserving safety through clearly bounded exception triggers.

### JIRA Issue Hierarchy (CRITICAL - Verified December 2025)

**Tasks CAN have Subtasks.** Previous agent notes incorrectly stated otherwise.

**Verified Hierarchy:**
- **Epic** → Story → Task (traditional hierarchy)
- **Task** → Subtask ✅ **CONFIRMED WORKING**

**Creating Subtasks under a Task:**
```python
mcp_atlassian_createJiraIssue(
    cloudId="...",
    projectKey="JVNAUTOSCI",
    issueTypeName="Subtask",  # Use "Subtask" type
    summary="...",
    description="...",
    parent="JVNAUTOSCI-XXX"  # Parent task key in additional_fields
)
```

**Key Points:**
- Use `issueTypeName="Subtask"` (capital S)
- Pass parent task key via `parent` field (not nested in `additional_fields`)
- Do NOT assume Tasks cannot have subtasks - this is incorrect
- The `parent` field is a top-level parameter in `createJiraIssue`

## 🖥️ CRITICAL SHELL ENVIRONMENT: POWERSHELL FIRST (NOT BASH)

**AI AGENTS MUST ASSUME THE PRIMARY INTERACTIVE SHELL IS WINDOWS POWERSHELL (`pwsh`).** Many habitual Linux / macOS Bash idioms **WILL FAIL** here and MUST NOT be emitted unless the user explicitly asks for a Bash script (e.g., inside `setup_py.sh`). Provide PowerShell-safe commands by default.

### ❌ DO NOT OUTPUT (Bash‑only or Unix‑assumptive)
- Heredocs: `<<EOF`, `<<'PY'`, `cat <<EOF > file`  (PowerShell does not support Bash heredoc syntax)
- `export VAR=value` (PowerShell uses `$env:VAR = 'value'`)
- Chained `&&` sequences for *every* multi-step example when failure semantics not required (prefer separate lines for clarity)
- Command substitutions: ``var=$(cmd)`` (use `$var = (cmd)` in PowerShell)
- Backslash escaping inside double quotes for PowerShell strings (use backtick `` ` `` if escape truly needed; often simpler to change quote style)
- `source venv/bin/activate` (use `./.venv/Scripts/Activate.ps1` or rely on `pdm run` which auto-manages the venv)

### ✅ DO OUTPUT (PowerShell‑correct forms)
- Multi-line Python via here-string variable:
    ```powershell
    $code = @'
    import json
    print("ok")
    '@
    pdm run python -c $code
    ```
- Pipe here-string to stdin:
    ```powershell
    @'\nprint("ok")\n'@ | pdm run python -
    ```
- Simple multi-step examples as separate lines:
    ```powershell
    pdm install
    pdm run pytest -q
    ```

### Mixed Environment Guidance
- If both Bash and PowerShell variants are genuinely needed, **LABEL THEM CLEARLY** with headings `PowerShell:` and `Bash:`.
- Prefer *not* to emit a Bash variant unless the task explicitly references `.sh` scripts or Linux container contexts.

### Model Compliance Rules
1. **Default to PowerShell.** If a user pastes a Bash heredoc and asks "why doesn't this work", convert it to a PowerShell form.
2. **Never fabricate heredoc support.** Do not suggest installing a module to "enable" heredocs; rewrite instead.
3. **Preserve NZ English spelling** in any explanatory text around commands.
4. **Validate quoting**: For Python one-liners prefer single quotes outside and double quotes inside to minimise escaping.
5. **Large / complex snippets** → propose a new script file rather than an unreadable single line.

### QUICK REFERENCE TABLE
| Intent | Bash (DON'T emit) | PowerShell (DO emit) |
|--------|-------------------|----------------------|
| Set env var | `export FOO=bar` | `$env:FOO = 'bar'` |
| Heredoc python | `python - <<'PY' ... PY` | `$code = @'... '@; python -c $code` |
| Command substitution | `VAL=$(cmd)` | `$VAL = (cmd)` |
| Activate venv | `source .venv/bin/activate` | Use `pdm run <cmd>` |
| Append to file | `echo "x" >> file` | `Add-Content -Path file -Value 'x'` |

**Any agent output violating these rules should be considered incorrect and may be rejected.**

## 🐛 CRITICAL UI DEBUGGING: Missing/Invisible Elements Protocol

**When users report "missing" UI elements (buttons, links, etc.), follow this diagnostic sequence BEFORE assuming DOM/JavaScript issues:**

### Phase 1: Verify Element Existence (DOM Analysis)
1. **Check if elements exist in DOM** using browser dev tools or debug scripts
2. **Verify correct IDs/classes** are present and match expected patterns
3. **Confirm event delegation** is working (event handlers attached to correct ancestors)

### Phase 2: CSS Visibility Analysis (Most Common Issue)
**CRITICAL**: Elements often exist but are invisible due to CSS styling problems.

**Common CSS Visibility Failures**:

**Positioning Issues**:
- **Absolute positioning without relative parent**: `position: absolute` elements need `position: relative` on parent container
- **Off-screen positioning**: `left: 100%`, `top: -9999px`, `transform: translateX(-100%)` can hide elements
- **Z-index layering**: Elements behind other elements due to stacking context

**Size & Dimension Issues**:
- **Zero dimensions**: `width: 0`, `height: 0`, `max-width: 0`, `max-height: 0`
- **Collapsed elements**: `display: none`, `visibility: hidden`
- **Tiny elements**: `font-size: 0`, `line-height: 0`, microscopic dimensions

**Visual Appearance Issues**:
- **Transparency**: `opacity: 0`, `color: transparent`, `background: transparent`
- **Colour matching background**: `color: white` on white background, invisible text
- **Overflow clipping**: Parent containers with `overflow: hidden` clipping child elements

**Layout & Flow Issues**:
- **Float problems**: Elements floated out of normal flow
- **Flexbox/Grid issues**: `display: none` on flex/grid items, incorrect flex properties
- **Transform issues**: `scale(0)`, `translateX(-9999px)`, rotation out of view

**Comprehensive CSS Debug Checklist**:
```javascript
// Complete visibility diagnostic (paste in browser console)
const element = document.querySelector('#your-element-id');
if (element) {
    const styles = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    console.log('Element found - Visibility Analysis:', {
        // Basic visibility
        display: styles.display,
        visibility: styles.visibility,
        opacity: styles.opacity,

        // Positioning
        position: styles.position,
        left: styles.left,
        top: styles.top,
        zIndex: styles.zIndex,

        // Dimensions
        width: rect.width,
        height: rect.height,
        maxWidth: styles.maxWidth,
        maxHeight: styles.maxHeight,

        // Appearance
        color: styles.color,
        backgroundColor: styles.backgroundColor,
        fontSize: styles.fontSize,
        lineHeight: styles.lineHeight,

        // Layout
        overflow: styles.overflow,
        transform: styles.transform,

        // Bounding box (viewport position)
        boundingBox: rect,
        isInViewport: rect.top >= 0 && rect.left >= 0 &&
                     rect.bottom <= window.innerHeight &&
                     rect.right <= window.innerWidth
    });

    // Check if element is effectively invisible
    const isInvisible = styles.display === 'none' ||
                       styles.visibility === 'hidden' ||
                       styles.opacity === '0' ||
                       rect.width === 0 ||
                       rect.height === 0;
    console.log('Element is invisible:', isInvisible);
} else {
    console.log('Element not found in DOM');
}
```

### Phase 3: Parent Container Analysis
**For absolutely positioned elements**, verify parent containers:
```javascript
// Check parent positioning context
const parent = element.parentElement;
const parentStyles = getComputedStyle(parent);
console.log('Parent position:', parentStyles.position); // Should be 'relative' for abs children
```

### Case Study: JVNAUTOSCI-604 (September 2025)
**Symptom**: "Buttons missing despite previous fixes"
**User Report**: "No, no buttons still. This is persistent!!!"
**Root Cause**: `.text-block-actions` used `position: absolute; left: 100%` but `.type-description-wrapper` lacked `position: relative`
**Solution**: Added CSS rule: `.type-description-wrapper { position: relative; }`
**Lesson**: Always check CSS positioning before debugging JavaScript/DOM issues

### Quick Decision Tree
1. **User reports missing UI element** → Check if element exists in DOM
2. **Element exists** → Run comprehensive CSS visibility diagnostic
3. **Check common causes in order**:
   - **Dimensions**: `width: 0`, `height: 0`, `display: none`
   - **Transparency**: `opacity: 0`, `visibility: hidden`, colour matches background
   - **Positioning**: Off-screen, z-index behind other elements, absolute without relative parent
   - **Overflow**: Parent containers clipping children
   - **Layout**: Float/flexbox/grid flow issues
4. **Element truly missing from DOM** → Then investigate JavaScript/template issues

**Priority**: CSS visibility issues are 10x more common than missing DOM elements. Start with comprehensive CSS analysis.

**When the element exists but looks wrong (e.g., centred, oddly indented, unexpectedly bold):** ask the user to use Chrome DevTools (Elements tab) to copy the element's `outerHTML` and capture key computed styles (especially `display`, `white-space`, `text-align`, `font-weight`, `margin`, `padding`, and `line-height`) for the element and its nearest container. This is often the fastest way to spot a single inherited CSS rule or an unexpected wrapper like `<h1>`.
## Core AI-Focused Documents

1.  **`docs/AINotes.md`**
    *   **Purpose:** Serves as the AI's primary short-term memory and tactical task management log.
    *   **Usage for AI:** Before starting work, AI agents should consult this file to understand the current immediate focus, recently completed sub-tasks, outputs of recent operations (like script executions or commit message summaries), and any specific contextual notes relevant to the ongoing development. Agents should update this file with their progress, decisions, and any new information that would be pertinent for the next work session or for other agents.

2.  **`docs/concept_refactoring.md`** (or similar high-level plan documents)
    *   **Purpose:** Outlines the strategic, high-level plan for major ongoing development efforts, such as the current "Concept Refactoring."
    *   **Usage for AI:** This document provides the overarching goals, phases, and key architectural decisions for large-scale changes. AI agents should refer to this to understand how their current tasks fit into the broader project roadmap and to ensure their contributions align with the strategic objectives. It should be updated as major phases are completed or if strategic decisions evolve.

3.  **`docs/software_engineering.md`**
    *   **Purpose:** A living document that accumulates software engineering best practices, lessons learned from the project, coding conventions, and debugging strategies.
    *   **Usage for AI:** AI agents should consult this file for guidance on how to approach development tasks, write maintainable code, and debug effectively within this specific project. It serves as a repository of shared knowledge to improve the quality and efficiency of development. AI agents are encouraged to contribute new lessons learned to this file (with user confirmation).

By utilising these documents, AI agents can better understand project context, maintain continuity, and adhere to established plans and practices.

## Research Prototype Engineering Philosophy

**CRITICAL:** This is a **research prototype system**, not a production enterprise application. AI agents must balance appropriate engineering practices with research needs.

### ✅ **DO: Focus on Research-Appropriate Quality**

- **Modular Design:** Write clean, modular code that can be easily extended and modified as research evolves
- **Extensible Architecture:** Design components to support future research directions without major rewrites
- **Careful Implementation:** Be thoughtful about core algorithms, data structures, and interfaces
- **Comprehensive Testing:** Write thorough tests to ensure reliability during research activities
- **Clear Documentation:** Document key decisions and interfaces for future researchers
- **Version Control:** Maintain clean git history and meaningful commit messages

### ❌ **DON'T: Over-Engineer for Enterprise Scale**

- **Avoid Premature Optimisation:** Don't optimise for massive scale unless research specifically requires it
- **Skip Enterprise Boilerplate:** Don't add complex configuration management, deployment pipelines, or enterprise monitoring unless needed
- **Resist Feature Creep:** Implement what's needed for research goals, not hypothetical future users
- **Don't Gold-Plate UI/UX:** Focus on functional, usable interfaces rather than pixel-perfect design
- **Skip Cross-Browser Testing:** Unless browser compatibility is part of the research, Chrome/Firefox compatibility is sufficient
- **Avoid Over-Abstraction:** Don't create abstract frameworks for single-use cases
- **Avoid inline CSS:** put all styles in the CSS file

### 🎯 **The Sweet Spot: Research-Ready Engineering**

Build systems that are:
- **Reliable** enough for research workflows
- **Flexible** enough to adapt as research evolves
- **Well-tested** enough to trust results
- **Documented** enough for other researchers to understand
- **Modular** enough to reuse components in different contexts

**Remember:** The goal is to support high-quality research, not to build the next enterprise software platform. Make engineering decisions that serve research objectives, not theoretical scalability requirements.

## JIRA and Repository Context

When creating or referencing JIRA issues, please note the following primary projects:

*   **`JVNAUTOSCI`**: This is the main project key for the **Jan Von Neumarkt Automated Science** initiative. Most work related to this repository (`Von-Private`) should be tracked under this key.
*   **`KKAT`**: This key is for the **KnowKat** project, which is a related but separate system. Work related to the KnowKat repository should be tracked here.

Please use the correct project key when creating new issues.

## Standard Development Workflow

To ensure consistency and maintain a clean history, all agents must follow this workflow for every task:

1.  **JIRA Issue**: Ensure a JIRA issue exists for the task. If not, create one and assign it to the current user by default (Project: `JVNAUTOSCI`) unless the user explicitly requests a different assignee.
2.  **Branching**: Create a new branch from `main` using the JIRA key: `git checkout -b JVNAUTOSCI-XXX-short-description`.
3.  **Implementation**:
    *   Make changes.
    *   Run tests.
    *   Commit with the JIRA key in the message: `JVNAUTOSCI-XXX: Description of changes`.
4.  **Pull Request**:
    *   Push the branch: `git push -u origin JVNAUTOSCI-XXX-short-description`.
    *   Create a PR using `gh pr create`.
    *   **Self-Review**: Verify the diff and ensure it matches the intent.
5.  **Merge**:
    *   Merge the PR using `gh pr merge --merge --delete-branch`.
    *   **Do not squash** unless specifically requested (preserve commit history for context).
    *   Transition the JIRA isseue to done, after commenting on the issue documenting the changes
6.  **Cleanup**:
    *   Switch to `main`: `git checkout main`.
    *   Pull latest changes: `git pull`.
    *   Delete local branch: `git branch -d JVNAUTOSCI-XXX-short-description`.

### Multi-Machine Development: Always Push to Remote

**CRITICAL:** This project is actively developed across multiple machines. When committing directly to `main` (or merging a PR), **always push to remote immediately** unless explicitly told otherwise. Consistent git state across machines is essential.

**Rule:** `git commit` → `git push` as a single action. Do not leave commits unpushed on main, even temporarily.

## Technical Standards & Constraints

**All AI agents must adhere to these technical standards. Deviations require explicit user approval.**

### 1. Dependency Management: PDM Only
*   **Standard**: This project uses **PDM** for Python dependency management.
*   **Forbidden**: Do NOT use `pip install` directly. Do NOT create or suggest `requirements.txt` files unless they are generated artifacts.
*   **Correct Usage**:
    *   Add dependency: `pdm add <package>`
    *   Add dev dependency: `pdm add -d <package>`
    *   Run script: `pdm run python script.py`
    *   Run tests: `pdm run pytest`

### 2. Frontend Architecture: Vanilla JS + ES6 Modules
*   **Standard**: The frontend uses **Vanilla JavaScript** with native **ES6 Modules**.
*   **Forbidden**: Do NOT introduce frontend frameworks (React, Vue, Angular) or build steps (Webpack, Vite) without explicit architectural approval.
*   **Icon System**: Use the `IconRegistry` in `src/frontend/web/von_interface/static/js/icon_registry.js`. Do NOT inline SVG strings repeatedly in components.
    *   *Usage*: `import { IconRegistry } from './icon_registry.js'; const icon = IconRegistry.getIcon('iconName');`

### 3. Logging: Knowledge Interaction Logger
*   **Standard**: All interactions with the Vontology (concepts, relations) must be logged using the `knowledge_interaction_logger`.
*   **Purpose**: Ensures a consistent audit trail of AI-driven knowledge base modifications.
*   **Correct Usage**:
    ```python
    from src.backend.utils.knowledge_interaction_logger import log_knowledge_interaction

    log_knowledge_interaction(
        interaction_type="create_concept",
        details={"name": "NewConcept", "parent": "Thing"},
        agent_id="agent_name"
    )
    ```

### 4. Code Quality: Black & Pyright
*   **Formatting**: Python code must be formatted with **Black**.
*   **Typing**: Python code must be type-checked with **Pyright**.
*   **Action**: If you modify a file, ensure it passes these checks before committing.
    *   `pdm run black .`
    *   `pdm run pyright`

### 5. Concept Identifiers
*   **Standard**: All internal concept identifiers must use the `#V#` prefix format (e.g., `#V#person`, `#V#thing`).
*   **Deprecated**: Do NOT use filesystem-like paths (e.g., `/Thing/Person`) or raw names without prefixes as IDs.
*   **Reason**: Ensures consistent identification across the graph database and application logic.

## Test Compatibility and Maintenance

**CRITICAL TESTING PHILOSOPHY**: This project is **NOT test-driven**. Tests are written to validate implementation correctness and catch regressions, but they do not drive design decisions. Dead code and its associated tests can be removed without hesitation when identified.

When modifying code or creating new features, AI agents MUST follow these testing guidelines:

1. **Export for Testability:**
   * All functions that might need to be tested should be properly exported
   * Consider test requirements when structuring code (e.g., separating business logic from DOM manipulation)
   * Make sure utility functions are accessible for testing
   * Document when a function is exported specifically for testing purposes with a comment like `// Export for testing`

2. **Test Updates:**
   * After ANY code changes, existing tests MUST be reviewed and updated
   * If adding new functionality, corresponding tests MUST be written
   * Test both success and failure cases
   * For UI components, test user interactions and state changes
   * Ensure mocks properly reflect the actual implementation

3. **Tests vs. Implementation Priority:**
   * When tests fail after implementation changes, **prioritize updating tests** to match the implementation rather than modifying working code to match outdated tests
   * This is especially important for tests involving third-party libraries (like OpenAI SDK) where the library's API may have changed
   * Tests are often generated automatically rather than through test-driven design, so implementation correctness should take precedence
   * Only modify implementation to match tests when the tests clearly represent the correct behaviour and the implementation is incorrect

4. **Test First Development:**
   * When possible, write or update tests before making code changes
   * Be very careful not to change existing code just because it does not match a test - the test may be a misguided interpretation of a requirement.
   * Use tests to verify that changes meet requirements
   * Document test cases that verify specific requirements or edge cases

5. **Test Structure:**
   * Group related tests using `describe` blocks
   * Use clear, descriptive test names that explain the scenario being tested
   * Follow the Arrange-Act-Assert pattern in test cases
   * Include setup and teardown code when needed

6. **Running Tests via Copilot tools:**
  * When using the `runTests` tool, always provide `tests` or specific test file paths in the `files` argument; invoking it with no targets can return `0/0` and hides real coverage.

Example of proper test-compatible exports:
```javascript
// Export for testing - allows direct testing of utility functions
export function validateInput(value) {
    return typeof value === 'string' && value.length > 0;
}

// Main event handler - also exported for testing
export async function handleSubmit(event) {
    if (!validateInput(event.target.value)) {
        throw new Error('Invalid input');
    }
    // ... rest of the handler
}
```

### CRITICAL: Test Database Environment Variable Hygiene

**Problem:** Setting `VON_DB_NAME=test_von_db` to run tests and forgetting to reset it can cause the production server to run against test data, leading to data loss or corruption.

**Prevention Mechanisms (Added October 2025):**

1. **Automatic Server Start Protection:**
   - `run.ps1` and `run.sh` now check for `VON_DB_NAME=test_von_db` before starting the server
   - Server start is blocked with clear error message if test database is detected
   - Provides fix command: `$env:VON_DB_NAME = 'von_db'` (PowerShell) or `export VON_DB_NAME='von_db'` (Bash)

2. **Best Practices for Running Tests:**

**PowerShell (Preferred Pattern):**
```powershell
# CORRECT: Set and reset in same command
$env:VON_DB_NAME = 'test_von_db'; pdm run pytest tests/test_file.py; $env:VON_DB_NAME = 'von_db'

# BETTER: Use temporary scope
& { $env:VON_DB_NAME = 'test_von_db'; pdm run pytest tests/test_file.py }
# Environment automatically resets after scriptblock exits
```

**Bash:**
```bash
# CORRECT: Set only for command duration
VON_DB_NAME='test_von_db' pdm run pytest tests/test_file.py

# Environment variable only set for that one command, automatically resets
```

3. **After Running Tests Manually:**
   - **ALWAYS** verify environment: `echo $env:VON_DB_NAME` (PowerShell) or `echo $VON_DB_NAME` (Bash)
   - **ALWAYS** reset if needed: `$env:VON_DB_NAME = 'von_db'` (PowerShell) or `export VON_DB_NAME='von_db'` (Bash)
   - Server start will now fail-fast if test database detected, but prevention is better than cure

4. **For AI Agents:**
   - When running tests, use scoped environment variables as shown above
   - Never leave test database set in environment after test completion
   - If you set `VON_DB_NAME` manually, **immediately add a reminder** to reset it in your response to the user
   - Consider the server start blocker as a safety net, not a primary prevention mechanism

**Why This Matters:**
- Production server against test database = data loss
- Test database has different data/structure than production
- Intermittent "missing data" bugs that are hard to diagnose
- Previous incidents have caused developer confusion and wasted time

## Core Database Information

When performing direct database operations, please use the following connection details to avoid errors:

*   **Database Name:** `von_db`
*   **Vontology Collection:** `vontology_nodes`
*   **Entities Collection:** `entities`

Always refer to this section to confirm the correct database and collection names before using MongoDB tools.

## IMPORTANT - the use of filesystem-like paths in the code is deprecated.
All valid concept names look like #V#person or #V#thing etc. Other forms are legacy and should be replaced over time.
Predicates like subconceptOf are used for relationships between concepts.

## Interacting with the Vontology: The MCP Server

To ensure a standardized, stable, and secure way for AI agents to interact with the project's core knowledge base, a dedicated **Model Context Protocol (MCP) server** has been established.

**Purpose:** The MCP server exposes core Vontology functions as a simple, well-defined API. This is the **preferred method** for all AI-driven interactions with the Vontology.

**Why use the MCP Server instead of custom scripts?**
*   **Stability:** The server provides a stable set of endpoints. This is much more reliable than writing temporary Python scripts, which can break as the underlying codebase changes.
*   **Simplicity:** Interacting with the server via simple web requests is more straightforward than writing and executing custom Python code.
*   **Security & Control:** The server acts as a controlled gateway to the Vontology, preventing direct, unrestricted access to the database and backend code.

### Available MCP Server Endpoints

The Vontology MCP server is provided as a **stdio** MCP server (no HTTP port) via `src/backend/mcp_server/mcp_stdio_server.py`.

Von's main web server runs separately (default `http://127.0.0.1:5000` when launched via `run.ps1`) and exposes admin endpoints such as `/admin/rag_status` on that same port.

| Endpoint                  | Method | Description                                                                                             |
| ------------------------- | ------ | ------------------------------------------------------------------------------------------------------- |
| `/create_concept`         | POST   | Creates a new concept in the Vontology. Requires `parent_id` and `new_concept_name`.                    |
| `/find_subconcepts`       | GET    | Finds the direct children (subconcepts) of a given `concept_id`.                                        |
| `/find_concepts_by_name`  | GET    | Searches for concepts with a name containing a specific string. Requires a `name` parameter.            |
| `/add_names`              | POST   | Adds one or more names/aliases to a concept using text relations (hasName predicate). Requires `concept_id` and `names` array. Each name can be a string or object with {name, language, name_type}. |

#### Understanding Language Codes and Name Types

When adding names to concepts via `/add_names`, you can specify:

**Language Codes** (ISO 639-1 / BCP 47 format):
- `en` - English (generic)
- `en-NZ` - New Zealand English (**default**)
- `en-US` - US English
- `en-GB` - British English
- `fr` - French (Français)
- `de` - German (Deutsch)
- `es` - Spanish (Español)
- `it` - Italian (Italiano)
- `mi` - Māori (te reo Māori)
- `zh` - Chinese (中文)
- `ja` - Japanese (日本語)
- `cycL` - CycL/OpenCyc formal language (for imported URIs)

**Name Types**:
- `NL` (Natural Language) - **Default**. Standard human-readable names, preferred labels, synonyms, translations
  - Examples: "Person", "European Union member state", "État membre de l'Union européenne"
  - Use for: Most names, including multilingual translations
- `ABBR` (Abbreviation) - Short forms, initialisms, acronyms
  - Examples: "EU", "USA", "PhD", "NATO", "EU MS"
  - Use for: Abbreviated forms that users might search for
- `CODE` - Technical identifiers, URIs, database keys, formal logic representations
  - Examples: OpenCyc URIs like `http://sw.opencyc.org/concept/Mx4rvViAkpwpEbGdrcN5Y29ycA`
  - Use for: System identifiers, rarely displayed to end users

**Example Usage**:
```json
{
  "concept_id": "#V#member_country_of_the_european_union",
  "names": [
    "EU member state",
    "EU member country",
    {"name": "member state of the European Union", "language": "en", "name_type": "NL"},
    {"name": "État membre de l'Union européenne", "language": "fr", "name_type": "NL"},
    {"name": "Mitgliedstaat der Europäischen Union", "language": "de", "name_type": "NL"},
    {"name": "EU MS", "language": "en", "name_type": "ABBR"}
  ]
}
```

**Usage for AI:** When a task requires interacting with the Vontology (e.g., creating a new concept, searching for existing concepts), AI agents should **always prefer using these MCP endpoints** over writing custom scripts.

**Troubleshooting MCP Access:** If an AI agent is unable to access the MCP server or other tools, the user should be prompted to check if the agent is in the correct mode. For example, asking "Are you in agent mode?" can help resolve context issues that may be preventing tool access.

### 🔧 CRITICAL: MCP Manifest Updates

**WHENEVER you add, modify, or remove an MCP endpoint in `src/backend/mcp_server/mcp_server.py`, you MUST update the manifest file `src/backend/mcp_server/vontology_mcp.json` to match.**

The manifest serves as the API contract for MCP clients and must stay in sync with the implementation:

1. **Adding an endpoint:** Add a corresponding tool definition to the `"tools"` array in `vontology_mcp.json` with:
   - `name`: The endpoint route (without leading `/`)
   - `description`: Clear description of what it does and when to use it
   - `inputSchema`: JSON Schema defining all parameters (with types, descriptions, required fields, defaults)

2. **Modifying an endpoint:** Update the corresponding tool definition's description and/or inputSchema to match the new behaviour

3. **Removing an endpoint:** Remove the corresponding tool definition from the manifest

**Example Pattern:**
```python
# In mcp_server.py
@app.route('/add_name', methods=['POST'])
def mcp_add_name():
    # Implementation using text relations (hasName predicate)
    pass
```

Must have corresponding manifest entry:
```json
{
  "name": "add_name",
  "description": "Adds a name (alias) to an existing concept using text relations...",
  "inputSchema": {
    "type": "object",
    "properties": {
      "concept_id": {"type": "string", "description": "..."},
      "name": {"type": "string", "description": "..."}
    },
    "required": ["concept_id", "name"]
  }
}
```

**Why This Matters:** MCP clients discover capabilities through the manifest. An outdated manifest means tools won't be discoverable or will have incorrect parameter definitions, breaking agent workflows.

**Data Consistency Rule:** All Vontology data modifications (names, descriptions, relationships) MUST use text relations via `text_value_service.py` functions (`upsert_text_for_concept`, etc.) with the appropriate predicate (`hasName`, `hasDescription`, etc.). NEVER directly update concept document fields for data that belongs in text relations.

### 🔧 CRITICAL: MCP stdio Server Import Pattern

**The MCP stdio server (`src/backend/mcp_server/mcp_stdio_server.py`) is run directly as a script, NOT as a module, so it MUST use absolute imports, not relative imports.**

**WRONG (will cause ImportError):**
```python
from ..vontology.utils_vontology import create_vontology_concept
from ..services.text_value_service import upsert_text_for_concept
```

**CORRECT (required pattern):**
```python
from src.backend.vontology.utils_vontology import create_vontology_concept
from src.backend.services.text_value_service import upsert_text_for_concept
```

**Why:** Python relative imports only work when a file is imported as part of a package. When `mcp_stdio_server.py` is executed directly (as it is by the MCP client), there is no parent package context, causing `ImportError: attempted relative import with no known parent package`.

**When adding new imports to `mcp_stdio_server.py`:**
1. Always use absolute imports starting with `src.backend...`
2. The `sys.path` adjustment at the top of the file ensures `src` is importable
3. Update BOTH `mcp_stdio_server.py` AND the corresponding tool handlers in `list_tools()` and `call_tool()`
4. Keep imports synchronized with `mcp_server.py` (which can use relative imports since it's imported as a module by Flask)

### 🔧 CRITICAL: Three MCP Implementations Must Stay Synchronized

**The project has THREE separate MCP tool implementations that MUST be kept in sync:**

1. **External HTTP Server** (`src/backend/mcp_server/mcp_server.py`) - Flask routes for HTTP/REST access
2. **External stdio Server** (`src/backend/mcp_server/mcp_stdio_server.py`) - MCP protocol over stdio for IDE integrations
3. **Internal Gateway** (`src/backend/integrations/internal_mcp/catalogue.py`) - Used by chat orchestrator for tool calls

**CRITICAL SYNCHRONIZATION RULE:** When you modify a tool's behaviour, return structure, or schema in ANY of these implementations, you MUST update ALL THREE immediately. Failure to do so causes subtle runtime errors that are hard to diagnose.

**Example Failure Pattern (JVNAUTOSCI-303, October 2025):**
- Fixed `get_context` in external HTTP and stdio servers to return `llm_model` (string) instead of `model` (dict)
- Forgot to update internal MCP catalogue
- Chat tool calls failed with: `"Field 'model' expected type NoneType, str but received dict"`
- Root cause: Internal catalogue still returning dict, but schema expected string
- Required: Update handler function AND output schema in catalogue.py

**Checklist for MCP Tool Changes:**
1. ✅ Update HTTP server (`mcp_server.py`) - Flask route handler
2. ✅ Update stdio server (`mcp_stdio_server.py`) - `call_tool()` case handler
3. ✅ Update internal catalogue (`catalogue.py`) - Handler function + input/output schemas
4. ✅ Update manifest (`vontology_mcp.json`) - Tool definition (if external-facing)
5. ✅ Test ALL THREE paths - HTTP endpoint, stdio call, internal orchestrator

**Why This Matters:** The internal gateway is used by the chat interface, while external servers are used by IDE integrations and external agents. Users may test one path successfully while the other remains broken, leading to "it works for me" confusion.

**Prevention:** When implementing new MCP tools, create a shared function in a service module and call it from all three implementations. This reduces duplication and ensures consistency.

### 📋 CRITICAL: Using MCP Tools vs Custom Python Scripts

**This section operationalizes the decision tree for AI agents: When to use MCP tools vs writing custom Python.**

#### Quick Rule
> **Use MCP tools by default.** Only write Python scripts when you need bulk operations, complex conditional logic, or data validation across multiple resources. See [docs/engineering/mcp_tools_best_practices.md](../docs/engineering/mcp_tools_best_practices.md) for comprehensive guidance.

#### ✅ Always Use MCP Tools For:

**1. Adding Text Content** (prompts, descriptions, instructions, notes)
```json
{
  "tool": "upsert_text_relation",
  "arguments": {
    "concept_id": "#V#my_concept",
    "predicate": "hasContent",
    "text": "Your text here...",
    "language": "en-NZ"
  }
}
```

**2. Querying Text Relations**
```json
{
  "tool": "get_text_relations",
  "arguments": {
    "concept_id": "#V#my_concept",
    "predicate": "hasContent"
  }
}
```

**3. Creating/Updating Concepts**
```json
{
  "tool": "create_concepts",
  "arguments": {
    "parent_id": "#V#thing",
    "concepts": [
      {"name": "my_concept", "kind": "type", "description": "..."}
    ]
  }
}
```

**4. Managing Relationships**
```json
{
  "tool": "add_relationship",
  "arguments": {
    "source_id": "#V#concept1",
    "predicate": "instance_of",
    "target": "#V#concept2"
  }
}
```

#### ❌ Never Do This:

- ❌ Write throwaway Python scripts for single Vontology operations
- ❌ Use `upsert_text_for_concept()` directly (use `upsert_text_relation` tool instead)
- ❌ Query MongoDB directly (use MCP query tools)
- ❌ Import internal services just to interact with Vontology

#### ✅ OK to Write Python For:

- ✅ Bulk migrations (1000+ items with conditional logic)
- ✅ Complex data validation across multiple resources
- ✅ One-time backfill operations with rollback
- ✅ Extracting/analysing large datasets
- **But**: Call MCP tools from within the script instead of internal services

#### Why MCP is Better

| Aspect | Python Script | MCP Tool |
|--------|---------------|----------|
| Stability | Breaks on code changes | Stable API contract |
| Discoverability | Need to know implementation | Tool schemas self-document |
| Logging | Manual implementation | Automatic tracing |
| Reusability | Python-only | Works from CLI, IDE, chat |
| Error Handling | Manual implementation | Consistent responses |

#### Reference

See [docs/engineering/mcp_tools_best_practices.md](../docs/engineering/mcp_tools_best_practices.md) for:
- Complete decision tree
- All available MCP tools with examples
- Common patterns and troubleshooting
- When to write scripts (with examples)

## External MCP Servers

Von can integrate with external MCP servers to access external services and data sources beyond its internal Vontology operations. External MCP servers are configured in `.vscode/mcp.json` and run as separate processes, communicating via stdio protocol.

### Currently Integrated External MCP Servers

#### arXiv MCP Server

**Purpose**: Scholarly article search, metadata retrieval, and PDF download from arXiv.org

**Configuration** (`.vscode/mcp.json`):
```jsonc
{
  "servers": {
    "arxiv": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "tool",
        "run",
        "arxiv-mcp-server",
        "--storage-path",
        "${workspaceFolder}/data/arxiv_cache"
      ]
    }
  }
}
```

**Installation**:
```powershell
uv tool install arxiv-mcp-server
```

**Storage**:
- **Cache**: Local filesystem cache at `data/arxiv_cache/` (used by the external `arxiv-mcp-server`)
- **Durable**: Von blob store (a general artefact store capability) — default local `data/blob_store/`, optionally OpenStack Swift (JVNAUTOSCI-878)

**Available Tools**:
- `search_arxiv`: Query by keywords, authors, categories, date ranges
- `get_paper_metadata`: Retrieve title, authors, abstract, publication date, DOI
- `download_paper`: Fetch PDF and store it durably via the blob store (with a local cache)

**Usage Example** (IDE/Copilot level):
```
@workspace Search arXiv for papers by Michael Witbrock on causal reasoning
@workspace Get details for arXiv:2506.16596
@workspace Download that paper
```

**Integration Architecture**:
- **Current** (Phase 1-2): External/IDE-level only (Copilot/@workspace)
- **Planned** (Phase 4, JVNAUTOSCI-655): Internal MCP gateway proxy for Von chat assistant access

**Documentation**: See `docs/engineering/arxiv_mcp_integration.md` for detailed setup, usage patterns, and troubleshooting.

For Catalyst Cloud (NZ) Swift configuration, see `docs/engineering/catalyst_cloud_swift_setup.md`.

**Related Issues**:
- JVNAUTOSCI-654: Plan external MCP integrations (arXiv) - Complete
- JVNAUTOSCI-655: Implement internal MCP proxy for arXiv tools - Planned
- JVNAUTOSCI-656: Automated arXiv monitoring for known authors - Planned
- JVNAUTOSCI-657: Scholarly article concept auto-population - Planned
- JVNAUTOSCI-878: Migrate arXiv storage to OpenStack Swift object store - Planned

### Adding New External MCP Servers

**Process**:
1. **Research & Selection**: Identify MCP server (GitHub, npm, PyPI) or build custom
2. **Installation**: Use `uv tool install`, `npm install -g`, or clone repository
3. **Configuration**: Add server definition to `.vscode/mcp.json`
4. **Storage Setup**: Create data directories, configure paths, update `.gitignore`
5. **Testing**: Restart VS Code, validate tool availability via Copilot
6. **Documentation**: Create `docs/engineering/<service>_mcp_integration.md`
7. **Internal Integration** (optional): Implement proxy in `src/backend/integrations/internal_mcp/catalogue.py`

**Considerations**:
- **MCP 128-tool limit**: Strategic selection, lazy-load external servers
- **Credential management**: Environment variables, secure storage patterns
- **Rate limiting**: Respect external API guidelines, implement backoff
- **Subprocess lifecycle**: Health checks, auto-restart, graceful degradation (for internal proxies)

## MCP Services and Tool Discovery

### Atlassian MCP Integration

**JIRA Integration:** JIRA functionality is available through the **Atlassian MCP** service. This includes creating issues, updating issue details, adding comments, searching with JQL, transitioning issues, and managing issue relationships.

### MCP Service Strategy

When a user requests an action, AI agents should:

1. **Identify the Service:** Work out which MCP service should be able to handle the requested functionality (e.g., GitHub for repository operations, Atlassian for JIRA/Confluence, MongoDB for database operations)

2. **Try Available Tools:** Attempt to use the available tools from that service to complete the task

3. **Request Missing Tools:** If the required functionality isn't available but other tools from the same service are present, ask the user to enable the missing tool. This is important due to the 128 tool limit constraint - not all tools from a service may be enabled simultaneously.

**Example:** If asked to create a GitHub repository but only GitHub file operations are available, request that the user enable GitHub repository creation tools rather than suggesting alternative approaches.

## Atlassian Cloud ID Retrieval (Fast Path)

When JIRA operations require `ATLASSIAN_CLOUD_ID`, do NOT spend excessive cycles only retrying the `/accessible-resources` endpoint if it fails. Use a dual-path strategy that gives a human / browser-accessible fallback immediately.

You can get the cloud ID using:
```
powershell -NoLogo -NoProfile -Command "Write-Output $env:ATLASSIAN_CLOUD_ID"
```
If that does not work, try the decision tree below.

### Decision Tree (Apply Immediately)
1. Need cloud ID.
2. Attempt a single call to `/accessible-resources` (or any JIRA issue fetch if previous context suggests credentials are already valid).
3. If it fails (401/403/network) OR still uncertain after one retry:
    - Instruct user (or self-document) to open: `https://<site-domain>/_edge/tenant_info` while logged into the Atlassian site in a browser.
4. Copy the value of `"cloudId"` from the returned JSON.
5. Set env: PowerShell → ``$env:ATLASSIAN_CLOUD_ID = '<uuid>'`` (and persist in `.env` if appropriate).
6. Resume automated tool calls with the confirmed ID.

### Why This Matters
- Eliminates wasted time when auth errors stem from subtle credential or token scoping issues.
- Provides a deterministic authoritative value without requiring API token correctness first.
- Avoids misleading troubleshooting loops focused on variable naming collisions.

### Anti-Patterns (Avoid)
- Repeatedly retrying `/accessible-resources` >2 times before offering the fallback.
- Introducing speculative environment variable permutations (e.g. cycling through `ATLASSIAN_SITE`, `ATLASSIAN_SITE_BASE`, etc.) without first validating the cloud ID via browser.
- Delaying user visibility of the low-friction browser method.

### Minimal Snippet (Copy/Paste)
PowerShell:
```powershell
$env:ATLASSIAN_CLOUD_ID = 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
```
Browser (already logged in):
```
https://<your-site>.atlassian.net/_edge/tenant_info
```

### Incorporate into Workflow
- Document discovery of a missing cloud ID as a blocking prerequisite in JIRA-related TODO items.
- If cloud ID proves wrong later (404 on valid endpoints), re-fetch via the tenant info page before deeper diagnostics.

### Rationale for Inclusion
This fallback was previously applied late in one remediation flow; codifying it prevents recurrence and shortens future incident cycles (#ACTUALLY_TRY principle: parallel fast verification over sequential slow failure).
