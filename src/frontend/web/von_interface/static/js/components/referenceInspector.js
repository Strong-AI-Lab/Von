import { getJsonDetailed } from '../apiService.js';
import { copyTextWithClipboardFallback } from '../utils/copyJsonButtonState.js';
import { showToast } from '../utils/toast.js';

export const REFERENCE_MANIFEST_SCHEMA_VERSION = 'turn_reference_manifest.v1';

let currentReference = null;
let initialised = false;

function byId(id) {
  return document.getElementById(id);
}

function clean(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function normaliseList(value) {
  return Array.isArray(value)
    ? value.map((item) => clean(item)).filter(Boolean)
    : [];
}

export function normaliseReferenceManifest(value) {
  if (!value || typeof value !== 'object') return null;
  if (value.schema_version !== REFERENCE_MANIFEST_SCHEMA_VERSION) return null;
  const references = Array.isArray(value.references)
    ? value.references.filter((item) => (
      item
      && typeof item === 'object'
      && clean(item.reference_id)
      && clean(item.reference_type)
    ))
    : [];
  return {
    ...value,
    references,
    reference_count: references.length,
  };
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function shouldSkipTextNode(node) {
  const parent = node?.parentElement;
  if (!parent || !clean(node.nodeValue)) return true;
  return !!parent.closest(
    'button, a, input, textarea, select, option, script, style, '
    + '.vontology-cartouche, .chat-reference-token'
  );
}

export function decorateInspectableReferences(
  container,
  manifestValue,
  { onOpen = openReferenceInspector } = {},
) {
  if (!(container instanceof Element)) return 0;
  const manifest = normaliseReferenceManifest(manifestValue);
  if (!manifest?.references?.length) return 0;

  const referenceById = new Map(
    manifest.references.map((item) => [clean(item.reference_id), item]),
  );
  const ids = [...referenceById.keys()].filter(Boolean).sort((a, b) => b.length - a.length);
  if (!ids.length) return 0;
  const pattern = new RegExp(`(${ids.map(escapeRegExp).join('|')})`, 'g');
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) {
    if (!shouldSkipTextNode(walker.currentNode)) nodes.push(walker.currentNode);
  }

  let decorated = 0;
  for (const node of nodes) {
    const text = node.nodeValue || '';
    pattern.lastIndex = 0;
    if (!pattern.test(text)) continue;
    pattern.lastIndex = 0;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > cursor) fragment.appendChild(document.createTextNode(text.slice(cursor, match.index)));
      const reference = referenceById.get(match[0]);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'chat-reference-token';
      button.dataset.referenceId = match[0];
      button.dataset.referenceType = reference.reference_type;
      button.textContent = match[0];
      button.title = `Inspect ${reference.label || 'reference'}`;
      button.setAttribute('aria-label', `Inspect ${reference.label || 'reference'} ${match[0]}`);
      button.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        void onOpen(reference, { trigger: button });
      });
      fragment.appendChild(button);
      cursor = match.index + match[0].length;
      decorated += 1;
    }
    if (cursor < text.length) fragment.appendChild(document.createTextNode(text.slice(cursor)));
    node.replaceWith(fragment);
  }
  return decorated;
}

function clearBody() {
  const body = byId('conversationReferenceInspectorBody');
  if (body) body.replaceChildren();
  return body;
}

function setStatus(message) {
  const status = byId('conversationReferenceInspectorStatus');
  if (status) status.textContent = message || '';
}

function setBusy(busy) {
  const body = byId('conversationReferenceInspectorBody');
  if (body) body.setAttribute('aria-busy', busy ? 'true' : 'false');
}

function appendText(parent, tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

function appendSection(parent, title) {
  const section = document.createElement('section');
  section.className = 'chat-reference-section';
  appendText(section, 'h4', 'chat-reference-section-title', title);
  parent.appendChild(section);
  return section;
}

function displayValue(value) {
  if (value === true) return 'Yes';
  if (value === false) return 'No';
  if (value === null || value === undefined || value === '') return 'Not recorded';
  if (Array.isArray(value)) return value.join(', ') || 'None';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function appendFields(parent, fields) {
  const list = document.createElement('dl');
  list.className = 'chat-reference-fields';
  for (const [label, value] of fields) {
    const term = document.createElement('dt');
    term.textContent = label;
    const description = document.createElement('dd');
    description.textContent = displayValue(value);
    list.append(term, description);
  }
  parent.appendChild(list);
  return list;
}

function appendConceptButton(parent, conceptId, label) {
  const id = clean(conceptId);
  if (!id) return null;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'chat-reference-native-link';
  button.textContent = label || id;
  button.title = `Open concept ${id}`;
  button.addEventListener('click', (event) => {
    document.dispatchEvent(new CustomEvent('von:selectConceptById', {
      detail: {
        conceptId: id,
        createConceptTab: true,
        promoteExistingTab: true,
        modifierKeys: { shiftKey: !!event.shiftKey },
      },
    }));
  });
  parent.appendChild(button);
  return button;
}

function appendTechnicalDetails(parent, payload) {
  const details = document.createElement('details');
  details.className = 'chat-reference-technical';
  appendText(details, 'summary', '', 'Technical details');
  const pre = document.createElement('pre');
  pre.textContent = JSON.stringify(payload, null, 2);
  details.appendChild(pre);
  parent.appendChild(details);
}

function appendProfileList(parent, label, values) {
  const cleanValues = normaliseList(values);
  if (!cleanValues.length) return;
  const row = document.createElement('div');
  row.className = 'chat-reference-profile-row';
  appendText(row, 'div', 'chat-reference-profile-label', label);
  const valuesElement = document.createElement('div');
  valuesElement.className = 'chat-reference-profile-values';
  for (const value of cleanValues) appendText(valuesElement, 'span', 'chat-reference-chip', value);
  row.appendChild(valuesElement);
  parent.appendChild(row);
}

function renderAssertion(payload) {
  const body = clearBody();
  if (!body) return;
  const assertion = payload?.assertion || {};
  const summary = appendSection(body, 'Summary');
  appendText(
    summary,
    'p',
    'chat-reference-statement',
    clean(assertion.human_statement) || 'This assertion has no displayable statement.',
  );
  const subject = assertion.subject || {};
  if (clean(subject.concept_id)) {
    const links = document.createElement('div');
    links.className = 'chat-reference-native-links';
    appendConceptButton(links, subject.concept_id, subject.display_name || subject.concept_id);
    if (clean(assertion.predicate?.concept_id)) {
      appendConceptButton(
        links,
        assertion.predicate.concept_id,
        assertion.predicate.display_name || assertion.predicate.concept_id,
      );
    }
    if (assertion.object?.kind === 'concept' && clean(assertion.object.concept_id)) {
      appendConceptButton(
        links,
        assertion.object.concept_id,
        assertion.object.display_name || assertion.object.concept_id,
      );
    }
    summary.appendChild(links);
  }

  const profile = assertion.presentation?.kind === 'paper_matching_profile'
    ? assertion.presentation
    : null;
  if (profile) {
    const content = appendSection(body, 'Paper-matching profile');
    if (clean(profile.project_description)) {
      appendText(content, 'p', 'chat-reference-profile-description', profile.project_description);
    }
    appendProfileList(content, 'Interest terms', profile.stated_interest_terms);
    appendProfileList(content, 'Avoid', profile.negative_interest_terms);
    appendProfileList(content, 'Preferred authors', profile.preferred_authors);
    appendProfileList(content, 'Preferred venues', profile.preferred_venues);
    if (clean(profile.notes)) appendFields(content, [['Notes', profile.notes]]);
  } else {
    const content = appendSection(body, 'Content');
    appendFields(content, [
      ['Form', assertion.assertion_form],
      ['Object', assertion.object?.display_name || assertion.object?.text],
      ['Language', assertion.object?.language],
    ]);
  }

  const provenance = appendSection(body, 'Provenance and scope');
  appendFields(provenance, [
    ['Visibility', assertion.source_context?.label],
    ['Recorded by', assertion.provenance?.asserted_by_user_concept_id],
    ['Source capability', assertion.provenance?.capability_name],
    ['Source turn', assertion.provenance?.turn_id],
    ['Evidence', assertion.provenance?.evidence],
  ]);
  const lifecycle = appendSection(body, 'Lifecycle');
  appendFields(lifecycle, [
    ['Status', assertion.status],
    ['Revision', assertion.assertion_revision],
    ['Created', assertion.created_at],
    ['Updated', assertion.updated_at],
    ['Context', assertion.assertion_context],
  ]);
  appendTechnicalDetails(body, payload);
}

function renderEvidence(reference) {
  const body = clearBody();
  if (!body) return;
  const envelope = reference.evidence || {};
  const summary = appendSection(body, 'Summary');
  appendText(
    summary,
    'p',
    'chat-reference-notice',
    'This ev_ ID was an exact actor-and-turn handle to a tool result. The complete result was retained only for that ordinary turn; this is the bounded projection saved with the conversation.',
  );
  appendFields(summary, [
    ['Source', envelope.tool_name || 'Structured effect fact'],
    ['Status', envelope.status],
    ['Content type', envelope.content_type],
    ['Shape', envelope.shape],
  ]);
  const content = appendSection(body, 'Saved preview');
  const preview = document.createElement('pre');
  preview.className = 'chat-reference-preview';
  preview.textContent = clean(envelope.preview) || 'No preview was retained for this handle.';
  content.appendChild(preview);
  if (envelope.preview_truncated === true) {
    appendText(content, 'p', 'chat-reference-muted', 'The saved preview is truncated.');
  }
  const provenance = appendSection(body, 'Provenance');
  appendFields(provenance, [
    ['Tool', envelope.tool_name],
    ['Call', envelope.call_id],
    ['Turn', envelope.turn_id],
    ['Trust boundary', envelope.trust_boundary],
    ['Source diagnostics', envelope.source_diagnostics],
    ['Provenance', envelope.provenance],
  ]);
  const lifecycle = appendSection(body, 'Lifecycle');
  appendFields(lifecycle, [
    ['Handle scope', reference.lifecycle?.handle_scope],
    ['Complete result lifetime', reference.lifecycle?.complete_result_lifetime],
    ['Saved form', reference.lifecycle?.persisted_projection],
    ['Durable evidence receipt', reference.lifecycle?.durable_evidence_receipt],
  ]);
  appendTechnicalDetails(body, {
    reference_id: reference.reference_id,
    sha256: envelope.sha256,
    size_bytes: envelope.size_bytes,
    char_count: envelope.char_count,
    available_selectors: envelope.available_selectors,
  });
}

function renderWorkflow(payload) {
  const body = clearBody();
  if (!body) return;
  const summary = appendSection(body, 'Summary');
  appendFields(summary, [
    ['Workflow', payload.workflow_name || payload.workflow_id],
    ['Status', payload.status || payload.current_state],
    ['Started', payload.started_at || payload.created_at],
    ['Updated', payload.updated_at],
    ['Completed', payload.completed_at],
    ['Error step', payload.error_step],
    ['Error', payload.error],
  ]);
  const workflowId = clean(payload.workflow_id);
  if (workflowId?.startsWith('#V#')) {
    const links = document.createElement('div');
    links.className = 'chat-reference-native-links';
    appendConceptButton(links, workflowId, 'Open workflow definition');
    summary.appendChild(links);
  }
  if (payload.outputs && typeof payload.outputs === 'object') {
    const output = appendSection(body, 'Outputs');
    appendTechnicalDetails(output, payload.outputs);
  }
  appendTechnicalDetails(body, payload);
}

function renderMutationReceipt(payload) {
  const body = clearBody();
  if (!body) return;
  const receipt = payload?.receipt || payload || {};
  const summary = appendSection(body, 'Summary');
  appendFields(summary, [
    ['Operation', receipt.operation || receipt.method_name],
    ['Status', receipt.status || receipt.effect_status],
    ['Changed', receipt.changed],
    ['Created', receipt.created_at],
    ['Updated', receipt.updated_at],
    ['Error code', receipt.error_code],
  ]);
  appendTechnicalDetails(body, receipt);
}

function renderError(message) {
  const body = clearBody();
  if (!body) return;
  appendText(body, 'p', 'chat-reference-error', message);
}

function setHeader(reference) {
  const eyebrow = byId('conversationReferenceInspectorEyebrow');
  const title = byId('conversationReferenceInspectorTitle');
  const explanation = byId('conversationReferenceInspectorExplanation');
  if (eyebrow) eyebrow.textContent = reference.label || 'Conversation reference';
  if (title) title.textContent = reference.label || 'Reference Inspector';
  if (explanation) explanation.textContent = reference.reference_id;
}

async function loadCurrentReference() {
  const reference = currentReference;
  if (!reference) return;
  setBusy(true);
  setStatus(`Loading ${reference.label || 'reference'}`);
  const body = clearBody();
  if (body) appendText(body, 'p', 'chat-reference-muted', 'Loading…');
  try {
    if (reference.reference_type === 'turn_evidence') {
      renderEvidence(reference);
    } else if (reference.reference_type === 'scoped_assertion') {
      const { data } = await getJsonDetailed(
        `/api/concepts/assertions/${encodeURIComponent(reference.reference_id)}`,
        { cache: 'no-store' },
      );
      if (reference !== currentReference) return;
      renderAssertion(data);
    } else if (reference.reference_type === 'workflow_instance') {
      const { data } = await getJsonDetailed(
        `/api/workflows/instances/${encodeURIComponent(reference.reference_id)}`,
        { cache: 'no-store' },
      );
      if (reference !== currentReference) return;
      renderWorkflow(data);
    } else if (reference.reference_type === 'ontology_mutation_receipt') {
      const { data } = await getJsonDetailed(
        `/api/ontology-authority/receipts/${encodeURIComponent(reference.reference_id)}`,
        { cache: 'no-store' },
      );
      if (reference !== currentReference) return;
      renderMutationReceipt(data);
    } else {
      throw new Error('This reference type does not have an inspector.');
    }
    setStatus(`${reference.label || 'Reference'} loaded`);
  } catch (error) {
    if (reference !== currentReference) return;
    const message = error?.status === 404
      ? 'This reference is not available in your current scope.'
      : (error?.message || 'The reference could not be loaded.');
    renderError(message);
    setStatus(message);
  } finally {
    if (reference === currentReference) setBusy(false);
  }
}

export async function openReferenceInspector(reference, { trigger = null } = {}) {
  if (!reference || typeof reference !== 'object') return false;
  const id = clean(reference.reference_id);
  const type = clean(reference.reference_type);
  if (!id || !type) return false;
  currentReference = { ...reference, reference_id: id, reference_type: type };
  const panel = byId('conversationReferenceInspector');
  if (!panel) return false;

  const situation = byId('conversationSituationPanel');
  const situationToggle = byId('conversationSituationToggleBtn');
  situation?.classList.add('hidden');
  situationToggle?.setAttribute('aria-expanded', 'false');
  panel.classList.remove('hidden');
  panel.dataset.referenceId = id;
  panel.dataset.referenceType = type;
  setHeader(currentReference);
  await loadCurrentReference();
  panel.focus({ preventScroll: true });
  if (trigger instanceof HTMLElement) panel.dataset.triggerReferenceId = trigger.dataset.referenceId || '';
  return true;
}

export function closeReferenceInspector() {
  const panel = byId('conversationReferenceInspector');
  panel?.classList.add('hidden');
  panel?.removeAttribute('data-reference-id');
  currentReference = null;
}

export function initializeReferenceInspector() {
  if (initialised) return;
  const panel = byId('conversationReferenceInspector');
  if (!panel) return;
  initialised = true;
  byId('conversationReferenceInspectorCloseBtn')?.addEventListener('click', closeReferenceInspector);
  byId('conversationReferenceInspectorRefreshBtn')?.addEventListener('click', () => {
    void loadCurrentReference();
  });
  byId('conversationReferenceInspectorCopyBtn')?.addEventListener('click', async () => {
    const id = currentReference?.reference_id;
    if (!id) return;
    const copied = await copyTextWithClipboardFallback(id);
    showToast(copied ? 'Reference ID copied.' : 'Could not copy reference ID.', copied ? 'success' : 'error');
  });
  panel.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeReferenceInspector();
    }
  });
}

export function __testOnly_resetReferenceInspector() {
  currentReference = null;
  initialised = false;
}
