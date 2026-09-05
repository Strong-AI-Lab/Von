import { ensureUniqueWindowSessionId, getWindowSessionId, WINDOW_SESSION_HEADER } from './apiService.js';
import {
  syncNamespaceFromLocalStorage,
  syncOrgContextFromLocalStorage
} from './utils/sessionScopedStorage.js';

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function uniqueStrings(values) {
  const seen = new Set();
  const output = [];
  asArray(values).forEach((value) => {
    const cleaned = cleanText(value);
    if (!cleaned || seen.has(cleaned)) return;
    seen.add(cleaned);
    output.push(cleaned);
  });
  return output;
}

function simplifyEdgeLabel(predicate) {
  const raw = cleanText(predicate);
  if (!raw) return 'next';
  const table = {
    nextStep: 'next',
    transition: 'transition',
    onTrueNextStep: 'true',
    onFalseNextStep: 'false',
    onFailureNextStep: 'failure',
    onUnknownNextStep: 'unknown',
    onApprovalRequiredNextStep: 'approval',
    onBreakNextStep: 'break',
    onContinueNextStep: 'continue',
    next_step: 'next',
    on_true: 'true',
    on_false: 'false',
    on_failure: 'failure',
    on_unknown: 'unknown'
  };
  return table[raw] || raw;
}

export function summarisePreviewDiff(diffSummary) {
  if (!diffSummary || typeof diffSummary !== 'object') {
    return 'No preview available.';
  }
  const parts = [];
  if (diffSummary.workflow_description_changed) parts.push('description');
  if (diffSummary.initial_state_changed) parts.push('initial state');
  const changedStateCount = Number(diffSummary.changed_state_count || 0);
  if (changedStateCount > 0) {
    parts.push(`${changedStateCount} step${changedStateCount === 1 ? '' : 's'}`);
  }
  if (!parts.length) return 'No structural changes detected.';
  return `Preview touches ${parts.join(', ')}.`;
}

export function normaliseAuthoringSpecForEditor(spec, workflowId) {
  const source = spec && typeof spec === 'object' ? spec : {};
  const steps = asArray(source.steps)
    .filter((step) => step && typeof step === 'object')
    .map((step, index) => {
      const stateId = cleanText(step.state_id || step.state_key) || `step_${index + 1}`;
      return {
        state_id: stateId,
        action_id: cleanText(step.action_id),
        subworkflow_id: cleanText(step.subworkflow_id),
        terminal: Boolean(step.terminal),
        next_state_key: cleanText(step.next_state_key),
        on_true_state_key: cleanText(step.on_true_state_key),
        on_false_state_key: cleanText(step.on_false_state_key),
        on_failure_state_key: cleanText(step.on_failure_state_key),
        reads_variables: uniqueStrings(step.reads_variables),
        writes_variables: uniqueStrings(step.writes_variables),
        writes_context_keys: uniqueStrings(step.writes_context_keys),
        tool_output_context_mappings: asArray(step.tool_output_context_mappings),
        context_input_mappings: asArray(step.context_input_mappings),
        metadata: step.metadata && typeof step.metadata === 'object' ? { ...step.metadata } : {}
      };
    });

  return {
    workflow_id: cleanText(source.workflow_id) || cleanText(workflowId),
    description: cleanText(source.description || source.workflow_description),
    initial_state_key: cleanText(source.initial_state_key) || (steps[0]?.state_id ?? ''),
    required_effects: uniqueStrings(source.required_effects),
    workflow_metadata: source.workflow_metadata && typeof source.workflow_metadata === 'object'
      ? { ...source.workflow_metadata }
      : {},
    postcondition_probe: source.postcondition_probe && typeof source.postcondition_probe === 'object'
      ? { ...source.postcondition_probe }
      : undefined,
    verification_inputs: source.verification_inputs && typeof source.verification_inputs === 'object'
      ? { ...source.verification_inputs }
      : undefined,
    steps
  };
}

function serialiseDraftSpec(spec) {
  return JSON.stringify(spec || {});
}

function prettyJson(value, fallback = '') {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  if (typeof value === 'string') {
    const cleaned = cleanText(value);
    if (!cleaned) return fallback;
    try {
      return JSON.stringify(JSON.parse(cleaned), null, 2);
    } catch (_error) {
      return cleaned;
    }
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value, null, 2);
    } catch (_error) {
      return fallback;
    }
  }
  return fallback;
}

function buildPolicyEditors(spec) {
  const metadata = spec?.workflow_metadata && typeof spec.workflow_metadata === 'object'
    ? spec.workflow_metadata
    : {};
  return {
    routing_profile: prettyJson(metadata.routing_profile, ''),
    discovery_exemplars: prettyJson(metadata.discovery_exemplars, ''),
    background_launch_policy: prettyJson(metadata.background_launch_policy, ''),
    launch_input_contract: prettyJson(metadata.launch_input_contract, ''),
    event_bindings: prettyJson(metadata.event_bindings, ''),
    schedule_specs: prettyJson(metadata.schedule_specs, '')
  };
}

function buildDraftSource(detail) {
  const proposal = detail?.proposal;
  if (proposal?.active && proposal?.authoring_spec) {
    return {
      spec: proposal.authoring_spec,
      label: 'pending proposal'
    };
  }
  return {
    spec: detail?.authoring?.current_spec,
    label: 'authoritative definition'
  };
}

function buildOutgoingCounts(definition) {
  const counts = new Map();
  asArray(definition?.edges).forEach((edge) => {
    const from = cleanText(edge?.from);
    const to = cleanText(edge?.to);
    if (!from || !to) return;
    counts.set(from, (counts.get(from) || 0) + 1);
  });
  return counts;
}

export function buildWorkflowLayout(definition) {
  const steps = asArray(definition?.steps).filter((step) => step && typeof step === 'object');
  const edges = asArray(definition?.edges).filter((edge) => edge && typeof edge === 'object');
  const stepIds = steps.map((step) => cleanText(step.step_id)).filter(Boolean);
  const initialStep = cleanText(definition?.initial_step) || stepIds[0] || '';
  const adjacency = new Map();
  stepIds.forEach((stepId) => adjacency.set(stepId, []));

  edges.forEach((edge) => {
    const from = cleanText(edge.from);
    const to = cleanText(edge.to);
    if (!from || !to || !adjacency.has(from)) return;
    adjacency.get(from).push(to);
  });

  const depths = new Map();
  const queue = [];
  if (initialStep && adjacency.has(initialStep)) {
    depths.set(initialStep, 0);
    queue.push(initialStep);
  }
  while (queue.length) {
    const stepId = queue.shift();
    const depth = depths.get(stepId) || 0;
    (adjacency.get(stepId) || []).forEach((target) => {
      if (!depths.has(target)) {
        depths.set(target, depth + 1);
        queue.push(target);
      }
    });
  }

  let fallbackDepth = Math.max(0, ...Array.from(depths.values()));
  steps.forEach((step) => {
    const stepId = cleanText(step.step_id);
    if (stepId && !depths.has(stepId)) {
      fallbackDepth += 1;
      depths.set(stepId, fallbackDepth);
    }
  });

  const columns = new Map();
  steps.forEach((step) => {
    const stepId = cleanText(step.step_id);
    const depth = depths.get(stepId) || 0;
    if (!columns.has(depth)) columns.set(depth, []);
    columns.get(depth).push(step);
  });

  const outgoingCounts = buildOutgoingCounts(definition);
  const nodes = [];
  const nodeMap = new Map();
  Array.from(columns.keys()).sort((a, b) => a - b).forEach((depth) => {
    const column = columns.get(depth) || [];
    column.forEach((step, index) => {
      const stepId = cleanText(step.step_id);
      const node = {
        stepId,
        name: cleanText(step.name) || stepId,
        x: 48 + (depth * 260),
        y: 56 + (index * 148),
        width: 208,
        height: 92,
        branch: (outgoingCounts.get(stepId) || 0) > 1 || asArray(step.control_flow?.conditions).length > 0,
        terminal: (outgoingCounts.get(stepId) || 0) === 0,
        actionId: cleanText(step.invokes_action_target || step.invokes_action),
        subworkflowId: cleanText(step.invokes_workflow)
      };
      nodes.push(node);
      nodeMap.set(stepId, node);
    });
  });

  const laidOutEdges = edges
    .map((edge, index) => {
      const from = nodeMap.get(cleanText(edge.from));
      const to = nodeMap.get(cleanText(edge.to));
      if (!from || !to) return null;
      const startX = from.x + from.width;
      const startY = from.y + (from.height / 2);
      const endX = to.x;
      const endY = to.y + (to.height / 2);
      const bend = Math.max(40, (endX - startX) / 2);
      return {
        edgeId: `edge_${index}`,
        label: simplifyEdgeLabel(edge.predicate),
        path: `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`,
        labelX: startX + ((endX - startX) / 2),
        labelY: startY + ((endY - startY) / 2) - 8
      };
    })
    .filter(Boolean);

  const width = Math.max(720, ...nodes.map((node) => node.x + node.width + 80));
  const height = Math.max(360, ...nodes.map((node) => node.y + node.height + 60));
  return { width, height, nodes, edges: laidOutEdges };
}

const state = {
  scheduleForm: null,
  scheduleReceipt: null,
  scheduleBusy: false,
  catalogue: [],
  filteredCatalogue: [],
  selectedWorkflowId: '',
  workflowDetail: null,
  activeView: 'topology',
  selectedStepId: '',
  draftSpec: null,
  preview: null,
  previewSignature: '',
  draftSourceLabel: '',
  policyEditors: buildPolicyEditors(null),
  proposalReviewReason: '',
  supersedeReplacementWorkflowId: '',
  search: '',
  showDesigns: true
};

const elements = {};

function cacheElements() {
  elements.catalogue = document.getElementById('workflowStudioCatalogue');
  elements.catalogueMeta = document.getElementById('workflowStudioCatalogueMeta');
  elements.searchInput = document.getElementById('workflowStudioSearchInput');
  elements.showDesignsToggle = document.getElementById('workflowStudioShowDesignsToggle');
  elements.refreshButton = document.getElementById('workflowStudioRefreshButton');
  elements.connectionBadge = document.getElementById('workflowStudioConnectionBadge');
  elements.title = document.getElementById('workflowStudioTitle');
  elements.summary = document.getElementById('workflowStudioSummary');
  elements.summaryChips = document.getElementById('workflowStudioSummaryChips');
  elements.canvas = document.getElementById('workflowStudioCanvas');
  elements.inspector = document.getElementById('workflowStudioInspector');
  elements.statusBanner = document.getElementById('workflowStudioStatusBanner');
  elements.tabs = Array.from(document.querySelectorAll('.workflow-studio-view-tab'));
}

function setConnectionBadge(text, tone = 'loading') {
  if (!elements.connectionBadge) return;
  elements.connectionBadge.textContent = text;
  elements.connectionBadge.dataset.tone = tone;
}

function setStatusBanner(message, tone = 'info') {
  if (!elements.statusBanner) return;
  const text = cleanText(message);
  if (!text) {
    elements.statusBanner.classList.add('hidden');
    elements.statusBanner.textContent = '';
    delete elements.statusBanner.dataset.tone;
    return;
  }
  elements.statusBanner.classList.remove('hidden');
  elements.statusBanner.dataset.tone = tone;
  elements.statusBanner.textContent = text;
}

function buildWorkflowStudioRequestHeaders(options = {}) {
  const customHeaders = options.headers && typeof options.headers === 'object'
    ? options.headers
    : {};
  return {
    Accept: 'application/json',
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    ...customHeaders,
    [WINDOW_SESSION_HEADER]: getWindowSessionId()
  };
}

async function fetchJson(url, options = {}) {
  await ensureUniqueWindowSessionId?.();
  const { headers: _ignoredHeaders, ...fetchOptions } = options || {};
  const response = await fetch(url, {
    ...fetchOptions,
    headers: buildWorkflowStudioRequestHeaders(options)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(cleanText(data.error) || `Request failed with ${response.status}`);
    error.payload = data;
    error.status = response.status;
    throw error;
  }
  return data;
}

function filterCatalogue() {
  const search = cleanText(state.search).toLowerCase();
  state.filteredCatalogue = state.catalogue.filter((item) => {
    if (!state.showDesigns && !item.is_executable) return false;
    if (!search) return true;
    const haystack = [
      item.workflow_id,
      item.description,
      item.source,
      item.executability_reason
    ].map((value) => cleanText(value).toLowerCase()).join(' ');
    return haystack.includes(search);
  });
}

function renderCatalogue() {
  if (!elements.catalogue) return;
  filterCatalogue();
  if (!state.filteredCatalogue.length) {
    elements.catalogue.innerHTML = `
      <div class="workflow-studio-empty compact">
        <h3>No workflows match</h3>
        <p>Adjust the search text or the design-artifact filter.</p>
      </div>
    `;
  } else {
    elements.catalogue.innerHTML = state.filteredCatalogue.map((item) => {
      const selected = item.workflow_id === state.selectedWorkflowId;
      const description = cleanText(item.description) || 'No workflow description yet.';
      const badges = [
        item.source ? `<span class="workflow-studio-pill">${escapeHtml(item.source)}</span>` : '',
        item.is_executable
          ? '<span class="workflow-studio-pill success">Executable</span>'
          : '<span class="workflow-studio-pill muted">Design</span>'
      ].join('');
      return `
        <button type="button" class="workflow-studio-catalogue-item${selected ? ' selected' : ''}"
          data-workflow-id="${escapeHtml(item.workflow_id)}">
          <div class="workflow-studio-catalogue-title">${escapeHtml(item.workflow_id)}</div>
          <div class="workflow-studio-catalogue-description">${escapeHtml(description)}</div>
          <div class="workflow-studio-catalogue-badges">${badges}</div>
        </button>
      `;
    }).join('');
  }

  if (elements.catalogueMeta) {
    elements.catalogueMeta.textContent = `${state.filteredCatalogue.length} of ${state.catalogue.length} workflows shown`;
  }
}

function getDefinition() {
  return state.workflowDetail?.views?.topology?.definition || null;
}

function getDefinitionSteps() {
  return asArray(getDefinition()?.steps).filter((step) => step && typeof step === 'object');
}

function getSelectedStepSummary() {
  const stepId = cleanText(state.selectedStepId);
  if (!stepId) return null;
  return getDefinitionSteps().find((step) => cleanText(step.step_id) === stepId) || null;
}

function ensureSelectedStep() {
  const stepIds = getDefinitionSteps().map((step) => cleanText(step.step_id)).filter(Boolean);
  if (!stepIds.length) {
    state.selectedStepId = '';
    return;
  }
  if (stepIds.includes(state.selectedStepId)) return;
  state.selectedStepId = cleanText(getDefinition()?.initial_step) || stepIds[0];
}

function renderSummaryHeader() {
  if (!elements.title || !elements.summary || !elements.summaryChips) return;
  if (!state.workflowDetail) {
    elements.title.textContent = 'Select a workflow';
    elements.summary.textContent = 'Choose a workflow from the catalogue to inspect topology, decision structure, dataflow, operations, and bounded authoring controls.';
    elements.summaryChips.innerHTML = '';
    return;
  }
  const summary = state.workflowDetail.summary || {};
  const lifecycle = summary.publication_lifecycle || {};
  const proposal = state.workflowDetail.proposal || {};
  elements.title.textContent = summary.workflow_id || state.selectedWorkflowId;
  elements.summary.textContent = cleanText(summary.description) || 'No workflow description yet. Use the editor to propose and publish one through the authoritative workflow path.';
  const chips = [];
  chips.push(`<span class="workflow-studio-hero-chip">${escapeHtml(cleanText(summary.source) || 'unknown')}</span>`);
  chips.push(`<span class="workflow-studio-hero-chip ${summary.is_executable ? 'success' : 'muted'}">${summary.is_executable ? 'Executable' : 'Design artefact'}</span>`);
  const stepCount = Number(state.workflowDetail?.views?.topology?.summary?.step_count || 0);
  chips.push(`<span class="workflow-studio-hero-chip">${stepCount} step${stepCount === 1 ? '' : 's'}</span>`);
  const activeCount = Number(state.workflowDetail?.operations?.instances?.active_count || 0);
  chips.push(`<span class="workflow-studio-hero-chip">${activeCount} active instance${activeCount === 1 ? '' : 's'}</span>`);
  const improvementCount = Number(state.workflowDetail?.improvement_guidance?.count || 0);
  if (improvementCount > 0) {
    chips.push(`<span class="workflow-studio-hero-chip warning">${improvementCount} improvement suggestion${improvementCount === 1 ? '' : 's'}</span>`);
  }
  chips.push(`<span class="workflow-studio-hero-chip">${escapeHtml(cleanText(lifecycle.phase) || 'phase unknown')}</span>`);
  if (cleanText(proposal.status || lifecycle.review_state)) {
    chips.push(`<span class="workflow-studio-hero-chip ${proposal?.active ? 'muted' : 'success'}">${escapeHtml(cleanText(proposal.status || lifecycle.review_state))}</span>`);
  }
  elements.summaryChips.innerHTML = chips.join('');
}

function renderImprovementGuidanceSection() {
  const guidance = state.workflowDetail?.improvement_guidance || {};
  const items = asArray(guidance.items);
  if (!items.length) {
    return `
      <div class="workflow-studio-section">
        <h3>Improvement guidance</h3>
        <div class="workflow-studio-muted">No recent episode-critique suggestions are linked to this workflow.</div>
      </div>
    `;
  }

  const cards = items.map((item) => {
    const target = cleanText(item.target_workflow_id) || cleanText(item.target_tool_name) || cleanText(item.target_prompt_concept_id) || cleanText(item.target_surface) || 'unspecified target';
    const evidenceRefs = asArray(item.evidence_refs)
      .map((ref) => `<span class="workflow-studio-hero-chip muted">${escapeHtml(cleanText(ref) || '')}</span>`)
      .join('');
    return `
      <div class="workflow-studio-callout ${cleanText(item.priority) === 'high' ? 'warning' : 'info'}">
        <strong>${escapeHtml(cleanText(item.title) || 'Untitled suggestion')}</strong>
        <span>${escapeHtml(cleanText(item.category) || 'workflow_change')} on ${escapeHtml(target)}</span>
      </div>
      <div class="workflow-studio-muted">${escapeHtml(cleanText(item.suggested_change) || '')}</div>
      <div class="workflow-studio-muted">${escapeHtml(cleanText(item.rationale) || '')}</div>
      <div class="workflow-studio-key-value">
        <span>Episode evidence</span>
        <strong>${escapeHtml(cleanText(item.request_id) || cleanText(item.memory_id) || 'unavailable')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Verdict</span>
        <strong>${escapeHtml(cleanText(item.verdict) || 'unknown')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Recursion level</span>
        <strong>${escapeHtml(String(item.recursion_level ?? 0))}</strong>
      </div>
      ${evidenceRefs ? `<div class="workflow-studio-chip-row">${evidenceRefs}</div>` : ''}
    `;
  }).join('<hr class="workflow-studio-divider" />');

  return `
    <div class="workflow-studio-section">
      <h3>Improvement guidance</h3>
      <div class="workflow-studio-muted">Recent episode-critique suggestions linked to this workflow. These are guidance surfaces only and are not auto-applied.</div>
      <div class="workflow-studio-key-value">
        <span>Suggestions</span>
        <strong>${escapeHtml(String(guidance.count || items.length))}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>High priority</span>
        <strong>${escapeHtml(String(guidance.high_priority_count || 0))}</strong>
      </div>
      ${cards}
    </div>
  `;
}

function renderTopologyView() {
  const definition = getDefinition();
  if (!definition) {
    return `
      <div class="workflow-studio-empty">
        <h3>No process graph</h3>
        <p>This workflow currently exposes narrative text only.</p>
      </div>
    `;
  }
  const layout = buildWorkflowLayout(definition);
  const svg = `
    <svg class="workflow-studio-graph" viewBox="0 0 ${layout.width} ${layout.height}" role="img" aria-label="Workflow topology">
      <defs>
        <marker id="workflowStudioArrow" markerWidth="10" markerHeight="10" refX="7" refY="3" orient="auto">
          <path d="M0,0 L0,6 L8,3 z" class="workflow-studio-graph-arrow"></path>
        </marker>
      </defs>
      ${layout.edges.map((edge) => `
        <g class="workflow-studio-edge">
          <path d="${edge.path}" class="workflow-studio-edge-path"></path>
          <text x="${edge.labelX}" y="${edge.labelY}" class="workflow-studio-edge-label">${escapeHtml(edge.label)}</text>
        </g>
      `).join('')}
      ${layout.nodes.map((node) => `
        <g class="workflow-studio-node ${node.branch ? 'branch' : ''} ${node.terminal ? 'terminal' : ''} ${node.stepId === state.selectedStepId ? 'selected' : ''}"
          data-step-id="${escapeHtml(node.stepId)}" tabindex="0" role="button" aria-label="Select step ${escapeHtml(node.name)}">
          <rect x="${node.x}" y="${node.y}" rx="18" ry="18" width="${node.width}" height="${node.height}"></rect>
          <text x="${node.x + 18}" y="${node.y + 28}" class="workflow-studio-node-title">${escapeHtml(node.name)}</text>
          <text x="${node.x + 18}" y="${node.y + 48}" class="workflow-studio-node-subtitle">${escapeHtml(node.actionId || node.subworkflowId || 'No bound action')}</text>
          <text x="${node.x + 18}" y="${node.y + 68}" class="workflow-studio-node-meta">${node.branch ? 'Branching' : node.terminal ? 'Terminal' : 'Linear step'}</text>
        </g>
      `).join('')}
    </svg>
  `;
  return `
    <div class="workflow-studio-view-intro">
      <h3>Topology</h3>
      <p>Derived process graph from authoritative workflow relationships and runtime metadata.</p>
    </div>
    <div class="workflow-studio-graph-shell">${svg}</div>
  `;
}

function renderDecisionView() {
  const decision = state.workflowDetail?.views?.decision || {};
  const decisionPoints = asArray(decision.decision_points);
  const terminalSteps = asArray(decision.terminal_steps);
  return `
    <div class="workflow-studio-view-intro">
      <h3>Decision structure</h3>
      <p>Conditional transitions, explicit preconditions, and terminal sinks.</p>
    </div>
    <div class="workflow-studio-card-grid">
      ${decisionPoints.length ? decisionPoints.map((point) => `
        <button type="button" class="workflow-studio-card workflow-studio-step-card" data-step-id="${escapeHtml(point.step_id)}">
          <div class="workflow-studio-card-title">${escapeHtml(point.name || point.step_id)}</div>
          <div class="workflow-studio-card-meta">${point.branch_targets.length} branch target${point.branch_targets.length === 1 ? '' : 's'}</div>
          <div class="workflow-studio-card-copy">
            ${point.preconditions?.length ? `Preconditions: ${escapeHtml(point.preconditions.join(', '))}` : 'No explicit preconditions'}
          </div>
        </button>
      `).join('') : `
        <div class="workflow-studio-empty compact">
          <h3>No branching points</h3>
          <p>This workflow is currently linear in its derived process graph.</p>
        </div>
      `}
    </div>
    <div class="workflow-studio-section">
      <h4>Terminal steps</h4>
      <div class="workflow-studio-chip-row">
        ${terminalSteps.map((step) => `
          <button type="button" class="workflow-studio-chip-button" data-step-id="${escapeHtml(step.step_id)}">${escapeHtml(step.name || step.step_id)}</button>
        `).join('') || '<span class="workflow-studio-muted">No terminal steps derived.</span>'}
      </div>
    </div>
  `;
}

function renderDataflowView() {
  const dataflow = state.workflowDetail?.views?.dataflow || {};
  const stepFlows = asArray(dataflow.step_dataflows);
  return `
    <div class="workflow-studio-view-intro">
      <h3>Dataflow</h3>
      <p>Context mappings, variable usage, and output propagation exposed by the workflow graph.</p>
    </div>
    <div class="workflow-studio-section">
      <h4>Produced context keys</h4>
      <div class="workflow-studio-chip-row">
        ${uniqueStrings(dataflow.produced_context_keys).map((item) => `<span class="workflow-studio-pill">${escapeHtml(item)}</span>`).join('') || '<span class="workflow-studio-muted">No explicit context keys.</span>'}
      </div>
    </div>
    <div class="workflow-studio-card-grid">
      ${stepFlows.map((step) => `
        <button type="button" class="workflow-studio-card workflow-studio-step-card" data-step-id="${escapeHtml(step.step_id)}">
          <div class="workflow-studio-card-title">${escapeHtml(step.name || step.step_id)}</div>
          <div class="workflow-studio-card-copy">
            Reads: ${escapeHtml((step.reads_variables || []).join(', ') || 'none')}<br>
            Writes: ${escapeHtml((step.writes_variables || []).join(', ') || 'none')}<br>
            Context: ${escapeHtml((step.writes_context_keys || []).join(', ') || 'none')}
          </div>
        </button>
      `).join('') || `
        <div class="workflow-studio-empty compact">
          <h3>No step dataflow metadata</h3>
          <p>This workflow does not currently declare variable or context mappings.</p>
        </div>
      `}
    </div>
  `;
}

function renderOperationsList(items, renderer) {
  if (!items.length) {
    return '<div class="workflow-studio-muted">None</div>';
  }
  return `<div class="workflow-studio-list">${items.map(renderer).join('')}</div>`;
}

function renderOperationsView() {
  const operations = state.workflowDetail?.operations || {};
  const instances = operations.instances || {};
  const bindings = operations.bindings || {};
  const schedules = operations.schedules || {};
  const executions = operations.executions || {};
  return `
    <div class="workflow-studio-view-intro">
      <h3>Operations</h3>
      <p>Live-ish operational overlays for schedules, event bindings, executions, and durable instances.</p>
    </div>
    ${instances.degraded ? `
      <div class="workflow-studio-callout warning">
        <strong>Instance view degraded.</strong>
        <span>${escapeHtml(cleanText(instances.detail) || 'Workflow instances are temporarily unavailable.')}</span>
      </div>
    ` : ''}
    <div class="workflow-studio-section">
      <h4>Event bindings</h4>
      ${renderOperationsList(asArray(bindings.items), (item) => `
        <div class="workflow-studio-list-row">
          <div><strong>${escapeHtml(item.event_type)}</strong></div>
          <div>${escapeHtml(item.enabled ? 'Enabled' : 'Disabled')}</div>
        </div>
      `)}
      ${asArray(bindings.diagnostics).length ? `
        <div class="workflow-studio-diagnostics">
          ${bindings.diagnostics.map((row) => `
            <div class="workflow-studio-diagnostic-row">${escapeHtml(row.event_type)}: ${escapeHtml((row.reason_codes || []).join(', ') || 'healthy')}</div>
          `).join('')}
        </div>
      ` : ''}
    </div>
    <div class="workflow-studio-section">
      <h4>Schedules</h4>
      ${state.workflowDetail?.summary?.is_executable ? `
        <div class="workflow-studio-section" aria-label="Create an actor-owned schedule">
          <label>Cadence <select data-schedule-field="schedule_type" aria-label="Schedule cadence">
            ${['once', 'interval', 'cron'].map(value => `<option value="${value}" ${state.scheduleForm?.schedule_type === value ? 'selected' : ''}>${value}</option>`).join('')}
          </select></label>
          ${state.scheduleForm?.schedule_type === 'once' ? `<label>Run at (local time) <input type="datetime-local" aria-label="Run at local time" data-schedule-field="run_at" value="${escapeHtml(state.scheduleForm?.run_at || '')}"></label>` : ''}
          ${state.scheduleForm?.schedule_type === 'interval' ? `<label>Interval in seconds <input type="number" min="1" step="1" aria-label="Interval in seconds" data-schedule-field="interval_seconds" value="${escapeHtml(state.scheduleForm?.interval_seconds || 3600)}"></label>` : ''}
          ${state.scheduleForm?.schedule_type === 'cron' ? `<label>Cron (UTC; weekday 0=Monday) <input aria-label="Cron expression" data-schedule-field="cron_expression" value="${escapeHtml(state.scheduleForm?.cron_expression || '')}" placeholder="0 9 * * 0-4"></label>` : ''}
          <label>Workflow inputs (JSON object)<textarea aria-label="Schedule workflow inputs" data-schedule-field="inputs" rows="5">${escapeHtml(state.scheduleForm?.inputs || '{}')}</textarea></label>
          <button type="button" class="btn-mini" data-action="preview-schedule" ${state.scheduleBusy ? 'disabled' : ''}>Check inputs and preview</button>
          <button type="button" class="btn-mini" data-action="create-schedule" ${state.scheduleBusy ? 'disabled' : ''}>Create schedule</button>
          <button type="button" class="btn-mini" data-action="reload-schedules">Reload schedules</button>
          ${state.scheduleReceipt ? `<div role="status">${escapeHtml(state.scheduleReceipt.preview ? 'Preview' : `Schedule ${state.scheduleReceipt.schedule_id}`)} · ${escapeHtml(state.scheduleReceipt.enabled ? 'Enabled' : 'Paused')} · Next: ${escapeHtml(state.scheduleReceipt.next_run_at ? `${new Date(state.scheduleReceipt.next_run_at).toLocaleString()} (local) / ${state.scheduleReceipt.next_run_at} (UTC)` : 'No further occurrence')}${state.scheduleReceipt.idempotent_replay ? ' · Existing schedule reused' : ''}</div>` : ''}
        </div>` : ''}
      ${renderOperationsList(asArray(schedules.items), (item) => `
        <div class="workflow-studio-list-row">
          <div><strong>${escapeHtml(item.schedule_type)}</strong> · ${escapeHtml(item.origin || 'legacy_unmanaged')} · ${escapeHtml(item.enabled ? 'Enabled' : 'Paused')}</div>
          <div>${escapeHtml(item.next_run_at || 'unscheduled')}</div>
          <div>${escapeHtml(item.schedule_id)}</div>
          ${item.origin === 'actor_owned' ? `<button type="button" class="btn-mini" data-action="toggle-schedule" data-schedule-id="${escapeHtml(item.schedule_id)}" data-enabled="${!item.enabled}" ${state.scheduleBusy ? 'disabled' : ''}>${item.enabled ? 'Pause' : 'Resume'}</button>` : ''}
        </div>
      `)}
    </div>
    <div class="workflow-studio-section">
      <h4>Durable instances</h4>
      ${renderOperationsList(asArray(instances.items), (item) => `
        <div class="workflow-studio-list-row">
          <div><strong>${escapeHtml(item.status || 'unknown')}</strong> ${escapeHtml(item.current_state || '')}</div>
          <div>${escapeHtml(item.created_at || '')}</div>
        </div>
      `)}
    </div>
    <div class="workflow-studio-section">
      <h4>Execution traces</h4>
      ${renderOperationsList(asArray(executions.items), (item) => `
        <div class="workflow-studio-list-row">
          <div><strong>${escapeHtml(item.execution_id || item.trace_id || 'trace')}</strong></div>
          <div>${escapeHtml(item.started_at || item.created_at || '')}</div>
        </div>
      `)}
    </div>
  `;
}

function getDraftStep(stepId) {
  return state.draftSpec?.steps?.find((step) => cleanText(step.state_id) === cleanText(stepId)) || null;
}

function syncPolicyEditorsFromDraft() {
  state.policyEditors = buildPolicyEditors(state.draftSpec);
}

function proposalTone(status) {
  const cleaned = cleanText(status).toLowerCase();
  if (cleaned === 'pending_review') return 'warning';
  if (cleaned === 'approved') return 'success';
  if (cleaned === 'rejected' || cleaned === 'rolled_back') return 'info';
  return 'info';
}

function buildStepOptions(selectedValue = '') {
  const selected = cleanText(selectedValue);
  const options = ['<option value="">None</option>'];
  asArray(state.draftSpec?.steps).forEach((step) => {
    const stateId = cleanText(step.state_id);
    options.push(`<option value="${escapeHtml(stateId)}"${stateId === selected ? ' selected' : ''}>${escapeHtml(stateId)}</option>`);
  });
  return options.join('');
}

function renderEditView() {
  if (!state.draftSpec) {
    return `
      <div class="workflow-studio-empty">
        <h3>Authoring unavailable</h3>
        <p>This workflow does not currently expose a runtime definition for bounded editing.</p>
      </div>
    `;
  }
  const draft = state.draftSpec;
  const currentStep = getDraftStep(state.selectedStepId) || draft.steps[0] || null;
  const proposal = state.workflowDetail?.proposal || {};
  const lifecycle = state.workflowDetail?.summary?.publication_lifecycle || {};
  const preview = state.preview?.preview || null;
  const previewValidation = preview?.contract_validation || null;
  const previewDiff = preview?.diff_summary || null;
  const pendingPreview = proposal?.preview_summary || null;
  const pendingCandidateValidation = proposal?.candidate_validation || null;
  const reviewReason = state.proposalReviewReason || '';
  const replacementWorkflowId = state.supersedeReplacementWorkflowId || '';
  const submitButtonLabel = proposal?.active ? 'Update proposal' : 'Submit for review';
  return `
    <div class="workflow-studio-view-intro">
      <h3>Bounded authoring</h3>
      <p>Edit workflow structure and policy metadata, preview the exact candidate, then submit it for review. Approval, rollback, demotion, and supersession are explicit lifecycle operations.</p>
    </div>
    <div class="workflow-studio-callout ${proposalTone(proposal?.status || lifecycle?.review_state)}">
      <strong>Lifecycle</strong>
      <span>Phase: ${escapeHtml(cleanText(lifecycle?.phase) || 'unknown')}</span>
      <span>Review: ${escapeHtml(cleanText(proposal?.status || lifecycle?.review_state) || 'none')}</span>
      <span>Routing eligible: ${escapeHtml(String(lifecycle?.routing_eligible ?? 'unspecified'))}</span>
      <span>Draft source: ${escapeHtml(state.draftSourceLabel || 'authoritative definition')}</span>
    </div>
    <div class="workflow-studio-editor-grid">
      <section class="workflow-studio-card">
        <div class="workflow-studio-card-title">Workflow</div>
        <label class="workflow-studio-field">
          <span>Description</span>
          <textarea data-edit-field="description" rows="5">${escapeHtml(draft.description || '')}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Initial state</span>
          <select data-edit-field="initial_state_key">${buildStepOptions(draft.initial_state_key)}</select>
        </label>
        <div class="workflow-studio-button-row">
          <button type="button" class="btn-mini" data-action="suggest-description">Suggest description</button>
          <button type="button" class="btn-mini" data-action="reset-draft">Reset draft</button>
        </div>
      </section>
      <section class="workflow-studio-card">
        <div class="workflow-studio-card-title">Governance</div>
        <div class="workflow-studio-key-value">
          <span>Stored proposal</span>
          <strong>${escapeHtml(cleanText(proposal?.status) || 'none')}</strong>
        </div>
        <div class="workflow-studio-key-value">
          <span>Proposal ID</span>
          <strong>${escapeHtml(cleanText(proposal?.proposal_id) || 'unavailable')}</strong>
        </div>
        <label class="workflow-studio-field">
          <span>Review reason / note</span>
          <textarea data-lifecycle-field="proposalReviewReason" rows="4">${escapeHtml(reviewReason)}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Replacement workflow ID for supersession</span>
          <input type="text" data-lifecycle-field="supersedeReplacementWorkflowId" value="${escapeHtml(replacementWorkflowId)}" placeholder="#V#replacement_workflow" />
        </label>
      </section>
      <section class="workflow-studio-card">
        <div class="workflow-studio-card-title">Workflow policy metadata</div>
        <label class="workflow-studio-field">
          <span>Routing profile JSON</span>
          <textarea data-policy-field="routing_profile" rows="6" placeholder='{"role":"authoring"}'>${escapeHtml(state.policyEditors.routing_profile || '')}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Discovery exemplars JSON</span>
          <textarea data-policy-field="discovery_exemplars" rows="6" placeholder='{"keywords":["..."],"examples":["..."]}'>${escapeHtml(state.policyEditors.discovery_exemplars || '')}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Background launch policy JSON</span>
          <textarea data-policy-field="background_launch_policy" rows="6" placeholder='{"enabled":true,"min_interval_seconds":300}'>${escapeHtml(state.policyEditors.background_launch_policy || '')}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Launch input contract JSON</span>
          <textarea data-policy-field="launch_input_contract" rows="6" placeholder='{"required":["prompt"]}'>${escapeHtml(state.policyEditors.launch_input_contract || '')}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Event bindings JSON array</span>
          <textarea data-policy-field="event_bindings" rows="6" placeholder='[{"event_type":"concept.created","input_mapping":{"concept_id":"event.concept_id"}}]'>${escapeHtml(state.policyEditors.event_bindings || '')}</textarea>
        </label>
        <label class="workflow-studio-field">
          <span>Schedule specs JSON array</span>
          <textarea data-policy-field="schedule_specs" rows="6" placeholder='[{"schedule_type":"interval","interval_seconds":300}]'>${escapeHtml(state.policyEditors.schedule_specs || '')}</textarea>
        </label>
      </section>
      <section class="workflow-studio-card">
        <div class="workflow-studio-card-title">Steps</div>
        <div class="workflow-studio-chip-row">
          ${draft.steps.map((step) => `
            <button type="button" class="workflow-studio-chip-button ${cleanText(step.state_id) === cleanText(currentStep?.state_id) ? 'selected' : ''}"
              data-step-select="${escapeHtml(step.state_id)}">${escapeHtml(step.state_id)}</button>
          `).join('')}
        </div>
        <div class="workflow-studio-button-row">
          <button type="button" class="btn-mini" data-action="add-step">Add step</button>
          <button type="button" class="btn-mini danger" data-action="remove-step"${currentStep ? '' : ' disabled'}>Remove selected step</button>
        </div>
      </section>
      ${currentStep ? `
        <section class="workflow-studio-card">
          <div class="workflow-studio-card-title">Selected step</div>
          <label class="workflow-studio-field">
            <span>State ID</span>
            <input type="text" value="${escapeHtml(currentStep.state_id)}" disabled />
          </label>
          <label class="workflow-studio-field">
            <span>Action ID</span>
            <input type="text" data-step-field="action_id" data-step-id="${escapeHtml(currentStep.state_id)}" value="${escapeHtml(currentStep.action_id || '')}" />
          </label>
          <label class="workflow-studio-field">
            <span>Subworkflow ID</span>
            <input type="text" data-step-field="subworkflow_id" data-step-id="${escapeHtml(currentStep.state_id)}" value="${escapeHtml(currentStep.subworkflow_id || '')}" />
          </label>
          <label class="workflow-studio-field checkbox">
            <input type="checkbox" data-step-field="terminal" data-step-id="${escapeHtml(currentStep.state_id)}"${currentStep.terminal ? ' checked' : ''} />
            <span>Terminal step</span>
          </label>
          <div class="workflow-studio-two-column-fields">
            <label class="workflow-studio-field">
              <span>Next</span>
              <select data-step-field="next_state_key" data-step-id="${escapeHtml(currentStep.state_id)}">${buildStepOptions(currentStep.next_state_key)}</select>
            </label>
            <label class="workflow-studio-field">
              <span>On true</span>
              <select data-step-field="on_true_state_key" data-step-id="${escapeHtml(currentStep.state_id)}">${buildStepOptions(currentStep.on_true_state_key)}</select>
            </label>
            <label class="workflow-studio-field">
              <span>On false</span>
              <select data-step-field="on_false_state_key" data-step-id="${escapeHtml(currentStep.state_id)}">${buildStepOptions(currentStep.on_false_state_key)}</select>
            </label>
            <label class="workflow-studio-field">
              <span>On failure</span>
              <select data-step-field="on_failure_state_key" data-step-id="${escapeHtml(currentStep.state_id)}">${buildStepOptions(currentStep.on_failure_state_key)}</select>
            </label>
          </div>
          <label class="workflow-studio-field">
            <span>Reads variables</span>
            <input type="text" data-step-field="reads_variables" data-step-id="${escapeHtml(currentStep.state_id)}" value="${escapeHtml((currentStep.reads_variables || []).join(', '))}" />
          </label>
          <label class="workflow-studio-field">
            <span>Writes variables</span>
            <input type="text" data-step-field="writes_variables" data-step-id="${escapeHtml(currentStep.state_id)}" value="${escapeHtml((currentStep.writes_variables || []).join(', '))}" />
          </label>
          <label class="workflow-studio-field">
            <span>Writes context keys</span>
            <input type="text" data-step-field="writes_context_keys" data-step-id="${escapeHtml(currentStep.state_id)}" value="${escapeHtml((currentStep.writes_context_keys || []).join(', '))}" />
          </label>
        </section>
      ` : ''}
      <section class="workflow-studio-card">
        <div class="workflow-studio-card-title">Evidence</div>
        ${preview ? `
          <div class="workflow-studio-callout info">
            <strong>Local preview ready</strong>
            <span>${escapeHtml(summarisePreviewDiff(previewDiff))}</span>
          </div>
          <div class="workflow-studio-key-value">
            <span>Contract validation</span>
            <strong>${previewValidation?.valid ? 'Valid' : 'Issues present'}</strong>
          </div>
          <div class="workflow-studio-muted">${escapeHtml((previewValidation?.errors || []).join(', ') || 'No contract errors recorded.')}</div>
        ` : `
          <div class="workflow-studio-muted">Preview the current draft to inspect its exact diff and contract state before submitting it.</div>
        `}
        ${proposal?.available ? `
          <div class="workflow-studio-section">
            <h4>Stored proposal</h4>
            <div class="workflow-studio-muted">Status: ${escapeHtml(cleanText(proposal.status) || 'unknown')}</div>
            <div class="workflow-studio-muted">${escapeHtml(summarisePreviewDiff(pendingPreview?.diff_summary))}</div>
            <div class="workflow-studio-muted">Candidate valid: ${escapeHtml(String(pendingCandidateValidation?.valid ?? 'unknown'))}</div>
            <div class="workflow-studio-muted">Repair hints: ${escapeHtml(String(pendingCandidateValidation?.repair_hints?.length || 0))}</div>
          </div>
        ` : ''}
      </section>
    </div>
    <div class="workflow-studio-button-row anchored">
      <button type="button" class="btn-mini" data-action="preview-authoring">Preview changes</button>
      <button type="button" class="btn-mini primary" data-action="submit-proposal"${preview && state.previewSignature === serialiseDraftSpec(state.draftSpec) ? '' : ' disabled'}>${escapeHtml(submitButtonLabel)}</button>
      <button type="button" class="btn-mini" data-action="approve-proposal"${proposal?.active ? '' : ' disabled'}>Approve & publish</button>
      <button type="button" class="btn-mini" data-action="reject-proposal"${proposal?.active ? '' : ' disabled'}>Reject proposal</button>
      <button type="button" class="btn-mini" data-action="rollback-proposal"${proposal?.previous_authoring_spec_available ? '' : ' disabled'}>Roll back promotion</button>
    </div>
    <div class="workflow-studio-button-row anchored">
      <button type="button" class="btn-mini" data-action="demote-routing">Demote routing</button>
      <button type="button" class="btn-mini danger" data-action="supersede-publication">Supersede current workflow</button>
    </div>
    ${preview ? `
      <div class="workflow-studio-callout info">
        <strong>Preview ready.</strong>
        <span>${escapeHtml(summarisePreviewDiff(previewDiff))}</span>
      </div>
    ` : ''}
  `;
}

function renderCanvas() {
  if (!elements.canvas) return;
  let html;
  if (!state.workflowDetail) {
    html = `
      <div class="workflow-studio-empty">
        <h3>No workflow selected</h3>
        <p>The studio will render a derived workflow view here.</p>
      </div>
    `;
  } else if (state.activeView === 'topology') {
    html = renderTopologyView();
  } else if (state.activeView === 'decision') {
    html = renderDecisionView();
  } else if (state.activeView === 'dataflow') {
    html = renderDataflowView();
  } else if (state.activeView === 'operations') {
    html = renderOperationsView();
  } else {
    html = renderEditView();
  }
  elements.canvas.innerHTML = html;
}

function renderInspector() {
  if (!elements.inspector) return;
  if (!state.workflowDetail) {
    elements.inspector.innerHTML = `
      <div class="workflow-studio-empty">
        <h3>Inspector</h3>
        <p>Workflow and step details appear here once a workflow is selected.</p>
      </div>
    `;
    return;
  }
  const summary = state.workflowDetail.summary || {};
  const selectedStep = getSelectedStepSummary();
  const validation = state.workflowDetail.authoring?.validation || {};
  const lifecycle = summary.publication_lifecycle || {};
  const proposal = state.workflowDetail.proposal || {};
  const policy = state.workflowDetail.authoring?.policy || {};
  elements.inspector.innerHTML = `
    <div class="workflow-studio-section">
      <h3>Authority</h3>
      <div class="workflow-studio-key-value">
        <span>Store</span>
        <strong>${escapeHtml(state.workflowDetail.authority?.authoritative_store || 'vontology')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Runtime source</span>
        <strong>${escapeHtml(summary.source || 'unknown')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Definition hash</span>
        <strong class="workflow-studio-hash">${escapeHtml(summary.definition_identity?.definition_hash || 'unavailable')}</strong>
      </div>
    </div>
    <div class="workflow-studio-section">
      <h3>Lifecycle</h3>
      <div class="workflow-studio-key-value">
        <span>Phase</span>
        <strong>${escapeHtml(cleanText(lifecycle.phase) || 'unknown')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Review state</span>
        <strong>${escapeHtml(cleanText(proposal.status || lifecycle.review_state) || 'none')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Routing eligible</span>
        <strong>${escapeHtml(String(lifecycle.routing_eligible ?? 'unspecified'))}</strong>
      </div>
    </div>
    <div class="workflow-studio-section">
      <h3>Validation</h3>
      <div class="workflow-studio-key-value">
        <span>Current authoring contract</span>
        <strong>${validation.valid ? 'Valid' : 'Issues present'}</strong>
      </div>
      <div class="workflow-studio-muted">${escapeHtml((validation.errors || []).join(', ') || 'No validation errors recorded.')}</div>
    </div>
    <div class="workflow-studio-section">
      <h3>Policy</h3>
      <div class="workflow-studio-key-value">
        <span>Routing profile</span>
        <strong>${escapeHtml(cleanText(policy.routing_profile_source) || 'none')}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Event bindings</span>
        <strong>${escapeHtml(String((state.workflowDetail.operations?.bindings?.count) ?? 0))}</strong>
      </div>
      <div class="workflow-studio-key-value">
        <span>Schedules</span>
        <strong>${escapeHtml(String((state.workflowDetail.operations?.schedules?.count) ?? 0))}</strong>
      </div>
    </div>
    <div class="workflow-studio-section">
      <h3>${selectedStep ? `Step ${escapeHtml(selectedStep.step_id)}` : 'Workflow selection'}</h3>
      ${selectedStep ? `
        <div class="workflow-studio-key-value"><span>Action</span><strong>${escapeHtml(selectedStep.invokes_action_target || selectedStep.invokes_action || 'none')}</strong></div>
        <div class="workflow-studio-key-value"><span>Subworkflow</span><strong>${escapeHtml(selectedStep.invokes_workflow || 'none')}</strong></div>
        <div class="workflow-studio-key-value"><span>Writes context</span><strong>${escapeHtml((selectedStep.writes_context_keys || []).join(', ') || 'none')}</strong></div>
      ` : `
        <div class="workflow-studio-muted">Select a step in the current view to inspect its metadata.</div>
      `}
    </div>
    ${proposal?.available ? `
      <div class="workflow-studio-section">
        <h3>Proposal</h3>
        <div class="workflow-studio-key-value"><span>Status</span><strong>${escapeHtml(cleanText(proposal.status) || 'unknown')}</strong></div>
        <div class="workflow-studio-key-value"><span>Candidate valid</span><strong>${escapeHtml(String(proposal.candidate_validation?.valid ?? 'unknown'))}</strong></div>
        <div class="workflow-studio-muted">${escapeHtml(summarisePreviewDiff(proposal.preview_summary?.diff_summary))}</div>
      </div>
    ` : ''}
    ${renderImprovementGuidanceSection()}
    ${state.preview?.preview ? `
      <div class="workflow-studio-section">
        <h3>Pending preview</h3>
        <div class="workflow-studio-muted">${escapeHtml(summarisePreviewDiff(state.preview.preview.diff_summary))}</div>
      </div>
    ` : ''}
  `;
}

function renderAll() {
  elements.tabs.forEach((button) => {
    const active = button.dataset.view === state.activeView;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', active ? 'true' : 'false');
  });
  ensureSelectedStep();
  renderCatalogue();
  renderSummaryHeader();
  renderCanvas();
  renderInspector();
}

async function loadCatalogue({ selectFirst = false } = {}) {
  setConnectionBadge('Loading', 'loading');
  try {
    const data = await fetchJson(`/api/workflow-studio/catalogue?include_designs=${state.showDesigns ? 'true' : 'false'}&limit=300`);
    state.catalogue = asArray(data.items);
    if (!state.selectedWorkflowId && selectFirst && state.catalogue[0]?.workflow_id) {
      state.selectedWorkflowId = state.catalogue[0].workflow_id;
    }
    renderCatalogue();
    setConnectionBadge('Ready', 'ready');
  } catch (error) {
    console.error('Workflow studio catalogue failed:', error);
    setConnectionBadge('Offline', 'error');
    setStatusBanner(cleanText(error?.payload?.detail) || cleanText(error.message) || 'Could not load workflow catalogue.', 'error');
  }
}

async function loadWorkflowDetail(workflowId) {
  const workflowIdClean = cleanText(workflowId);
  if (!workflowIdClean) return;
  if (workflowIdClean !== state.selectedWorkflowId || !state.scheduleForm) {
    state.workflowDetail = null;
    state.scheduleForm = { schedule_type: 'interval', interval_seconds: 3600, inputs: '{}', idempotency_key: crypto.randomUUID() };
    state.scheduleReceipt = null;
  }
  state.selectedWorkflowId = workflowIdClean;
  setConnectionBadge('Loading', 'loading');
  renderAll();
  try {
    const data = await fetchJson(`/api/workflow-studio/workflows/${encodeURIComponent(workflowIdClean)}`);
    if (workflowIdClean !== state.selectedWorkflowId) return;
    state.workflowDetail = data;
    const draftSource = buildDraftSource(data);
    state.draftSpec = data.authoring?.available && draftSource.spec
      ? normaliseAuthoringSpecForEditor(draftSource.spec, workflowIdClean)
      : null;
    state.draftSourceLabel = draftSource.label || '';
    syncPolicyEditorsFromDraft();
    state.preview = null;
    state.previewSignature = '';
    ensureSelectedStep();
    renderAll();
    setStatusBanner('', 'info');
    setConnectionBadge('Ready', 'ready');
  } catch (error) {
    if (workflowIdClean !== state.selectedWorkflowId) return;
    console.error('Workflow studio detail failed:', error);
    setConnectionBadge('Error', 'error');
    setStatusBanner(cleanText(error?.payload?.detail) || cleanText(error.message) || 'Could not load workflow detail.', 'error');
  }
}

function markDraftChanged() {
  const currentSignature = serialiseDraftSpec(state.draftSpec);
  if (state.previewSignature && currentSignature !== state.previewSignature) {
    state.preview = null;
    state.previewSignature = '';
  }
  renderAll();
}

function parseStringList(rawValue) {
  return uniqueStrings(String(rawValue ?? '').split(',').map((item) => item.trim()));
}

function updateDraftWorkflowField(field, value) {
  if (!state.draftSpec) return;
  state.draftSpec[field] = field === 'description' ? String(value ?? '') : cleanText(value);
  markDraftChanged();
}

function updateDraftPolicyField(field, rawValue) {
  if (!state.draftSpec) return false;
  if (!state.draftSpec.workflow_metadata || typeof state.draftSpec.workflow_metadata !== 'object') {
    state.draftSpec.workflow_metadata = {};
  }
  state.policyEditors[field] = String(rawValue ?? '');
  const text = cleanText(rawValue);
  if (!text) {
    delete state.draftSpec.workflow_metadata[field];
    markDraftChanged();
    return true;
  }
  try {
    const parsed = JSON.parse(text);
    state.draftSpec.workflow_metadata[field] = parsed;
    markDraftChanged();
    return true;
  } catch (_error) {
    setStatusBanner(`${field} must be valid JSON before preview or submission.`, 'warning');
    return false;
  }
}

function clearReferencesToStep(stepId) {
  asArray(state.draftSpec?.steps).forEach((step) => {
    ['next_state_key', 'on_true_state_key', 'on_false_state_key', 'on_failure_state_key'].forEach((field) => {
      if (cleanText(step[field]) === stepId) {
        step[field] = '';
      }
    });
  });
}

function updateDraftStepField(stepId, field, value, inputType = 'text') {
  const step = getDraftStep(stepId);
  if (!step) return;
  if (field === 'terminal') {
    step.terminal = Boolean(value);
  } else if (['reads_variables', 'writes_variables', 'writes_context_keys'].includes(field)) {
    step[field] = parseStringList(value);
  } else {
    step[field] = inputType === 'checkbox' ? Boolean(value) : cleanText(value);
  }
  markDraftChanged();
}

function addDraftStep() {
  if (!state.draftSpec) return;
  let index = state.draftSpec.steps.length + 1;
  let stateId = `new_step_${index}`;
  const existingIds = new Set(state.draftSpec.steps.map((step) => cleanText(step.state_id)));
  while (existingIds.has(stateId)) {
    index += 1;
    stateId = `new_step_${index}`;
  }
  state.draftSpec.steps.push({
    state_id: stateId,
    action_id: '',
    subworkflow_id: '',
    terminal: false,
    next_state_key: '',
    on_true_state_key: '',
    on_false_state_key: '',
    on_failure_state_key: '',
    reads_variables: [],
    writes_variables: [],
    writes_context_keys: [],
    tool_output_context_mappings: [],
    context_input_mappings: [],
    metadata: {}
  });
  state.selectedStepId = stateId;
  if (!cleanText(state.draftSpec.initial_state_key)) {
    state.draftSpec.initial_state_key = stateId;
  }
  markDraftChanged();
}

function removeDraftStep(stepId) {
  if (!state.draftSpec) return;
  if (state.draftSpec.steps.length <= 1) {
    setStatusBanner('A workflow needs at least one step in the draft authoring spec.', 'warning');
    return;
  }
  state.draftSpec.steps = state.draftSpec.steps.filter((step) => cleanText(step.state_id) !== cleanText(stepId));
  clearReferencesToStep(cleanText(stepId));
  if (cleanText(state.draftSpec.initial_state_key) === cleanText(stepId)) {
    state.draftSpec.initial_state_key = cleanText(state.draftSpec.steps[0]?.state_id);
  }
  state.selectedStepId = cleanText(state.draftSpec.steps[0]?.state_id);
  markDraftChanged();
}

async function requestDescriptionProposal() {
  if (!state.selectedWorkflowId) return;
  setStatusBanner('Generating workflow description proposal...', 'info');
  try {
    const response = await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/proposals/description`,
      {
        method: 'POST',
        body: JSON.stringify({ mode: 'auto' })
      }
    );
    const proposalText = cleanText(response?.proposal?.text);
    if (proposalText && state.draftSpec) {
      state.draftSpec.description = proposalText;
      markDraftChanged();
      setStatusBanner(`Description proposal loaded from ${cleanText(response?.proposal?.source) || 'proposal service'}.`, 'success');
    } else {
      setStatusBanner('Description proposal did not return usable text.', 'warning');
    }
  } catch (error) {
    console.error('Workflow description proposal failed:', error);
    setStatusBanner(cleanText(error?.payload?.detail) || cleanText(error.message) || 'Could not generate workflow description proposal.', 'error');
  }
}

async function previewAuthoringDraft() {
  if (!state.selectedWorkflowId || !state.draftSpec) return;
  setStatusBanner('Previewing bounded workflow changes...', 'info');
  try {
    const response = await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/authoring/preview`,
      {
        method: 'POST',
        body: JSON.stringify({
          authoring_spec: state.draftSpec,
          base_definition_hash: state.workflowDetail?.authoring?.base_definition_hash || null
        })
      }
    );
    state.preview = response;
    state.previewSignature = serialiseDraftSpec(state.draftSpec);
    renderAll();
    if (response?.preview?.contract_validation?.valid) {
      setStatusBanner('Preview passed contract validation. Apply is enabled for this exact draft.', 'success');
    } else {
      setStatusBanner('Preview completed, but validation issues remain in the proposed workflow definition.', 'warning');
    }
  } catch (error) {
    console.error('Workflow authoring preview failed:', error);
    setStatusBanner(cleanText(error?.payload?.error) || cleanText(error.message) || 'Could not preview workflow authoring changes.', 'error');
  }
}

async function submitAuthoringProposal() {
  if (!state.selectedWorkflowId || !state.draftSpec) return;
  if (!state.preview || state.previewSignature !== serialiseDraftSpec(state.draftSpec)) {
    setStatusBanner('Preview the current draft before submitting it for review.', 'warning');
    return;
  }
  setStatusBanner('Submitting workflow proposal for review...', 'info');
  try {
    const response = await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/proposals/authoring`,
      {
        method: 'POST',
        body: JSON.stringify({
          authoring_spec: state.draftSpec,
          base_definition_hash: state.workflowDetail?.authoring?.base_definition_hash || null
        })
      }
    );
    setStatusBanner(`Proposal ${cleanText(response?.proposal?.status) || 'submitted'} and awaiting explicit review. Reloading detail...`, 'success');
    await loadCatalogue();
    await loadWorkflowDetail(state.selectedWorkflowId);
  } catch (error) {
    console.error('Workflow authoring proposal submit failed:', error);
    setStatusBanner(cleanText(error?.payload?.error) || cleanText(error.message) || 'Could not submit workflow authoring proposal.', 'error');
  }
}

async function reviewAuthoringProposal(action) {
  if (!state.selectedWorkflowId) return;
  setStatusBanner(`${action === 'approve' ? 'Approving' : 'Rejecting'} workflow proposal...`, 'info');
  try {
    const response = await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/proposals/review`,
      {
        method: 'POST',
        body: JSON.stringify({
          action,
          review_reason: cleanText(state.proposalReviewReason) || null
        })
      }
    );
    setStatusBanner(`Proposal ${cleanText(response?.review_action) || action} completed. Reloading authoritative detail...`, 'success');
    await loadCatalogue();
    await loadWorkflowDetail(state.selectedWorkflowId);
  } catch (error) {
    console.error('Workflow authoring proposal review failed:', error);
    setStatusBanner(cleanText(error?.payload?.error) || cleanText(error.message) || 'Could not review workflow authoring proposal.', 'error');
  }
}

async function rollbackAuthoringProposal() {
  if (!state.selectedWorkflowId) return;
  setStatusBanner('Rolling back the latest approved proposal...', 'info');
  try {
    await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/proposals/rollback`,
      {
        method: 'POST',
        body: JSON.stringify({
          review_reason: cleanText(state.proposalReviewReason) || null
        })
      }
    );
    setStatusBanner('Rollback applied. Reloading authoritative detail...', 'success');
    await loadCatalogue();
    await loadWorkflowDetail(state.selectedWorkflowId);
  } catch (error) {
    console.error('Workflow authoring proposal rollback failed:', error);
    setStatusBanner(cleanText(error?.payload?.error) || cleanText(error.message) || 'Could not roll back the workflow promotion.', 'error');
  }
}

async function demoteRoutingPublication() {
  if (!state.selectedWorkflowId) return;
  setStatusBanner('Demoting workflow routing eligibility...', 'info');
  try {
    await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/publication/demote`,
      {
        method: 'POST',
        body: JSON.stringify({
          review_reason: cleanText(state.proposalReviewReason) || null
        })
      }
    );
    setStatusBanner('Workflow routing demoted. Reloading detail...', 'success');
    await loadCatalogue();
    await loadWorkflowDetail(state.selectedWorkflowId);
  } catch (error) {
    console.error('Workflow routing demotion failed:', error);
    setStatusBanner(cleanText(error?.payload?.error) || cleanText(error.message) || 'Could not demote workflow routing.', 'error');
  }
}

async function supersedePublication() {
  if (!state.selectedWorkflowId) return;
  const replacementWorkflowId = cleanText(state.supersedeReplacementWorkflowId);
  if (!replacementWorkflowId) {
    setStatusBanner('Provide a replacement workflow ID before superseding the current workflow.', 'warning');
    return;
  }
  setStatusBanner('Superseding the current workflow publication...', 'info');
  try {
    await fetchJson(
      `/api/workflow-studio/workflows/${encodeURIComponent(state.selectedWorkflowId)}/publication/supersede`,
      {
        method: 'POST',
        body: JSON.stringify({
          replacement_workflow_id: replacementWorkflowId,
          review_reason: cleanText(state.proposalReviewReason) || null
        })
      }
    );
    setStatusBanner('Workflow publication superseded. Reloading catalogue and detail...', 'success');
    await loadCatalogue();
    await loadWorkflowDetail(replacementWorkflowId);
  } catch (error) {
    console.error('Workflow publication supersession failed:', error);
    setStatusBanner(cleanText(error?.payload?.error) || cleanText(error.message) || 'Could not supersede the workflow publication.', 'error');
  }
}

async function runScheduleCommand(action, button) {
  if (state.scheduleBusy) return;
  const workflowId = state.selectedWorkflowId;
  state.scheduleBusy = true;
  renderAll();
  try {
    let receipt;
    if (action === 'reload-schedules') {
      const schedules = await fetchJson(`/api/workflows/schedules?workflow_id=${encodeURIComponent(workflowId)}`);
      if (workflowId !== state.selectedWorkflowId) return;
      state.workflowDetail.operations.schedules = schedules;
      setStatusBanner('Schedules reloaded from canonical state.', 'success');
      return;
    } else if (action === 'toggle-schedule') {
      receipt = await fetchJson(`/api/workflows/schedules/${encodeURIComponent(button.dataset.scheduleId)}/enabled`, {
        method: 'PUT', body: JSON.stringify({ enabled: button.dataset.enabled === 'true' })
      });
    } else {
      const form = state.scheduleForm;
      const inputs = JSON.parse(form.inputs || '{}');
      if (!inputs || Array.isArray(inputs) || typeof inputs !== 'object') throw new Error('Workflow inputs must be a JSON object.');
      const body = { workflow_id: workflowId, schedule_type: form.schedule_type, default_inputs: inputs, idempotency_key: form.idempotency_key };
      if (form.schedule_type === 'interval') body.interval_seconds = Number(form.interval_seconds);
      if (form.schedule_type === 'cron') body.cron_expression = form.cron_expression;
      if (form.schedule_type === 'once') body.run_at = new Date(form.run_at).toISOString();
      receipt = await fetchJson(`/api/workflows/schedules${action === 'preview-schedule' ? '/preview' : ''}`, { method: 'POST', body: JSON.stringify(body) });
    }
    if (workflowId !== state.selectedWorkflowId) return;
    state.scheduleReceipt = receipt;
    if (!receipt.preview) {
      const schedules = state.workflowDetail.operations.schedules;
      const items = asArray(schedules.items).filter(item => item.schedule_id !== receipt.schedule_id);
      items.push(receipt);
      state.workflowDetail.operations.schedules = { ...schedules, items, count: items.length };
    }
    setStatusBanner(receipt.preview ? 'Inputs checked; next occurrence previewed.' : 'Schedule state saved and read back.', 'success');
  } catch (error) {
    if (workflowId === state.selectedWorkflowId) setStatusBanner(cleanText(error?.payload?.detail) || cleanText(error.message) || 'Schedule command could not be confirmed.', 'error');
  } finally {
    state.scheduleBusy = false;
    renderAll();
  }
}

function handleCanvasClick(event) {
  const stepNode = event.target.closest('[data-step-id]');
  if (stepNode) {
    state.selectedStepId = cleanText(stepNode.dataset.stepId);
    renderAll();
    return;
  }
  const stepSelect = event.target.closest('[data-step-select]');
  if (stepSelect) {
    state.selectedStepId = cleanText(stepSelect.dataset.stepSelect);
    renderAll();
    return;
  }
  const workflowAction = event.target.closest('[data-action]');
  if (!workflowAction) return;
  const action = cleanText(workflowAction.dataset.action);
  if (['preview-schedule', 'create-schedule', 'toggle-schedule', 'reload-schedules'].includes(action)) {
    void runScheduleCommand(action, workflowAction);
  } else if (action === 'suggest-description') {
    void requestDescriptionProposal();
  } else if (action === 'reset-draft') {
    const draftSource = buildDraftSource(state.workflowDetail);
    state.draftSpec = state.workflowDetail?.authoring?.available && draftSource.spec
      ? normaliseAuthoringSpecForEditor(draftSource.spec, state.selectedWorkflowId)
      : null;
    state.draftSourceLabel = draftSource.label || '';
    syncPolicyEditorsFromDraft();
    state.preview = null;
    state.previewSignature = '';
    renderAll();
    setStatusBanner(`Draft reset to the currently loaded ${state.draftSourceLabel || 'authoritative definition'}.`, 'info');
  } else if (action === 'preview-authoring') {
    void previewAuthoringDraft();
  } else if (action === 'submit-proposal') {
    void submitAuthoringProposal();
  } else if (action === 'approve-proposal') {
    void reviewAuthoringProposal('approve');
  } else if (action === 'reject-proposal') {
    void reviewAuthoringProposal('reject');
  } else if (action === 'rollback-proposal') {
    void rollbackAuthoringProposal();
  } else if (action === 'demote-routing') {
    void demoteRoutingPublication();
  } else if (action === 'supersede-publication') {
    void supersedePublication();
  } else if (action === 'add-step') {
    addDraftStep();
  } else if (action === 'remove-step') {
    removeDraftStep(state.selectedStepId);
  }
}

function handleCanvasInput(event) {
  const scheduleField = event.target.closest('[data-schedule-field]');
  if (scheduleField && state.scheduleForm) {
    state.scheduleForm[scheduleField.dataset.scheduleField] = scheduleField.value;
    state.scheduleForm.idempotency_key = crypto.randomUUID();
    state.scheduleReceipt = null;
    if (scheduleField.dataset.scheduleField === 'schedule_type') renderAll();
    return;
  }
  const lifecycleField = event.target.closest('[data-lifecycle-field]');
  if (lifecycleField) {
    const field = cleanText(lifecycleField.dataset.lifecycleField);
    if (field === 'proposalReviewReason') {
      state.proposalReviewReason = String(lifecycleField.value ?? '');
    } else if (field === 'supersedeReplacementWorkflowId') {
      state.supersedeReplacementWorkflowId = String(lifecycleField.value ?? '');
    }
    return;
  }
  const workflowField = event.target.closest('[data-edit-field]');
  if (workflowField) {
    updateDraftWorkflowField(workflowField.dataset.editField, workflowField.value);
    return;
  }
  const stepField = event.target.closest('[data-step-field]');
  if (!stepField) return;
  const stepId = cleanText(stepField.dataset.stepId);
  const field = cleanText(stepField.dataset.stepField);
  const value = stepField.type === 'checkbox' ? stepField.checked : stepField.value;
  updateDraftStepField(stepId, field, value, stepField.type);
}

function handleCanvasChange(event) {
  const policyField = event.target.closest('[data-policy-field]');
  if (policyField) {
    updateDraftPolicyField(cleanText(policyField.dataset.policyField), policyField.value);
    return;
  }
  handleCanvasInput(event);
}

function bindEvents() {
  elements.searchInput?.addEventListener('input', (event) => {
    state.search = event.target.value;
    renderCatalogue();
  });
  elements.showDesignsToggle?.addEventListener('change', (event) => {
    state.showDesigns = Boolean(event.target.checked);
    void loadCatalogue();
  });
  elements.refreshButton?.addEventListener('click', async () => {
    await loadCatalogue();
    if (state.selectedWorkflowId) {
      await loadWorkflowDetail(state.selectedWorkflowId);
    }
  });
  elements.catalogue?.addEventListener('click', (event) => {
    const item = event.target.closest('[data-workflow-id]');
    if (!item) return;
    void loadWorkflowDetail(item.dataset.workflowId);
  });
  elements.tabs.forEach((button) => {
    button.addEventListener('click', () => {
      state.activeView = button.dataset.view || 'topology';
      renderAll();
    });
  });
  elements.canvas?.addEventListener('click', handleCanvasClick);
  elements.canvas?.addEventListener('input', handleCanvasInput);
  elements.canvas?.addEventListener('change', handleCanvasChange);
  elements.canvas?.addEventListener('keydown', (event) => {
    const node = event.target.closest('[data-step-id]');
    if (!node) return;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      state.selectedStepId = cleanText(node.dataset.stepId);
      renderAll();
    }
  });
}

async function initialiseWorkflowStudio() {
  // Keep the standalone studio page aligned with the main Von window-scoped context model.
  syncOrgContextFromLocalStorage();
  syncNamespaceFromLocalStorage();
  await ensureUniqueWindowSessionId?.();
  cacheElements();
  bindEvents();
  renderAll();
  await loadCatalogue({ selectFirst: true });
  if (state.selectedWorkflowId) {
    await loadWorkflowDetail(state.selectedWorkflowId);
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    void initialiseWorkflowStudio();
  });
}

export {
  simplifyEdgeLabel,
  buildDraftSource,
  buildPolicyEditors,
  buildWorkflowStudioRequestHeaders
};
