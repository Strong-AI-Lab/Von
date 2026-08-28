import { getJsonDetailed } from '../apiService.js';
import { activateTab } from '../tabNavigation.js';
import { copyTextWithClipboardFallback } from '../utils/copyJsonButtonState.js';

const PANEL_VARIANTS = new Set([
  'identity',
  'document',
  'task',
  'event',
  'location',
  'conversation',
  'generic',
]);
const LONG_PROMOTED_TEXT_MIN_CHARS = 60;
let promotedTextDisclosureSequence = 0;

function getMountElement(stepContainer, suffix) {
  return (
    document.getElementById(`conceptSummaryRendererMount_${suffix}`) ||
    document.getElementById('conceptSummaryRendererMount') ||
    stepContainer.querySelector('.concept-summary-renderer-mount')
  );
}

function createPanelElement(suffix) {
  const panel = document.createElement('section');
  panel.id = `conceptSummaryRendererPanel_${suffix}`;
  panel.className = 'concept-summary-card hidden';
  return panel;
}

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function appendTextElement(parent, tagName, className, text) {
  const value = cleanText(text);
  if (!value) return null;
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = value;
  parent.appendChild(element);
  return element;
}

function createBadgeRow(badges = []) {
  const values = Array.isArray(badges)
    ? badges.map(cleanText).filter(Boolean)
    : [];
  if (values.length === 0) return null;
  const row = document.createElement('div');
  row.className = 'concept-summary-badges';
  values.forEach((value) => {
    appendTextElement(row, 'span', 'concept-summary-badge', value);
  });
  return row;
}

function createFacts(facts = []) {
  const values = Array.isArray(facts)
    ? facts.filter(
        (item) => item && cleanText(item.label) && cleanText(item.value),
      )
    : [];
  if (values.length === 0) return null;
  const list = document.createElement('dl');
  list.className = 'concept-summary-facts';
  values.forEach((item) => {
    const fact = document.createElement('div');
    fact.className = 'concept-summary-fact';
    appendTextElement(fact, 'dt', '', item.label);
    appendTextElement(fact, 'dd', '', item.value);
    list.appendChild(fact);
  });
  return list;
}

function createPromotedValueMeta(value) {
  const predicateId = cleanText(value?.predicate_id);
  const sourceLabel = cleanText(value?.source_context?.label);
  const rawScopeKind = cleanText(value?.source_context?.kind);
  const scopeKind = ['canonical', 'personal', 'organisation'].includes(rawScopeKind)
    ? rawScopeKind
    : 'canonical';
  if (!predicateId && !sourceLabel) return null;
  const meta = document.createElement('div');
  meta.className = 'concept-summary-promoted-value-meta';
  if (sourceLabel) {
    appendTextElement(
      meta,
      'span',
      `concept-summary-promoted-scope scope-${scopeKind}`,
      sourceLabel,
    );
  }
  if (predicateId) {
    const predicate = appendTextElement(
      meta,
      'span',
      'concept-summary-promoted-predicate',
      predicateId,
    );
    if (predicate) predicate.title = predicateId;
  }
  return meta;
}

function createPromotedTextValue(value) {
  const exactText = typeof value?.text === 'string' ? value.text : '';
  if (!exactText.trim()) return null;
  const item = document.createElement('article');
  item.className = 'concept-summary-promoted-value concept-summary-promoted-text-value';
  const text = document.createElement('p');
  text.className = 'concept-summary-promoted-text';
  text.textContent = exactText;
  const isLong = exactText.length > LONG_PROMOTED_TEXT_MIN_CHARS || exactText.includes('\n');
  if (isLong) text.classList.add('is-collapsed');
  const disclosureId = `conceptSummaryPromotedText_${++promotedTextDisclosureSequence}`;
  text.id = disclosureId;
  item.appendChild(text);

  const controls = document.createElement('div');
  controls.className = 'concept-summary-promoted-controls';
  if (isLong) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'concept-summary-promoted-control concept-summary-promoted-toggle';
    toggle.textContent = 'Show full text';
    toggle.setAttribute('aria-controls', disclosureId);
    toggle.setAttribute('aria-expanded', 'false');
    toggle.addEventListener('click', () => {
      const shouldExpand = text.classList.contains('is-collapsed');
      text.classList.toggle('is-collapsed', !shouldExpand);
      toggle.setAttribute('aria-expanded', shouldExpand ? 'true' : 'false');
      toggle.textContent = shouldExpand ? 'Show less' : 'Show full text';
    });
    controls.appendChild(toggle);
  }

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'concept-summary-promoted-control concept-summary-promoted-copy';
  copy.textContent = 'Copy';
  copy.setAttribute('aria-label', 'Copy full text');
  copy.setAttribute('aria-live', 'polite');
  copy.addEventListener('click', async () => {
    const copied = await copyTextWithClipboardFallback(exactText);
    copy.textContent = copied ? 'Copied' : 'Copy failed';
  });
  controls.appendChild(copy);
  item.appendChild(controls);
  const meta = createPromotedValueMeta(value);
  if (meta) item.appendChild(meta);
  return item;
}

function createPromotedConceptValue(value) {
  const conceptId = cleanText(value?.concept_id);
  if (!conceptId.startsWith('#V#')) return null;
  const item = document.createElement('article');
  item.className = 'concept-summary-promoted-value concept-summary-promoted-concept-value';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'concept-summary-promoted-concept';
  button.textContent = cleanText(value?.display_name) || conceptId;
  button.title = conceptId;
  button.addEventListener('click', () => {
    document.dispatchEvent(new CustomEvent('von:selectConceptById', {
      detail: {
        conceptId,
        createConceptTab: true,
        promoteExistingTab: true,
        modifierKeys: { shiftKey: true },
      },
    }));
  });
  item.appendChild(button);
  const meta = createPromotedValueMeta(value);
  if (meta) item.appendChild(meta);
  return item;
}

function createPromotedFields(rawFields = []) {
  const fields = Array.isArray(rawFields) ? rawFields.slice(0, 100) : [];
  const container = document.createElement('section');
  container.className = 'concept-summary-promoted-fields';
  let renderedFieldCount = 0;
  fields.forEach((field) => {
    const label = cleanText(field?.label);
    const rawValues = Array.isArray(field?.values) ? field.values.slice(0, 250) : [];
    if (!label || rawValues.length === 0) return;
    const values = rawValues.flatMap((value) => {
      const element = value?.kind === 'concept'
        ? createPromotedConceptValue(value)
        : value?.kind === 'text'
          ? createPromotedTextValue(value)
          : null;
      return element ? [element] : [];
    });
    if (values.length === 0) return;
    const section = document.createElement('section');
    section.className = 'concept-summary-promoted-field';
    appendTextElement(section, 'h4', 'concept-summary-promoted-label', label);
    const valueList = document.createElement('div');
    valueList.className = 'concept-summary-promoted-values';
    valueList.append(...values);
    section.appendChild(valueList);
    container.appendChild(section);
    renderedFieldCount += 1;
  });
  return renderedFieldCount > 0 ? container : null;
}

function createExpandedSections(expandedSections = []) {
  const sections = Array.isArray(expandedSections)
    ? expandedSections.filter(
        (section) =>
          section &&
          cleanText(section.title) &&
          Array.isArray(section.items) &&
          section.items.some((item) => cleanText(item)),
      )
    : [];
  if (sections.length === 0) return null;
  const expanded = document.createElement('div');
  expanded.className = 'concept-summary-expanded hidden';
  sections.forEach((sectionData) => {
    const section = document.createElement('section');
    section.className = 'concept-summary-expanded-section';
    appendTextElement(section, 'h4', '', sectionData.title);
    const list = document.createElement('ul');
    sectionData.items.map(cleanText).filter(Boolean).forEach((item) => {
      appendTextElement(list, 'li', '', item);
    });
    section.appendChild(list);
    expanded.appendChild(section);
  });
  return expanded;
}

function normaliseFocalConcepts(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  return raw.slice(0, 4).flatMap((item) => {
    const conceptId = cleanText(item?.concept_id);
    if (!conceptId.startsWith('#V#') || seen.has(conceptId)) return [];
    seen.add(conceptId);
    return [{
      conceptId,
      displayName: cleanText(item?.display_name) || conceptId,
    }];
  });
}

function createFocalConcepts(raw, { label = 'Discussing' } = {}) {
  const focalConcepts = normaliseFocalConcepts(raw);
  if (focalConcepts.length === 0) return null;
  const container = document.createElement('div');
  container.className = 'concept-summary-focus';
  appendTextElement(container, 'span', 'concept-summary-focus-label', label);
  const links = document.createElement('div');
  links.className = 'concept-summary-focus-links';
  focalConcepts.forEach(({ conceptId, displayName }) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'concept-summary-focus-link';
    button.textContent = displayName;
    button.addEventListener('click', () => {
      document.dispatchEvent(new CustomEvent('von:selectConceptById', {
        detail: {
          conceptId,
          createConceptTab: true,
          promoteExistingTab: true,
          modifierKeys: { shiftKey: true },
        },
      }));
    });
    links.appendChild(button);
  });
  container.appendChild(links);
  return container;
}

function normaliseOpenConversationAction(raw) {
  if (!raw || raw.kind !== 'open_conversation') return null;
  const sessionId = cleanText(raw.session_id);
  if (!sessionId || sessionId.length > 512) return null;
  return { kind: 'open_conversation', sessionId };
}

function createActionStatus() {
  const status = document.createElement('span');
  status.className = 'concept-summary-action-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  return status;
}

function createOpenConversationButton(rawAction, { label = 'Open conversation' } = {}) {
  const action = normaliseOpenConversationAction(rawAction);
  if (!action) return null;
  const wrapper = document.createElement('div');
  wrapper.className = 'concept-summary-action';
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'concept-summary-open-conversation';
  button.textContent = label;
  const status = createActionStatus();
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = 'Opening…';
    status.textContent = '';
    try {
      const { switchToChatSession } = await import('../chatTab.js');
      const result = await switchToChatSession(action.sessionId);
      if (!result || result.ok !== true) {
        throw new Error(cleanText(result?.error) || 'Conversation unavailable');
      }
      button.disabled = false;
      button.textContent = label;
      activateTab('chatTab');
    } catch (error) {
      console.warn('[conceptSummaryRendererPanel] Failed to open conversation', error);
      button.disabled = false;
      button.textContent = label;
      status.textContent = 'Could not open this conversation. Please try again.';
    }
  });
  wrapper.append(button, status);
  return wrapper;
}

function createConversationBacklinks(raw) {
  const items = Array.isArray(raw?.items) ? raw.items.slice(0, 25) : [];
  const cards = items.filter((item) => cleanText(item?.title));
  if (cards.length === 0) return null;
  const section = document.createElement('section');
  section.className = 'concept-summary-conversations';
  appendTextElement(
    section,
    'h4',
    'concept-summary-conversations-title',
    'Conversations about this concept',
  );
  const list = document.createElement('div');
  list.className = 'concept-summary-conversation-list';
  cards.forEach((card) => {
    const item = document.createElement('article');
    item.className = 'concept-summary-conversation-card';
    appendTextElement(item, 'h5', 'concept-summary-conversation-title', card.title);
    appendTextElement(item, 'p', 'concept-summary-conversation-activity', card.last_activity_at);
    const focus = createFocalConcepts(card.focal_concepts, { label: 'Focus' });
    if (focus) item.appendChild(focus);
    const action = createOpenConversationButton(card.open_action, { label: 'Continue' });
    if (action) item.appendChild(action);
    list.appendChild(item);
  });
  section.appendChild(list);
  const moreCount = Number.isInteger(raw?.more_count) && raw.more_count > 0
    ? raw.more_count
    : 0;
  if (moreCount > 0) {
    appendTextElement(
      section,
      'p',
      'concept-summary-conversations-more',
      `${moreCount} more in Conversations`,
    );
  }
  return section;
}

function applyPanelPayload(panel, payload) {
  const panelData = payload?.panel || {};
  const variantCandidate = cleanText(panelData.variant);
  const variant = PANEL_VARIANTS.has(variantCandidate) ? variantCandidate : 'generic';
  const expanded = createExpandedSections(panelData.expanded_sections);

  panel.className = `concept-summary-card concept-summary-card-${variant}`;
  panel.replaceChildren();

  const header = document.createElement('div');
  header.className = 'concept-summary-header';
  appendTextElement(
    header,
    'div',
    'concept-summary-eyebrow',
    cleanText(panelData.eyebrow) || 'Concept',
  );
  if (expanded) {
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'concept-summary-toggle';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.textContent = 'Show more';
    toggle.addEventListener('click', () => {
      const isExpanded = !expanded.classList.contains('hidden');
      expanded.classList.toggle('hidden', isExpanded);
      toggle.setAttribute('aria-expanded', isExpanded ? 'false' : 'true');
      toggle.textContent = isExpanded ? 'Show more' : 'Show less';
    });
    header.appendChild(toggle);
  }
  panel.appendChild(header);
  appendTextElement(panel, 'h3', 'concept-summary-title', panelData.title);
  appendTextElement(panel, 'p', 'concept-summary-subtitle', panelData.subtitle);
  const badges = createBadgeRow(panelData.badges);
  if (badges) panel.appendChild(badges);
  const facts = createFacts(panelData.facts);
  if (facts) panel.appendChild(facts);
  appendTextElement(panel, 'p', 'concept-summary-text', panelData.summary);
  const promotedFields = createPromotedFields(panelData.promoted_fields);
  if (promotedFields) panel.appendChild(promotedFields);
  if (cleanText(panelData?.promoted_fields_status?.message)) {
    appendTextElement(
      panel,
      'p',
      'concept-summary-promoted-unavailable',
      panelData.promoted_fields_status.message,
    );
  }
  const focus = createFocalConcepts(panelData.focal_concepts);
  if (focus) panel.appendChild(focus);
  const primaryAction = createOpenConversationButton(panelData.open_action);
  if (primaryAction) panel.appendChild(primaryAction);
  if (expanded) panel.appendChild(expanded);
  const backlinks = createConversationBacklinks(payload?.conversation_backlinks);
  if (backlinks) panel.appendChild(backlinks);
}

function clearPanel(panel) {
  panel.className = 'concept-summary-card hidden';
  panel.replaceChildren();
}

export async function ensureConceptSummaryRendererPanelForConceptTab({
  conceptId,
  suffix,
}) {
  const stepContainer =
    document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
  if (!stepContainer) return false;

  const mount = getMountElement(stepContainer, suffix);
  if (!mount) return false;

  let panel = document.getElementById(`conceptSummaryRendererPanel_${suffix}`);
  if (!panel) {
    panel = createPanelElement(suffix);
    mount.replaceChildren(panel);
  }

  try {
    const { data } = await getJsonDetailed(
      `/api/concepts/${encodeURIComponent(conceptId)}/summary_renderer`,
      { cache: 'no-store' },
    );
    if (!data || !data.panel) {
      clearPanel(panel);
      return false;
    }
    applyPanelPayload(panel, data);
    return true;
  } catch (error) {
    console.warn('[conceptSummaryRendererPanel] Failed to load concept summary renderer', error);
    clearPanel(panel);
    return false;
  }
}
