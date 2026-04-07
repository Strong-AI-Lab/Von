Code Review: JVNAUTOSCI-1763 Branch vs Jira Requirements & No-Thin-Shim Rule
Critical Concerns (architectural/rule violations)
1. von_routes.py still owns the arXiv dispatch decision — violates 1766

The route handler at von_routes.py explicitly inspects workflow_discovery_result.get("selected_workflow_id") and branches into execute_conversation_turn_supervised() vs orchestrator.run(). This is the route-level bypass that 1766's "ADDITIONAL ANTI-SPECIAL-CASE CONSTRAINT" forbids:

"It must not introduce an arXiv-only reply-composition architecture, an arXiv-only control-plane contract, or a permanent route-level bypass around generic turn_execution.* surfaces."

The if is_arxiv_workflow: branch is an arXiv-specific control-plane fork at the route layer. The route should ideally call the Master Turn Workflow for all turns and let the workflow's turn_execution.route step handle the arXiv/non-arXiv dispatch internally.

2. Narration prompt is hardcoded in Python — violates rules 4/11 of AGENTS.md

In turn_execution_actions.py:115, _build_turn_execution_narrate_handler() contains a full system_prompt string:

AGENTS.md rule 11: "Do not place Vontology-governed prompt bodies in Python." JVNAUTOSCI-1766 explicitly says to use #V#prompt_turn_execution_compose_workflow_outcome_report. This prompt should live in a Vontology text relation, not inline in the handler.

3. Critic and completion gate are not implemented — 1767 claims Done

turn_execution_actions.py has no handlers registered for turn_execution.critic or turn_execution.completion_gate. The registration function's final comment says:

JVNAUTOSCI-1767 requires: "completion_gate.decision=completed is blocked on unresolved acquisition/materialisation verification." Without a completion gate handler, nothing prevents the workflow from calling completed regardless of verification status. The arxiv.build_completion_report step always returns status="success" even when verification_status="failed".

4. execute_conversation_turn_supervised() fallback narration is Python-owned

In orchestrator.py, lines ~20540-20545 contain a fallback response composition:

This is the orchestrator composing the user-facing reply from structured data — exactly the "summary-field scraping" pattern 1766 forbids. The reply should come from a workflow-owned narration step, not from the orchestrator's Python fallback.

Significant Concerns (completeness gaps)
5. Scholarly paper workflow decomposition is good but incomplete

The step graph decomposition in the seed bundle (normalise → ensure_paper_concept → link_file_copy → attach_metadata → resolve_authors → resolve_topics → assert_source_identity → verify) matches the 1765 spec closely. However, the old monolithic actions (scholarly_paper.materialise_from_file_copy, scholarly_paper.enrich_from_metadata) are still registered with a (monolithic) annotation but not removed. This creates two parallel paths through the same domain.

6. arxiv.inspect_existing_state searches by substring match

In _build_arxiv_inspect_existing_state_handler(), the search logic does if arxiv_id.lower() in cid.lower() against concept IDs. This is fragile — an arXiv ID like 2401.0001 would match 2401.00010 (a different paper). The handler should use exact matching or a canonical lookup predicate.

7. turn_execution_record_service.py schema extension lacks producers

The build_turn_execution_record() function now accepts selected_workflow_trace, critic_verdict, completion_gate_verdict, and arxiv_completion_report — but I don't see any caller in the diff that actually passes these new fields. The schema extension exists on paper but isn't wired.

Minor Concerns
8. No reacquire_partial_cache transition in the seed bundle

JVNAUTOSCI-1765 lists the acquisition mode graph as including reuse_existing_file_copy | recover_partial_cache_state | download_paper. The seed bundle's decide_acquisition_mode conditional transitions handle existing_file_copy and finalise_cached_pdf with a fallback always → download_paper, but there's no explicit reacquire_partial_cache transition — it falls through to download. This might be intentional but isn't obvious.

9. Step callbacks are fire-and-forget with swallowed exceptions

Both engine.py and durable_executor.py catch and log step_callback exceptions without surfacing them. If the progress tracker expects reliable delivery, silent failures could mask problems.

10. build_arxiv_ingestion_completion_report() has legacy/new field ambiguity

The function has both decisions and decisions_taken, done and created_ids with fallback logic (decisions or decisions_taken). The Jira spec says fields should be mandatory, not best-effort. The dual naming invites confusion about which field is canonical.

Summary Assessment
The workflow graph structure in the seed bundles is solid and matches the 1765 spec well. The decomposition from monolithic handlers into explicit reportable steps (ensure_paper_concept, link_file_copy, etc.) with writes_context_keys is a genuine step forward.

However, the implementation violates the "no thin shim" rule in three places: the route-level arXiv fork (concern 1), the Python-embedded narration prompt (concern 2), and the orchestrator fallback reply composition (concern 4). The critic/completion-gate enforcement required by 1767 is absent (concern 3). Together, these mean the turn lifecycle is still Python-owned for arXiv paths — the workflow provides the step execution but Python still controls dispatch, narration, and completion gating.

Claude Opus 4.6 • 3x

Findings

Critical: JVNAUTOSCI-1763 is not actually closed on the authoritative VWL/Vontology surface. The runtime registry is explicitly read-only and does not bootstrap workflow authority during registry construction (registry_factory.py (line 945), registry_factory.py (line 1077)). I checked the live registry, and it still exposes the old Vontology definitions: #V#conversation_turn_execution_workflow is still the old 3-state critic/gate flow, #V#arxiv_paper_representation_workflow is still the older 10-state wrapper without arxiv.build_completion_report, #V#scholarly_paper_representation_workflow is still the older 7-state monolith, and prompt_turn_execution_compose_workflow_outcome_report does not exist. So the branch mainly edits repo seed bundles and Python around unchanged authoritative workflows.

Critical: The new arXiv “supervised” path is currently broken. execute_conversation_turn_supervised() imports a non-existent module at orchestrator.py (line 20494), and the new turn-execution action module imports the same non-existent path at turn_execution_actions.py (line 74). I verified that src.backend.workflows.registry_factory raises ModuleNotFoundError. Even beyond that, the durable action registry currently does not register turn_execution.completion_gate, turn_execution.critic, or kb_mutation.critic, while the new conversation-turn seed workflow depends on them (canonical_workflow_publication_seed_bundle.json (line 1425)).

High: The control plane is still Python-owned rather than workflow-owned, which is the exact architectural smell 1766 was meant to remove. The route special-cases arXiv in Python (von_routes.py (line 8415)), the workflow routing action just trusts route-supplied discovery and leaves a TODO instead of owning selection (turn_execution_actions.py (line 43)), execution is explicitly synchronous “for now” rather than a durable handoff (orchestrator.py (line 20531)), and narration uses a Python-authored inline prompt plus direct llm_client.generate() (turn_execution_actions.py (line 114)). That is still Python pretending to be a hosted workflow.

High: The new conversation-turn graph does not match the Jira-required sequence and cannot truthfully gate the final answer. The seed bundle now says routing -> execution -> narration -> critic -> completion_gate -> completed (canonical_workflow_publication_seed_bundle.json (line 1383)), but JVNAUTOSCI-1766 required ... -> run_postcondition_critic -> apply_completion_gate -> compose_user_report -> completed|failed. It also unconditionally transitions from completion_gate to completed (canonical_workflow_publication_seed_bundle.json (line 1438)), so the workflow graph itself does not encode the 1767 rule that unresolved verification must block completion.

High: The structured arXiv reporting layer required by 1764/1765 is mostly declarative on paper, not real at runtime. The schema supports decisions, done, not_done, and reuse facts (execution_contracts.py (line 365)), but arxiv.build_completion_report() never populates those fields and instead emits a generic "Processed arXiv paper..." summary (paper_representation_workflow.py (line 1242)). The booleans that are supposed to distinguish fresh download vs reuse vs recovery are not produced anywhere else in the backend. Even one of the few new factual flags is wrong: paper_concept_created is computed after creation, so it will usually report false on the arXiv path (paper_representation_workflow.py (line 797)).

Medium: The turn-execution record extension is not actually wired through, and I don’t see the exact-path test coverage 1767 asked for. New fields were added to the record builder (turn_execution_record_service.py (line 4230), turn_execution_record_service.py (line 4472)), but the main call sites still invoke build_turn_execution_record() without passing them (orchestrator.py (line 6733), von_routes.py (line 4110)). I also don’t see any test file changes in this branch for the fresh-download / reuse / partial-recovery / failure scenarios named in JVNAUTOSCI-1767.

I would not consider JVNAUTOSCI-1763 or subtasks 1764-1767 genuinely done in their current state. The strongest concern is not just code quality; it’s that the branch still leaves the real authority in the wrong place, and the actual authoritative Vontology workflows have not been brought along.

I didn’t change code. I checked the working tree, the Jira task texts/comments, the authoritative workflow registry, and a couple of targeted runtime validations; I did not run a full /von/generate end-to-end because the import/registration failures and unchanged authoritative workflows already show the path is not in a closable state.



