import { getJsonDetailed } from '../apiService.js';

const DEFAULT_PAGE_LIMIT = 25;
const ACTOR_CONTEXT_STATE_KEY = '__vonConceptEffectiveAssertionContextState';
const actorContextState = globalThis[ACTOR_CONTEXT_STATE_KEY] || {
  registrations: new Map(),
  listenersBound: false,
  reload: null,
  refreshKnowledge: null,
};
globalThis[ACTOR_CONTEXT_STATE_KEY] = actorContextState;
const panelRegistrations = actorContextState.registrations;

function cleanText(value) {
  return typeof value === 'string' ? value.trim() : '';
}

function getMountElement(suffix) {
  return (
    document.getElementById(`conceptEffectiveAssertionsMount_${suffix}`) ||
    document.getElementById('conceptEffectiveAssertionsMount') ||
    null
  );
}

function appendText(parent, tagName, className, text) {
  const value = cleanText(text);
  if (!value) return null;
  const element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = value;
  parent.appendChild(element);
  return element;
}

function createPanel(mount, suffix) {
  let panel = document.getElementById(`conceptEffectiveAssertionsPanel_${suffix}`);
  if (panel) return panel;

  panel = document.createElement('section');
  panel.id = `conceptEffectiveAssertionsPanel_${suffix}`;
  panel.className = 'concept-effective-assertions hidden';
  panel.setAttribute('aria-labelledby', `conceptEffectiveAssertionsTitle_${suffix}`);

  const header = document.createElement('div');
  header.className = 'concept-effective-assertions-header';
  const headingGroup = document.createElement('div');
  const title = appendText(
    headingGroup,
    'h3',
    'concept-effective-assertions-title',
    'Knowledge visible to you',
  );
  if (title) title.id = `conceptEffectiveAssertionsTitle_${suffix}`;
  appendText(
    headingGroup,
    'p',
    'concept-effective-assertions-intro',
    'Personal and organisation assertions are shown with their source context; they do not replace base publication facts.',
  );
  header.appendChild(headingGroup);

  const status = document.createElement('span');
  status.id = `conceptEffectiveAssertionsStatus_${suffix}`;
  status.className = 'concept-effective-assertions-status';
  status.setAttribute('role', 'status');
  status.setAttribute('aria-live', 'polite');
  header.appendChild(status);
  panel.appendChild(header);

  const rows = document.createElement('div');
  rows.id = `conceptEffectiveAssertionsRows_${suffix}`;
  rows.className = 'concept-effective-assertions-rows';
  panel.appendChild(rows);

  const actions = document.createElement('div');
  actions.className = 'concept-effective-assertions-actions';
  const loadMore = document.createElement('button');
  loadMore.id = `conceptEffectiveAssertionsLoadMore_${suffix}`;
  loadMore.type = 'button';
  loadMore.className = 'concept-effective-assertions-load-more hidden';
  loadMore.textContent = 'Load more';
  actions.appendChild(loadMore);
  panel.appendChild(actions);

  mount.appendChild(panel);
  return panel;
}

function formatDate(value) {
  const token = cleanText(value);
  if (!token) return '';
  const date = new Date(token);
  return Number.isNaN(date.getTime()) ? token : date.toLocaleString();
}

function createDetailRow(list, label, value) {
  const text = cleanText(value);
  if (!text) return;
  appendText(list, 'dt', '', label);
  appendText(list, 'dd', '', text);
}

function createAssertionDetails(item) {
  const details = document.createElement('details');
  details.className = 'concept-effective-assertion-details';
  appendText(details, 'summary', '', 'Details');
  const list = document.createElement('dl');
  createDetailRow(list, 'Assertion ID', item?.assertion_id);
  createDetailRow(list, 'Status', item?.status);
  createDetailRow(list, 'Updated', formatDate(item?.updated_at));
  createDetailRow(
    list,
    'Recorded by',
    item?.provenance?.asserted_by_user_concept_id,
  );
  createDetailRow(list, 'Source capability', item?.provenance?.capability_name);
  const evidence = item?.provenance?.evidence;
  if (evidence && typeof evidence === 'object' && Object.keys(evidence).length > 0) {
    createDetailRow(list, 'Evidence', JSON.stringify(evidence));
  }
  details.appendChild(list);
  return details;
}

function assertionRelevanceKind(item) {
  return cleanText(item?.concept_relevance?.kind).toLowerCase();
}

function assertionDirection(item) {
  const relevanceKind = assertionRelevanceKind(item);
  if (relevanceKind === 'grounded_object') return 'incoming';
  if (relevanceKind === 'grounded_subject') return 'outgoing';
  if (relevanceKind === 'grounded_subject_and_object') return 'reflexive';
  if (relevanceKind === 'aboutness_only') return 'aboutness';
  return 'unspecified';
}

function assertionArgumentLabel(argument) {
  if (!argument || typeof argument !== 'object') return '';
  if (argument.kind === 'text') return cleanText(argument.text);
  return cleanText(argument.display_name) || cleanText(argument.concept_id);
}

function assertionStatementPresentation(item) {
  const predicate = cleanText(item?.predicate?.display_name) || 'Assertion';
  const value = assertionArgumentLabel(item?.object);
  if (!value) return null;

  const humanStatement = cleanText(item?.human_statement);
  if (humanStatement) {
    return { complete: true, predicate, text: humanStatement, value };
  }

  const relevanceKind = assertionRelevanceKind(item);
  if (relevanceKind === 'aboutness_only') {
    return { complete: true, predicate, text: value, value };
  }

  const subject = assertionArgumentLabel(item?.subject);
  if (subject) {
    return {
      complete: true,
      predicate,
      text: [subject, predicate, value].join(' '),
      value,
    };
  }
  return { complete: false, predicate, text: '', value };
}

function createAssertionRow(item) {
  const presentation = assertionStatementPresentation(item);
  if (!presentation) return null;

  const row = document.createElement('article');
  row.className = 'concept-effective-assertion-row';
  row.dataset.assertionId = cleanText(item?.assertion_id);
  row.dataset.relationDirection = assertionDirection(item);

  const statement = document.createElement('div');
  statement.className = 'concept-effective-assertion-statement';
  if (presentation.complete) {
    statement.classList.add('is-complete-statement');
    appendText(
      statement,
      'span',
      'concept-effective-assertion-human-statement',
      presentation.text,
    );
  } else {
    appendText(
      statement,
      'span',
      'concept-effective-assertion-predicate',
      presentation.predicate,
    );
    appendText(
      statement,
      'span',
      'concept-effective-assertion-value',
      presentation.value,
    );
  }
  row.appendChild(statement);

  if (item?.concept_relevance?.aboutness_only === true
      || item?.concept_relevance?.kind === 'aboutness_only') {
    const relevance = appendText(
      row,
      'p',
      'concept-effective-assertion-relevance aboutness-only',
      'Exact text linked to this concept; no typed relation asserted.',
    );
    if (relevance) relevance.setAttribute('role', 'note');
  }

  const context = item?.source_context || {};
  const contextKind = context.kind === 'organisation' ? 'organisation' : 'personal';
  const epistemicStatus = cleanText(item?.epistemic_status).toLowerCase();
  const aboutnessOnly = item?.concept_relevance?.aboutness_only === true
    || item?.concept_relevance?.kind === 'aboutness_only';
  if (epistemicStatus === 'tentative' && !aboutnessOnly) {
    const tentative = appendText(
      row,
      'span',
      'concept-effective-assertion-epistemic concept-effective-assertion-epistemic-tentative',
      'Tentative typed relation; not confirmed/authority-active',
    );
    if (tentative) tentative.setAttribute('aria-label', tentative.textContent);
  }
  const badge = appendText(
    row,
    'span',
    `concept-effective-assertion-scope concept-effective-assertion-scope-${contextKind}`,
    context.label || (contextKind === 'organisation' ? 'Organisation' : 'Personal'),
  );
  if (badge) badge.setAttribute('aria-label', `Assertion context: ${badge.textContent}`);
  row.appendChild(createAssertionDetails(item));
  return row;
}

function appendAssertionToPredicateGroup(rows, item) {
  const predicateId = cleanText(item?.predicate?.storage_id)
    || cleanText(item?.predicate?.concept_id)
    || cleanText(item?.predicate?.display_name)
    || 'assertion';
  let group = Array.from(rows.querySelectorAll('.concept-effective-assertion-group'))
    .find((candidate) => candidate.dataset.predicateId === predicateId);
  if (!group) {
    group = document.createElement('section');
    group.className = 'concept-effective-assertion-group';
    group.dataset.predicateId = predicateId;
    appendText(
      group,
      'h4',
      'concept-effective-assertion-group-title',
      item?.predicate?.display_name || 'Assertion',
    );
    const groupRows = document.createElement('div');
    groupRows.className = 'concept-effective-assertion-group-rows';
    group.appendChild(groupRows);
    rows.appendChild(group);
  }
  const row = createAssertionRow(item);
  if (row) {
    group.querySelector('.concept-effective-assertion-group-rows')?.appendChild(row);
  }
  return row;
}

function setPanelStatus(panel, suffix, message, { error = false } = {}) {
  const status = panel.querySelector(`#conceptEffectiveAssertionsStatus_${suffix}`);
  if (!status) return;
  status.textContent = message || '';
  status.classList.toggle('error', error);
}

function setLoadMoreState(panel, suffix, payload, onLoadMore) {
  const button = panel.querySelector(`#conceptEffectiveAssertionsLoadMore_${suffix}`);
  if (!button) return;
  const hasMore = payload?.has_more === true && Number.isInteger(payload?.next_offset);
  button.classList.toggle('hidden', !hasMore);
  button.disabled = false;
  button.textContent = 'Load more';
  button.onclick = hasMore ? onLoadMore : null;
}

async function loadPage({ conceptId, suffix, offset, append }) {
  const mount = getMountElement(suffix);
  if (!mount) return { rendered: false, contextView: null };
  const panel = createPanel(mount, suffix);
  const rows = panel.querySelector(`#conceptEffectiveAssertionsRows_${suffix}`);
  const token = `${conceptId}:${offset}:${Date.now()}:${Math.random()}`;
  panel.dataset.loadToken = token;
  panel.dataset.conceptId = conceptId;
  if (!append) {
    panel.classList.add('hidden');
    rows?.replaceChildren();
  }

  try {
    const { data } = await getJsonDetailed(
      `/api/concepts/${encodeURIComponent(conceptId)}/effective_assertions?limit=${DEFAULT_PAGE_LIMIT}&offset=${offset}&argument_concept_id=${encodeURIComponent(conceptId)}`,
      { cache: 'no-store' },
    );
    if (panel.dataset.loadToken !== token || panel.dataset.conceptId !== conceptId) {
      return { rendered: false, contextView: null };
    }

    const contextView = cleanText(data?.context_view);
    const items = Array.isArray(data?.items)
      ? data.items.filter((item) => (
        item?.status === 'asserted'
        || cleanText(item?.epistemic_status).toLowerCase() === 'tentative'
      ))
      : [];
    if (contextView !== 'actor_effective' || (items.length === 0 && !append)) {
      panel.classList.add('hidden');
      setPanelStatus(panel, suffix, '');
      return { rendered: false, contextView };
    }

    panel.classList.remove('hidden');
    for (const item of items) {
      if (rows) appendAssertionToPredicateGroup(rows, item);
    }
    const assertionCount = rows?.querySelectorAll('.concept-effective-assertion-row').length || 0;
    const lowerBound = data?.counts_are_lower_bounds === true;
    setPanelStatus(
      panel,
      suffix,
      data?.has_more === true || lowerBound
        ? 'Showing a bounded page. More visible assertions are available.'
        : `${assertionCount} visible assertion${assertionCount === 1 ? '' : 's'}.`,
    );
    setLoadMoreState(panel, suffix, data, async () => {
      const button = panel.querySelector(`#conceptEffectiveAssertionsLoadMore_${suffix}`);
      if (button) button.disabled = true;
      await loadPage({
        conceptId,
        suffix,
        offset: data.next_offset,
        append: true,
      });
    });
    return { rendered: true, contextView };
  } catch (error) {
    if (panel.dataset.loadToken !== token || panel.dataset.conceptId !== conceptId) {
      return { rendered: false, contextView: null };
    }
    console.warn('[conceptEffectiveAssertionsPanel] Failed to load assertions', error);
    panel.classList.remove('hidden');
    setPanelStatus(
      panel,
      suffix,
      'Knowledge visible to you is temporarily unavailable.',
      { error: true },
    );
    const button = panel.querySelector(`#conceptEffectiveAssertionsLoadMore_${suffix}`);
    if (button) {
      button.classList.remove('hidden');
      button.disabled = false;
      button.textContent = 'Retry';
      button.onclick = () => loadPage({ conceptId, suffix, offset: 0, append: false });
    }
    return { rendered: false, contextView: 'degraded' };
  }
}

function clearAndReloadRegisteredPanels() {
  for (const [suffix, registration] of panelRegistrations.entries()) {
    const mount = getMountElement(suffix);
    if (!mount?.isConnected) {
      panelRegistrations.delete(suffix);
      continue;
    }
    const panel = mount.querySelector(`#conceptEffectiveAssertionsPanel_${suffix}`);
    panel?.classList.add('hidden');
    panel?.querySelector(`#conceptEffectiveAssertionsRows_${suffix}`)?.replaceChildren();
    void loadPage({
      conceptId: registration.conceptId,
      suffix,
      offset: 0,
      append: false,
    });
  }
}

export function refreshConceptEffectiveAssertionsPanel({ conceptId, suffix = null } = {}) {
  const targetConceptId = cleanText(conceptId);
  const matching = [];
  for (const [registeredSuffix, registration] of panelRegistrations.entries()) {
    const registeredMount = document.getElementById(
      registeredSuffix
        ? `conceptEffectiveAssertionsMount_${registeredSuffix}`
        : 'conceptEffectiveAssertionsMount',
    );
    if (!registeredMount?.isConnected) {
      panelRegistrations.delete(registeredSuffix);
      continue;
    }
    if (suffix !== null && registeredSuffix !== suffix) continue;
    if (targetConceptId && registration.conceptId !== targetConceptId) continue;
    matching.push(loadPage({
      conceptId: registration.conceptId,
      suffix: registeredSuffix,
      offset: 0,
      append: false,
    }));
  }
  return Promise.allSettled(matching);
}

function registerActorContextInvalidation(conceptId, suffix) {
  panelRegistrations.set(suffix, { conceptId });
  actorContextState.reload = clearAndReloadRegisteredPanels;
  actorContextState.refreshKnowledge = refreshConceptEffectiveAssertionsPanel;
  if (actorContextState.listenersBound) return;
  actorContextState.listenersBound = true;
  const reloadLatestProjection = () => actorContextState.reload?.();
  document.addEventListener('orgSwitched', reloadLatestProjection);
  document.addEventListener('authStatusChanged', reloadLatestProjection);
  document.addEventListener('von:conceptKnowledgeChanged', (event) => {
    const conceptId = cleanText(event?.detail?.conceptId);
    if (!conceptId) return;
    void actorContextState.refreshKnowledge?.({ conceptId });
  });
}

export async function ensureConceptEffectiveAssertionsPanel({ conceptId, suffix }) {
  registerActorContextInvalidation(conceptId, suffix);
  return loadPage({ conceptId, suffix, offset: 0, append: false });
}

export const _test = {
  createAssertionRow,
  appendAssertionToPredicateGroup,
  assertionDirection,
  assertionStatementPresentation,
  formatDate,
};
