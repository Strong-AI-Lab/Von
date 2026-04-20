# Automated Policy Learning Design

**Status**: Proposed design guidance
**Date**: 2026-04-04

## 1. Purpose

This document outlines the framework for transitioning Von from manual, hard-coded prompt tweaks to automated policy learning. By leveraging the rich telemetry provided by Turn Execution Records (TERs), Von will implement automated learning loops that induce guidelines, update retrieval strategies, and adjust routing, achieving true policy learning.

## 2. Core Concepts

### 2.1 Turn Execution Records (TERs) as the Learning Substrate

All automated learning loops will be grounded in TERs. A TER captures the complete context, action, and outcome of an agent's execution turn, including:
- The initial prompt and context.
- The tools invoked and their arguments.
- The intermediate reasoning steps (e.g., ephemeral theories).
- The final output and any associated reward or validation signals.

### 2.2 The Learning Loop Architecture

The automated policy learning framework consists of three primary phases:

1. **Collection & Telemetry**: Gathering high-fidelity TERs and explicit validation signals (user feedback, test results, execution success).
2. **Analysis & Induction**: Parsing TERs offline or asynchronously to identify patterns of failure, successful workarounds, and capability gaps.
3. **Policy Update**: Automatically generating and applying updates to authoritative policy surfaces (guidelines, prompts, routers).

## 3. Mechanisms for Policy Update

### 3.1 Guideline Induction

Instead of humans manually writing new rules in `AGENTS.md` for every edge case, the system will induce guidelines from experience.

- **Pattern Recognition**: An offline evaluator agent analyzes TERs to find recurring failure modes.
- **Guideline Proposal**: The evaluator proposes a new, concise guideline to prevent the failure.
- **Vontology Integration**: Approved guidelines are materialised into Vontology as contextual policy objects, retrieved dynamically when relevant context is encountered.

### 3.2 Retrieval Strategy Updates

Retrieval policies will be dynamically adjusted based on usage patterns.

- **Relevance Feedback**: Track which retrieved memory fragments were actually utilised in the agent's final reasoning.
- **Weight Adjustment**: Automatically down-weight or archive noise, and boost the retrieval scores of high-utility structures (e.g., specific graph traversals or Vontology predicates).

### 3.3 Dynamic Routing Adjustment

Model and agent routing should improve through empirical performance data.

- **Cost-Performance Tracking**: Continuously evaluate the cost, latency, and success rate of different models/agents for specific task categories.
- **Router Re-weighting**: Update routing metadata to favour models that demonstrate high reliability for a given task, reserving frontier models for novel or high-complexity tasks.

## 4. Safety and Governance

Automated policy learning requires strict governance to prevent catastrophic forgetting or policy degradation.

- **Evaluation Gates**: All induced guidelines and policy updates must pass a regression suite before being promoted to production.
- **Human-in-the-Loop Thresholds**: High-impact policy changes require explicit human review and approval.
- **Rollback Capabilities**: Every policy update must be versioned, allowing the system to instantly revert to a previous state if performance degrades.

## 5. Conclusion

Transitioning to automated policy learning ensures that Von's capabilities compound over time. By systematically learning from its own execution history, Von will evolve from a statically prompted system into a continuously improving, self-optimising neuro-symbolic assistant.
