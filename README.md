# Von

**A provenance-bearing research-team assistant and a platform for testing
agentic architectures.**

Von is an open-source project initiated by the Strong AI Lab at the University
of Auckland. Its near-term job is practical: to become a reliably useful
assistant for research teams, producing recurring scientific and administrative
work products, taking bounded authorised actions, preserving continuity, and
failing honestly when it cannot complete the job.

Von is also an experimental platform. We are testing whether explicit knowledge,
behavioural authority, workflows, memory, provenance, and evaluation make an
agent more reliable, adaptable, and inspectable than a fair simpler system using
the same models and tools. That is a hypothesis to measure, not an assumption
that justifies complexity.

The governing design rule is simple:

> Deliver the smallest dependable end-to-end capability that satisfies the
> user job. Add representation, workflow, memory, telemetry, evaluation, or
> formal machinery only when the capability or the evidence shows why it is
> needed.

## What Von is — and is not

Von is not merely an ontology browser, and it is not an LLM hidden behind an
ever-growing collection of Python rules. It combines several kinds of machinery,
choosing the smallest adequate path for each capability:

| Need | Usual surface in Von |
| --- | --- |
| Interpretation, synthesis, planning, and recovery under ambiguity | Model judgement |
| Exact algorithms, validation, execution, persistence, and integrations | Code and bounded tools |
| Durable concepts, relations, prompts, policies, and provenance-bearing knowledge | Vontology and related knowledge stores |
| Reusable, inspectable, recoverable multi-step behaviour | Von Workflow Language (VWL) |
| Evolving objectives, referents, assumptions, observations, and outcome criteria | The conversation's shared situation |
| Raw documents, blobs, traces, events, caches, and transactional data | Fit-for-purpose stores linked by provenance where needed |
| Claims about success | Proportionate tests, telemetry, effect receipts, and canonical read-back |

Vontology is therefore first-class, but not all-consuming. It gives represented
knowledge and behaviour stable identities, explicit relationships, provenance,
and revision paths. It does not make every task ontological, and it does not make
an assertion true merely because the assertion has been represented.

## The reliability claim

An ontology is not a truth machine. It can contain incomplete, stale,
context-dependent, or false claims. A language model can ignore or misuse good
evidence. A tool can report success before the intended state exists. A workflow
can faithfully execute the wrong plan.

Accordingly, Von makes no general claim that combining language models with an
ontology or another represented layer eliminates errors, or that all
Von output is verified or calibrated.

What this architecture can provide is a better basis for inspection and
evaluation: identifiable sources, explicit uncertainty where it is available,
typed knowledge, bounded authority, observable actions, durable receipts,
recovery paths, and read-back from the system that owns the final state.

A defensible reliability claim must name its scope. For a specified task set,
release, model and tool profile, we should compare Von with the best fair simpler
baseline and report the evidence that matters: useful task completion, grounded
provenance, final world state, unnecessary human intervention, recovery from
mistakes, latency, and cost. Until such a comparison supports a bounded claim,
the architectural advantage remains a research question.

## What is in the repository now

The current repository already contains much of the substrate for that
architecture:

- a browser application centred on persistent, multi-turn conversations, with
  inspectable shared situations, uploads, tasks, tool progress, and workflow
  state;
- model support for local Ollama, configured OpenAI, and actor-scoped, opt-in
  Gemini, Meta Muse, and OpenRouter providers;
- an internal Model Context Protocol (MCP) gateway and capability catalogue spanning represented
  knowledge, retrieval, scholarly sources, web search, tasks, workflows, and
  configured external services;
- Vontology concept, relation, text, search, provenance, import, and export
  surfaces;
- graph-native VWL definitions compiled into executable workflows, including
  durable instances, schedules, event bindings, checkpoints, and recovery;
- a Workflow Studio and feature-gated expert interfaces for Vontology,
  annotation, import/export, and diagnostics;
- MongoDB-backed persistence, document/RAG and concept indexing, conversation
  continuity, traces, telemetry, and replay/evaluation support; and
- actor- and organisation-scoped authority mechanisms for private data,
  provider eligibility, and bounded effects.

This list describes implemented substrate, not universal availability or a
certification claim. A connector may need credentials; a model may need explicit
actor-scoped eligibility; a workflow may need a published live Vontology
definition; and a feature may be disabled in a particular deployment. Presence
in the repository is not proof that every path is ready in every environment.

Von remains research software under active development. Do not treat it as a
hardened multi-tenant service for adversarial deployment. The current security
profiles and known limitations are documented in
[`docs/engineering/security_considerations.md`](docs/engineering/security_considerations.md).

## Repository map

| Path | Role |
| --- | --- |
| [`src/backend/server`](src/backend/server) | Flask application, routes, health, and runtime assembly |
| [`src/backend/services`](src/backend/services) | Domain services and reusable capability support |
| [`src/backend/integrations/internal_mcp`](src/backend/integrations/internal_mcp) | Internal tool gateway, catalogue, and integration adapters |
| [`src/backend/workflows`](src/backend/workflows) | VWL loading, execution, durability, telemetry, and authoring support |
| [`src/frontend/web/von_interface`](src/frontend/web/von_interface) | Browser interface |
| [`tests`](tests) | Backend, frontend, launcher, replay, and integration tests |
| [`docs`](docs) | Current guidance, dated evidence, designs, and operational notes |

For claims about present behaviour, live user-visible results and world-state
read-back outrank prose. Current code and targeted tests outrank dated status
documents. [`docs/design_index.md`](docs/design_index.md) explains how the
documentation is classified and which sources govern which questions.

## Quick start

### Requirements

- Git
- Python 3.11 or newer
- Node.js and npm; the current locked frontend dependencies require Node
  20.19+, 22.13+, or 24+
- MongoDB, either local or Atlas
- Bash on macOS/Linux, or PowerShell on Windows
- a usable language-model route, such as local Ollama or an enabled hosted
  provider
- Tesseract is optional for core Von, but required for image and scanned-PDF
  OCR. The setup scripts detect and validate it without installing system
  software unless you explicitly request that effect.

### macOS or Linux

```bash
git clone https://github.com/Strong-AI-Lab/Von.git
cd Von
cp .env.template .env
./setup_all.sh
```

If setup reports `OCR STATUS: UNAVAILABLE`, run exactly this command from the
repository as your ordinary user (do not prefix the whole command with
`sudo`):

```bash
./setup_all.sh --install-system-deps
```

On apt-based Linux, the installer uses passwordless `sudo` only for the
`apt-get` package operations when available; otherwise it installs the same
distro packages under the current user's local prefix. On macOS it uses
Homebrew. `--with-ocr` is also available when you want OCR to be mandatory but
do not authorise package installation; `--install-system-deps` implies that
strict check.

### Windows PowerShell

```powershell
git clone https://github.com/Strong-AI-Lab/Von.git
Set-Location Von
Copy-Item .env.template .env
.\setup_all.ps1
```

If setup reports `OCR STATUS: UNAVAILABLE`, run exactly:

```powershell
.\setup_all.ps1 -InstallSystemDeps
```

`-InstallSystemDeps` is the only setup option that authorises the Windows
installer to invoke `winget`; it also makes working OCR mandatory.

### Configure data and models

The copied `.env` starts with a local MongoDB configuration:

```dotenv
MONGO_URI=mongodb://localhost:27017/
VON_DB_NAME=von_db
```

Run MongoDB locally, or replace `MONGO_URI` with an Atlas connection string.
Remote database failures do not silently fall back to a possibly stale local
database unless that fallback is explicitly enabled.

For local models, install and run Ollama and configure `OLLAMA_HOSTS_LIST`. For
hosted providers, set the applicable credential, such as `OPENAI_API_KEY`,
`GEMINI_API_KEY`, or `META_API_KEY`, then select an eligible provider and model
in Von's settings. Meta Muse supports the fixed model `muse-spark-1.3`; managed
hosts may supply its key through `META_API_KEY_FILE`.
You may also configure `OPENAI_API_BACKUP_KEY` or `GEMINI_API_BACKUP_KEY` (or
their `*_FILE` forms). Von uses a backup once only after the primary credential
is rejected for authentication, quota, or rate limiting, and marks the model
footer with `backup key` when it succeeds. It does not switch provider or model.
Never commit `.env`.

### Run Von

Before starting Von, make sure the configured MongoDB service and the selected
local model service, if any, are running.

On macOS or Linux:

```bash
./run.sh start
./run.sh status
```

On Windows PowerShell:

```powershell
.\run.ps1 start
.\run.ps1 status
```

The launcher reports the effective browser address. Its default is
`http://localhost:5001` on macOS and `http://localhost:5000` elsewhere. Stop Von
with `./run.sh stop` or `.\run.ps1 stop`, as appropriate.

For the complete environment matrix, hosted deployment settings, feature flags,
and database recovery options, see
[`docs/engineering/environment_minimums.md`](docs/engineering/environment_minimums.md).

## Engineering and research documentation

- [`AGENTS.md`](AGENTS.md) contains the repository's governing product,
  architecture, authority, and evidence invariants.
- [`docs/design_index.md`](docs/design_index.md) routes current manuals, dated
  implementation records, proposals, and historical material.
- [The VWL manual](docs/engineering/von_workflow_language_manual.md) defines the
  represented workflow language and its runtime interfaces.
- [Agent evaluation and research uptake](docs/engineering/agent_evaluation_and_research_uptake.md)
  defines the evidence expected for behavioural and architectural claims.
- [The operational engineering guide](docs/engineering/operational_engineering_guide.md)
  covers day-to-day development and validation.

## Contributing

Contributions are welcome. Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and
the governing [`AGENTS.md`](AGENTS.md) before changing behaviour. In particular:

- begin with the user job and the simplest adequate path;
- preserve provenance and distinguish represented evidence from truth;
- do not hide durable task-specific semantic policy in incidental code;
- validate the affected real path at a level proportionate to the claim; and
- preserve unrelated work and never commit secrets or runtime data.

## Licence and acknowledgements

See [`LICENSE`](LICENSE) for the repository's licence terms.

Von was initiated by the Strong AI Lab at the University of Auckland and is
developed with contributions from researchers and engineers working on useful,
inspectable, and increasingly dependable AI assistance.
