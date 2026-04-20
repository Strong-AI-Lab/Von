# Multi-Agent Coordination Design

**Status**: Proposed design guidance
**Date**: 2026-04-04

## 1. Purpose

This document outlines the architectural requirements for extending the Von Workflow Language (VWL) and the Orchestrator to support mature multi-agent coordination. The goal is to move beyond monolithic agent execution and enable a robust ecosystem of specialised agents that can dynamically delegate tasks, negotiate capabilities, and share context.

## 2. Architectural Requirements

### 2.1 Agent-to-Agent Delegation

The Orchestrator must support explicit delegation from a supervisor agent to specialised sub-agents.

- **Explicit Handoffs**: Delegation must be a first-class operation in VWL, allowing an agent to yield execution to another agent with a specific sub-task and expected output schema.
- **Hierarchical Supervision**: Supervisor agents must be able to monitor the progress of delegated tasks and intervene if a sub-agent fails or hallucinates.
- **Turn Execution Records (TERs)**: Every delegated execution must produce a standard Turn Execution Record, ensuring that sub-agent actions are fully observable by the supervisor and the broader system.

### 2.2 Capability Negotiation

Agents must be able to discover and negotiate capabilities dynamically.

- **Capability Registries**: The system must maintain a registry of available agents and their documented capabilities, accessible via Vontology or a dedicated MCP tool.
- **Dynamic Routing**: The Orchestrator should support dynamic routing of tasks based on the required capabilities, cost constraints, and available agent profiles.
- **Fallback Mechanisms**: If a specialised agent is unavailable or lacks the required capability, the system must gracefully fall back to a more general agent or escalate to a human supervisor.

### 2.3 Shared Episodic Memory Contexts

To prevent context fragmentation, delegated agents must operate with a shared understanding of the episodic context.

- **Context Propagation**: When delegating a task, the supervisor must pass relevant slices of the shared turn context, episodic memory, and necessary Vontology pointers to the sub-agent.
- **Result Synthesis**: Upon completion of a delegated task, the sub-agent's output and relevant memory updates must be synthesised back into the supervisor's main episodic context.
- **Lineage Tracking**: The Orchestrator must track the lineage of context across agent boundaries, ensuring that memory updates can be traced back to the specific agent that generated them.

## 3. Extension of VWL and Orchestrator

### 3.1 VWL Extensions

- Introduce new VWL constructs for `Delegate`, `WaitFor`, and `Synthesise`.
- Define standard schemas for agent capability manifests.

### 3.2 Orchestrator Enhancements

- Extend the Orchestrator's event loop to handle asynchronous sub-agent executions.
- Implement robust telemetry for cross-agent communication to aid in debugging and policy learning.
- Ensure that the Orchestrator can enforce access controls and namespace isolation between different agents operating on behalf of the same user.

## 4. Conclusion

By maturing multi-agent coordination, Von will be able to tackle increasingly complex tasks through the collaborative effort of specialised, observable, and accountable agents, moving closer to its vision as a scalable neuro-symbolic assistant.
