import { fetchConceptList, resetConceptTab, updateConceptTabUI } from './conceptTab.js';
import { clearContainer, elements, getCurrentUserConceptId } from './domUtils.js';
import { handleVontologyNodeSelection } from './dynamicTabs.js';
import { getWindowSessionId, WINDOW_SESSION_HEADER } from './apiService.js';
import { createPredicateBadge, getPredicateType } from './predicateUtils.js';
import { ProgressManager, ProgressPhase } from './progress.js';
import {
  currentVontologyNodeId,
  defaultSelectedConceptType,
  getCurrentConceptType,
  getSelectedVontologyConceptId,
  setCurrentConceptType,
  setCurrentVontologyNodeId,
  setSelectedVontologyConceptId,
  setVontologyTreeData,
  vontologyTreeData
} from './state.js';
import { finishBackgroundTask, startBackgroundTask } from './backgroundTaskTracker.js';
import { initialiseResizableViewport } from './utils/resizableViewport.js';
import { createAnnotatedFragment, createVontologyCartouche, normalisePotentialConceptId } from './utils/textDecorator.js';

// Search configuration constants
const ENABLE_VONTOLOGY_SEARCH_TOOLTIPS = false; // Disabled for now

// Global variables for Vontology tab
let currentSelectedVontologyPath = null;
let currentInteractionId = null;
// Latest entity counts snapshot populated when available
let __vontologyLatestEntityCounts = null;

// Lightweight in-memory preload cache for early background fetch
let __vontologyPreloadedTreeData = null;
let __vontologyPreloadedEntityCounts = null;
let __vontologyPreloadInFlight = null;
let __vontologyPreloadGeneration = 0;
// Global-ish busy indicator to signal heavy ontology operations (preload/build)
if (typeof window !== 'undefined' && !window.__VONTOLOGY_BUSY) {
  window.__VONTOLOGY_BUSY = false;
}

// Lightweight debug gate for verbose client-side logging
function debugLog(...args) {
  try {
    if (typeof window !== 'undefined' && window.__VONTOLOGY_DEBUG__) {
      console.debug(...args);
    }
  } catch (_) {
    // no-op
  }
}

function vontologyFetch(url, options = {}) {
  // Keep identity context consistent across Vontology endpoints.
  // Access control can fall back to X-User-Concept-ID when session state is absent.
  const userConceptId = getCurrentUserConceptId();
  const baseHeaders = (options && typeof options === 'object' ? options.headers : null) || {};
  const mergedHeaders = {
    ...baseHeaders,
    [WINDOW_SESSION_HEADER]: getWindowSessionId(),
  };
  if (userConceptId) {
    mergedHeaders['X-User-Concept-ID'] = userConceptId;
  }
  return fetch(url, { ...options, headers: mergedHeaders });
}

const RENDER_BATCH_SIZE = 500;
const PROGRESS_INTERVAL = 250;
async function yieldThread() {
  await new Promise((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => resolve());
    } else {
      setTimeout(resolve, 0);
    }
  });
}

// Ensure a single global progress overlay exists as early as possible.
function initGlobalProgressOverlay() {
  try {
    if (typeof window === 'undefined') return null;
    if (window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP) return window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP;

    const create = () => {
      const wrap = document.createElement('div');
      wrap.className = 'vontology-progress-wrap';
      wrap.dataset.vontologyProgressGlobal = '1';
      wrap.style.position = 'fixed';
      wrap.style.top = '6px';
      wrap.style.left = '6px';
      wrap.style.right = '6px';
      wrap.style.zIndex = '2147483647';
      wrap.style.width = 'auto';
      wrap.style.margin = '0';
      wrap.style.padding = '6px';
      wrap.style.background = 'rgba(255,255,255,0.98)';
      wrap.style.border = '1px solid rgba(0,0,0,0.08)';
      wrap.style.boxShadow = '0 2px 8px rgba(0,0,0,0.08)';
      wrap.style.display = 'none';
      wrap.style.opacity = '0';

      const barOuter = document.createElement('div');
      barOuter.className = 'vontology-progress-outer';
      barOuter.style.background = '#eee';
      barOuter.style.border = '1px solid #ddd';
      barOuter.style.borderRadius = '4px';
      barOuter.style.height = '10px';
      barOuter.style.overflow = 'hidden';

      const barInner = document.createElement('div');
      barInner.className = 'vontology-progress-inner';
      barInner.style.height = '100%';
      barInner.style.minHeight = '10px';
      barInner.style.width = '0%';
      barInner.style.background = 'linear-gradient(90deg,#2b8cff,#68d1ff)';
      barInner.style.transition = 'width 240ms linear';

      const label = document.createElement('div');
      label.className = 'vontology-progress-label';
      label.style.fontSize = '0.85em';
      label.style.color = '#444';
      label.style.marginTop = '6px';
      label.style.textAlign = 'left';
      label.style.paddingLeft = '6px';

      barOuter.appendChild(barInner);
      wrap.appendChild(barOuter);
      wrap.appendChild(label);

      document.body.appendChild(wrap);
      window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP = wrap;
      return wrap;
    };

    if (document && document.body) {
      return create();
    } else {
      document.addEventListener('DOMContentLoaded', () => { try { create(); } catch (_) { } });
      return null;
    }
  } catch (_) {
    return null;
  }
}

// Clear selection if a selected concept is deleted elsewhere (tab or tree)
document.addEventListener('concept-deleted', (evt) => {
  try {
    const deleted = (evt.detail || {}).conceptId;
    if (!deleted) return;
    const selectedId = getSelectedVontologyConceptId();
    if (deleted === selectedId || deleted === currentVontologyNodeId) {
      setSelectedVontologyConceptId(null);
      setCurrentVontologyNodeId(null);
      setCurrentConceptType(null);
      const span = document.getElementById('selectedNodePath');
      if (span) span.textContent = 'None';
    }
  } catch (e) {
    console.warn('[vontology] selection cleanup after deletion failed', e);
  }
});

// initialize early
initGlobalProgressOverlay();

// Explicit helper to show (or create) the global overlay immediately.
function showGlobalProgressOverlay(initialText = 'Loading...') {
  try {
    if (typeof window === 'undefined') return;
    let wrap = window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP || initGlobalProgressOverlay();
    if (!wrap) return;
    // Ensure required inner structure exists (in case earlier creation failed partially)
    if (!wrap.querySelector('.vontology-progress-inner')) {
      const barOuter = document.createElement('div');
      barOuter.className = 'vontology-progress-outer';
      barOuter.style.background = '#eee';
      barOuter.style.border = '1px solid #ddd';
      barOuter.style.borderRadius = '4px';
      barOuter.style.height = '10px';
      barOuter.style.overflow = 'hidden';
      const barInner = document.createElement('div');
      barInner.className = 'vontology-progress-inner';
      barInner.style.height = '100%';
      barInner.style.minHeight = '10px';
      barInner.style.width = '0%';
      barInner.style.background = 'linear-gradient(90deg,#2b8cff,#68d1ff)';
      barInner.style.transition = 'width 240ms linear';
      const label = document.createElement('div');
      label.className = 'vontology-progress-label';
      label.style.fontSize = '0.85em';
      label.style.color = '#444';
      label.style.marginTop = '6px';
      label.style.textAlign = 'left';
      label.style.paddingLeft = '6px';
      barOuter.appendChild(barInner);
      wrap.appendChild(barOuter);
      wrap.appendChild(label);
    }
    wrap.style.display = 'block';
    wrap.style.opacity = '1';
    const labelEl = wrap.querySelector('.vontology-progress-label');
    if (labelEl && initialText) labelEl.textContent = initialText;
  } catch (_) { }
}

// Lightweight progress bar helpers (created dynamically inside the tree container)
function ensureProgressBarContainer() {
  // Ensure we have a valid container reference; re-query DOM if necessary.
  if (!elements.vontologyTreeContainer) {
    debugLog('[ensureProgressBarContainer] elements.vontologyTreeContainer missing, re-querying DOM for #vontologyTreeContainer');
    elements.vontologyTreeContainer = document.getElementById('vontologyTreeContainer');
  }

  // Deduplicate any existing progress wraps: keep the first, remove extras
  const existingWraps = Array.from(document.querySelectorAll('.vontology-progress-wrap'));
  let wrap = existingWraps.length ? existingWraps[0] : null;
  if (existingWraps.length > 1) {
    debugLog('[ensureProgressBarContainer] found multiple progress wraps; removing duplicates');
    for (let i = 1; i < existingWraps.length; i++) {
      try { existingWraps[i].remove(); } catch (_) { }
    }
  }

  if (!wrap && elements.vontologyTreeContainer) {
    wrap = elements.vontologyTreeContainer.querySelector('.vontology-progress-wrap');
  }

  // Create the wrapper if it doesn't exist
  if (!wrap) {
    debugLog('[ensureProgressBarContainer] creating progress wrap');
    wrap = document.createElement('div');
    wrap.className = 'vontology-progress-wrap';

    // Make visual layout depend on whether we have the tree container available
    if (elements.vontologyTreeContainer) {
      wrap.style.position = 'relative';
      wrap.style.width = '100%';
      wrap.style.margin = '6px 0 10px 0';
      wrap.style.padding = '6px';
      wrap.style.background = 'rgba(255,255,255,0.95)';
      wrap.style.border = '1px solid rgba(0,0,0,0.06)';
      wrap.style.boxShadow = '0 1px 4px rgba(0,0,0,0.04)';
      wrap.style.zIndex = '9999';
      wrap.style.display = 'block';
      wrap.style.opacity = '1';
    } else {
      // Fallback: fixed small bar at the top of the page so users see progress
      wrap.style.position = 'fixed';
      wrap.style.top = '6px';
      wrap.style.left = '6px';
      wrap.style.right = '6px';
      wrap.style.zIndex = '2147483647';
      wrap.style.width = 'auto';
      wrap.style.margin = '0';
      wrap.style.padding = '6px';
      wrap.style.background = 'rgba(255,255,255,0.98)';
      wrap.style.border = '1px solid rgba(0,0,0,0.08)';
      wrap.style.boxShadow = '0 2px 8px rgba(0,0,0,0.08)';
      wrap.dataset.vontologyProgressFallback = '1';
      wrap.style.display = 'block';
      wrap.style.opacity = '1';
    }

    const barOuter = document.createElement('div');
    barOuter.className = 'vontology-progress-outer';
    barOuter.style.background = '#eee';
    barOuter.style.border = '1px solid #ddd';
    barOuter.style.borderRadius = '4px';
    barOuter.style.height = '10px';
    barOuter.style.overflow = 'hidden';

    const barInner = document.createElement('div');
    barInner.className = 'vontology-progress-inner';
    barInner.style.height = '100%';
    barInner.style.minHeight = '10px';
    barInner.style.width = '0%';
    barInner.style.background = 'linear-gradient(90deg,#2b8cff,#68d1ff)';
    barInner.style.transition = 'width 240ms linear';

    const label = document.createElement('div');
    label.className = 'vontology-progress-label';
    label.style.fontSize = '0.85em';
    label.style.color = '#444';
    label.style.marginTop = '6px';
    label.style.textAlign = 'left';
    label.style.paddingLeft = '6px';

    barOuter.appendChild(barInner);
    wrap.appendChild(barOuter);
    wrap.appendChild(label);

    // Prefer inserting the progress bar immediately below the top-of-page search input (if present).
    if (elements.vontologySearchInput && elements.vontologySearchInput.parentElement) {
      try {
        elements.vontologySearchInput.insertAdjacentElement('afterend', wrap);
      } catch (e) {
        // Fallback to tree container insertion if DOM operation fails
        if (elements.vontologyTreeContainer) {
          if (elements.vontologyTreeContainer.firstChild) {
            elements.vontologyTreeContainer.insertBefore(wrap, elements.vontologyTreeContainer.firstChild);
          } else {
            elements.vontologyTreeContainer.appendChild(wrap);
          }
        } else {
          document.body.appendChild(wrap);
        }
      }
    } else if (elements.vontologyTreeContainer) {
      // If the top-of-page search input is missing, prefer the container wrapper so the bar shows above the tree.
      const parentContainer = document.querySelector('.vontology-container');
      if (parentContainer) {
        try {
          parentContainer.insertBefore(wrap, elements.vontologyTreeContainer);
          debugLog('[ensureProgressBarContainer] moved progress wrap into .vontology-container before tree');
        } catch (err) {
          if (elements.vontologyTreeContainer.firstChild) {
            elements.vontologyTreeContainer.insertBefore(wrap, elements.vontologyTreeContainer.firstChild);
          } else {
            elements.vontologyTreeContainer.appendChild(wrap);
          }
        }
      } else {
        if (elements.vontologyTreeContainer.firstChild) {
          elements.vontologyTreeContainer.insertBefore(wrap, elements.vontologyTreeContainer.firstChild);
        } else {
          elements.vontologyTreeContainer.appendChild(wrap);
        }
      }
    } else {
      // As a final fallback, ensure the wrap is attached to document.body
      if (!wrap.parentElement) document.body.appendChild(wrap);
    }
  }

  return wrap;
}

// Timing diagnostics for progress bar updates
let __vontologyProgressStart = null;   // performance.now() baseline
let __vontologyProgressLast = null;    // last performance.now()
if (typeof window !== 'undefined') {
  window.__VONTOLOGY_PROGRESS_EVENTS = window.__VONTOLOGY_PROGRESS_EVENTS || [];
  window.dumpVontologyProgressTimeline = function () { return (window.__VONTOLOGY_PROGRESS_EVENTS || []).slice(); };
}

function updateProgressBar(percent, text) {
  try {
    const wrapCandidate = ensureProgressBarContainer();
    let actualWrap = wrapCandidate || (typeof window !== 'undefined' ? window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP : null);
    if (!actualWrap) {
      showGlobalProgressOverlay();
      actualWrap = (typeof window !== 'undefined') ? window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP : null;
    }
    if (!actualWrap) return; // give up if still missing

    // If we're still using the global fixed overlay but the Vontology tab container is now present, relocate it for better visibility.
    try {
      if (actualWrap.dataset && actualWrap.dataset.vontologyProgressGlobal && !actualWrap.dataset.vontologyProgressRelocated) {
        const container = document.querySelector('.vontology-container');
        if (container) {
          // Insert just above the tree container or at top inside container.
          const treeEl = document.getElementById('vontologyTreeContainer');
          if (treeEl) {
            container.insertBefore(actualWrap, treeEl);
          } else {
            container.insertBefore(actualWrap, container.firstChild);
          }
          // Restyle from fixed overlay to inline bar
          actualWrap.style.position = 'relative';
          actualWrap.style.top = 'auto';
          actualWrap.style.left = 'auto';
          actualWrap.style.right = 'auto';
          actualWrap.style.zIndex = '10';
          actualWrap.style.margin = '0 0 8px 0';
          actualWrap.style.background = 'linear-gradient(180deg,#ffffff, #f7f9fc)';
          actualWrap.style.border = '1px solid rgba(0,0,0,0.1)';
          actualWrap.style.boxShadow = '0 1px 3px rgba(0,0,0,0.08)';
          actualWrap.dataset.vontologyProgressRelocated = '1';
        }
      }
    } catch (_) { }

    const pct = Math.max(0, Math.min(100, Math.round(percent)));
    try { actualWrap.style.display = 'block'; actualWrap.style.opacity = '1'; } catch (_) { }
    const innerEl = actualWrap.querySelector('.vontology-progress-inner');
    const labelEl = actualWrap.querySelector('.vontology-progress-label');
    if (innerEl) innerEl.style.width = pct + '%';
    if (labelEl) labelEl.textContent = text ? `${text} (${pct}%)` : `${pct}%`;

    // Timestamp diagnostics
    const nowPerf = (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now();
    if (__vontologyProgressStart === null) __vontologyProgressStart = nowPerf;
    const sinceStart = nowPerf - __vontologyProgressStart;
    const delta = (__vontologyProgressLast == null) ? 0 : (nowPerf - __vontologyProgressLast);
    __vontologyProgressLast = nowPerf;
    const sinceStartStr = sinceStart.toFixed(1);
    const deltaStr = delta.toFixed(1);
    const wallClock = new Date().toISOString();
    if (typeof window !== 'undefined') {
      try {
        window.__VONTOLOGY_PROGRESS_EVENTS.push({
          pct,
          text: text || '',
          sinceStartMs: sinceStart,
          deltaMs: delta,
          wallClock,
          perfNow: nowPerf
        });
      } catch (_) { }
    }
    // Always emit a console log for ordering diagnostics
    console.log(`[vontology-progress] t+${sinceStartStr}ms (Δ${deltaStr}ms) ${pct}%${text ? ' - ' + text : ''}`);
    debugLog(`[updateProgressBar] updated to ${pct}%${text ? ' - ' + text : ''}`);
  } catch (_) { }
}

// --- Progress enrichment state ---
// Track last concept encountered during tree rendering to enrich progress text.
let __vontologyLastConceptId = null;
let __vontologyLastConceptName = null;
let __vontologyRenderCount = 0;
let __vontologyEstimatedTotal = null; // set when treeData first available


function hideProgressBar() {
  try {
    const globalWrap = (typeof window !== 'undefined') ? window.__VONTOLOGY_GLOBAL_PROGRESS_WRAP : null;
    const wraps = Array.from(document.querySelectorAll('.vontology-progress-wrap'));
    for (const w of wraps) {
      if (globalWrap && w === globalWrap) {
        try { w.style.display = 'none'; w.style.opacity = '0'; } catch (_) { }
      } else {
        try { w.remove(); } catch (_) { }
      }
    }
    debugLog('[hideProgressBar] cleaned up progress wraps (global hidden, others removed).');
  } catch (_) { }
}

// Controls whether linear chains should be collapsed when rendering the tree
export let collapseLinearEnabled = false;

// Controls whether redundant nodes should be filtered from the tree
export let filterRedundantEnabled = true;

// Controls whether to show only key concepts
export let showOnlyKeyConceptsEnabled = false;

// IDs to force-keep visible in the rendered tree (even if filters would remove them)
const forcedVisibleIds = new Set();

// IDs marked as key concepts by the current user
const keyConceptIds = new Set();

// Export getter for key concept IDs (used by dynamicTabs)
export function getKeyConceptIds() {
  return keyConceptIds;
}

// Update tree badge for a specific concept
export function updateTreeKeyConceptBadge(conceptId, isKey) {
  try {
    // Find all tree nodes with this concept ID
    const nodes = document.querySelectorAll(`.vontology-node-name[data-id="${conceptId}"]`);
    nodes.forEach(nodeSpan => {
      const li = nodeSpan.closest('li');
      if (!li) return;

      // Remove existing badge
      const existingBadge = li.querySelector('.vontology-key-concept-badge');
      if (existingBadge) {
        existingBadge.remove();
      }

      // Add new badge if marked as key
      if (isKey) {
        const keyStar = document.createElement('span');
        keyStar.textContent = '⭐';
        keyStar.className = 'vontology-key-concept-badge';
        keyStar.title = 'Key Concept';
        keyStar.style.marginLeft = '6px';
        keyStar.style.fontSize = '0.9em';
        keyStar.style.cursor = 'help';
        // Insert after the node name span
        if (nodeSpan.nextSibling) {
          li.insertBefore(keyStar, nodeSpan.nextSibling);
        } else {
          li.appendChild(keyStar);
        }
      }
    });
  } catch (error) {
    console.error('[updateTreeKeyConceptBadge] Error:', error);
  }
}

// Update all concept tab header star buttons for a specific concept
export function updateTabHeaderStarButtons(conceptId, isKey) {
  try {
    // Find all tab content divs for this concept
    const tabs = document.querySelectorAll(`.tab-content[data-concept-id="${conceptId}"]`);
    tabs.forEach(tab => {
      const starBtn = tab.querySelector('.key-concept-star-button');
      if (starBtn) {
        starBtn.textContent = isKey ? '⭐' : '☆';
        starBtn.title = isKey ? 'Unmark as key concept' : 'Mark as key concept';
        if (isKey) {
          starBtn.classList.add('marked');
        } else {
          starBtn.classList.remove('marked');
        }
      }
    });
  } catch (error) {
    console.error('[updateTabHeaderStarButtons] Error:', error);
  }
}

// Cache for subtree entity counts (built once per tree render)
let subtreeCountMap = null; // Map<string, number>

// Track recently created nodes for a transient UI badge
const recentlyCreated = new Map(); // concept_id -> timestamp
const RECENT_BADGE_TTL_MS = 60_000; // 1 minute

function markRecentlyCreated(conceptId) {
  if (!conceptId) return;
  recentlyCreated.set(conceptId, Date.now());
}

function isRecentlyCreated(conceptId) {
  const ts = conceptId ? recentlyCreated.get(conceptId) : undefined;
  if (!ts) return false;
  const fresh = (Date.now() - ts) < RECENT_BADGE_TTL_MS;
  if (!fresh) {
    recentlyCreated.delete(conceptId);
  }
  return fresh;
}

export async function insertNodeIntoVontologyTree(parentId, createdConcept) {
  if (!createdConcept || !createdConcept.id) return;

  forcedVisibleIds.add(createdConcept.id);
  markRecentlyCreated(createdConcept.id);

  try {
    if (typeof __vontologyLatestEntityCounts === 'object' && __vontologyLatestEntityCounts) {
      __vontologyLatestEntityCounts[createdConcept.id] = { name: createdConcept.name, entity_count: 0, has_entities: false };
    }
    if (typeof __vontologyPreloadedEntityCounts === 'object' && __vontologyPreloadedEntityCounts) {
      __vontologyPreloadedEntityCounts[createdConcept.id] = { name: createdConcept.name, entity_count: 0, has_entities: false };
    }
  } catch (err) { console.error('Error updating entity counts in insertNodeIntoVontologyTree:', err); }


  if (vontologyTreeData && vontologyTreeData.tree) {
    const roots = Array.isArray(vontologyTreeData.tree) ? vontologyTreeData.tree : [vontologyTreeData.tree];
    const newNode = { id: createdConcept.id, name: createdConcept.name, mongo_id: createdConcept.mongo_id || createdConcept.id, children: [] };
    const addToParent = (nodes) => {
      for (const node of nodes) {
        if (node.id === parentId || node.mongo_id === parentId) {
          node.children = node.children || [];
          node.children.push(newNode);
          return true;
        }
        if (node.children && addToParent(node.children)) return true;
      }
      return false;
    };

    try { addToParent(roots); setVontologyTreeData(vontologyTreeData); } catch (err) { console.error('Failed to update Vontology tree data:', err); }
  }

  if (!elements.vontologyTreeContainer) {
    elements.vontologyTreeContainer = document.getElementById('vontologyTreeContainer');
  }
  if (!elements.vontologyTreeContainer) return;
  const parentSpan = elements.vontologyTreeContainer.querySelector(`[data-concept-id="${parentId}"]`);
  if (!parentSpan) return;
  const parentLi = parentSpan.closest('li');
  if (!parentLi) return;
  let childUl = parentLi.querySelector('ul');
  if (!childUl) {
    childUl = document.createElement('ul');
    childUl.style.listStyleType = 'none';
    childUl.style.margin = '0';
    childUl.style.paddingLeft = '20px';
    parentLi.appendChild(childUl);
  }
  const li = await createTreeElement({ id: createdConcept.id, name: createdConcept.name, mongo_id: createdConcept.mongo_id || createdConcept.id, children: [] }, null);
  childUl.appendChild(li);
}

export function __test_getForcedVisibleIds() {
  return forcedVisibleIds;
} // Export for testing

export function setCollapseLinearEnabled(value) {
  collapseLinearEnabled = value;
}

export function getCollapseLinearEnabled() {
  return collapseLinearEnabled;
}

export function setFilterRedundantEnabled(value) {
  filterRedundantEnabled = value;
}

export function getFilterRedundantEnabled() {
  return filterRedundantEnabled;
}

export function setShowOnlyKeyConceptsEnabled(value) {
  showOnlyKeyConceptsEnabled = value;
}

export function getShowOnlyKeyConceptsEnabled() {
  return showOnlyKeyConceptsEnabled;
}

// Replace text nodes that contain Vontology tokens with annotated fragments
function annotateTokensInContainer(container) {
  if (!container) return;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null);
  const toReplace = [];
  let node;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (!parent) continue;
    const tag = parent.tagName;
    // Skip tags where we shouldn't alter text content
    if (tag === 'A' || tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEXTAREA' || tag === 'INPUT') {
      continue;
    }
    if (node.nodeValue && node.nodeValue.includes('#V#')) {
      toReplace.push(node);
    }
  }
  for (const textNode of toReplace) {
    const frag = createAnnotatedFragment(textNode.nodeValue);
    if (textNode.parentNode) {
      textNode.parentNode.replaceChild(frag, textNode);
    }
  }
}

// When deferred entity counts arrive, update node tooltips and classes in-place
if (typeof window !== 'undefined') {
  window.addEventListener('vontologyEntityCountsLoaded', (evt) => {
    try {
      const counts = (evt && evt.detail && evt.detail.entityCounts) ? evt.detail.entityCounts : null;
      if (!counts) return;
      __vontologyLatestEntityCounts = counts;
      const td = (typeof vontologyTreeData !== 'undefined' && vontologyTreeData) ? vontologyTreeData.tree : null;
      const roots = Array.isArray(td) ? td : (td ? [td] : []);
      const map = buildSubtreeCountCache(roots, counts);
      const nodes = document.querySelectorAll('.vontology-node-name');
      nodes.forEach((span) => {
        try {
          const id = span.dataset && span.dataset.id ? span.dataset.id : null;
          if (!id) return;
          const info = counts[id] || {};
          const direct = info.entity_count || 0;
          const hasEntities = !!info.has_entities;
          const subtree = map.get(id) || 0;
          const baseLines = (span.title || '').split('\n').filter(l => l && !/Entities in subtree:/i.test(l) && !/Direct instances:/i.test(l));
          const newLines = [...baseLines, `Entities in subtree: ${subtree}`];
          if (hasEntities) newLines.push(`Direct instances: ${direct}`);
          span.title = newLines.join('\n');
          if (hasEntities) span.classList.add('vontology-node-with-instances');
          else span.classList.remove('vontology-node-with-instances');
        } catch (_) { /* continue */ }
      });
    } catch (e) {
      console.warn('[vontology] Failed to apply post-load entity counts to DOM:', e);
    }
  });
}

/**
 * Load key concepts for the current user from the backend.
 * Populates the keyConceptIds Set.
 */
export async function loadKeyConceptsForUser() {
  console.log('[loadKeyConceptsForUser] START - Clearing keyConceptIds Set');
  keyConceptIds.clear();

  try {
    // Get current user concept ID
    const userConceptId = getCurrentUserConceptId();
    console.log('[loadKeyConceptsForUser] Got user concept ID:', userConceptId);

    if (!userConceptId) {
      console.warn('[loadKeyConceptsForUser] No user concept ID found, skipping key concepts load');
      console.warn('[loadKeyConceptsForUser] localStorage von_current_user:', localStorage.getItem('von_current_user'));
      return;
    }

    const url = `/vontology/api/vontology/key-concepts?user_concept_id=${encodeURIComponent(userConceptId)}`;
    console.log('[loadKeyConceptsForUser] Fetching from:', url);
    const response = await vontologyFetch(url);
    console.log('[loadKeyConceptsForUser] Response status:', response.status, response.statusText);

    if (!response.ok) {
      console.error('[loadKeyConceptsForUser] Failed to fetch key concepts:', response.statusText);
      return;
    }

    const result = await response.json();
    console.log('[loadKeyConceptsForUser] Backend response:', result);

    if (!result.success) {
      console.error('[loadKeyConceptsForUser] Backend error:', result.error);
      return;
    }

    // Populate the Set
    if (Array.isArray(result.concept_ids)) {
      console.log('[loadKeyConceptsForUser] Adding concept IDs to Set:', result.concept_ids);
      result.concept_ids.forEach(id => keyConceptIds.add(id));
      console.log(`[loadKeyConceptsForUser] Loaded ${keyConceptIds.size} key concepts for user ${userConceptId}:`, Array.from(keyConceptIds));

      // Dispatch event so other components can refresh their UI
      if (typeof window !== 'undefined') {
        console.log('[loadKeyConceptsForUser] Dispatching keyConceptsLoaded event');
        window.dispatchEvent(new CustomEvent('keyConceptsLoaded', {
          detail: { conceptIds: Array.from(keyConceptIds) }
        }));
      }
    } else {
      console.warn('[loadKeyConceptsForUser] result.concept_ids is not an array:', result.concept_ids);
    }
  } catch (error) {
    console.error('[loadKeyConceptsForUser] Exception:', error);
  }
  console.log('[loadKeyConceptsForUser] END - keyConceptIds Set size:', keyConceptIds.size);
}

export async function fetchAndRenderVontologyTree({ forceRefresh = false } = {}) {
  console.log("[fetchAndRenderVontologyTree] Entered function.");

  if (forceRefresh) {
    retireVontologyPreload();
  }

  // Load key concepts FIRST - this needs to happen regardless of tree rendering
  // Do this early so concept tabs that open can get the correct state
  loadKeyConceptsForUser().catch(err => {
    console.error('[fetchAndRenderVontologyTree] Failed to load key concepts:', err);
  });

  // If a background preload is in-flight, await it to reuse the results and avoid double-fetch
  const progressMgr = new ProgressManager((pct, text) => updateProgressBar(pct, text));
  // Determine whether to decouple counts from initial load based on global flag set during preload
  const decoupleCounts = !!(typeof window !== 'undefined' && window.__VONTOLOGY_DECOUPLE_COUNTS__);
  if (!forceRefresh && __vontologyPreloadInFlight) {
    progressMgr.setPhase(ProgressPhase.FETCH);
    try {
      await __vontologyPreloadInFlight;
      // We consider preload equivalent to server fetch completion
      progressMgr.updateProgress(1, '(cached preload)');
    } catch (e) {
      progressMgr.updateProgress(1, '(preload failed; refetch)');
      console.warn('[fetchAndRenderVontologyTree] Preload awaited but failed, proceeding to direct fetch.', e);
    }
  }

  // Defensive check: if global element is null, try to get it directly
  if (!elements.vontologyTreeContainer) {
    console.warn("[fetchAndRenderVontologyTree] vontologyTreeContainer is null. Attempting to re-fetch from DOM.");
    elements.vontologyTreeContainer = document.getElementById("vontologyTreeContainer");
  }

  if (!elements.vontologyTreeContainer) {
    console.error("[fetchAndRenderVontologyTree] vontologyTreeContainer element still not found after re-fetch. Aborting tree rendering.");
    if (elements.selectedNodePathSpan) {
      elements.selectedNodePathSpan.textContent = "Error: Tree container UI element missing.";
    }
    return;
  }

  // Update loading message
  elements.vontologyTreeContainer.innerHTML = '<p>Fetching Vontology data structure from server...</p>';

  try {
    // NOTE: We now intentionally defer showing the progress bar until an actual network fetch begins.
    // If we end up using preloaded data, the tree should appear quickly without a distracting bar.
    // Prefer preloaded data if available, otherwise fetch now
    let treeData;
    let entityCounts;

    if (!forceRefresh && __vontologyPreloadedTreeData && (decoupleCounts || __vontologyPreloadedEntityCounts)) {
      // Fast path: use preloaded data, skip progress bar entirely (too fast to warrant one)
      treeData = __vontologyPreloadedTreeData;
      entityCounts = __vontologyPreloadedEntityCounts || {};
      __vontologyPreloadedTreeData = null;
      __vontologyPreloadedEntityCounts = null;
      // Diagnostic (commented): updateProgressBar(85, 'Using preloaded Vontology data');
      // If decoupled and counts were not preloaded, kick off deferred counts fetch now
      if (decoupleCounts && (!entityCounts || Object.keys(entityCounts).length === 0)) {
        (async () => {
          try {
            const countsResp = await vontologyFetch('/vontology/api/vontology/entity_counts');
            if (!countsResp.ok) throw new Error(`HTTP error! status: ${countsResp.status}`);
            const countsJson = await countsResp.json();
            if (countsJson && countsJson.entity_counts) {
              try { __vontologyLatestEntityCounts = countsJson.entity_counts; } catch (_) { }
              try {
                const container = elements.vontologyTreeContainer;
                if (container && container.firstChild && container.firstChild.tagName === 'UL') {
                  const evt = new CustomEvent('vontologyEntityCountsLoaded', { detail: { entityCounts: countsJson.entity_counts } });
                  window.dispatchEvent(evt);
                }
              } catch (e) { console.warn('[fetchAndRenderVontologyTree] Post-load counts integration warning (preload fast path):', e); }
            }
          } catch (e) {
            console.warn('[fetchAndRenderVontologyTree] Deferred entity counts fetch failed (preload fast path):', e);
          }
        })();
      }
    } else {
      // Start progress at the exact moment we begin network I/O and keep a heartbeat while waiting.
      const fetchStartPerf = performance.now();
      const fetchStartWall = new Date().toISOString();
      progressMgr.setPhase(ProgressPhase.FETCH);
      const heartbeatStart = Date.now();
      let heartbeat = null;
      let sawServerProgress = false;
      heartbeat = setInterval(() => {
        if (sawServerProgress) { try { clearInterval(heartbeat); } catch (_) { } heartbeat = null; return; }
        const elapsedMs = Date.now() - heartbeatStart;
        const frac = Math.min(1, elapsedMs / 8000);
        progressMgr.updateProgress(frac, `(${Math.round(elapsedMs / 1000)}s)`);
      }, 1000);

      let fetchError = null;
      let treeResponseJson = null;
      let entityCountsResponseJson = null;
      const decouple = decoupleCounts;
      try {
        const treeEndpoint = forceRefresh
          ? '/vontology/api/vontology/tree_async?refresh=1'
          : '/vontology/api/vontology/tree_async';
        const initResp = await vontologyFetch(treeEndpoint, { method: 'POST' });
        if (!initResp.ok) throw new Error(`HTTP error! status: ${initResp.status}`);
        const { job_id } = await initResp.json();

        let countsPromise = null;
        if (!decouple) {
          countsPromise = vontologyFetch('/vontology/api/vontology/entity_counts')
            .then(r => r.ok ? r.json() : Promise.reject(`HTTP error! status: ${r.status}`));
        }

        let done = false;
        while (!done) {
          const progResp = await vontologyFetch(`/vontology/api/vontology/tree_progress/${job_id}`);
          if (!progResp.ok) throw new Error(`HTTP error! status: ${progResp.status}`);
          const progJson = await progResp.json();
          const pct = (progJson.progress || 0) / 100;
          if (!sawServerProgress && (progJson.progress != null)) {
            sawServerProgress = true;
            if (heartbeat) { try { clearInterval(heartbeat); } catch (_) { } heartbeat = null; }
          }
          progressMgr.updateProgress(pct, `(${progJson.progress || 0}%)`);
          if (progJson.done) {
            if (progJson.error) throw new Error(progJson.error);
            treeResponseJson = progJson.result;
            done = true;
          } else {
            await new Promise(res => setTimeout(res, 1000));
          }
        }

        if (decouple) {
          // Kick off counts fetch asynchronously (do not await here)
          (async () => {
            try {
              const countsResp = await vontologyFetch('/vontology/api/vontology/entity_counts');
              if (!countsResp.ok) throw new Error(`HTTP error! status: ${countsResp.status}`);
              const countsJson = await countsResp.json();
              entityCountsResponseJson = countsJson;
              if (countsJson && countsJson.entity_counts) {
                try { __vontologyLatestEntityCounts = countsJson.entity_counts; } catch (_) { }
                try {
                  const container = elements.vontologyTreeContainer;
                  if (container && container.firstChild && container.firstChild.tagName === 'UL') {
                    const evt = new CustomEvent('vontologyEntityCountsLoaded', { detail: { entityCounts: countsJson.entity_counts } });
                    window.dispatchEvent(evt);
                  }
                } catch (e) { console.warn('[fetchAndRenderVontologyTree] Post-load counts integration warning:', e); }
              }
            } catch (e) {
              console.warn('[fetchAndRenderVontologyTree] Deferred entity counts fetch failed:', e);
            }
          })();
        } else if (countsPromise) {
          entityCountsResponseJson = await countsPromise;
        }
      } catch (err) {
        fetchError = err;
      }
      const fetchEndPerf = performance.now();
      clearInterval(heartbeat);
      if (fetchError) {
        updateProgressBar(100, `Fetch failed: ${fetchError}`);
        console.error('[fetchAndRenderVontologyTree] Fetch error:', fetchError);
        elements.vontologyTreeContainer.innerHTML = `<p>Error fetching Vontology data: ${fetchError}</p>`;
        hideProgressBar();
        return;
      }
      treeData = treeResponseJson;
      entityCounts = entityCountsResponseJson?.entity_counts || {};
      const elapsed = fetchEndPerf - fetchStartPerf;
      progressMgr.setPhase(ProgressPhase.PROCESS);
      try {
        // Estimate total number of concepts (nodes) for richer progress
        if (treeData && treeData.tree) {
          const stack = Array.isArray(treeData.tree) ? [...treeData.tree] : [treeData.tree];
          let count = 0;
          while (stack.length) {
            const n = stack.pop();
            if (!n) continue;
            count++;
            if (n.children && n.children.length) stack.push(...n.children);
          }
          __vontologyEstimatedTotal = count || null;
        }
      } catch (_) { }
      progressMgr.updateProgress(0.05, `(fetched in ${Math.round(elapsed)}ms${__vontologyEstimatedTotal ? '; ~' + __vontologyEstimatedTotal + ' concepts' : ''})`);
      try { localStorage.setItem('vontologyLastServerLoadMs', String(Math.round(elapsed))); } catch (_) { }
      // Frontend performance metric POST (tree load latency + node count).
      try {
        const nodeCount = __vontologyEstimatedTotal || (function computeCount(td) {
          try {
            if (!td || !td.tree) return null;
            const s = Array.isArray(td.tree) ? [...td.tree] : [td.tree];
            let c = 0; while (s.length) { const x = s.pop(); if (!x) continue; c++; if (x.children && x.children.length) s.push(...x.children); }
            return c;
          } catch { return null; }
        })(treeData);
        const cacheHit = !!(treeData && treeData._cache && treeData._cache.hit); // backend may attach cache metadata later
        const payload = { load_ms: Math.round(elapsed), node_count: nodeCount, cache_hit: cacheHit };
        vontologyFetch('/vontology/api/vontology/record_frontend_tree_load', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        }).catch(() => { });
      } catch (e) {
        // Silently ignore metric errors
      }
    }

    debugLog("[fetchAndRenderVontologyTree] Raw treeData from server:", JSON.stringify(treeData, null, 2));
    debugLog("[fetchAndRenderVontologyTree] Entity counts fetched:", Object.keys(entityCounts).length, "concepts");

    if (treeData.error) {
      console.error("[fetchAndRenderVontologyTree] Error from server:", treeData.error);
      elements.vontologyTreeContainer.innerHTML = `<p>Error loading Vontology tree: ${treeData.error}</p>`;
    } else if (treeData.tree !== undefined && treeData.tree !== null) {
      debugLog("[fetchAndRenderVontologyTree] treeData.tree structure:", JSON.stringify(treeData.tree, null, 2));

      // CHICKEN-AND-EGG FIX: Check if tree is empty array (database is empty)
      if (Array.isArray(treeData.tree) && treeData.tree.length === 0) {
        console.log("[fetchAndRenderVontologyTree] Tree is empty - auto-creating Thing root concept");

        // Show loading message
        elements.vontologyTreeContainer.innerHTML = `
          <div style="padding: 20px; text-align: center; color: #666;">
            <p style="font-size: 1.2em; margin-bottom: 10px;">🌱 <strong>Empty Vontology</strong></p>
            <p>Automatically creating "Thing" root concept...</p>
            <div style="margin-top: 15px;">
              <div style="display: inline-block; width: 200px; height: 4px; background: #eee; border-radius: 2px; overflow: hidden;">
                <div style="width: 100%; height: 100%; background: linear-gradient(90deg, #2b8cff, #68d1ff); animation: slide 1.5s infinite;"></div>
              </div>
            </div>
          </div>
        `;

        // Auto-create Thing
        try {
          const response = await vontologyFetch('/vontology/api/vontology/ensure_thing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
          });

          const result = await response.json();

          if (result.success) {
            console.log(`[fetchAndRenderVontologyTree] Thing auto-created: created=${result.thing_created}, orphans_linked=${result.orphans_linked}`);

            // Show success message briefly
            elements.vontologyTreeContainer.innerHTML = `
              <div style="padding: 20px; text-align: center; color: #2b8cff;">
                <p style="font-size: 1.2em; margin-bottom: 10px;">✅ <strong>Thing Created!</strong></p>
                <p>Root concept established. Refreshing tree...</p>
              </div>
            `;

            // Refresh the tree after a brief delay
            setTimeout(() => {
              console.log("[fetchAndRenderVontologyTree] Refreshing tree after Thing creation");
              handleRefreshTree();
            }, 800);

            hideProgressBar();
            return;
          } else {
            throw new Error(result.message || 'Failed to create Thing');
          }
        } catch (error) {
          console.error("[fetchAndRenderVontologyTree] Failed to auto-create Thing:", error);

          // Show error with manual fallback
          elements.vontologyTreeContainer.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #666;">
              <p style="font-size: 1.2em; margin-bottom: 10px; color: #ff4444;">⚠️ <strong>Auto-creation Failed</strong></p>
              <p style="color: #ff4444; margin-bottom: 15px;">${error.message}</p>
              <p>You can create the root concept manually below.</p>
            </div>
          `;

          // Enable manual creation mode as fallback
          handleNodeSelect(null, null, null, false, { mutateConceptTab: false });
          hideProgressBar();
          return;
        }
      }
      clearContainer(elements.vontologyTreeContainer);
      progressMgr.updateProgress(0.1, `${__vontologyEstimatedTotal ? '0/' + __vontologyEstimatedTotal : ''}`);
      __vontologyRenderCount = 0;
      __vontologyLastConceptName = null;

      const treeRoot = document.createElement('ul');
      treeRoot.style.listStyleType = 'none';
      treeRoot.style.paddingLeft = '0';

      // Process the tree with filters
      let processedTree = treeData.tree;

      // The two filters are mutually exclusive. Collapsing is a more advanced form of filtering.
      if (collapseLinearEnabled) {
        debugLog("[fetchAndRenderVontologyTree] Collapsing linear chains (filter redundant disabled)...");
        const parentCount = buildParentCountMap(processedTree);
        const collapsedTree = await filterLinearTreeData(processedTree, parentCount, entityCounts);
        if (collapsedTree !== null && (Array.isArray(collapsedTree) ? collapsedTree.length > 0 : true)) {
          processedTree = collapsedTree;
        } else {
          debugLog("[fetchAndRenderVontologyTree] Linear chain collapsing removed all nodes, keeping original tree");
        }
      } else if (filterRedundantEnabled) {
        // Only run this if collapsing is not enabled
        debugLog("[fetchAndRenderVontologyTree] Filtering redundant nodes (collapse linear disabled)...");
        debugLog("[fetchAndRenderVontologyTree] Entity counts available for filtering:", Object.keys(entityCounts).length, "concepts");
        const filteredTree = await filterRedundantNodes(processedTree, entityCounts, forcedVisibleIds);
        debugLog("[fetchAndRenderVontologyTree] Original tree size:", Array.isArray(processedTree) ? processedTree.length : 1);
        debugLog("[fetchAndRenderVontologyTree] Filtered tree size:", Array.isArray(filteredTree) ? filteredTree.length : (filteredTree ? 1 : 0));
        if (filteredTree !== null && (Array.isArray(filteredTree) ? filteredTree.length > 0 : true)) {
          processedTree = filteredTree;
        } else {
          debugLog("[fetchAndRenderVontologyTree] Filtering removed all nodes, keeping original tree");
        }
      } else {
        debugLog("[fetchAndRenderVontologyTree] No filtering applied - showing full tree");
      }

      // Handle array or single object tree structure
      if (Array.isArray(processedTree)) {
        // Build subtree counts cache once
        subtreeCountMap = buildSubtreeCountCache(processedTree, entityCounts);
        if (processedTree.length === 0) {
          console.warn("[fetchAndRenderVontologyTree] processedTree is an empty array. No tree to render.");
          elements.vontologyTreeContainer.innerHTML = '<p><i>No Vontology data to display (empty tree array).</i></p>';
          hideProgressBar();
          return;
        }
        debugLog("[fetchAndRenderVontologyTree] processedTree is an array, processing each item as a root.");
        for (const rootNode of processedTree) {
          if (rootNode) {
            const el = await createTreeElement(rootNode, entityCounts);
            treeRoot.appendChild(el);
          } else {
            console.warn("[fetchAndRenderVontologyTree] Encountered a null/undefined root node in the processedTree array.");
          }
        }
      } else if (typeof processedTree === 'object' && processedTree !== null) {
        // CHICKEN-AND-EGG FIX: Check for auto-injected Thing placeholder before rendering
        // The backend auto-injects a Thing node even when DB is empty (mongo_id will be "")
        const isPlaceholderTree = processedTree.id === '#V#thing' &&
          processedTree.mongo_id === '' &&
          (!processedTree.children || processedTree.children.length === 0);

        if (isPlaceholderTree) {
          console.log("[fetchAndRenderVontologyTree] Detected auto-injected Thing placeholder - auto-creating real Thing");

          // Show loading message
          elements.vontologyTreeContainer.innerHTML = `
            <div style="padding: 20px; text-align: center; color: #666;">
              <p style="font-size: 1.2em; margin-bottom: 10px;">🌱 <strong>Empty Vontology</strong></p>
              <p>Creating "Thing" root concept...</p>
              <div style="margin-top: 15px;">
                <div style="display: inline-block; width: 200px; height: 4px; background: #eee; border-radius: 2px; overflow: hidden;">
                  <div style="width: 100%; height: 100%; background: linear-gradient(90deg, #2b8cff, #68d1ff); animation: slide 1.5s infinite;"></div>
                </div>
              </div>
            </div>
          `;

          // Auto-create Thing
          try {
            const response = await vontologyFetch('/vontology/api/vontology/ensure_thing', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' }
            });

            const result = await response.json();

            if (result.success) {
              console.log(`[fetchAndRenderVontologyTree] Thing auto-created from placeholder: created=${result.thing_created}, orphans_linked=${result.orphans_linked}`);

              // Show success and refresh
              elements.vontologyTreeContainer.innerHTML = `
                <div style="padding: 20px; text-align: center; color: #2b8cff;">
                  <p style="font-size: 1.2em; margin-bottom: 10px;">✅ <strong>Thing Created!</strong></p>
                  <p>Refreshing tree...</p>
                </div>
              `;

              setTimeout(() => {
                handleRefreshTree();
              }, 800);

              hideProgressBar();
              return;
            } else {
              throw new Error(result.message || 'Failed to create Thing');
            }
          } catch (error) {
            console.error("[fetchAndRenderVontologyTree] Failed to auto-create Thing from placeholder:", error);

            // Show error with manual fallback
            elements.vontologyTreeContainer.innerHTML = `
              <div style="padding: 20px; text-align: center; color: #666;">
                <p style="font-size: 1.2em; margin-bottom: 10px; color: #ff4444;">⚠️ <strong>Auto-creation Failed</strong></p>
                <p style="color: #ff4444; margin-bottom: 15px;">${error.message}</p>
                <p>Manual creation mode enabled below.</p>
              </div>
            `;

            handleNodeSelect(null, null, null, false, { mutateConceptTab: false });
            hideProgressBar();
            return;
          }
        }

        // Build subtree counts cache once
        subtreeCountMap = buildSubtreeCountCache([processedTree], entityCounts);
        debugLog("[fetchAndRenderVontologyTree] processedTree is a single object, processing as the root.");
        treeRoot.appendChild(await createTreeElement(processedTree, entityCounts));
      } else {
        console.error("[fetchAndRenderVontologyTree] processedTree is not in an expected format. Actual type:", typeof processedTree, "Value:", processedTree);
        elements.vontologyTreeContainer.innerHTML = '<p><i>Vontology data is in an unexpected format.</i></p>';
        hideProgressBar();
        return;
      }

      elements.vontologyTreeContainer.appendChild(treeRoot);
      progressMgr.setPhase(ProgressPhase.RENDER);
      progressMgr.updateProgress(0.3);
      console.log("[fetchAndRenderVontologyTree] Vontology tree rendered.");

      // Clear cache reference after render to avoid stale state on next refresh
      // (A new render will rebuild the cache.)
      // Note: keep it available for selection highlight steps in the same tick.
      setTimeout(() => { subtreeCountMap = null; }, 0);

      // Finalize progress bar
      setTimeout(() => {
        const summaryParts = [];
        if (__vontologyRenderCount) summaryParts.push(`${__vontologyRenderCount}${__vontologyEstimatedTotal ? '/' + __vontologyEstimatedTotal : ''} concepts`);
        if (__vontologyLastConceptName) summaryParts.push(`last: ${__vontologyLastConceptName}`);
        progressMgr.complete(summaryParts.length ? '(' + summaryParts.join('; ') + ')' : '');
        hideProgressBar();
      }, 350);

      // Store tree data globally
      setVontologyTreeData(treeData);

      // Programmatically select the node corresponding to currentConceptType
      let conceptTypeToSelect = getCurrentConceptType();
      console.log(`[fetchAndRenderVontologyTree] Attempting to select initial node with currentConceptType: ${conceptTypeToSelect}`);

      if (!conceptTypeToSelect) {
        conceptTypeToSelect = defaultSelectedConceptType;
        setCurrentConceptType(conceptTypeToSelect);
        console.log(`[fetchAndRenderVontologyTree] Using default concept type: ${conceptTypeToSelect}`);
      }

      if (conceptTypeToSelect) {
        console.log(`[fetchAndRenderVontologyTree] About to select node with conceptTypeToSelect: ${conceptTypeToSelect}`);
        // Select the node in the tree but don't automatically create a concept tab
        selectVontologyNodeByIdentifier(conceptTypeToSelect, false); // Pass false to skip tab creation
      } else {
        console.warn("[fetchAndRenderVontologyTree] No concept type available for initial selection.");
      }

    } else {
      // CHICKEN-AND-EGG FIX: Empty database - show helpful message and enable root creation
      console.log("[fetchAndRenderVontologyTree] Tree is empty - enabling root concept creation mode");
      elements.vontologyTreeContainer.innerHTML = `
        <div style="padding: 20px; text-align: center; color: #666;">
          <p style="font-size: 1.2em; margin-bottom: 10px;">🌱 <strong>Empty Vontology</strong></p>
          <p>No concepts exist yet. Create your first root concept below.</p>
          <p style="font-size: 0.9em; margin-top: 10px; color: #888;">
            Tip: Start with "Thing" as the root of your ontology.
          </p>
        </div>
      `;

      // Enable empty-tree creation mode by triggering handleNodeSelect with null
      // This will show the creation panel with appropriate messaging
      handleNodeSelect(null, null, null, false, { mutateConceptTab: false });
    }
  } catch (error) {
    console.error("[fetchAndRenderVontologyTree] Error during Vontology tree fetch or render:", error);
    if (elements.vontologyTreeContainer) {
      elements.vontologyTreeContainer.innerHTML = `<p style="color: red;">Failed to load or display Vontology tree: ${error.message}. Please check the console for more details.</p>`;
    }
    if (elements.selectedNodePathSpan) {
      elements.selectedNodePathSpan.textContent = "Error loading tree.";
      elements.selectedNodePathSpan.style.color = "red";
    }
    if (elements.vontologyNodeContentDiv) {
      elements.vontologyNodeContentDiv.innerHTML = '<p style="color: red;"><i>Tree data could not be loaded.</i></p>';
    }
    // Disable buttons that depend on the tree
    if (elements.showSubtreeDetailsButton) {
      elements.showSubtreeDetailsButton.disabled = true;
    }
  }
}

// Recursive function to create HTML elements for the tree
async function createTreeElement(node, entityCounts) {
  console.log(`[createTreeElement] Creating element for Node Name: "${node.name}", ID: "${node.id}", MongoID: "${node.mongo_id}"`);

  const li = document.createElement('li');

  // Create a span for the node name, make it clickable
  const nodeNameSpan = document.createElement('span');
  nodeNameSpan.textContent = node.name;
  nodeNameSpan.style.cursor = 'pointer';
  nodeNameSpan.style.fontWeight = 'normal';
  nodeNameSpan.dataset.id = node.id; // Store the concept ID
  nodeNameSpan.dataset.mongoId = node.mongo_id; // Store the MongoDB _id
  nodeNameSpan.classList.add('vontology-node-name');
  try { if (node.id) __idToElement.set(node.id, nodeNameSpan); } catch (_) { }
  // Backwards-compatible attribute and class used by other modules
  try {
    nodeNameSpan.setAttribute('data-concept-id', node.id);
    nodeNameSpan.classList.add('vontology-node');
  } catch (e) {
    // Ignore if DOM API not available in some environments
  }

  // Add collapsed class if this is a collapsed chain
  if (node.isCollapsed) {
    nodeNameSpan.classList.add('vontology-collapsed-chain');
    // Store the chain path for potential tooltip or expansion
    nodeNameSpan.dataset.chainPath = JSON.stringify(node.chainPath);
    // Add tooltip showing the full chain
    nodeNameSpan.title = `Collapsed chain: ${node.chainPath.join(' → ')}`;
  }

  // Add tooltip for entity count
  if (entityCounts) {
    // Prefer cached subtree count if available to avoid O(n^2) recomputation
    const subtreeCount = (subtreeCountMap && node.id) ? (subtreeCountMap.get(node.id) || 0)
      : calculateSubtreeEntityCount(node, entityCounts);
    const existingTitle = nodeNameSpan.title ? `${nodeNameSpan.title}\n` : '';
    nodeNameSpan.title = `${existingTitle}Entities in subtree: ${subtreeCount}`;

    // Check if this node has direct instances and style it differently
    const nodeEntityCount = entityCounts[node.id];
    if (nodeEntityCount && nodeEntityCount.has_entities) {
      nodeNameSpan.classList.add('vontology-node-with-instances');
      const instanceTitle = `${nodeNameSpan.title}\nDirect instances: ${nodeEntityCount.entity_count}`;
      nodeNameSpan.title = instanceTitle;
    }
  }

  console.log(`[createTreeElement] Created span for "${node.name}" with data-id="${node.id}" and data-mongo-id="${node.mongo_id}"`);

  // Centralized Click Handling
  nodeNameSpan.addEventListener('click', (event) => {
    // Reset all node styles
    document.querySelectorAll('.vontology-node-name').forEach(span => {
      span.style.fontWeight = 'normal';
      span.style.backgroundColor = '';
    });

    // Highlight the clicked node
    event.target.style.fontWeight = 'bold';
    event.target.style.backgroundColor = '#e0e0e0';

    // Update Vontology panel content without mutating Concept tab state
    handleNodeSelect(node.name, node.id, node.mongo_id, /*createConceptTab*/ false, { mutateConceptTab: false });

    // Open a TYPE concept tab (in background) via global event
    if (node.id && node.name) {
      const evt = new CustomEvent('open-concept-tab', {
        detail: { conceptId: node.id, conceptName: node.name, kind: 'unknown', activate: false }
      });
      document.dispatchEvent(evt);
    }
  });

  // Add hover effects
  nodeNameSpan.addEventListener('mouseover', () => {
    if (nodeNameSpan.style.fontWeight !== 'bold') {
      nodeNameSpan.style.textDecoration = 'underline';
    }
  });

  nodeNameSpan.addEventListener('mouseout', () => {
    nodeNameSpan.style.textDecoration = 'none';
  });

  // Add context menu (right-click) functionality
  nodeNameSpan.addEventListener('contextmenu', (event) => {
    event.preventDefault(); // Prevent browser's default context menu
    showVontologyContextMenu(event, node, nodeNameSpan);
  });

  li.appendChild(nodeNameSpan);

  // Progress enrichment bookkeeping
  try {
    if (node && (node.id || node.concept_id)) {
      __vontologyLastConceptId = node.id || node.concept_id;
      __vontologyLastConceptName = node.name || node.label || null;
      __vontologyRenderCount += 1;
      // Opportunistically update progress bar label during heavy render phases every ~250 nodes
      if (__vontologyRenderCount % PROGRESS_INTERVAL === 0 && typeof updateProgressBar === 'function') {
        const pct = __vontologyEstimatedTotal
          ? Math.min(95, Math.round((__vontologyRenderCount / __vontologyEstimatedTotal) * 100))
          : 55;
        const enrich = `Processing tree data… ${__vontologyRenderCount}${__vontologyEstimatedTotal ? '/' + __vontologyEstimatedTotal : ''}` +
          (__vontologyLastConceptName ? ` (last: ${__vontologyLastConceptName})` : '');
        updateProgressBar(pct, enrich);
      }
      if (__vontologyRenderCount % RENDER_BATCH_SIZE === 0) {
        await yieldThread();
      }
    }
  } catch (_) { }

  // If this node was just created in this session, show a small badge
  if (isRecentlyCreated(node.id)) {
    const recent = document.createElement('span');
    recent.textContent = 'Created just now';
    recent.className = 'vontology-new-badge';
    recent.style.marginLeft = '8px';
    recent.style.fontSize = '0.8em';
    recent.style.color = '#2e7d32';
    recent.style.background = '#e8f5e9';
    recent.style.border = '1px solid #c8e6c9';
    recent.style.borderRadius = '10px';
    recent.style.padding = '1px 6px';
    li.appendChild(recent);
  }

  // If this node is marked as a key concept, show a star badge
  if (node.id && keyConceptIds.has(node.id)) {
    const keyStar = document.createElement('span');
    keyStar.textContent = '⭐';
    keyStar.className = 'vontology-key-concept-badge';
    keyStar.title = 'Key Concept';
    keyStar.style.marginLeft = '6px';
    keyStar.style.fontSize = '0.9em';
    keyStar.style.cursor = 'help';
    li.appendChild(keyStar);
  }

  // If the node has children, recursively create elements for them
  if (node.children && node.children.length > 0) {
    const childrenUl = document.createElement('ul');
    childrenUl.style.listStyleType = 'none';
    childrenUl.style.margin = '0';
    childrenUl.style.paddingLeft = '20px';

    for (let i = 0; i < node.children.length; i++) {
      const child = node.children[i];
      const childElement = await createTreeElement(child, entityCounts);
      childrenUl.appendChild(childElement);
      if ((i + 1) % RENDER_BATCH_SIZE === 0) {
        await yieldThread();
      }
    }

    li.appendChild(childrenUl);
  }

  return li;
}

// Calculate total entity count for a node and its subtree
function calculateSubtreeEntityCount(node, entityCounts) {
  if (!node || !entityCounts) {
    return 0;
  }

  // Get count for the current node. The backend provides 'entity_count' for the node itself.
  let totalCount = entityCounts[node.id]?.entity_count || 0;

  // Recursively add counts from children
  if (node.children && node.children.length > 0) {
    for (const child of node.children) {
      totalCount += calculateSubtreeEntityCount(child, entityCounts);
    }
  }

  return totalCount;
}

// Build a cache of subtree entity counts in a single pass to avoid repeated recomputation
function buildSubtreeCountCache(roots, entityCounts) {
  const map = new Map();
  if (!Array.isArray(roots) || !entityCounts) return map;

  const dfs = (node) => {
    if (!node) return 0;
    let total = 0;
    if (node.id && entityCounts[node.id]) {
      total += entityCounts[node.id].entity_count || 0;
    }
    if (node.children && node.children.length) {
      for (const child of node.children) {
        total += dfs(child);
      }
    }
    if (node.id) map.set(node.id, total);
    return total;
  };

  for (const root of roots) {
    dfs(root);
  }
  return map;
}

// Context menu functionality for vontology nodes
async function showVontologyContextMenu(event, node, targetElement = null) {
  console.log(`[showVontologyContextMenu] Showing context menu for node: ${node.name} (${node.id})`);

  // Store the target element for use in menu actions
  const actualTargetElement = targetElement || event.target;

  // Remove any existing context menu
  hideVontologyContextMenu();

  const contextMenu = document.createElement('div');
  contextMenu.className = 'vontology-context-menu';
  contextMenu.id = 'vontology-context-menu';

  // Add a header with node name and concept ID
  const menuHeader = document.createElement('div');
  menuHeader.className = 'vontology-context-menu-header';
  menuHeader.innerHTML = `
    <div style="font-weight: bold; padding: 8px 12px; border-bottom: 1px solid #ddd; background-color: #f5f5f5; font-size: 0.9em;">
      ${node.name}<br>
      <span style="color: #666; font-weight: normal; font-style: italic;">${node.id}</span>
    </div>
  `;
  contextMenu.appendChild(menuHeader);

  // Create a placeholder menu item while we check for children
  const expandChildrenItem = document.createElement('div');
  expandChildrenItem.className = 'vontology-context-menu-item';
  expandChildrenItem.innerHTML = `
    <span class="vontology-context-menu-icon">⏳</span>
    <span>Checking for children...</span>
  `;

  // Create menu item for showing immediate parents
  const expandParentsItem = document.createElement('div');
  expandParentsItem.className = 'vontology-context-menu-item';
  expandParentsItem.innerHTML = `
    <span class="vontology-context-menu-icon">⏳</span>
    <span>Checking for parents...</span>
  `;

  const collapseChildrenItem = document.createElement('div');
  collapseChildrenItem.className = 'vontology-context-menu-item';
  collapseChildrenItem.innerHTML = `
    <span class="vontology-context-menu-icon">👁‍🗨</span>
    <span>Hide temporary children</span>
  `;
  collapseChildrenItem.addEventListener('click', () => {
    hideVontologyContextMenu();
    hideTempChildren(node);
  });

  const collapseParentsItem = document.createElement('div');
  collapseParentsItem.className = 'vontology-context-menu-item';
  collapseParentsItem.innerHTML = `
    <span class="vontology-context-menu-icon">👁‍🗨</span>
    <span>Hide temporary parents</span>
  `;
  collapseParentsItem.addEventListener('click', () => {
    hideVontologyContextMenu();
    hideTempParents(node);
  });

  const deleteConceptItem = document.createElement('div');
  deleteConceptItem.className = 'vontology-context-menu-item';
  deleteConceptItem.innerHTML = `
    <span class="vontology-context-menu-icon">🗑️</span>
    <span>Delete Concept</span>
  `;
  deleteConceptItem.addEventListener('click', () => {
    hideVontologyContextMenu();
    handleDeleteConcept(node);
  });

  // Create toggle key concept menu item
  const isKeyConceptNow = keyConceptIds.has(node.id);
  console.log(`[showVontologyContextMenu] Checking key concept status for ${node.id}: ${isKeyConceptNow}, keyConceptIds has ${keyConceptIds.size} items:`, Array.from(keyConceptIds));
  const toggleKeyConceptItem = document.createElement('div');
  toggleKeyConceptItem.className = 'vontology-context-menu-item';
  toggleKeyConceptItem.innerHTML = `
    <span class="vontology-context-menu-icon">${isKeyConceptNow ? '⭐' : '☆'}</span>
    <span>${isKeyConceptNow ? 'Unmark' : 'Mark'} as Key Concept</span>
  `;
  toggleKeyConceptItem.addEventListener('click', async () => {
    hideVontologyContextMenu();
    await toggleKeyConceptMarking(node, actualTargetElement);
  });

  // Create show instances menu item (will be updated asynchronously)
  const showInstancesItem = document.createElement('div');
  showInstancesItem.className = 'vontology-context-menu-item';
  showInstancesItem.innerHTML = `
    <span class="vontology-context-menu-icon">⏳</span>
    <span>Checking for instances...</span>
  `;

  // Create hide instances menu item
  const hideInstancesItem = document.createElement('div');
  hideInstancesItem.className = 'vontology-context-menu-item';
  hideInstancesItem.innerHTML = `
    <span class="vontology-context-menu-icon">👁‍🗨</span>
    <span>Hide temporary instances</span>
  `;
  hideInstancesItem.addEventListener('click', () => {
    hideVontologyContextMenu();
    hideTempInstances(node, actualTargetElement);
  });

  contextMenu.appendChild(expandChildrenItem);
  contextMenu.appendChild(expandParentsItem);
  // Don't append showInstancesItem yet - will be added conditionally after async check
  contextMenu.appendChild(collapseChildrenItem);
  contextMenu.appendChild(collapseParentsItem);
  contextMenu.appendChild(hideInstancesItem);
  contextMenu.appendChild(toggleKeyConceptItem);
  contextMenu.appendChild(deleteConceptItem);

  // Position the context menu
  const x = event.pageX;
  const y = event.pageY;
  contextMenu.style.left = `${x}px`;
  contextMenu.style.top = `${y}px`;

  document.body.appendChild(contextMenu);

  // Add click listener to hide context menu when clicking elsewhere
  setTimeout(() => {
    document.addEventListener('click', hideVontologyContextMenu, { once: true });
  }, 0);

  // Now check for children asynchronously and update the menu
  try {
    console.log(`[showVontologyContextMenu] Checking children for node: ${node.id}`);
    const response = await vontologyFetch(`/vontology/api/vontology/children?node_id=${encodeURIComponent(node.id)}`);

    if (response.ok) {
      const data = await response.json();
      const hasChildren = data.children && data.children.length > 0;
      const childrenCount = data.children ? data.children.length : 0;

      console.log(`[showVontologyContextMenu] Node ${node.name} has ${childrenCount} children, data:`, data);

      // Update the menu item with the actual status
      const expandIcon = hasChildren ? '📁' : '📄';
      const expandText = hasChildren ? `Show immediate children (${childrenCount})` : 'No children available';

      expandChildrenItem.className = hasChildren ? 'vontology-context-menu-item' : 'vontology-context-menu-item disabled';
      expandChildrenItem.innerHTML = `
        <span class="vontology-context-menu-icon">${expandIcon}</span>
        <span>${expandText}</span>
      `;

      // Remove any existing click listeners and add new one if has children
      const clonedExpandItem = expandChildrenItem.cloneNode(true);
      expandChildrenItem.replaceWith(clonedExpandItem);

      if (hasChildren) {
        clonedExpandItem.addEventListener('click', () => {
          hideVontologyContextMenu();
          expandImmediateChildren(node);
        });
      }
    } else {
      console.warn(`[showVontologyContextMenu] Failed to fetch children for ${node.name}: ${response.status}`);
      expandChildrenItem.innerHTML = `
        <span class="vontology-context-menu-icon">❌</span>
        <span>Error checking children</span>
      `;
      expandChildrenItem.className = 'vontology-context-menu-item disabled';
    }
  } catch (error) {
    console.warn(`[showVontologyContextMenu] Could not check children for ${node.name}:`, error);
    expandChildrenItem.innerHTML = `
      <span class="vontology-context-menu-icon">❌</span>
      <span>Error checking children</span>
    `;
    expandChildrenItem.className = 'vontology-context-menu-item disabled';
  }

  // Check for parents asynchronously and update the parents menu item
  try {
    console.log(`[showVontologyContextMenu] Checking parents for node: ${node.id}`);
    const parentsResponse = await vontologyFetch(`/vontology/api/vontology/parents?identifier=${encodeURIComponent(node.id)}`);

    if (parentsResponse.ok) {
      const parentsData = await parentsResponse.json();
      const allParents = parentsData.parents || [];

      // Filter out parents that are already visible in the main tree (not temporary)
      const hiddenParents = allParents.filter(parent => {
        const existingNodes = document.querySelectorAll(`.vontology-node-name[data-id="${parent.id}"]`);
        // Check if any of the existing nodes are NOT temporary parents
        const hasMainTreeNode = Array.from(existingNodes).some(node => {
          return !node.closest('.vontology-temp-parent');
        });
        // A parent is "hidden" only if it has NO representation in the main tree
        return !hasMainTreeNode;
      });

      const hasHiddenParents = hiddenParents.length > 0;
      const hiddenParentsCount = hiddenParents.length;

      console.log(`[showVontologyContextMenu] Node ${node.name} has ${allParents.length} total parents, ${hiddenParentsCount} hidden parents`);

      // Update the parents menu item with the actual status
      const parentsIcon = hasHiddenParents ? '📂' : '🔝';
      const parentsText = hasHiddenParents ? `Show immediate parents (${hiddenParentsCount})` : 'All parents already visible';

      expandParentsItem.className = hasHiddenParents ? 'vontology-context-menu-item' : 'vontology-context-menu-item disabled';
      expandParentsItem.innerHTML = `
        <span class="vontology-context-menu-icon">${parentsIcon}</span>
        <span>${parentsText}</span>
      `;

      // Remove any existing click listeners and add new one if has hidden parents
      const clonedParentsItem = expandParentsItem.cloneNode(true);
      expandParentsItem.replaceWith(clonedParentsItem);

      if (hasHiddenParents) {
        clonedParentsItem.addEventListener('click', () => {
          hideVontologyContextMenu();
          expandImmediateParents(node);
        });
      }
    } else {
      console.warn(`[showVontologyContextMenu] Failed to fetch parents for ${node.name}: ${parentsResponse.status}`);
      expandParentsItem.innerHTML = `
        <span class="vontology-context-menu-icon">❌</span>
        <span>Error checking parents</span>
      `;
      expandParentsItem.className = 'vontology-context-menu-item disabled';
    }
  } catch (error) {
    console.warn(`[showVontologyContextMenu] Could not check parents for ${node.name}:`, error);
    expandParentsItem.innerHTML = `
      <span class="vontology-context-menu-icon">❌</span>
      <span>Error checking parents</span>
    `;
    expandParentsItem.className = 'vontology-context-menu-item disabled';
  }

  // Check for instances asynchronously and update the instances menu item
  try {
    console.log(`[showVontologyContextMenu] Checking instances for node: ${node.id}`);
    const instancesResponse = await vontologyFetch(`/vontology/api/vontology/instances?node_id=${encodeURIComponent(node.id)}&include_subtypes=true`);

    if (instancesResponse.ok) {
      const instancesData = await instancesResponse.json();
      const hasInstances = instancesData.instances && instancesData.instances.length > 0;
      const instancesCount = instancesData.count || 0;

      console.log(`[showVontologyContextMenu] Node ${node.name} has ${instancesCount} instances`);

      // Only add the show instances menu item if there are actually instances
      if (hasInstances) {
        const instancesIcon = '👥';
        const instancesText = `Show instances (${instancesCount})`;

        showInstancesItem.className = 'vontology-context-menu-item';
        showInstancesItem.innerHTML = `
          <span class="vontology-context-menu-icon">${instancesIcon}</span>
          <span>${instancesText}</span>
        `;

        showInstancesItem.addEventListener('click', () => {
          hideVontologyContextMenu();
          expandImmediateInstances(node, actualTargetElement);
        });

        // Insert the instances menu item after the parents item
        const parentsItem = contextMenu.children[1]; // expandParentsItem is at index 1
        contextMenu.insertBefore(showInstancesItem, parentsItem.nextSibling);
      }
      // If no instances, don't add the menu item at all
    } else {
      console.warn(`[showVontologyContextMenu] Failed to fetch instances for ${node.name}: ${instancesResponse.status}`);
      // Don't add instances menu item on error
    }
  } catch (error) {
    console.warn(`[showVontologyContextMenu] Could not check instances for ${node.name}:`, error);
    // Don't add instances menu item on error
  }
}

function hideVontologyContextMenu() {
  const existingMenu = document.getElementById('vontology-context-menu');
  if (existingMenu) {
    existingMenu.remove();
  }
}

/**
 * Toggle key concept marking for a node.
 * Updates backend, local keyConceptIds Set, and refreshes the badge.
 */
export async function toggleKeyConceptMarking(node, targetElement) {
  if (!node || !node.id) {
    console.error('[toggleKeyConceptMarking] Invalid node');
    return;
  }

  let isCurrentlyKey = false;
  try {
    // Get current user concept ID
    const userConceptId = getCurrentUserConceptId();

    if (!userConceptId) {
      alert('Cannot mark key concepts: No user ID found. Please ensure you are logged in.');
      console.error('[toggleKeyConceptMarking] No user concept ID found');
      return;
    }

    isCurrentlyKey = keyConceptIds.has(node.id);
    const action = isCurrentlyKey ? 'remove' : 'add';

    console.log(`[toggleKeyConceptMarking] ${action === 'add' ? 'Marking' : 'Unmarking'} ${node.name} (${node.id}) as key concept`);

    // Call backend
    const response = await vontologyFetch('/vontology/api/vontology/concept/key-concept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        concept_id: node.id,
        user_concept_id: userConceptId,
        action: action
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const result = await response.json();

    if (!result.success) {
      throw new Error(result.error || 'Unknown error');
    }

    // Update local state
    const isNowKey = action === 'add';
    if (isNowKey) {
      keyConceptIds.add(node.id);
      console.log(`[toggleKeyConceptMarking] Added ${node.id} to keyConceptIds`);
    } else {
      keyConceptIds.delete(node.id);
      console.log(`[toggleKeyConceptMarking] Removed ${node.id} from keyConceptIds`);
    }

    // Update tree badge (for this specific node and any duplicates)
    updateTreeKeyConceptBadge(node.id, isNowKey);

    // Update any open concept tab header star buttons
    updateTabHeaderStarButtons(node.id, isNowKey);

    console.log(`[toggleKeyConceptMarking] Successfully ${action === 'add' ? 'marked' : 'unmarked'} ${node.name} as key concept`);
  } catch (error) {
    console.error('[toggleKeyConceptMarking] Error:', error);
    alert(`Failed to ${isCurrentlyKey ? 'unmark' : 'mark'} key concept: ${error.message}`);
  }
}

async function expandImmediateChildren(node) {
  console.log(`[expandImmediateChildren] Expanding children for node: ${node.name} (${node.id})`);

  try {
    // Fetch children from the backend
    const response = await vontologyFetch(`/vontology/api/vontology/children?node_id=${encodeURIComponent(node.id)}`);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    console.log(`[expandImmediateChildren] Fetched ${data.children.length} children for ${node.name}`);

    if (data.children.length === 0) {
      console.log(`[expandImmediateChildren] No children found for ${node.name}`);
      return;
    }

    // Find the node element in the DOM
    const nodeElement = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
    if (!nodeElement) {
      console.error(`[expandImmediateChildren] Could not find DOM element for node ${node.id}`);
      return;
    }

    const parentLi = nodeElement.closest('li');

    // Check if temp children are already expanded (not regular children from main tree)
    let tempChildrenUl = parentLi.querySelector('ul.vontology-temp-children');
    if (tempChildrenUl) {
      console.log(`[expandImmediateChildren] Temp children already expanded for ${node.name}`);
      return;
    }

    // Create ul for temporary children
    tempChildrenUl = document.createElement('ul');
    tempChildrenUl.className = 'vontology-temp-children';
    tempChildrenUl.style.listStyleType = 'none';
    tempChildrenUl.style.margin = '0';
    tempChildrenUl.style.paddingLeft = '20px';

    console.log(`[expandImmediateChildren] Processing ${data.children.length} children for ${node.name}`);

    // Add each child (but only if it doesn't already exist in the tree)
    let addedCount = 0;
    data.children.forEach(child => {
      // Check if this child node already exists in the main tree
      const existingNode = document.querySelector(`.vontology-node-name[data-id="${child.id}"]`);
      if (existingNode && !existingNode.closest('.vontology-temp-children')) {
        console.log(`[expandImmediateChildren] Child ${child.name} already exists in main tree, skipping`);
        return; // Skip this child as it's already in the main tree
      }

      console.log(`[expandImmediateChildren] Adding child ${child.name} (${child.id})`);
      addedCount++;

      const childLi = document.createElement('li');
      childLi.className = 'vontology-temp-child';

      const childSpan = document.createElement('span');
      childSpan.textContent = child.name;
      childSpan.style.cursor = 'pointer';
      childSpan.style.fontWeight = 'normal';
      childSpan.dataset.id = child.id;
      childSpan.dataset.mongoId = child.mongo_id;
      childSpan.className = 'vontology-node-name';
      childSpan.title = child.description || 'No description available';
      // Add click handler for temporary children
      childSpan.addEventListener('click', (event) => {
        document.querySelectorAll('.vontology-node-name').forEach(span => {
          span.style.fontWeight = 'normal';
          span.style.backgroundColor = '';
        });

        event.target.style.fontWeight = 'bold';
        event.target.style.backgroundColor = '#e0e0e0';

        // Update Vontology panel without mutating Concept tab state
        handleNodeSelect(child.name, child.id, child.mongo_id, /*createConceptTab*/ false, { mutateConceptTab: false });

        // Open a TYPE tab for the child via global event (background)
        if (child.id && child.name) {
          const evt = new CustomEvent('open-concept-tab', {
            detail: { conceptId: child.id, conceptName: child.name, kind: 'unknown', activate: false }
          });
          document.dispatchEvent(evt);
        }
      });

      // Add context menu functionality to temporary children
      childSpan.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        showVontologyContextMenu(event, child, childSpan);
      });

      // Add hover effects
      childSpan.addEventListener('mouseover', () => {
        if (childSpan.style.fontWeight !== 'bold') {
          childSpan.style.textDecoration = 'underline';
        }
      });

      childSpan.addEventListener('mouseout', () => {
        childSpan.style.textDecoration = 'none';
      });

      childLi.appendChild(childSpan);
      tempChildrenUl.appendChild(childLi);
    });

    parentLi.appendChild(tempChildrenUl);
    console.log(`[expandImmediateChildren] Successfully added ${addedCount} new children out of ${data.children.length} total children for ${node.name}`);

  } catch (error) {
    console.error(`[expandImmediateChildren] Error expanding children for ${node.name}:`, error);
  }
}

function hideTempChildren(node) {
  console.log(`[hideTempChildren] Hiding temporary children for node: ${node.name} (${node.id})`);

  const nodeElement = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
  if (!nodeElement) {
    console.error(`[hideTempChildren] Could not find DOM element for node ${node.id}`);
    return;
  }

  const parentLi = nodeElement.closest('li');
  const tempChildrenUl = parentLi.querySelector('ul.vontology-temp-children');

  if (tempChildrenUl) {
    tempChildrenUl.remove();
    console.log(`[hideTempChildren] Removed temporary children for ${node.name}`);
  } else {
    console.log(`[hideTempChildren] No temporary children found for ${node.name}`);
  }
}

function hideTempParents(node) {
  console.log(`[hideTempParents] Hiding temporary parents for node: ${node.name} (${node.id})`);

  const nodeElement = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
  if (!nodeElement) {
    console.error(`[hideTempParents] Could not find DOM element for node ${node.id}`);
    return;
  }

  const parentLi = nodeElement.closest('li');
  const parentUl = parentLi.parentNode;

  // Find all temporary parent nodes for this specific node
  const tempParents = parentUl.querySelectorAll('li.vontology-temp-parent-for-' + node.id.replace(/[^a-zA-Z0-9]/g, '_'));

  if (tempParents.length > 0) {
    tempParents.forEach(tempParent => tempParent.remove());
    console.log(`[hideTempParents] Removed ${tempParents.length} temporary parents for ${node.name}`);
  } else {
    console.log(`[hideTempParents] No temporary parents found for ${node.name}`);
  }
}

async function expandImmediateParents(node) {
  console.log(`[expandImmediateParents] Expanding parents for node: ${node.name} (${node.id})`);

  try {
    // Fetch parents from the backend
    const response = await vontologyFetch(`/vontology/api/vontology/parents?identifier=${encodeURIComponent(node.id)}`);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    console.log(`[expandImmediateParents] Fetched ${data.parents.length} parents for ${node.name}`);
    console.log(`[expandImmediateParents] Parent data:`, data.parents);

    if (data.parents.length === 0) {
      console.log(`[expandImmediateParents] No parents found for ${node.name}`);
      return;
    }

    // Find the node element in the DOM
    const nodeElement = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
    if (!nodeElement) {
      console.error(`[expandImmediateParents] Could not find DOM element for node ${node.id}`);
      return;
    }

    const parentLi = nodeElement.closest('li');
    const parentUl = parentLi.parentNode; // The UL that contains this node

    // Check if temp parents are already expanded by looking for existing temp parent nodes
    const existingTempParents = parentUl.querySelectorAll('li.vontology-temp-parent-for-' + node.id.replace(/[^a-zA-Z0-9]/g, '_'));
    if (existingTempParents.length > 0) {
      console.log(`[expandImmediateParents] Temp parents already expanded for ${node.name}`);
      return;
    }

    console.log(`[expandImmediateParents] Processing ${data.parents.length} parents for ${node.name}`);

    // Add each parent as a sibling LI above the current node (override redundant filtering)
    let addedCount = 0;
    data.parents.forEach(parent => {
      // Map the API response fields correctly
      const parentId = parent.concept_id || parent.id;
      const parentName = parent.name;

      if (!parentId) {
        console.log(`[expandImmediateParents] Parent ${parentName} has no ID, skipping`);
        return;
      }

      // Check if this parent already exists in the main tree (not as a temporary parent)
      const existingNodes = document.querySelectorAll(`.vontology-node-name[data-id="${parentId}"]`);
      const hasMainTreeNode = Array.from(existingNodes).some(node => {
        return !node.closest('.vontology-temp-parent');
      });

      if (hasMainTreeNode) {
        console.log(`[expandImmediateParents] Parent ${parentName} already exists in main tree, skipping`);
        return; // Skip this parent as it's already in the main tree
      }

      console.log(`[expandImmediateParents] Adding parent ${parentName} (${parentId}) - not in main tree`);
      addedCount++;

      const parentItemLi = document.createElement('li');
      parentItemLi.className = 'vontology-temp-parent vontology-temp-parent-for-' + node.id.replace(/[^a-zA-Z0-9]/g, '_');
      parentItemLi.style.borderLeft = '3px solid #4CAF50';
      parentItemLi.style.paddingLeft = '8px';
      parentItemLi.style.marginBottom = '2px';
      parentItemLi.style.backgroundColor = '#f8fff8';

      const parentSpan = document.createElement('span');
      parentSpan.textContent = parentName;
      parentSpan.style.cursor = 'pointer';
      parentSpan.style.fontWeight = 'normal';
      parentSpan.style.color = '#666'; // Slightly different color to indicate temporary parent
      parentSpan.dataset.id = parentId;
      parentSpan.dataset.mongoId = parent.mongo_id || parent._id || '';
      parentSpan.className = 'vontology-node-name vontology-temp-parent-node';
      parentSpan.title = `Temporary parent: ${parent.description || 'No description available'}`;

      // Create a proper parent object for the event handlers
      const parentObj = {
        name: parentName,
        id: parentId,
        mongo_id: parent.mongo_id || parent._id || '',
        concept_id: parentId
      };

      // Add click handler for temporary parents
      parentSpan.addEventListener('click', (event) => {
        document.querySelectorAll('.vontology-node-name').forEach(span => {
          span.style.fontWeight = 'normal';
          span.style.backgroundColor = '';
        });

        event.target.style.fontWeight = 'bold';
        event.target.style.backgroundColor = '#e0e0e0';

        handleNodeSelect(parentName, parentId, parentObj.mongo_id);
      });

      // Add context menu functionality to temporary parents
      parentSpan.addEventListener('contextmenu', (event) => {
        event.preventDefault();
        showVontologyContextMenu(event, parentObj, parentSpan);
      });

      // Add hover effects
      parentSpan.addEventListener('mouseover', () => {
        if (parentSpan.style.fontWeight !== 'bold') {
          parentSpan.style.textDecoration = 'underline';
        }
      });

      parentSpan.addEventListener('mouseout', () => {
        parentSpan.style.textDecoration = 'none';
      });

      parentItemLi.appendChild(parentSpan);

      // Insert each parent LI directly before the current node's LI
      parentUl.insertBefore(parentItemLi, parentLi);
    });

    console.log(`[expandImmediateParents] Successfully added ${addedCount} parent nodes above ${node.name}`);

  } catch (error) {
    console.error(`[expandImmediateParents] Error expanding parents for ${node.name}:`, error);
  }
}

async function expandImmediateInstances(node, targetElement = null) {
  console.log(`[expandImmediateInstances] Expanding instances for node: ${node.name} (${node.id})`);

  try {
    // Fetch instances from the backend
    const response = await vontologyFetch(`/vontology/api/vontology/instances?node_id=${encodeURIComponent(node.id)}&include_subtypes=true`);
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const data = await response.json();
    console.log(`[expandImmediateInstances] Fetched ${data.instances.length} instances for ${node.name}`);

    if (data.instances.length === 0) {
      console.log(`[expandImmediateInstances] No instances found for ${node.name}`);
      return;
    }

    // Use the provided target element or fall back to searching
    let nodeElement;
    if (targetElement) {
      nodeElement = targetElement;
      console.log(`[expandImmediateInstances] Using provided target element for ${node.name}`);
    } else {
      nodeElement = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
      console.log(`[expandImmediateInstances] Searching for DOM element for ${node.name}`);
    }

    if (!nodeElement) {
      console.error(`[expandImmediateInstances] Could not find DOM element for node ${node.id}`);
      return;
    }

    const parentLi = nodeElement.closest('li');

    // Check if temp instances are already expanded
    let tempInstancesDiv = parentLi.querySelector('div.vontology-temp-instances');
    if (tempInstancesDiv) {
      console.log(`[expandImmediateInstances] Temp instances already expanded for ${node.name}`);
      return;
    }

    // Create div for temporary instances
    tempInstancesDiv = document.createElement('div');
    tempInstancesDiv.className = 'vontology-temp-instances';

    // Add header with close button
    const headerDiv = document.createElement('div');
    headerDiv.className = 'vontology-instances-header';
    headerDiv.style.display = 'flex';
    headerDiv.style.justifyContent = 'space-between';
    headerDiv.style.alignItems = 'center';
    headerDiv.style.padding = '4px 8px';
    headerDiv.style.backgroundColor = '#f0f8ff';
    headerDiv.style.borderRadius = '4px';
    headerDiv.style.marginBottom = '4px';

    const headerText = document.createElement('span');
    headerText.textContent = `Instances (${data.instances.length}):`;
    headerText.style.fontWeight = 'bold';
    headerText.style.fontSize = '0.9em';

    const closeButton = document.createElement('button');
    closeButton.innerHTML = '×';
    closeButton.title = 'Hide instances';
    closeButton.style.border = 'none';
    closeButton.style.background = 'transparent';
    closeButton.style.fontSize = '18px';
    closeButton.style.cursor = 'pointer';
    closeButton.style.padding = '0 4px';
    closeButton.style.lineHeight = '1';
    closeButton.style.color = '#666';
    closeButton.addEventListener('click', (event) => {
      event.stopPropagation();
      // Find the original node element by going back to the parent li and finding the node span
      const tempInstancesDiv = event.target.closest('.vontology-temp-instances');
      const parentLi = tempInstancesDiv.closest('li');
      const nodeElement = parentLi.querySelector('.vontology-node-name');
      hideTempInstances(node, nodeElement);
    });
    closeButton.addEventListener('mouseover', () => {
      closeButton.style.color = '#ff4444';
    });
    closeButton.addEventListener('mouseout', () => {
      closeButton.style.color = '#666';
    });

    headerDiv.appendChild(headerText);
    headerDiv.appendChild(closeButton);
    tempInstancesDiv.appendChild(headerDiv);

    // Create container for instances
    const instancesContainer = document.createElement('div');
    instancesContainer.className = 'vontology-instances-container';

    console.log(`[expandImmediateInstances] Processing ${data.instances.length} instances for ${node.name}`);

    // Add each instance
    data.instances.forEach(instance => {
      const instanceDiv = document.createElement('div');
      instanceDiv.className = 'vontology-instance-item';
      instanceDiv.textContent = instance.name;
      instanceDiv.title = `Instance: ${instance.name}${instance.notes ? '\nNotes: ' + instance.notes : ''}`;
      instanceDiv.dataset.id = instance.id;

      // Add click handler for instance selection -> open in a NEW tab (individual kind)
      instanceDiv.addEventListener('click', (event) => {
        event.stopPropagation(); // Prevent parent node selection

        // Reset all instance styles
        document.querySelectorAll('.vontology-instance-item').forEach(item => {
          item.style.fontWeight = 'normal';
          item.style.backgroundColor = '';
        });

        // Highlight the clicked instance
        instanceDiv.style.fontWeight = 'bold';
        instanceDiv.style.backgroundColor = '#e8f4f8';

        console.log(`[expandImmediateInstances] Selected instance: ${instance.name} (${instance.id})`);

        // Open a dedicated concept tab for the individual without mutating current tab
        if (instance.id && instance.name) {
          const evt = new CustomEvent('open-concept-tab', {
            detail: { conceptId: instance.id, conceptName: instance.name, kind: 'individual', activate: true }
          });
          document.dispatchEvent(evt);
        }
      });

      instancesContainer.appendChild(instanceDiv);
    });

    tempInstancesDiv.appendChild(instancesContainer);

    // Insert instances directly after the node span, before any child <ul> elements
    const childUl = parentLi.querySelector('ul');
    if (childUl) {
      // Insert before the child ul (subtypes)
      parentLi.insertBefore(tempInstancesDiv, childUl);
    } else {
      // No child ul, append at the end
      parentLi.appendChild(tempInstancesDiv);
    }

    console.log(`[expandImmediateInstances] Added ${data.instances.length} instances for ${node.name}`);

  } catch (error) {
    console.error(`[expandImmediateInstances] Error expanding instances for ${node.name}:`, error);
  }
}

function hideTempInstances(node, targetElement = null) {
  console.log(`[hideTempInstances] Hiding temporary instances for node: ${node.name} (${node.id})`);

  // Use the provided target element or fall back to searching
  let nodeElement;
  if (targetElement) {
    nodeElement = targetElement;
    console.log(`[hideTempInstances] Using provided target element for ${node.name}`);
  } else {
    nodeElement = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
    console.log(`[hideTempInstances] Searching for DOM element for ${node.name}`);
  }

  if (!nodeElement) {
    console.error(`[hideTempInstances] Could not find DOM element for node ${node.id}`);
    return;
  }

  const parentLi = nodeElement.closest('li');
  const tempInstancesDiv = parentLi.querySelector('div.vontology-temp-instances');

  if (tempInstancesDiv) {
    tempInstancesDiv.remove();
    console.log(`[hideTempInstances] Removed temporary instances for ${node.name}`);
  } else {
    console.log(`[hideTempInstances] No temporary instances found for ${node.name}`);
  }
}

async function handleDeleteConcept(node) {
  console.log(`[handleDeleteConcept] Attempting to delete node: ${node.name} (${node.id})`);

  if (!confirm(`Are you sure you want to delete the concept "${node.name}"? This action cannot be undone.`)) {
    console.log("[handleDeleteConcept] Deletion cancelled by user.");
    return;
  }

  try {
    const url = `/vontology/api/vontology/node?concept_id=${encodeURIComponent(node.id)}&simulate=0`;
    const response = await vontologyFetch(url, { method: 'DELETE' });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.success) {
      throw new Error(data.error || data.message || `HTTP ${response.status}`);
    }
    alert(`Concept \"${node.name}\" deleted successfully.`);
    console.log(`[handleDeleteConcept] Successfully deleted node: ${node.id}`);
    // Dispatch global deletion event
    try {
      const evt = new CustomEvent('concept-deleted', { detail: { conceptId: node.id, correlation_id: data.correlation_id } });
      document.dispatchEvent(evt);
    } catch (_) { /* no-op */ }
    // Refresh the tree to reflect the deletion
    fetchAndRenderVontologyTree();
  } catch (error) {
    console.error(`[handleDeleteConcept] Error deleting node ${node.id}:`, error);
    alert(`Failed to delete concept: ${error.message}`);
  }
}

// Function to handle node selection
// options: { mutateConceptTab?: boolean }
export async function handleNodeSelect(nodeName, nodeId, mongoId, createConceptTab = true, options = {}) {
  console.log(`[handleNodeSelect] Node selected: Name='${nodeName}', ID='${nodeId}', MongoID='${mongoId}', createConceptTab='${createConceptTab}'`);
  const mutateConceptTab = options.mutateConceptTab !== false; // default true

  // CHICKEN-AND-EGG FIX: Check if this is empty tree mode (all params null)
  const isEmptyTreeMode = !nodeName && !nodeId && !mongoId;

  // Prefer the semantic concept_id (nodeId, e.g. '#V#person') for API calls
  // since backend endpoints expect concept identifiers; fall back to mongoId
  // only if the concept_id is missing.
  const identifier = nodeId || mongoId;
  debugLog(`[handleNodeSelect] Using identifier for API calls: ${identifier} (nodeId=${nodeId}, mongoId=${mongoId}, emptyTreeMode=${isEmptyTreeMode})`);

  // 1. Update UI for selected node path display
  if (elements.selectedNodePathSpan) {
    if (isEmptyTreeMode) {
      // Empty tree mode - show special message
      elements.selectedNodePathSpan.textContent = 'Empty ontology - create root concept below';
      elements.selectedNodePathSpan.style.fontStyle = 'italic';
      elements.selectedNodePathSpan.style.color = '#888';
    } else {
      // If an Individual tab is active, prefer singular; otherwise types may display plurals elsewhere.
      // Here we keep the node label itself as provided (nodeName), and include the ID.
      renderSelectedNodePathContent(
        elements.selectedNodePathSpan,
        { name: nodeName, conceptId: nodeId },
        []
      );
      elements.selectedNodePathSpan.style.fontStyle = 'normal';
      elements.selectedNodePathSpan.style.color = '';
    }
    // Add/Update forced-visible badge
    const existingBadge = elements.selectedNodePathSpan.querySelector('.forced-visible-badge');
    // Determine if current node is visible only due to temporary reveal or forced filter state
    let isForced = !!(nodeId && forcedVisibleIds.has(nodeId));
    if (!isForced && nodeId) {
      const span = document.querySelector(`.vontology-node-name[data-id="${nodeId}"]`);
      if (span) {
        const li = span.closest('li');
        if (li && li.classList.contains('vontology-temp')) {
          isForced = true;
        }
      }
    }
    if (isForced) {
      if (!existingBadge) {
        const badge = document.createElement('span');
        badge.className = 'forced-visible-badge';
        badge.title = 'Kept visible despite filtering';
        badge.textContent = 'forced visible';
        badge.style.marginLeft = '8px';
        elements.selectedNodePathSpan.appendChild(badge);
      }
    } else if (existingBadge) {
      existingBadge.remove();
    }
    if (identifier) {
      updateParentDisplay(identifier, nodeName, nodeId); // Pass additional parameters for enhanced display
    }
    // Update dynamic labels for create buttons based on current selection
    if (elements.createTypeButton) {
      elements.createTypeButton.textContent = `Create new type of ${nodeName}`;
    }
    if (elements.createInstanceButton) {
      elements.createInstanceButton.textContent = `Create new instance of ${nodeName}`;
    }
  } else {
    console.warn("[handleNodeSelect] selectedNodePathSpan element not found.");
  }

  // 2. Store the Vontology state
  setCurrentVontologyNodeId(identifier);
  console.log(`[handleNodeSelect] currentVontologyNodeId (for API calls) updated to: ${identifier}`);

  // Always persist the selected TYPE concept_id for Vontology actions
  if (nodeId && typeof nodeId === 'string' && nodeId.startsWith('#V#')) {
    setSelectedVontologyConceptId(nodeId);
  }

  // Update the global concept type for the Concept tab unless suppressed
  if (mutateConceptTab) {
    // ALWAYS use the semantic concept_id (nodeId) for the concept type.
    setCurrentConceptType(nodeId);
    console.log(`[handleNodeSelect] currentConceptType updated to: ${nodeId}`);
  } else {
    console.log(`[handleNodeSelect] Skipping currentConceptType mutation per options`);
  }

  // 3. Enable/Disable the "Show Subtree Details" button
  if (elements.showSubtreeDetailsButton) {
    elements.showSubtreeDetailsButton.disabled = !nodeId;
  } else {
    console.warn("handleNodeSelect: showSubtreeDetailsButton element not found.");
  }

  // 4. Reset the subtree details display area
  if (elements.subtreeDetailsDisplay) {
    elements.subtreeDetailsDisplay.textContent = 'Select a node and click "Show Subtree Details".';
  }
  if (elements.subtreeDetailsStatus) {
    elements.subtreeDetailsStatus.textContent = '';
  }
  if (elements.subtreeDetailsContainer) {
    // Hide the subtree details area until user explicitly opens it with the button
    elements.subtreeDetailsContainer.classList.add('hidden');
    // Clear any conflicting inline style from older logic
    elements.subtreeDetailsContainer.style.removeProperty('display');
  }

  // 5. Fetch and display the main content for the selected node
  if (elements.vontologyNodeContentDiv) {
    if (identifier) {
      fetchVontologyContent(identifier, elements.vontologyNodeContentDiv);
      elements.vontologyNodeActions.style.display = 'block';
    } else {
      elements.vontologyNodeContentDiv.innerHTML = '<p><i>Select a node to view its content.</i></p>';
      elements.vontologyNodeActions.style.display = 'none';
    }
  } else {
    console.warn("handleNodeSelect: vontologyNodeContentDiv element not found.");
    // Try to find it directly as a fallback
    const contentDiv = document.getElementById('vontologyNodeContent');
    if (contentDiv) {
      console.log("handleNodeSelect: Found vontologyNodeContent directly, updating elements");
      elements.vontologyNodeContentDiv = contentDiv;
      if (identifier) {
        fetchVontologyContent(identifier, elements.vontologyNodeContentDiv);
      } else {
        elements.vontologyNodeContentDiv.innerHTML = '<p><i>Select a node to view its content.</i></p>';
      }
    } else {
      console.error("handleNodeSelect: vontologyNodeContent element not found even with direct lookup");
    }
  }

  // 6. Handle the "Create Concept" section visibility and state
  if (elements.createConceptDiv) {
    if (isEmptyTreeMode) {
      // CHICKEN-AND-EGG FIX: Empty tree mode - show creation UI with special instructions
      elements.createConceptDiv.classList.remove('hidden');
      elements.createConceptDiv.style.removeProperty('display');
      if (elements.createConceptStatusP) {
        elements.createConceptStatusP.textContent = 'Create root concept (typically "Thing"):';
        elements.createConceptStatusP.style.fontWeight = 'bold';
        elements.createConceptStatusP.style.color = '#2b8cff';
      }
      // Enable creation UI
      if (elements.newConceptNameInput) {
        elements.newConceptNameInput.value = '';
        elements.newConceptNameInput.disabled = false;
        elements.newConceptNameInput.placeholder = 'e.g., Thing';
      }
      // Only show Create Type button in root mode (no parent)
      if (elements.createTypeButton) {
        elements.createTypeButton.disabled = false;
        elements.createTypeButton.textContent = 'Create Root Concept';
        elements.createTypeButton.style.display = '';
      }
      // Hide instance button in root mode (can't create instance without types)
      if (elements.createInstanceButton) {
        elements.createInstanceButton.style.display = 'none';
      }
    } else if (nodeId) {
      elements.createConceptDiv.classList.remove('hidden');
      // Clear any conflicting inline style from older logic
      elements.createConceptDiv.style.removeProperty('display');
      if (elements.createConceptStatusP) {
        elements.createConceptStatusP.textContent = `Create new concept under: ${nodeName}`;
        elements.createConceptStatusP.style.fontWeight = 'normal';
        elements.createConceptStatusP.style.color = '';
      }
      // Show both buttons in normal mode
      if (elements.createTypeButton) {
        elements.createTypeButton.style.display = '';
      }
      if (elements.createInstanceButton) {
        elements.createInstanceButton.style.display = '';
      }
    } else {
      elements.createConceptDiv.classList.add('hidden');
      // Clear any conflicting inline style from older logic
      elements.createConceptDiv.style.removeProperty('display');
      if (elements.createConceptStatusP) {
        elements.createConceptStatusP.textContent = '';
      }
    }
    if (elements.newConceptNameInput && !isEmptyTreeMode) {
      elements.newConceptNameInput.value = '';
    }

    // Disable create actions if no type is selected (unless empty tree mode)
    const disabled = !nodeId && !isEmptyTreeMode;
    if (elements.createTypeButton) elements.createTypeButton.disabled = disabled;
    if (elements.createInstanceButton) elements.createInstanceButton.disabled = disabled;
    if (elements.newConceptNameInput) elements.newConceptNameInput.disabled = disabled;
  }

  // 7. End any active interaction and reset concept tab when type changes
  if (currentInteractionId) {
    console.log(`[handleNodeSelect] Entity type changed from previous interaction. Ending active interaction: ${currentInteractionId}`);
    currentInteractionId = null;
  }

  // 8. Update the Concept tab UI based on the selected Vontology type (only if concept tab is loaded)
  if (mutateConceptTab && document.getElementById('conceptListUl')) {
    console.log(`[handleNodeSelect] Concept tab is loaded, updating concept tab UI for ${nodeId}`);
    // Reset the concept tab to clear any previous concept data and interaction UI
    resetConceptTab();
    updateConceptTabUI();
    fetchConceptList();
  } else {
    console.log(`[handleNodeSelect] Concept tab not updated (either not loaded or suppressed)`);
  }

  // 9. JVNAUTOSCI-322: Create or activate dynamic concept tab for the selected vontology node (only if createConceptTab is true)
  if (createConceptTab && nodeId && nodeName) {
    console.log(`[handleNodeSelect] Creating dynamic concept tab (no auto-activate) for: ${nodeId} (${nodeName})`);
    // Add the tab if missing, but do not switch focus to it
    handleVontologyNodeSelection(nodeId, nodeName, /*activate*/ false);
  } else if (!createConceptTab) {
    console.log(`[handleNodeSelect] Skipping dynamic concept tab creation (createConceptTab=false)`);
  }
}

async function handleSaveDescription() {
  const identifier = currentVontologyNodeId;
  const newDescription = elements.descriptionTextarea.value;

  if (!identifier) {
    alert("Please select a node first.");
    return;
  }

  try {
    const res = await vontologyFetch('/vontology/api/vontology/update_description', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        identifier: identifier,
        description: newDescription
      })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const response = await res.json();

    if (response.success) {
      alert("Description updated successfully!");
      elements.editDescriptionContainer.style.display = 'none';
      fetchVontologyContent(identifier, elements.vontologyNodeContentDiv);
    } else {
      throw new Error(response.error || "Unknown error occurred.");
    }
  } catch (error) {
    console.error('Error updating description:', error);
    alert(`Error updating description: ${error.message}`);
  }
}

// New function to fetch and display the parent list
async function updateParentDisplay(identifier, nodeName = null, nodeId = null) {
  if (!elements.selectedNodePathSpan) return;

  try {
    const response = await vontologyFetch(`/vontology/api/vontology/parents?identifier=${encodeURIComponent(identifier)}`);
    if (!response.ok) {
      console.warn(`Could not fetch parents for ${identifier}. Status: ${response.status}`);
      return;
    }
    const data = await response.json();
    if (data.error) {
      console.warn(`API error fetching parents: ${data.error}`);
      return;
    }

    // Use provided nodeName and nodeId if available, otherwise fall back to API data
    const displayNodeName = nodeName || data.node.name;
    const displayNodeId = nodeId || data?.node?.concept_id || data?.node?.id;
    renderSelectedNodePathContent(
      elements.selectedNodePathSpan,
      { name: displayNodeName, conceptId: displayNodeId },
      Array.isArray(data.parents) ? data.parents.map((parent) => ({
        name: parent?.name,
        conceptId: parent?.concept_id || parent?.id
      })) : []
    );
    elements.selectedNodePathSpan.title = `Identifier: ${identifier}`;

  } catch (error) {
    console.error(`Error in updateParentDisplay:`, error);
  }
}

function normaliseSelectedNodePathConceptId(conceptId) {
  const raw = String(conceptId ?? '').trim();
  if (!raw) return '';

  const normalised = normalisePotentialConceptId(raw);
  if (normalised) {
    return normalised;
  }

  if (/^[A-Za-z0-9_./:–—-]+$/.test(raw)) {
    return `#V#${raw}`;
  }

  return '';
}

function formatSelectedNodePathLabel(name, conceptId) {
  const displayName = String(name ?? '').trim();
  if (displayName) {
    return displayName;
  }

  const rawConceptId = String(conceptId ?? '').trim();
  if (!rawConceptId) {
    return 'Unknown concept';
  }

  return rawConceptId.replace(/^#V#/, '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim() || rawConceptId;
}

function createSelectedNodePathReference(entry) {
  const name = formatSelectedNodePathLabel(entry?.name, entry?.conceptId);
  const conceptId = normaliseSelectedNodePathConceptId(entry?.conceptId);
  if (!conceptId) {
    const fallback = String(entry?.conceptId ?? '').trim();
    return document.createTextNode(fallback && fallback !== name ? `${name} (${fallback})` : name);
  }

  const cartouche = createVontologyCartouche(conceptId, {
    name,
    title: conceptId
  });
  cartouche.classList.add('cartouche-hide-kind');
  return cartouche;
}

function renderSelectedNodePathContent(target, selectedNode, parents = []) {
  if (!target) return;

  const preservedBadges = Array.from(target.querySelectorAll('.forced-visible-badge, .predicate-badge'))
    .map((badge) => badge.cloneNode(true));

  target.textContent = '';
  target.appendChild(createSelectedNodePathReference(selectedNode));

  if (Array.isArray(parents) && parents.length > 0) {
    target.appendChild(document.createTextNode('. Parents: '));
    parents.forEach((parent, index) => {
      if (index > 0) {
        target.appendChild(document.createTextNode(', '));
      }
      target.appendChild(createSelectedNodePathReference(parent));
    });
  }

  preservedBadges.forEach((badge) => {
    target.appendChild(badge);
  });
}

// Function to fetch the main content of a node
async function fetchVontologyContent(identifier, containerElement) {
  console.log(`[fetchVontologyContent] Starting fetch for identifier: ${identifier}`);
  console.log(`[fetchVontologyContent] Container element:`, containerElement);

  if (!identifier) {
    console.warn("[fetchVontologyContent] Identifier is null or undefined. Aborting fetch.");
    containerElement.innerHTML = '<p><i>Error: Identifier not available for content fetching.</i></p>';
    return;
  }

  containerElement.innerHTML = '<p><i>Loading content...</i></p>';

  const url = `/vontology/api/vontology/node_content?identifier=${encodeURIComponent(identifier)}`;
  console.log(`[fetchVontologyContent] Fetching from URL: ${url}`);

  try {
    const response = await vontologyFetch(url);
    console.log(`[fetchVontologyContent] Response status: ${response.status}`);

    if (!response.ok) {
      let errorMsg = `HTTP error! status: ${response.status}`;
      try {
        const errorData = await response.json();
        console.log(`[fetchVontologyContent] Error response data:`, errorData);
        errorMsg = errorData.error || errorMsg;
      } catch (e) {
        // If response is not JSON, use the status message
        console.log(`[fetchVontologyContent] Could not parse error response as JSON`);
      }
      throw new Error(errorMsg);
    }

    const data = await response.json();
    console.log(`[fetchVontologyContent] Response data:`, data);

    // Helper: load raw hasDescription text (authoritative, newline-preserving)
    const loadDescriptionIntoTextarea = async (conceptId) => {
      try {
        if (!(elements && elements.descriptionTextarea)) return;
        if (!conceptId) {
          elements.descriptionTextarea.value = '';
          return;
        }
        const encodedId = encodeURIComponent(conceptId);
        const descResp = await vontologyFetch(`/api/concepts/${encodedId}/texts?predicate=hasDescription&limit=1`);
        if (!descResp.ok) {
          // Do not overwrite with lossy fallbacks.
          elements.descriptionTextarea.value = '';
          return;
        }
        const descData = await descResp.json().catch(() => ({}));
        const text = (descData && Array.isArray(descData.texts) && descData.texts.length)
          ? (descData.texts[0]?.text || '')
          : '';
        elements.descriptionTextarea.value = text;
      } catch (e) {
        try {
          if (elements && elements.descriptionTextarea) elements.descriptionTextarea.value = '';
        } catch (_) { }
      }
    };

    if (data.error) {
      console.log(`[fetchVontologyContent] API returned error: ${data.error}`);
      containerElement.innerHTML = `<p style="color: red;">Error: ${data.error}</p>`;
    } else if (data.content_html !== undefined) {
      console.log(`[fetchVontologyContent] Setting content HTML, length: ${data.content_html.length}`);
      containerElement.innerHTML = data.content_html;

      // Check if this concept is a predicate and add badge if so (using backend's kind)
      if (data.kind === 'predicate' && data.raw_doc) {
        const predicateType = getPredicateType(data.raw_doc);
        console.log(`[fetchVontologyContent] Detected predicate type: ${predicateType}`);

        // Add predicate badge to the selected node display
        if (elements.selectedNodePathSpan) {
          // Remove any existing predicate badge
          const existingPredicateBadge = elements.selectedNodePathSpan.querySelector('.predicate-badge');
          if (existingPredicateBadge) {
            existingPredicateBadge.remove();
          }

          // Add new predicate badge
          const predicateBadge = createPredicateBadge(predicateType);
          elements.selectedNodePathSpan.appendChild(predicateBadge);
        }
      }

      // Post-process to annotate any Vontology tokens inside text nodes
      annotateTokensInContainer(containerElement);
      // If this is our hidden placeholder, reveal it now that we have content.
      try {
        if (containerElement && containerElement.style && containerElement.style.display === 'none') {
          containerElement.style.display = 'block';
          containerElement.classList.remove('vontology-node-content-placeholder');
          console.log('[fetchVontologyContent] Revealed placeholder content container.');
        }
      } catch (_) { }
      // Safely set the description textarea if it exists (placeholder/tab may not be mounted yet)
      try {
        if (elements && elements.descriptionTextarea) {
          // node_content intentionally suppresses top-level `description`; fetch raw text separately.
          const conceptId = data.concept_id || (data.raw_doc && data.raw_doc.concept_id) || identifier;
          await loadDescriptionIntoTextarea(conceptId);
        } else {
          console.log('[fetchVontologyContent] descriptionTextarea not available yet; skipping setting its value.');
        }
      } catch (e) {
        console.warn('[fetchVontologyContent] Failed to set description textarea value:', e);
      }
    } else {
      console.log(`[fetchVontologyContent] No content_html in response, available keys:`, Object.keys(data));
      containerElement.innerHTML = '<p><i>No content available for this node.</i></p>';
    }
  } catch (error) {
    console.error('[fetchVontologyContent] Error fetching node content:', error);
    containerElement.innerHTML = `<p style="color: red;">Failed to load content: ${error.message}</p>`;
  }
}

// Function to programmatically select a Vontology node
export async function selectVontologyNodeByIdentifier(identifier, createConceptTab = true) {
  const __perfStartSel = (typeof window !== 'undefined' && window.performance ? performance.now() : Date.now());
  console.log(`[selectVontologyNodeByIdentifier] Attempting to select node with identifier: ${identifier}, createConceptTab: ${createConceptTab}`);
  let nodeElement = __idToElement.get(identifier);

  // Try to find by mongo_id, then concept_id, then path
  if (identifier) {
    if (!nodeElement) nodeElement = document.querySelector(`.vontology-node-name[data-mongo-id="${identifier}"]`);
    if (!nodeElement) nodeElement = document.querySelector(`.vontology-node-name[data-id="${identifier}"]`);
    if (!nodeElement) nodeElement = document.querySelector(`.vontology-node-name[data-path="${identifier}"]`);
    // If not found and looks like a bare concept id without prefix, try with '#V#'
    if (!nodeElement && typeof identifier === 'string' && !identifier.startsWith('#V#')) {
      const prefixed = `#V#${identifier}`;
      nodeElement = __idToElement.get(prefixed) || document.querySelector(`.vontology-node-name[data-id="${prefixed}"]`) ||
        document.querySelector(`.vontology-node-name[data-path="${prefixed}"]`);
    }
  }

  if (nodeElement) {
    console.log(`selectVontologyNodeByIdentifier: Found node for identifier: ${identifier}`);
    // Selection is already visible in current render; clear any temporary reveals and stale forced state
    clearTemporaryReveals();
    if (forcedVisibleIds.size) {
      forcedVisibleIds.clear();
    }

    // Get node data
    const nodeId = nodeElement.dataset.id;
    const nodeName = nodeElement.textContent;
    const mongoId = nodeElement.dataset.mongoId;

    handleNodeSelect(nodeName, nodeId, mongoId, createConceptTab);
    highlightSelectedNode(nodeElement);

    // Expand all parent nodes to make the selected node visible
    expandParentNodes(nodeElement);

  } else {
    console.warn(`selectVontologyNodeByIdentifier: Node not found in DOM for identifier: ${identifier}. Trying raw tree fallback...`);

    // Fallback: resolve from raw tree data (unfiltered), then ensure it is visible by incrementally revealing its path
    const resolved = resolveNodeFromRawTree(identifier);
    if (resolved) {
      const { name, id, mongo_id } = resolved;
      console.log(`[selectVontologyNodeByIdentifier] Resolved from raw tree: ${name} (${id})`);
      // Incrementally reveal the path in the current DOM
      await ensurePathVisibleById(id);
      // After reveal, find the element and proceed with normal selection visuals
      const el = document.querySelector(`.vontology-node-name[data-id="${id}"]`);
      if (el) {
        const nodeName = el.textContent;
        const mongoId = el.dataset.mongoId;
        handleNodeSelect(nodeName, id, mongoId || mongo_id, createConceptTab);
        highlightSelectedNode(el);
        expandParentNodes(el);
      } else {
        // As a final fallback, proceed without DOM element
        handleNodeSelect(name, id, mongo_id, createConceptTab);
      }
      return;
    }

    console.debug(`selectVontologyNodeByIdentifier: Raw tree fallback failed for ${identifier}. Trying server lookup via node_content.`);
    // Server-side lookup fallback: ask the backend for node content which resolves concept_id/_id/path
    try {
      const resp = await vontologyFetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(identifier)}`);
      if (resp.ok) {
        const nodeData = await resp.json();
        if (nodeData && !nodeData.error) {
          const resolvedId = nodeData.concept_id || nodeData.concept_id || nodeData.path || null;
          const resolvedName = nodeData.display_name || nodeData.name || nodeData.node?.display_name || nodeData.node?.name || nodeData.path || String(identifier);
          const mongoId = nodeData.raw_doc && (nodeData.raw_doc._id || nodeData.raw_doc.id) ? (nodeData.raw_doc._id || nodeData.raw_doc.id) : null;

          if (resolvedId) {
            // Try to find element now that we have canonical concept_id
            const el = document.querySelector(`.vontology-node-name[data-id="${resolvedId}"]`) || document.querySelector(`.vontology-node-name[data-mongo-id="${mongoId}"]`);
            if (el) {
              handleNodeSelect(el.textContent, resolvedId, el.dataset.mongoId || mongoId, createConceptTab);
              highlightSelectedNode(el);
              expandParentNodes(el);
              return;
            }

            // If no DOM element exists for the resolved node, try to fetch its parents to decide where to insert a temporary node
            try {
              const parentsResp = await vontologyFetch(`/vontology/api/vontology/parents?identifier=${encodeURIComponent(resolvedId)}`);
              if (parentsResp.ok) {
                const parentsData = await parentsResp.json();
                const parents = parentsData.parents || [];
                // Find first parent that exists in the DOM
                let parentEl = null;
                let parentIdFound = null;
                for (const p of parents) {
                  const pid = p.concept_id || p.id;
                  const candidate = document.querySelector(`.vontology-node-name[data-id="${pid}"]`);
                  if (candidate) {
                    parentEl = candidate;
                    parentIdFound = pid;
                    break;
                  }
                }

                // Create a temp node under the parent if found, otherwise append to root
                const rootUl = elements.vontologyTreeContainer?.querySelector('ul') || elements.vontologyTreeContainer;
                if (parentEl) {
                  const parentLi = parentEl.closest('li');
                  let childUl = parentLi.querySelector(':scope > ul');
                  if (!childUl) {
                    childUl = document.createElement('ul');
                    childUl.style.listStyleType = 'none';
                    childUl.style.margin = '0';
                    childUl.style.paddingLeft = '20px';
                    parentLi.appendChild(childUl);
                  }
                  const tempLi = await createTreeElement({ id: resolvedId, name: resolvedName, mongo_id: mongoId, children: [] }, null);
                  tempLi.classList.add('vontology-temp');
                  childUl.appendChild(tempLi);
                } else if (rootUl) {
                  const tempLi = await createTreeElement({ id: resolvedId, name: resolvedName, mongo_id: mongoId, children: [] }, null);
                  tempLi.classList.add('vontology-temp');
                  // If rootUl is the container root element, append appropriately
                  if (rootUl.tagName === 'UL') rootUl.appendChild(tempLi);
                  else rootUl.appendChild(tempLi);
                }

                // After inserting temp node, try to find and select it
                const newEl = document.querySelector(`.vontology-node-name[data-id="${resolvedId}"]`);
                if (newEl) {
                  handleNodeSelect(newEl.textContent, resolvedId, newEl.dataset.mongoId || mongoId, createConceptTab);
                  highlightSelectedNode(newEl);
                  expandParentNodes(newEl);
                  return;
                }
              } else {
                console.debug(`selectVontologyNodeByIdentifier: parents lookup failed for ${resolvedId}: ${parentsResp.status}`);
              }
            } catch (pe) {
              console.warn(`selectVontologyNodeByIdentifier: error fetching parents for ${resolvedId}:`, pe);
            }
            // If parents lookup didn't help, select without DOM element
            handleNodeSelect(resolvedName, resolvedId, mongoId, createConceptTab);
            return;
          }
        } else {
          console.debug(`selectVontologyNodeByIdentifier: node_content returned error or empty for ${identifier}`, nodeData);
        }
      } else {
        console.debug(`selectVontologyNodeByIdentifier: node_content HTTP ${resp.status} for ${identifier}`);
      }
    } catch (e) {
      console.warn(`selectVontologyNodeByIdentifier: server lookup failed for ${identifier}:`, e);
    }

    console.warn(`selectVontologyNodeByIdentifier: Unable to resolve identifier from raw tree or server: ${identifier}`);
  }
  if (typeof window !== 'undefined' && window.VON_PERF_LOG) {
    const dur = (window.performance ? performance.now() : Date.now()) - __perfStartSel;
    console.log('[perf] selectVontologyNodeByIdentifier duration(ms)=', dur.toFixed(2), 'identifier=', identifier);
  }
}

// Attempt to find a node in the stored (unfiltered) tree data by id or mongo_id
function resolveNodeFromRawTree(identifier) {
  if (!identifier) return null;
  // Try both with and without #V# prefix
  const candidates = (typeof identifier === 'string' && !identifier.startsWith('#V#'))
    ? [identifier, `#V#${identifier}`]
    : [identifier];

  if (!vontologyTreeData || !vontologyTreeData.tree) return null;

  const roots = Array.isArray(vontologyTreeData.tree) ? vontologyTreeData.tree : [vontologyTreeData.tree];

  for (const id of candidates) {
    const found = dfsFindById(roots, id);
    if (found) return found;
  }
  return null;
}

function dfsFindById(nodes, id) {
  for (const node of nodes) {
    if (!node) continue;
    if (node.mongo_id === id || node.id === id) return node;
    if (node.children && node.children.length) {
      const found = dfsFindById(node.children, id);
      if (found) return found;
    }
  }
  return null;
}

// Reveal a hidden path by incrementally inserting missing nodes as temporary elements
async function ensurePathVisibleById(targetId) {
  if (!elements.vontologyTreeContainer) return false;
  const treeRootUl = elements.vontologyTreeContainer.querySelector('ul');
  if (!treeRootUl) return false;
  const pathNodes = computePathNodesToRoot(targetId);
  if (!pathNodes.length) return false;

  let parentUl = treeRootUl;
  for (const node of pathNodes) {
    // If span exists, use its LI
    let span = document.querySelector(`.vontology-node-name[data-id="${node.id}"]`);
    let li;
    if (!span) {
      // Create a minimal LI for this node
      li = await createTreeElement({ id: node.id, name: node.name, mongo_id: node.mongo_id, children: [] }, null);
      li.classList.add('vontology-temp');
      parentUl.appendChild(li);
    } else {
      li = span.closest('li');
    }
    // Ensure a UL exists for next child in the path
    let childUl = li.querySelector(':scope > ul');
    if (!childUl) {
      childUl = document.createElement('ul');
      childUl.style.listStyleType = 'none';
      childUl.style.margin = '0';
      childUl.style.paddingLeft = '20px';
      li.appendChild(childUl);
    }
    parentUl = childUl;
  }
  return true;
}

function clearTemporaryReveals() {
  document.querySelectorAll('.vontology-temp').forEach(el => el.remove());
}

// Compute ancestor path IDs from the stored raw tree for a given node id
function computePathIdsToRoot(targetId) {
  if (!vontologyTreeData || !vontologyTreeData.tree || !targetId) return [];
  const roots = Array.isArray(vontologyTreeData.tree) ? vontologyTreeData.tree : [vontologyTreeData.tree];
  const path = [];

  function dfs(node, acc) {
    if (!node) return false;
    const next = [...acc, node.id].filter(Boolean);
    if (node.id === targetId) {
      path.push(...next);
      return true;
    }
    if (node.children && node.children.length) {
      for (const child of node.children) {
        if (dfs(child, next)) return true;
      }
    }
    return false;
  }

  for (const root of roots) {
    if (dfs(root, [])) break;
  }
  return path;
}

// Compute ancestor path nodes from raw tree for a given node id
function computePathNodesToRoot(targetId) {
  if (!vontologyTreeData || !vontologyTreeData.tree || !targetId) return [];
  const roots = Array.isArray(vontologyTreeData.tree) ? vontologyTreeData.tree : [vontologyTreeData.tree];
  const path = [];

  function dfs(node, acc) {
    if (!node) return false;
    const next = [...acc, node];
    if (node.id === targetId) {
      path.push(...next);
      return true;
    }
    if (node.children && node.children.length) {
      for (const child of node.children) {
        if (dfs(child, next)) return true;
      }
    }
    return false;
  }

  for (const root of roots) {
    if (dfs(root, [])) break;
  }
  return path;
}

// Function to highlight the selected node
export function highlightSelectedNode(nodeElement) {
  // Remove highlight from previously selected node
  const currentHighlighted = document.querySelector('.vontology-node-selected');
  if (currentHighlighted) {
    currentHighlighted.classList.remove('vontology-node-selected');
  }

  // Highlight the new node
  if (nodeElement) {
    nodeElement.classList.add('vontology-node-selected');

    // Ensure the selected node is visible in the tree container
    scrollToSelectedNode(nodeElement);

    // Add glow effect temporarily
    addSelectionGlowEffect(nodeElement);
  }
}

// Function to ensure the selected node is visible by scrolling
export function scrollToSelectedNode(nodeElement) {
  if (!nodeElement) return;

  // Find the tree container (the scrollable parent)
  const treeContainer = nodeElement.closest('#vontologyTreeContainer, #vontology-tree-container, .vontology-tree, .tree-container, #vontology-tree');
  if (!treeContainer) {
    // Fallback: try to find any scrollable parent
    let parent = nodeElement.parentElement;
    while (parent && parent !== document.body) {
      const style = window.getComputedStyle(parent);
      if (style.overflowY === 'auto' || style.overflowY === 'scroll' ||
        style.overflow === 'auto' || style.overflow === 'scroll') {
        break;
      }
      parent = parent.parentElement;
    }
    if (parent && parent !== document.body) {
      // Scroll the container instead of the element itself
      const containerRect = parent.getBoundingClientRect();
      const nodeRect = nodeElement.getBoundingClientRect();

      const isVisible = (
        nodeRect.top >= containerRect.top &&
        nodeRect.bottom <= containerRect.bottom
      );

      if (!isVisible) {
        const scrollTop = parent.scrollTop;
        const containerHeight = containerRect.height;
        const nodeTop = nodeRect.top - containerRect.top + scrollTop;
        const targetScroll = nodeTop - containerHeight / 2;

        parent.scrollTo({
          top: targetScroll,
          behavior: 'smooth'
        });
      }
      return;
    }

    // Final fallback: scroll element into view
    nodeElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }

  // Calculate if the element is currently visible
  const containerRect = treeContainer.getBoundingClientRect();
  const nodeRect = nodeElement.getBoundingClientRect();

  const isVisible = (
    nodeRect.top >= containerRect.top &&
    nodeRect.bottom <= containerRect.bottom &&
    nodeRect.left >= containerRect.left &&
    nodeRect.right <= containerRect.right
  );

  // Only scroll if the node is not currently visible
  if (!isVisible) {
    nodeElement.scrollIntoView({
      behavior: 'smooth',
      block: 'center',
      inline: 'nearest'
    });
  }
}

// Function to add a temporary glow effect to indicate selection
export function addSelectionGlowEffect(nodeElement) {
  if (!nodeElement) return;

  // Add the glow class
  nodeElement.classList.add('vontology-node-glow');

  // Remove the glow after animation completes
  setTimeout(() => {
    if (nodeElement && nodeElement.classList) {
      nodeElement.classList.remove('vontology-node-glow');
    }
  }, 1500); // Match the CSS animation duration
}

// Function to expand parent nodes
export function expandParentNodes(nodeElement) {
  let parent = nodeElement.parentElement.parentElement; // li -> ul
  while (parent && parent.tagName === 'UL') {
    parent.style.display = 'block';
    const grandparentLi = parent.parentElement;
    if (grandparentLi && grandparentLi.tagName === 'LI') {
      const toggle = grandparentLi.querySelector('.toggle');
      if (toggle) {
        toggle.textContent = '-'; // Assuming '-' means expanded
      }
      parent = grandparentLi.parentElement?.parentElement;
    } else {
      break;
    }
  }
}

// --- JVNAUTOSCI-365: Top-of-page Vontology search UI ---
let __vontologySearchState = {
  items: [],
  activeIndex: -1,
  lastQuery: '',
  abortController: null,
  requestGeneration: 0,
  mode: 'search',
  concepts: {
    items: [],
    status: 'idle',
    error: ''
  },
  conversations: {
    items: [],
    status: 'idle',
    error: '',
    nextCursor: null,
    coverage: null
  },
  recoveryCount: null,
  recoveryCountConfirmedZero: false,
  recoveryCountCoverageComplete: false
};
// JVNAUTOSCI-550 additions: queue selections before tree ready & guard init
let __vontologyTreeReady = false;
const __pendingTreeSelections = [];
let __searchUiInitialised = false;
let __conversationRecoveryInitialised = false;
let __conversationRecoveryCountAbortController = null;
let __conversationRecoveryCountGeneration = 0;
// Session-scoped caches for performance
const __instanceCountsCache = new Map(); // key: joined candidate ids -> counts object
const __idToElement = new Map(); // concept_id -> span.vontology-node-name

// Choose the best type for an individual given candidate type concept_ids.
// Calls backend /api/vontology/instance_counts and picks the type with smallest total instances.
export async function chooseBestTypeForIndividual(candidateTypeIds = []) {
  const __perfStart = (typeof window !== 'undefined' && window.performance ? performance.now() : Date.now());
  if (!candidateTypeIds || !candidateTypeIds.length) return null;
  let countFetchTask = null;

  try {
    const idsParam = candidateTypeIds.join(',');
    if (__instanceCountsCache.has(idsParam)) {
      const cached = __instanceCountsCache.get(idsParam) || {};
      const scoredCached = candidateTypeIds.map(id => {
        const c = cached[id] || {};
        return { id, total: c.total != null ? c.total : Number.MAX_SAFE_INTEGER, direct: c.direct || 0, indirect: c.indirect || 0 };
      });
      scoredCached.sort((a, b) => a.total - b.total);
      const chosenCached = scoredCached[0] && scoredCached[0].id;
      if (typeof window !== 'undefined' && window.VON_PERF_LOG) {
        const dur = (window.performance ? performance.now() : Date.now()) - __perfStart;
        console.log('[perf] chooseBestTypeForIndividual duration(ms)=', dur.toFixed(2), 'candidates=', candidateTypeIds.length, 'cacheMiss=0');
      }
      return chosenCached;
    }
    countFetchTask = startBackgroundTask('instance_counts', {
      label: 'Instance counts',
      detail: `${candidateTypeIds.length} candidate types`
    });
    const resp = await fetch(`/api/vontology/instance_counts?ids=${encodeURIComponent(idsParam)}`);
    if (!resp.ok) {
      console.warn('Failed to fetch instance counts for candidate types');
      // Ensure no stale negative/empty cache persists for this key
      try { __instanceCountsCache.delete(idsParam); } catch (_) { }
      if (countFetchTask) {
        finishBackgroundTask(countFetchTask, {
          status: 'error',
          detail: `HTTP ${resp.status}`
        });
        countFetchTask = null;
      }
      return candidateTypeIds[0];
    }
    const data = await resp.json();
    const counts = data.instance_counts || {};
    __instanceCountsCache.set(idsParam, counts);

    console.debug('[chooseBestTypeForIndividual] fetched counts:', counts, 'candidates:', candidateTypeIds);

    // Build an array of { id, total, direct, indirect }
    const scored = candidateTypeIds.map(id => {
      const c = counts[id] || {};
      return {
        id,
        total: c.total != null ? c.total : Number.MAX_SAFE_INTEGER,
        direct: c.direct || 0,
        indirect: c.indirect || 0
      };
    });

    console.debug('[chooseBestTypeForIndividual] scored before sort:', JSON.parse(JSON.stringify(scored)));

    // Primary sort: smallest total
    scored.sort((a, b) => a.total - b.total);

    console.debug('[chooseBestTypeForIndividual] scored after sort:', JSON.parse(JSON.stringify(scored)));

    // If tie in total, keep the first (stable). Optionally, we could prefer deeper nodes if depth info is available.
    const chosen = scored[0] && scored[0].id;
    console.debug('[chooseBestTypeForIndividual] chosen best type:', chosen);
    if (countFetchTask) {
      finishBackgroundTask(countFetchTask, {
        status: 'success',
        detail: `${candidateTypeIds.length} candidate types`
      });
      countFetchTask = null;
    }
    if (typeof window !== 'undefined' && window.VON_PERF_LOG) {
      const dur = (window.performance ? performance.now() : Date.now()) - __perfStart;
      console.log('[perf] chooseBestTypeForIndividual duration(ms)=', dur.toFixed(2), 'candidates=', candidateTypeIds.length, 'cacheMiss=1');
    }
    return chosen;
  } catch (e) {
    if (countFetchTask) {
      finishBackgroundTask(countFetchTask, {
        status: 'error',
        error: e
      });
    }
    console.error('Error choosing best type for individual:', e);
    // Defensive: clear any partial cache entry that may have been set just before error
    try { /* error stage cleanup */ } catch (_) { }
    if (typeof window !== 'undefined' && window.VON_PERF_LOG) {
      const dur = (window.performance ? performance.now() : Date.now()) - __perfStart;
      console.log('[perf] chooseBestTypeForIndividual error duration(ms)=', dur.toFixed(2));
    }
    return candidateTypeIds[0];
  }
}

// TESTING ONLY: reset instance counts cache for deterministic unit tests
export function __resetInstanceCountsCache() {
  try { __instanceCountsCache.clear(); } catch (_) { }
}

// --- JVNAUTOSCI-365: Top-of-page Vontology search UI ---
export function setupVontologySearchUI() {
  if (__searchUiInitialised) return;
  // Support primary ID 'vontologySearchInput' and fallback legacy/test ID 'vontologySearch'
  const input = elements.vontologySearchInput || document.getElementById('vontologySearch');
  const results = elements.vontologySearchResults || document.getElementById('vontologySearchResults');
  if (input && !elements.vontologySearchInput) {
    // Cache fallback so downstream code uses the resolved element
    elements.vontologySearchInput = input;
  }
  if (results && !elements.vontologySearchResults) {
    elements.vontologySearchResults = results;
  }
  if (!input || !results) return;
  __searchUiInitialised = true;

  // --- Autofill Suppression Strategy -----------------------------------------------------------
  // HTML sets multiple attributes (autocomplete/off, autocapitalize/off, etc.) plus a placeholder
  // disposable name="_vontology_search". Some browsers (notably Chromium) still attempt to
  // surface saved form history/autofill if a stable name attribute persists. We schedule a
  // microtask-timeout removal of the name attribute, leaving the field anonymous post-hydration.
  // If you replicate this pattern elsewhere: mirror the HTML attributes AND this removal.
  // (See comment block in `vontology_tab.html` for rationale.)
  try { setTimeout(() => { if (input.getAttribute('name') === '_vontology_search') input.removeAttribute('name'); }, 0); } catch (_) { }

  // Ensure semantic combobox roles reflect open/closed state
  function setExpanded(open) {
    try { input.setAttribute('aria-expanded', open ? 'true' : 'false'); } catch (_) { }
  }

  const debounced = debounce(async () => {
    const q = input.value.trim();
    if (!q) { clearSearchResults({ invalidate: true }); return; }
    await performVontologySearch(q);
  }, 180);

  // Input handlers
  const handleInput = () => {
    if (!input.value.trim()) {
      clearSearchResults({ invalidate: true });
      return;
    }
    if (__vontologySearchState.mode !== 'search') {
      __vontologySearchState.mode = 'search';
      setRecoveryButtonPressed(false);
    }
    debounced();
  };
  input.addEventListener('input', handleInput);
  input.addEventListener('search', handleInput);
  input.addEventListener('focus', () => {
    if (searchHasVisibleContent()) openResults();
    setExpanded(searchHasVisibleContent());
  });

  // Keyboard navigation
  input.addEventListener('keydown', (e) => {
    const { items } = __vontologySearchState;
    // Allow Escape even if no items
    if (e.key === 'Escape') {
      e.preventDefault();
      clearSearchResults({ invalidate: true });
      input.blur();
      return;
    }
    if (!items.length) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveActive(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveActive(-1);
    } else if (e.key === 'Enter') {
      // Unified behaviour (JVNAUTOSCI-617):
      // 1. If exactly one result -> select it
      // 2. Else if activeIndex set -> select active
      // 3. Else attempt exact match on query
      // 4. Fallback to first item
      e.preventDefault();
      if (items.length === 1) {
        selectSearchItem(items[0]);
        return;
      }
      if (__vontologySearchState.activeIndex >= 0 && __vontologySearchState.activeIndex < items.length) {
        selectSearchItem(items[__vontologySearchState.activeIndex]);
        return;
      }
      const q = input.value.trim().toLowerCase();
      if (q) {
        const exact = items.find(it => ((it.name || '').toLowerCase() === q) || ((it.id || '').toLowerCase() === q));
        if (exact) {
          selectSearchItem(exact);
          return;
        }
      }
      selectSearchItem(items[0]);
    }
  });

  // Click outside to close
  document.addEventListener('click', (e) => {
    const recoveryButton = document.getElementById('conversationRecoveryButton');
    if (!results.contains(e.target) && e.target !== input && !recoveryButton?.contains(e.target)) {
      clearSearchResults({ invalidate: true });
      setExpanded(false);
    }
  });

  initialiseConversationRecoveryUI();
}

function debounce(fn, wait) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(null, args), wait);
  };
}

function beginUnifiedSearchRequest(mode = 'search') {
  try { __vontologySearchState.abortController?.abort(); } catch (_) { }
  const abortController = new AbortController();
  const requestGeneration = __vontologySearchState.requestGeneration + 1;
  __vontologySearchState.abortController = abortController;
  __vontologySearchState.requestGeneration = requestGeneration;
  __vontologySearchState.mode = mode;
  __vontologySearchState.activeIndex = -1;
  return { abortController, requestGeneration };
}

function isCurrentUnifiedSearchRequest(requestGeneration) {
  return __vontologySearchState.requestGeneration === requestGeneration;
}

function sortConceptSearchItems(items) {
  if (!items.length || !items.some(it => typeof it.relevance_score === 'number')) {
    return items;
  }
  return items.slice().sort((a, b) => {
    const aScore = typeof a.relevance_score === 'number' ? a.relevance_score : -1;
    const bScore = typeof b.relevance_score === 'number' ? b.relevance_score : -1;
    return bScore - aScore;
  });
}

async function fetchConceptSearchItems(q, signal) {
  const url = `/vontology/api/vontology/search?q=${encodeURIComponent(q)}&limit=20&include_individuals=true`;
  const res = await vontologyFetch(url, { signal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  let items = Array.isArray(data?.results) ? data.results : [];

  const trimmedQuery = q?.trim() || '';
  if (trimmedQuery.startsWith('#V#')) {
    const idLower = trimmedQuery.toLowerCase();
    const alreadyPresent = items.some(it => (it.id || '').toLowerCase() === idLower);
    if (!alreadyPresent) {
      try {
        const nodeRes = await vontologyFetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(trimmedQuery)}`, { signal });
        if (nodeRes.ok) {
          const nodeData = await nodeRes.json();
          if (nodeData && !nodeData.error) {
            const deriveName = () => {
              if (typeof nodeData.name === 'string' && nodeData.name.trim()) return nodeData.name.trim();
              if (typeof nodeData.display_name === 'string' && nodeData.display_name.trim()) return nodeData.display_name.trim();
              const raw = nodeData.raw_doc || {};
              if (typeof raw.name === 'string' && raw.name.trim()) return raw.name.trim();
              if (typeof raw.display_name === 'string' && raw.display_name.trim()) return raw.display_name.trim();
              return trimmedQuery;
            };
            const deriveKind = () => {
              if (typeof nodeData.kind === 'string') return nodeData.kind;
              if (typeof nodeData.computed_kind === 'string') return nodeData.computed_kind;
              const relationships = nodeData.raw_doc?.relationships || {};
              const instanceRels = Array.isArray(nodeData.is_an_instance_of) ? nodeData.is_an_instance_of : relationships.is_an_instance_of;
              const typeRels = Array.isArray(nodeData.is_a_type_of) ? nodeData.is_a_type_of : relationships.is_a_type_of;
              if (Array.isArray(instanceRels) && instanceRels.length && (!Array.isArray(typeRels) || !typeRels.length)) {
                return 'individual';
              }
              return 'type';
            };
            items = [{
              id: trimmedQuery,
              name: deriveName(),
              kind: deriveKind() || 'type'
            }, ...items];
          }
        } else {
          let errorPayload = null;
          try { errorPayload = await nodeRes.json(); } catch (_) { }
          items = [makeExactIdSearchErrorItem(trimmedQuery, nodeRes.status, errorPayload), ...items];
        }
      } catch (lookupErr) {
        if (lookupErr?.name === 'AbortError') throw lookupErr;
        console.debug('[vontology search] id lookup failed', lookupErr);
        items = [makeExactIdSearchErrorItem(trimmedQuery, null, null), ...items];
      }
    }
  }
  return sortConceptSearchItems(items);
}

async function fetchConversationSearchPage(query, {
  cursor = null,
  signal = null,
  trashedOnly = false,
  pageSize = 8,
  sort = 'relevance'
} = {}) {
  const params = new URLSearchParams({
    q: query,
    // The interactive UI uses the indexed title/content path for predictable
    // typeahead latency. The MCP conversation_search capability retains hybrid
    // RAG search for model-driven retrieval.
    match_mode: 'lexical',
    page_size: String(pageSize),
    sort,
    trashed_only: trashedOnly ? 'true' : 'false'
  });
  if (cursor) params.set('cursor', cursor);
  const response = await vontologyFetch(`/von/api/session/conversation_search?${params.toString()}`, {
    cache: 'no-store',
    signal
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data?.error || `Conversation search failed (HTTP ${response.status})`);
  }
  return data;
}

export async function performVontologySearch(q) {
  const results = elements.vontologySearchResults;
  const trimmedQuery = String(q || '').trim();
  if (!results) return;
  if (!trimmedQuery) {
    clearSearchResults({ invalidate: true });
    return;
  }

  const { abortController, requestGeneration } = beginUnifiedSearchRequest('search');
  __vontologySearchState.lastQuery = trimmedQuery;
  __vontologySearchState.concepts = { items: [], status: 'loading', error: '' };
  __vontologySearchState.conversations = {
    items: [], status: 'loading', error: '', nextCursor: null, coverage: null
  };
  setRecoveryButtonPressed(false);
  renderSearchResults();

  const conceptTask = fetchConceptSearchItems(trimmedQuery, abortController.signal)
    .then(conceptItems => {
      if (!isCurrentUnifiedSearchRequest(requestGeneration)) return;
      __vontologySearchState.concepts = {
        items: conceptItems,
        status: conceptItems.length ? 'ready' : 'empty',
        error: ''
      };
      renderSearchResults();
    })
    .catch(error => {
      if (error?.name === 'AbortError' || !isCurrentUnifiedSearchRequest(requestGeneration)) return;
      console.warn('[unified search] concept provider failed:', error);
      const exactIdItems = trimmedQuery.startsWith('#V#')
        ? [makeExactIdSearchErrorItem(trimmedQuery, null, null)]
        : [];
      __vontologySearchState.concepts = {
        items: exactIdItems,
        status: 'error',
        error: 'Concept search is temporarily unavailable.'
      };
      renderSearchResults();
    });
  const conversationTask = fetchConversationSearchPage(trimmedQuery, { signal: abortController.signal })
    .then(data => {
      if (!isCurrentUnifiedSearchRequest(requestGeneration)) return;
      const conversationItems = Array.isArray(data?.results) ? data.results : [];
      __vontologySearchState.conversations = {
        items: conversationItems,
        status: conversationItems.length ? 'ready' : 'empty',
        error: '',
        nextCursor: data?.next_cursor || null,
        coverage: data?.index_coverage || null
      };
      renderSearchResults();
    })
    .catch(error => {
      if (error?.name === 'AbortError' || !isCurrentUnifiedSearchRequest(requestGeneration)) return;
      console.warn('[unified search] conversation provider failed:', error);
      __vontologySearchState.conversations = {
        items: [],
        status: 'error',
        error: 'Conversation search is temporarily unavailable.',
        nextCursor: null,
        coverage: null
      };
      renderSearchResults();
    });
  await Promise.all([conceptTask, conversationTask]);
}

function makeExactIdSearchErrorItem(conceptId, status, payload) {
  const errorCode = payload?.error_code;
  let message = `No accessible concept found for ${conceptId}`;
  if (errorCode === 'access_denied' || status === 403) {
    message = `Concept exists but is not accessible here: ${conceptId}`;
  } else if (status && status >= 500) {
    message = `Could not check concept ID right now: ${conceptId}`;
  }
  return {
    id: conceptId,
    name: message,
    kind: 'status',
    disabled: true,
    error: true,
    error_code: errorCode || (status ? `http_${status}` : 'lookup_failed')
  };
}

async function copySearchConceptIdToClipboard(conceptId) {
  const value = String(conceptId ?? '');
  if (!value) return;

  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
  } catch (_) {
    // Fall through to legacy approach.
  }

  const textarea = document.createElement('textarea');
  textarea.value = value;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  textarea.style.top = '-9999px';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    document.execCommand('copy');
  } finally {
    try { textarea.remove(); } catch (_) { }
  }
}

function isConversationWorkspaceActive() {
  return document.body?.dataset?.activeTab === 'chatTab'
    || document.getElementById('chatTab')?.classList.contains('active') === true;
}

function searchHasVisibleContent() {
  return __vontologySearchState.mode === 'recovery'
    || Boolean(__vontologySearchState.lastQuery)
    || ['loading', 'ready', 'empty', 'error'].includes(__vontologySearchState.concepts.status)
    || ['loading', 'ready', 'empty', 'error', 'loading-more'].includes(__vontologySearchState.conversations.status);
}

function normaliseConceptSearchItem(item) {
  return { ...item, searchType: 'concept' };
}

function normaliseConversationSearchItem(item) {
  const sessionId = String(item?.session_id || '').trim();
  const displayName = String(item?.display_name || item?.session_name || '').trim() || 'Unnamed conversation';
  return {
    ...item,
    id: sessionId,
    name: displayName,
    searchType: 'conversation'
  };
}

function getOrderedSearchGroups() {
  if (__vontologySearchState.mode === 'recovery') return ['conversations'];
  return isConversationWorkspaceActive()
    ? ['conversations', 'concepts']
    : ['concepts', 'conversations'];
}

function rebuildSearchKeyboardItems() {
  const providers = {
    concepts: __vontologySearchState.concepts.items.map(normaliseConceptSearchItem),
    conversations: __vontologySearchState.mode === 'recovery'
      ? []
      : __vontologySearchState.conversations.items.map(normaliseConversationSearchItem)
  };
  __vontologySearchState.items = getOrderedSearchGroups()
    .flatMap(groupKey => providers[groupKey] || [])
    .filter(item => !item.disabled && item.id);
}

function formatConversationSearchDate(item) {
  const raw = item?.last_message_at || item?.created_at;
  if (!raw) return '';
  const date = new Date(raw);
  if (!Number.isFinite(date.getTime())) return '';
  const options = { day: 'numeric', month: 'short' };
  if (date.getFullYear() !== new Date().getFullYear()) options.year = 'numeric';
  return date.toLocaleDateString(undefined, options);
}

function createSearchGroupHeading(text, id) {
  const heading = document.createElement('div');
  heading.className = 'unified-search-group-heading';
  heading.id = id;
  heading.textContent = text;
  return heading;
}

function createSearchProviderNotice(text, className = '') {
  const notice = document.createElement('div');
  notice.className = `unified-search-provider-notice ${className}`.trim();
  notice.setAttribute('role', 'status');
  notice.textContent = text;
  return notice;
}

function renderConceptSearchRow(item, keyboardIndex) {
  const row = document.createElement('div');
  row.className = item.error
    ? 'vontology-search-item unified-search-option vontology-search-item-error'
    : 'vontology-search-item unified-search-option';
  row.setAttribute('role', 'option');
  if (keyboardIndex !== null) row.dataset.searchIndex = String(keyboardIndex);
  if (item.disabled) row.setAttribute('aria-disabled', 'true');

  const name = document.createElement('span');
  name.className = 'vontology-search-item-name';
  name.textContent = item.name || item.id;
  if (ENABLE_VONTOLOGY_SEARCH_TOOLTIPS) {
    name.title = `${item.name || item.id} \u2014 ${item.id}`;
  }
  const badgeKind = item.kind;
  const badgeText = item.kind === 'predicate'
    ? 'Predicate'
    : (item.kind === 'individual' ? 'Individual' : (item.kind === 'status' ? 'Status' : 'Type'));
  const kind = document.createElement('span');
  kind.className = `vontology-search-item-kind ${badgeKind}`;
  kind.textContent = badgeText;
  row.appendChild(name);
  if (!ENABLE_VONTOLOGY_SEARCH_TOOLTIPS) {
    const idInline = document.createElement('span');
    idInline.className = 'vontology-search-item-id-inline';
    idInline.textContent = item.id;
    row.appendChild(idInline);
  }
  row.appendChild(kind);

  if (!item.disabled && keyboardIndex !== null) {
    row.addEventListener('mouseenter', () => setActiveIndex(keyboardIndex));
    row.addEventListener('mouseleave', () => setActiveIndex(-1));
    row.addEventListener('click', () => selectSearchItem(normaliseConceptSearchItem(item)));
    row.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      event.stopPropagation();
      void copySearchConceptIdToClipboard(item.id);
    });
  }
  return row;
}

function renderConversationSearchRow(item, keyboardIndex, { recovery = false } = {}) {
  const normalised = normaliseConversationSearchItem(item);
  const row = document.createElement('div');
  row.className = 'unified-search-conversation-item unified-search-option';
  row.setAttribute('role', recovery ? 'group' : 'option');
  if (keyboardIndex !== null) row.dataset.searchIndex = String(keyboardIndex);

  const main = document.createElement('div');
  main.className = 'unified-search-conversation-main';

  const title = document.createElement('span');
  title.className = 'unified-search-conversation-title';
  title.textContent = normalised.name;
  const date = document.createElement('span');
  date.className = 'unified-search-conversation-date';
  date.textContent = formatConversationSearchDate(item);
  main.appendChild(title);
  if (date.textContent) main.appendChild(date);

  const snippetText = String(item?.match?.snippet || '').trim();
  if (snippetText) {
    const snippet = document.createElement('span');
    snippet.className = 'unified-search-conversation-snippet';
    snippet.textContent = snippetText;
    main.appendChild(snippet);
  }

  if (!recovery && keyboardIndex !== null) {
    row.addEventListener('click', () => selectSearchItem(normalised));
    row.addEventListener('mouseenter', () => setActiveIndex(keyboardIndex));
    row.addEventListener('mouseleave', () => setActiveIndex(-1));
  }
  row.appendChild(main);

  if (recovery) {
    const restore = document.createElement('button');
    restore.type = 'button';
    restore.className = 'unified-search-restore-button';
    restore.textContent = 'Restore';
    restore.addEventListener('click', () => { void restoreConversationFromRecovery(item, restore); });
    row.appendChild(restore);
  }
  return row;
}

function appendSearchProviderState(group, groupKey, provider) {
  const label = groupKey === 'concepts' ? 'concepts' : 'conversations';
  if (provider.status === 'loading') {
    group.appendChild(createSearchProviderNotice(`Searching ${label}\u2026`, 'is-loading'));
  } else if (provider.status === 'loading-more') {
    group.appendChild(createSearchProviderNotice(`Loading more ${label}\u2026`, 'is-loading'));
  } else if (provider.status === 'error') {
    group.appendChild(createSearchProviderNotice(provider.error || `${label} search is unavailable.`, 'is-error'));
  } else if (provider.status === 'empty' && provider.items.length === 0) {
    const emptyMessage = __vontologySearchState.mode === 'recovery' && groupKey === 'conversations'
      ? 'No removed conversations.'
      : `No matching ${label}.`;
    group.appendChild(createSearchProviderNotice(emptyMessage, 'is-empty'));
  }

  if (groupKey === 'conversations' && provider.coverage) {
    const coverageIncomplete = provider.coverage.complete_for_accessible_window === false
      || provider.coverage.candidate_window_complete === false;
    if (coverageIncomplete) {
      const notice = createSearchProviderNotice('Some older conversations may not appear yet.', 'is-partial');
      const limitations = Array.isArray(provider.coverage.limitations)
        ? provider.coverage.limitations.join(', ')
        : '';
      if (limitations) {
        notice.title = limitations;
        notice.setAttribute('data-keep-title', 'true');
      }
      group.appendChild(notice);
    }
  }
}

function renderSearchResults() {
  const results = elements.vontologySearchResults;
  if (!results) return;
  results.innerHTML = '';
  rebuildSearchKeyboardItems();
  __vontologySearchState.activeIndex = -1;

  if (!searchHasVisibleContent()) {
    results.classList.remove('open');
    try { elements.vontologySearchInput?.setAttribute('aria-expanded', 'false'); } catch (_) { }
    return;
  }

  const keyboardIndexByKey = new Map(
    __vontologySearchState.items.map((item, index) => [`${item.searchType}:${item.id}`, index])
  );
  getOrderedSearchGroups().forEach(groupKey => {
    const provider = __vontologySearchState[groupKey];
    const group = document.createElement('section');
    const headingId = `unifiedSearch${groupKey === 'concepts' ? 'Concepts' : 'Conversations'}Heading`;
    const headingText = __vontologySearchState.mode === 'recovery' && groupKey === 'conversations'
      ? 'Removed conversations'
      : (groupKey === 'concepts' ? 'Concepts' : 'Conversations');
    group.className = `unified-search-group unified-search-group-${groupKey}`;
    group.setAttribute('role', 'group');
    group.setAttribute('aria-labelledby', headingId);
    group.appendChild(createSearchGroupHeading(headingText, headingId));

    provider.items.forEach(item => {
      if (groupKey === 'concepts') {
        const normalised = normaliseConceptSearchItem(item);
        const index = normalised.disabled ? null : keyboardIndexByKey.get(`concept:${normalised.id}`);
        group.appendChild(renderConceptSearchRow(item, Number.isInteger(index) ? index : null));
      } else {
        const normalised = normaliseConversationSearchItem(item);
        const index = __vontologySearchState.mode === 'recovery'
          ? null
          : keyboardIndexByKey.get(`conversation:${normalised.id}`);
        group.appendChild(renderConversationSearchRow(item, Number.isInteger(index) ? index : null, {
          recovery: __vontologySearchState.mode === 'recovery'
        }));
      }
    });
    appendSearchProviderState(group, groupKey, provider);

    if (groupKey === 'conversations' && provider.nextCursor) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'unified-search-more-button';
      more.textContent = __vontologySearchState.mode === 'recovery'
        ? 'More removed conversations'
        : 'More conversations';
      more.addEventListener('click', () => { void loadMoreConversationSearchResults(); });
      group.appendChild(more);
    }
    results.appendChild(group);
  });
  openResults();
}

function openResults() {
  const results = elements.vontologySearchResults;
  if (!results) return;
  const open = searchHasVisibleContent();
  results.classList.toggle('open', open);
  try { elements.vontologySearchInput?.setAttribute('aria-expanded', open ? 'true' : 'false'); } catch (_) { }
}

function clearSearchResults({ invalidate = false } = {}) {
  const results = elements.vontologySearchResults;
  if (invalidate) {
    try { __vontologySearchState.abortController?.abort(); } catch (_) { }
    __vontologySearchState.requestGeneration += 1;
  }
  if (results) {
    results.classList.remove('open');
    results.innerHTML = '';
  }
  __vontologySearchState.items = [];
  __vontologySearchState.activeIndex = -1;
  __vontologySearchState.lastQuery = '';
  __vontologySearchState.concepts = { items: [], status: 'idle', error: '' };
  __vontologySearchState.conversations = {
    items: [], status: 'idle', error: '', nextCursor: null, coverage: null
  };
  __vontologySearchState.mode = 'search';
  setRecoveryButtonPressed(false);
  try { elements.vontologySearchInput?.setAttribute('aria-expanded', 'false'); } catch (_) { }
}

function setActiveIndex(idx) {
  __vontologySearchState.activeIndex = idx;
  const results = elements.vontologySearchResults;
  if (!results) return;
  results.querySelectorAll('.unified-search-option[data-search-index]').forEach(el => {
    const active = Number(el.dataset.searchIndex) === idx;
    el.classList.toggle('active', active);
    el.setAttribute('aria-selected', active ? 'true' : 'false');
  });
}

function moveActive(delta) {
  const n = __vontologySearchState.items.length;
  if (!n) return;
  let idx = __vontologySearchState.activeIndex;
  idx = (idx + delta + n) % n;
  setActiveIndex(idx);
  const results = elements.vontologySearchResults;
  const el = results?.querySelector(`.unified-search-option[data-search-index="${idx}"]`);
  if (el && results) {
    const resultBounds = results.getBoundingClientRect();
    const itemBounds = el.getBoundingClientRect();
    if (itemBounds.bottom > resultBounds.bottom) {
      results.scrollTop += (itemBounds.bottom - resultBounds.bottom);
    } else if (itemBounds.top < resultBounds.top) {
      results.scrollTop -= (resultBounds.top - itemBounds.top);
    }
  }
}

function setRecoveryButtonPressed(pressed) {
  const button = document.getElementById('conversationRecoveryButton');
  button?.setAttribute('aria-pressed', pressed ? 'true' : 'false');
}

function updateConversationRecoveryButton() {
  const button = document.getElementById('conversationRecoveryButton');
  const badge = document.getElementById('conversationRecoveryCount');
  if (!button || !badge) return;
  const active = isConversationWorkspaceActive();
  button.hidden = !active || __vontologySearchState.recoveryCountConfirmedZero;
  const count = __vontologySearchState.recoveryCount;
  if (Number.isFinite(count) && count > 0) {
    badge.hidden = false;
    badge.textContent = count > 99 ? '99+' : String(count);
    const countDescription = __vontologySearchState.recoveryCountCoverageComplete
      ? `${count} removed conversation${count === 1 ? '' : 's'}`
      : `at least ${count} removed conversation${count === 1 ? '' : 's'}`;
    button.title = `Open ${countDescription}`;
    button.setAttribute('aria-label', `Open ${countDescription}`);
  } else {
    badge.hidden = true;
    badge.textContent = '';
    button.title = 'Open removed conversations';
    button.setAttribute('aria-label', 'Open removed conversations');
  }
}

function acceptConversationRecoveryCountPayload(data) {
  const candidateCount = Number(data?.index_coverage?.accessible_window_count);
  const count = Number.isFinite(candidateCount)
    ? Math.max(0, Math.trunc(candidateCount))
    : null;
  const coverageComplete = data?.index_coverage?.candidate_window_complete === true;
  __vontologySearchState.recoveryCount = count;
  __vontologySearchState.recoveryCountCoverageComplete = coverageComplete;
  __vontologySearchState.recoveryCountConfirmedZero = count === 0 && coverageComplete;
  updateConversationRecoveryButton();
}

async function refreshConversationRecoveryCount() {
  const requestGeneration = __conversationRecoveryCountGeneration + 1;
  __conversationRecoveryCountGeneration = requestGeneration;
  try { __conversationRecoveryCountAbortController?.abort(); } catch (_) { }
  const abortController = new AbortController();
  __conversationRecoveryCountAbortController = abortController;
  try {
    const data = await fetchConversationSearchPage('*', {
      trashedOnly: true,
      pageSize: 1,
      sort: 'updated',
      signal: abortController.signal
    });
    if (__conversationRecoveryCountGeneration !== requestGeneration) return;
    acceptConversationRecoveryCountPayload(data);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    console.debug('[conversation recovery] count unavailable', error);
    if (__conversationRecoveryCountGeneration === requestGeneration) {
      __vontologySearchState.recoveryCountConfirmedZero = false;
      updateConversationRecoveryButton();
    }
  }
}

async function openRemovedConversations() {
  const results = elements.vontologySearchResults;
  if (!results) return;
  if (__vontologySearchState.mode === 'recovery' && results.classList.contains('open')) {
    clearSearchResults({ invalidate: true });
    return;
  }
  const input = elements.vontologySearchInput;
  if (input) input.value = '';
  const { abortController, requestGeneration } = beginUnifiedSearchRequest('recovery');
  __vontologySearchState.lastQuery = '';
  __vontologySearchState.concepts = { items: [], status: 'idle', error: '' };
  __vontologySearchState.conversations = {
    items: [], status: 'loading', error: '', nextCursor: null, coverage: null
  };
  setRecoveryButtonPressed(true);
  renderSearchResults();
  try {
    const data = await fetchConversationSearchPage('*', {
      trashedOnly: true,
      pageSize: 20,
      sort: 'updated',
      signal: abortController.signal
    });
    if (!isCurrentUnifiedSearchRequest(requestGeneration)) return;
    const items = Array.isArray(data?.results) ? data.results : [];
    __vontologySearchState.conversations = {
      items,
      status: items.length ? 'ready' : 'empty',
      error: '',
      nextCursor: data?.next_cursor || null,
      coverage: data?.index_coverage || null
    };
    acceptConversationRecoveryCountPayload(data);
    renderSearchResults();
  } catch (error) {
    if (error?.name === 'AbortError' || !isCurrentUnifiedSearchRequest(requestGeneration)) return;
    __vontologySearchState.conversations = {
      items: [], status: 'error', error: 'Removed conversations are temporarily unavailable.', nextCursor: null, coverage: null
    };
    renderSearchResults();
  }
}

async function loadMoreConversationSearchResults() {
  const current = __vontologySearchState.conversations;
  if (!current.nextCursor) return;
  const mode = __vontologySearchState.mode;
  const query = mode === 'recovery' ? '*' : __vontologySearchState.lastQuery;
  const existingItems = current.items.slice();
  const { abortController, requestGeneration } = beginUnifiedSearchRequest(mode);
  __vontologySearchState.conversations = { ...current, status: 'loading-more', error: '' };
  renderSearchResults();
  try {
    const data = await fetchConversationSearchPage(query, {
      cursor: current.nextCursor,
      trashedOnly: mode === 'recovery',
      pageSize: mode === 'recovery' ? 20 : 8,
      sort: mode === 'recovery' ? 'updated' : 'relevance',
      signal: abortController.signal
    });
    if (!isCurrentUnifiedSearchRequest(requestGeneration)) return;
    const appended = Array.isArray(data?.results) ? data.results : [];
    const seen = new Set();
    const items = [...existingItems, ...appended].filter(item => {
      const id = String(item?.session_id || '');
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
    __vontologySearchState.conversations = {
      items,
      status: items.length ? 'ready' : 'empty',
      error: '',
      nextCursor: data?.next_cursor || null,
      coverage: data?.index_coverage || current.coverage || null
    };
    if (mode === 'recovery') acceptConversationRecoveryCountPayload(data);
    renderSearchResults();
  } catch (error) {
    if (error?.name === 'AbortError' || !isCurrentUnifiedSearchRequest(requestGeneration)) return;
    __vontologySearchState.conversations = {
      ...current,
      items: existingItems,
      status: 'error',
      error: 'More conversations could not be loaded.'
    };
    renderSearchResults();
  }
}

async function restoreConversationFromRecovery(item, button) {
  const sessionId = String(item?.session_id || '').trim();
  if (!sessionId) return false;
  const previousLabel = button?.textContent || 'Restore';
  if (button) {
    button.disabled = true;
    button.textContent = 'Restoring\u2026';
  }
  try {
    const response = await vontologyFetch('/von/api/session/delete_chat_session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId, action: 'restore' })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data?.status !== 'restored' || data?.trashed !== false) {
      throw new Error(data?.error || 'Conversation restore could not be verified.');
    }
    __vontologySearchState.conversations.items = __vontologySearchState.conversations.items.filter(
      row => row?.session_id !== sessionId
    );
    if (__vontologySearchState.conversations.items.length === 0 && !__vontologySearchState.conversations.nextCursor) {
      __vontologySearchState.conversations.status = 'empty';
    }
    if (Number.isFinite(__vontologySearchState.recoveryCount)) {
      __vontologySearchState.recoveryCount = Math.max(0, __vontologySearchState.recoveryCount - 1);
      __vontologySearchState.recoveryCountConfirmedZero = false;
    }
    renderSearchResults();
    document.dispatchEvent(new CustomEvent('von:conversation-trash-changed', {
      detail: { action: 'restored', sessionId, source: 'recovery' }
    }));
    await refreshConversationRecoveryCount();
    if (__vontologySearchState.recoveryCountConfirmedZero) {
      clearSearchResults({ invalidate: true });
      elements.vontologySearchInput?.focus();
    }
    return true;
  } catch (error) {
    __vontologySearchState.conversations.status = 'error';
    __vontologySearchState.conversations.error = error?.message || 'Conversation restore failed.';
    renderSearchResults();
    return false;
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = previousLabel;
    }
  }
}

function initialiseConversationRecoveryUI() {
  if (__conversationRecoveryInitialised) return;
  const button = document.getElementById('conversationRecoveryButton');
  if (!button) return;
  __conversationRecoveryInitialised = true;
  button.addEventListener('click', () => { void openRemovedConversations(); });
  document.addEventListener('von:conversation-trash-changed', event => {
    if (event?.detail?.source === 'recovery') return;
    void refreshConversationRecoveryCount();
  });
  document.addEventListener('von:tab-activated', event => {
    updateConversationRecoveryButton();
    if (event?.detail?.tabId === 'chatTab') void refreshConversationRecoveryCount();
  });
  const resetForActorContext = () => {
    __vontologySearchState.recoveryCount = null;
    __vontologySearchState.recoveryCountConfirmedZero = false;
    __vontologySearchState.recoveryCountCoverageComplete = false;
    clearSearchResults({ invalidate: true });
    if (elements.vontologySearchInput) elements.vontologySearchInput.value = '';
    updateConversationRecoveryButton();
    void refreshConversationRecoveryCount();
  };
  document.addEventListener('orgSwitched', resetForActorContext);
  document.addEventListener('authStatusChanged', resetForActorContext);
  updateConversationRecoveryButton();
  void refreshConversationRecoveryCount();
}


async function selectSearchItem(item) {
  const __perfStartSearchSel = (typeof window !== 'undefined' && window.performance ? performance.now() : Date.now());
  if (!item) return;
  if (item.disabled) return;
  if (item.searchType === 'conversation') {
    const input = elements?.vontologySearchInput;
    if (input) input.value = item.name || '';
    clearSearchResults({ invalidate: true });
    try {
      document.dispatchEvent(new CustomEvent('von:open-conversation-search-result', {
        detail: { conversation: item }
      }));
    } catch (error) {
      console.error('[selectSearchItem] Failed to dispatch conversation selection', error);
    }
    return;
  }
  const inputEl = elements && elements.vontologySearchInput;
  if (inputEl) {
    try {
      inputEl.classList.add('loading');
      inputEl.setAttribute('aria-busy', 'true');
    } catch (_) { }
  }
  // Populate the search input with the selected item's display label BEFORE clearing results so we don't
  // momentarily lose focus value or leave a truncated partial (test expectation: full name e.g. 'Alpha').
  try {
    if (elements && elements.vontologySearchInput) {
      const label = item.name || item.id || '';
      if (label) elements.vontologySearchInput.value = label;
    }
  } catch (_) { /* non-fatal */ }
  clearSearchResults();

  // Open the concept tab immediately so selection never feels "dead".
  // Any slower tree-reveal work runs in the background.
  try {
    // Use backend's three-way kind classification directly
    const evt = new CustomEvent('open-concept-tab', {
      detail: {
        conceptId: item.id,
        conceptName: item.name || item.id,
        kind: item.kind,
        activate: false
      }
    });
    document.dispatchEvent(evt);
  } catch (e) {
    console.error('[selectSearchItem] Failed to dispatch open-concept-tab event', e);
  }

  try {
    void (async () => {
      try {
        if (item.kind === 'individual') {
          try {
            const resp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(item.id)}`);
            if (resp.ok) {
              const nodeData = await resp.json();
              const candidatesRaw = nodeData?.is_an_instance_of || nodeData?.is_a || [];
              // Normalize to concept_id strings
              const candidateIds = [];
              if (Array.isArray(candidatesRaw)) {
                for (const c of candidatesRaw) {
                  if (!c) continue;
                  if (typeof c === 'string') candidateIds.push(c);
                  else if (c.concept_id) candidateIds.push(c.concept_id);
                  else if (c.id) candidateIds.push(c.id);
                  else if (c['@id']) candidateIds.push(c['@id']);
                }
              }

              if (candidateIds.length) {
                try {
                  const chosenType = await chooseBestTypeForIndividual(candidateIds);
                  if (chosenType) {
                    // Reveal and select the chosen TYPE in the tree (don't create a concept tab)
                    try { selectVontologyNodeByIdentifier(chosenType, /*createConceptTab*/ false); } catch (_) { }
                  } else {
                    console.debug('[selectSearchItem] No chosen type returned; skipping tree reveal for instance', item.id);
                  }
                } catch (e) {
                  console.warn('[selectSearchItem] Error choosing best type for individual:', e);
                }
              } else {
                console.debug('[selectSearchItem] No is_an_instance_of candidates found for individual', item.id);
              }
            } else {
              console.debug('[selectSearchItem] node_content lookup failed for', item.id, 'status', resp.status);
            }
          } catch (e) {
            console.warn('[selectSearchItem] Failed to fetch node_content for individual', item.id, e);
          }
        } else {
          if (__vontologyTreeReady) {
            try { selectVontologyNodeByIdentifier(item.id, false); } catch (_) { }
          } else {
            __pendingTreeSelections.push(item.id);
          }
        }
      } catch (bgErr) {
        console.warn('[selectSearchItem] Error during background tree reveal logic:', bgErr);
      }
    })();
  } catch (err) {
    console.warn('[selectSearchItem] Error during tree reveal logic:', err);
  }
  finally {
    if (inputEl) {
      try {
        inputEl.classList.remove('loading');
        inputEl.removeAttribute('aria-busy');
      } catch (_) { }
    }
    if (typeof window !== 'undefined' && window.VON_PERF_LOG) {
      const dur = (window.performance ? performance.now() : Date.now()) - __perfStartSearchSel;
      console.log('[perf] selectSearchItem duration(ms)=', dur.toFixed(2), 'kind=', item && item.kind);
    }
  }
}

// Handle Show Subtree Details button click
async function handleShowSubtreeDetails() {
  console.log("[handleShowSubtreeDetails] Button clicked.");
  if (elements.subtreeDetailsContainer) {
    elements.subtreeDetailsContainer.classList.remove('hidden');
    // Clear any conflicting inline style from older logic
    elements.subtreeDetailsContainer.style.removeProperty('display');
  }
  const vtSelected = getSelectedVontologyConceptId();
  // Prefer explicit Vontology selection; fallback to currentConceptType; then currentVontologyNodeId
  const conceptType = vtSelected || getCurrentConceptType();
  const identifier = conceptType || currentVontologyNodeId;
  if (!identifier) {
    console.warn("[handleShowSubtreeDetails] No Vontology identifier selected.");
    return;
  }

  if (!conceptType) {
    if (elements.subtreeDetailsStatus) {
      elements.subtreeDetailsStatus.textContent = "Please select a node from the tree first.";
      elements.subtreeDetailsStatus.style.color = "orange";
    }
    console.log("[ShowSubtreeDetailsButton] No node selected.");
    return;
  }

  console.log(`[ShowSubtreeDetailsButton] Clicked. Fetching details for path: ${conceptType}`);

  elements.showSubtreeDetailsButton.disabled = true;
  if (elements.subtreeDetailsStatus) {
    elements.subtreeDetailsStatus.textContent = "Loading subtree details...";
    elements.subtreeDetailsStatus.style.color = "black";
  }
  if (elements.subtreeDetailsDisplay) {
    elements.subtreeDetailsDisplay.textContent = "";
  }

  try {
    const encodedIdentifier = encodeURIComponent(identifier);
    const response = await fetch(`/vontology/api/vontology/nodes_details?identifier=${encodedIdentifier}`);
    const data = await response.json();

    if (!response.ok) {
      console.error(`[ShowSubtreeDetailsButton] HTTP error! status: ${response.status}`, data);
      throw new Error(data.error || `HTTP error! status: ${response.status}`);
    }

    if (elements.subtreeDetailsDisplay) {
      elements.subtreeDetailsDisplay.textContent = JSON.stringify(data, null, 2);
      console.log("[ShowSubtreeDetailsButton] Subtree details displayed:", data);
    }
    if (elements.subtreeDetailsStatus) {
      elements.subtreeDetailsStatus.textContent = `Details loaded for subtree starting at: ${identifier}`;
      elements.subtreeDetailsStatus.style.color = "green";
    }

  } catch (error) {
    console.error("[ShowSubtreeDetailsButton] Error fetching subtree details:", error);
    if (elements.subtreeDetailsDisplay) {
      elements.subtreeDetailsDisplay.textContent = `Error: ${error.message}`;
    }
    if (elements.subtreeDetailsStatus) {
      elements.subtreeDetailsStatus.textContent = "Failed to load details.";
      elements.subtreeDetailsStatus.style.color = "red";
    }
  } finally {
    elements.showSubtreeDetailsButton.disabled = !conceptType;
  }
}

// Handle Create Concept as TYPE
// Export for testing
export async function handleCreateType() {
  const newConceptName = elements.newConceptNameInput?.value.trim();
  // Always use the explicitly selected Vontology TYPE as parent (decoupled from tab state)
  const parentId = getSelectedVontologyConceptId() || getCurrentConceptType();

  if (!newConceptName) {
    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = "Please enter a concept name.";
      elements.createConceptStatusP.style.color = "red";
    }
    return;
  }

  // CHICKEN-AND-EGG FIX: Allow root creation when tree is empty (no parentId)
  // Check if tree is truly empty by looking for any vontology-node-name elements
  const hasExistingNodes = document.querySelector('.vontology-node-name[data-concept-id]') !== null;
  const isRootCreation = !parentId && !hasExistingNodes;

  if (!parentId && !isRootCreation) {
    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = "Please select a parent node first.";
      elements.createConceptStatusP.style.color = "red";
    }
    return;
  }

  try {
    if (elements.createTypeButton) elements.createTypeButton.disabled = true;
    if (elements.createInstanceButton) elements.createInstanceButton.disabled = true;
    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = isRootCreation ? "Creating root concept..." : "Creating type...";
      elements.createConceptStatusP.style.color = "black";
    }

    // CHICKEN-AND-EGG FIX: For root creation, omit parent_id entirely
    const requestPayload = {
      new_concept_name: newConceptName,
      create_as_instance: false
    };
    if (!isRootCreation && parentId) {
      requestPayload.parent_id = parentId;
    }

    const res = await vontologyFetch('/vontology/api/vontology/create_concept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestPayload)
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const response = await res.json();

    const createdConceptId = response?.concept_id || response?.concept?.concept_id;
    const createdConceptName = response?.concept?.name || newConceptName;
    const createdMongoId = response?.concept?.mongo_id;

    if (isRootCreation) {
      // For root creation, refresh the entire tree to show the new root
      console.log('[handleCreateType] Root concept created, refreshing tree...');
      await handleRefreshTree();
      // Auto-select the newly created root
      if (createdConceptId) {
        setTimeout(() => {
          selectVontologyNodeByIdentifier(createdConceptId, false);
        }, 500);
      }
    } else if (createdConceptId) {
      // Normal node insertion
      await insertNodeIntoVontologyTree(parentId, {
        id: createdConceptId,
        name: createdConceptName,
        mongo_id: createdMongoId
      });
    }

    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = `Type "${newConceptName}" created successfully!`;
      elements.createConceptStatusP.style.color = "green";
    }

    // Clear input after successful creation
    if (elements.newConceptNameInput) {
      elements.newConceptNameInput.value = '';
    }

    if (createdConceptId) {
      try {
        await selectVontologyNodeByIdentifier(createdConceptId, /*createConceptTab*/ false);
      } catch (e) {
        console.warn('Could not auto-select newly created node:', e);
      }
      const evt = new CustomEvent('open-concept-tab', {
        detail: { conceptId: createdConceptId, conceptName: createdConceptName, kind: 'unknown', activate: false, newlyCreated: true }
      });
      document.dispatchEvent(evt);
    }

  } catch (error) {
    console.error('Error creating type:', error);
    if (elements.createConceptStatusP) {
      const msg = (error && error.message) || 'Unknown error creating concept';
      elements.createConceptStatusP.textContent = `Error: ${msg}`;
      elements.createConceptStatusP.style.color = "red";
    }
  } finally {
    if (elements.createTypeButton) elements.createTypeButton.disabled = false;
    if (elements.createInstanceButton) elements.createInstanceButton.disabled = false;
  }
}

// Handle Create Concept as INDIVIDUAL INSTANCE
// Export for testing
export async function handleCreateInstance() {
  const newConceptName = elements.newConceptNameInput?.value.trim();
  // Always use the explicitly selected Vontology TYPE as parent (decoupled from tab state)
  const parentId = getSelectedVontologyConceptId() || getCurrentConceptType();

  if (!newConceptName) {
    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = "Please enter a concept name.";
      elements.createConceptStatusP.style.color = "red";
    }
    return;
  }

  if (!parentId) {
    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = "Please select a parent node first.";
      elements.createConceptStatusP.style.color = "red";
    }
    return;
  }

  try {
    if (elements.createTypeButton) elements.createTypeButton.disabled = true;
    if (elements.createInstanceButton) elements.createInstanceButton.disabled = true;
    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = "Creating instance...";
      elements.createConceptStatusP.style.color = "black";
    }

    const res = await vontologyFetch('/vontology/api/vontology/create_concept', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        new_concept_name: newConceptName,
        parent_id: parentId,
        create_as_instance: true
      })
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const response = await res.json();

    const createdConceptId = response?.concept_id || response?.concept?.concept_id;
    const createdConceptName = response?.concept?.name || newConceptName;
    const createdMongoId = response?.concept?.mongo_id;
    if (createdConceptId) {
      await insertNodeIntoVontologyTree(parentId, {
        id: createdConceptId,
        name: createdConceptName,
        mongo_id: createdMongoId
      });
    }

    if (elements.createConceptStatusP) {
      elements.createConceptStatusP.textContent = `Instance "${newConceptName}" created successfully!`;
      elements.createConceptStatusP.style.color = "green";
    }

    if (elements.newConceptNameInput) {
      elements.newConceptNameInput.value = '';
    }

    if (createdConceptId) {
      try {
        await selectVontologyNodeByIdentifier(createdConceptId, /*createConceptTab*/ false);
      } catch (e) {
        console.warn('Could not auto-select newly created node:', e);
      }
      // Open a new tab for the created instance in background and mark as NEW
      const evt = new CustomEvent('open-concept-tab', {
        detail: { conceptId: createdConceptId, conceptName: createdConceptName, kind: 'individual', activate: false, newlyCreated: true }
      });
      document.dispatchEvent(evt);

      // Notify other tabs (Concept tabs) that instances changed for this parent type
      try {
        const evt2 = new CustomEvent('von:instancesUpdated', {
          detail: { parentTypeId: parentId, createdInstanceId: createdConceptId }
        });
        document.dispatchEvent(evt2);
      } catch (e) {
        console.warn('[vontology] Failed to dispatch instancesUpdated event', e);
      }
    }

  } catch (error) {
    console.error('Error creating instance:', error);
    if (elements.createConceptStatusP) {
      const msg = (error && error.message) || 'Unknown error creating concept';
      elements.createConceptStatusP.textContent = `Error: ${msg}`;
      elements.createConceptStatusP.style.color = "red";
    }
  } finally {
    if (elements.createTypeButton) elements.createTypeButton.disabled = false;
    if (elements.createInstanceButton) elements.createInstanceButton.disabled = false;
  }
}

// Import/Export functions moved to importExportTab.js as part of JVNAUTOSCI-244 refactoring
// handleOntologyFileSelected function is now handled by the dedicated Import/Export tab



// Handle Export Ontology button click
// handleExportOntology function moved to importExportTab.js as part of JVNAUTOSCI-244 refactoring

// Handle Refresh Tree button click
async function handleRefreshTree() {
  console.log("[handleRefreshTree] Manual tree refresh requested");

  if (elements.refreshTreeButton) {
    elements.refreshTreeButton.disabled = true;
    elements.refreshTreeButton.textContent = "🔄 Refreshing...";
  }

  try {
    // Clear any cached tree data
    setVontologyTreeData(null);

    // Force a fresh fetch and render
    await fetchAndRenderVontologyTree({ forceRefresh: true });

    console.log("[handleRefreshTree] Tree refresh completed");

    if (elements.refreshTreeButton) {
      elements.refreshTreeButton.textContent = "✅ Refreshed!";
    }

    // Reset button text after a short delay
    setTimeout(() => {
      if (elements.refreshTreeButton) {
        elements.refreshTreeButton.textContent = "🔄 Refresh Tree";
      }
    }, 2000);

  } catch (error) {
    console.error("[handleRefreshTree] Error during tree refresh:", error);

    if (elements.refreshTreeButton) {
      elements.refreshTreeButton.textContent = "❌ Error";
    }

    // Reset button text after a short delay
    setTimeout(() => {
      if (elements.refreshTreeButton) {
        elements.refreshTreeButton.textContent = "🔄 Refresh Tree";
      }
    }, 2000);
  } finally {
    if (elements.refreshTreeButton) {
      elements.refreshTreeButton.disabled = false;
    }
  }
}

// Helper function to read file as text
// readFileAsText function moved to importExportTab.js as part of JVNAUTOSCI-244 refactoring

// collectExistingNodeIds function moved to importExportTab.js as part of JVNAUTOSCI-244 refactoring

function initializeVontologyTabDomElements() {
  console.log("Initializing Vontology tab DOM elements...");
  // Cache all DOM elements in the Vontology tab to avoid repeated lookups
  elements.vontologyTab = document.getElementById('vontologyTab');
  elements.vontologyTreeContainer = document.getElementById('vontologyTreeContainer');
  elements.selectedNodePathSpan = document.getElementById('selectedNodePath');
  // Ensure search elements are (re)bound when tab content is loaded dynamically
  elements.vontologySearchInput = document.getElementById('vontologySearchInput');
  elements.vontologySearchResults = document.getElementById('vontologySearchResults');
  elements.vontologyNodeContentDiv = document.getElementById('vontologyNodeContent');
  elements.vontologyNodeActions = document.getElementById('vontologyNodeActions');
  // If the content area isn't present yet (dynamic tab HTML hasn't been inserted),
  // create a minimal placeholder so selection logic can safely write into it.
  if (!elements.vontologyNodeContentDiv) {
    try {
      debugLog('[initializeVontologyTabDomElements] vontologyNodeContent not found; creating placeholder');
      const placeholder = document.createElement('div');
      placeholder.id = 'vontologyNodeContent';
      placeholder.className = 'vontology-node-content-placeholder';
      placeholder.dataset.vontologyPlaceholder = '1';
      // Keep it visually minimal and non-intrusive until the real UI mounts
      placeholder.style.minHeight = '80px';
      placeholder.style.padding = '8px';
      placeholder.style.border = '1px dashed rgba(0,0,0,0.06)';
      placeholder.style.display = 'none'; // hide until real container appears

      // Try to insert next to the tree container or into the vontologyTab if available
      if (elements.vontologyTreeContainer && elements.vontologyTreeContainer.parentElement) {
        elements.vontologyTreeContainer.parentElement.insertBefore(placeholder, elements.vontologyTreeContainer.nextSibling);
      } else if (elements.vontologyTab) {
        elements.vontologyTab.appendChild(placeholder);
      } else {
        document.body.appendChild(placeholder);
      }
      elements.vontologyNodeContentDiv = placeholder;
      // Observe DOM changes so we can swap the placeholder with the real container
      try {
        const observer = new MutationObserver((mutationsList, obs) => {
          try {
            const real = document.getElementById('vontologyNodeContent');
            if (real && real !== placeholder) {
              debugLog('[initializeVontologyTabDomElements] Real vontologyNodeContent detected; swapping placeholder');
              // Transfer innerHTML if placeholder had content
              if (placeholder.innerHTML && (!real.innerHTML || real.innerHTML.trim() === '')) {
                real.innerHTML = placeholder.innerHTML;
              }
              // If placeholder was hidden but real should be visible, copy display
              try {
                if (placeholder.style.display && placeholder.style.display !== 'none') {
                  real.style.display = placeholder.style.display;
                }
              } catch (e) { /* ignore style copy errors */ }

              // Update cached reference and clean up placeholder
              elements.vontologyNodeContentDiv = real;
              try { placeholder.remove(); } catch (e) { }
              obs.disconnect();
            }
          } catch (e) {
            // swallow errors inside observer callback
          }
        });
        observer.observe(document.body, { childList: true, subtree: true });
        // Safety: stop observing after 30s to avoid leaked observers
        setTimeout(() => { try { observer.disconnect(); } catch (_) { } }, 30000);
      } catch (e) {
        // Ignore observer installation failures
      }
    } catch (e) {
      console.warn('[initializeVontologyTabDomElements] failed to create placeholder vontologyNodeContent:', e);
    }
  }
  // Ensure a minimal actions container exists so callers can toggle .style.display safely
  if (!elements.vontologyNodeActions) {
    try {
      debugLog('[initializeVontologyTabDomElements] vontologyNodeActions not found; creating minimal actions placeholder');
      const actions = document.createElement('div');
      actions.id = 'vontologyNodeActions';
      actions.style.display = 'none';
      if (elements.vontologyNodeContentDiv && elements.vontologyNodeContentDiv.parentElement) {
        elements.vontologyNodeContentDiv.parentElement.insertBefore(actions, elements.vontologyNodeContentDiv.nextSibling);
      } else if (elements.vontologyTab) {
        elements.vontologyTab.appendChild(actions);
      } else {
        document.body.appendChild(actions);
      }
      elements.vontologyNodeActions = actions;
    } catch (e) {
      console.warn('[initializeVontologyTabDomElements] failed to create placeholder vontologyNodeActions:', e);
    }
  }
  elements.editDescriptionButton = document.getElementById('editDescriptionButton');
  elements.editDescriptionContainer = document.getElementById('editDescriptionContainer');
  elements.descriptionTextarea = document.getElementById('descriptionTextarea');
  elements.saveDescriptionButton = document.getElementById('saveDescriptionButton');
  elements.cancelEditDescriptionButton = document.getElementById('cancelEditDescriptionButton');
  elements.createConceptDiv = document.getElementById('vontologyCreateConcept');
  elements.newConceptNameInput = document.getElementById('newConceptName');
  elements.createTypeButton = document.getElementById('createTypeButton');
  elements.createInstanceButton = document.getElementById('createInstanceButton');
  elements.createConceptStatusP = document.getElementById('createConceptStatus');
  elements.showSubtreeDetailsButton = document.getElementById('showSubtreeDetailsButton');
  elements.subtreeDetailsDisplay = document.getElementById('subtreeDetailsDisplay');
  elements.subtreeDetailsStatus = document.getElementById('subtreeDetailsStatus');
  elements.subtreeDetailsContainer = document.getElementById('subtreeDetailsContainer');
  elements.ontologyFileInput = document.getElementById('ontologyFileInput');
  elements.importOntologyButton = document.getElementById('importOntologyButton');
  elements.importStatus = document.getElementById('importStatus');
  elements.exportOntologyButton = document.getElementById('exportOntologyButton');
  elements.exportStatus = document.getElementById('exportStatus');
  elements.refreshTreeButton = document.getElementById('refreshTreeButton');
  elements.collapseLinearCheckbox = document.getElementById('collapseLinearCheckbox');
  elements.filterRedundantCheckbox = document.getElementById('filterRedundantCheckbox');
  elements.showOnlyKeyConceptsCheckbox = document.getElementById('showOnlyKeyConceptsCheckbox');
  console.log("Vontology tab DOM elements initialized.");
}

// Initialize Vontology tab
export function initializeVontologyTab() {
  console.log("Initializing Vontology tab...");

  // Initialize DOM elements since the tab content is loaded dynamically
  initializeVontologyTabDomElements();
  initialiseResizableViewport(
    elements.vontologyTreeContainer,
    document.getElementById('vontologyTreeResizeControls'),
    {
      defaultHeightPx: 420,
      minHeightPx: 180,
      maxHeightPx: 900,
      stepPx: 80,
      minWidthPx: 320,
      widthStepPx: 120,
      viewportOffsetPx: 160,
      resizeAxis: 'both'
    }
  );
  const loadTreeButton = document.getElementById('loadVontologyTreeButton');

  // Wire up show only key concepts checkbox
  if (elements.showOnlyKeyConceptsCheckbox) {
    elements.showOnlyKeyConceptsCheckbox.addEventListener('change', (event) => {
      showOnlyKeyConceptsEnabled = event.target.checked;
      console.log('[initializeVontologyTab] Show only key concepts:', showOnlyKeyConceptsEnabled);

      // When enabled, add key concepts to forcedVisibleIds
      // When disabled, clear forcedVisibleIds
      if (showOnlyKeyConceptsEnabled) {
        forcedVisibleIds.clear();
        keyConceptIds.forEach(id => forcedVisibleIds.add(id));
        console.log(`[initializeVontologyTab] Added ${keyConceptIds.size} key concepts to forcedVisibleIds`);
      } else {
        forcedVisibleIds.clear();
        console.log('[initializeVontologyTab] Cleared forcedVisibleIds');
      }

      // Re-render the tree with the new filter
      fetchAndRenderVontologyTree();
    });
  }

  // CRITICAL FIX: Wire up create concept buttons
  if (elements.createTypeButton) {
    elements.createTypeButton.addEventListener('click', async () => {
      await handleCreateType();
    });
    console.log('[initializeVontologyTab] Create Type button wired up');
  }

  if (elements.createInstanceButton) {
    elements.createInstanceButton.addEventListener('click', async () => {
      await handleCreateInstance();
    });
    console.log('[initializeVontologyTab] Create Instance button wired up');
  }

  // Also wire up refresh tree button while we're at it
  if (elements.refreshTreeButton) {
    elements.refreshTreeButton.addEventListener('click', () => {
      handleRefreshTree();
    });
    console.log('[initializeVontologyTab] Refresh Tree button wired up');
  }

  if (loadTreeButton) {
    loadTreeButton.addEventListener('click', async () => {
      let loadedOk = false;
      loadTreeButton.disabled = true;
      loadTreeButton.textContent = 'Loading...';
      try {
        await fetchAndRenderVontologyTree();
        loadedOk = true;
        updateManualLoadUi(false);
      } finally {
        if (loadedOk) {
          try { loadTreeButton.classList.add('hidden'); } catch (_) { }
        } else {
          try { loadTreeButton.classList.remove('hidden'); } catch (_) { }
        }
        loadTreeButton.disabled = false;
        loadTreeButton.textContent = 'Load Vontology Tree';
      }
    });
  }

  if (elements.vontologyTreeContainer) {
    console.log("Calling maybeLoadVontologyTree...");
    void maybeLoadVontologyTree();
  } else {
    console.error("vontologyTreeContainer not found during initialization!");
  }

  console.log("Vontology tab initialization complete.");
}

// Export aliases for testing compatibility
export async function fetchVontologyData() {
  try {
    const response = await fetch('/vontology/api/vontology/tree');
    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }
    return await response.json();
  } catch (error) {
    console.error('Error fetching vontology data:', error);
    return null;
  }
}

// Background preloader to fetch Vontology data early (while Chat tab is active)
export function preloadVontologyData() {
  if (__vontologyPreloadInFlight) {
    return __vontologyPreloadInFlight;
  }
  console.log('[preloadVontologyData] Checking whether to preload Vontology tree...');
  const preloadGeneration = __vontologyPreloadGeneration;
  __vontologyPreloadInFlight = (async () => {
    let preloadTask = null;
    try {
      // Fetch settings to determine if counts should be fetched on load
      let fetchCountsOnLoad = true;
      let preloadVontologyTree = false;
      try {

        const settingsRes = await fetch('/api/settings');
        if (settingsRes.ok) {
          const settingsJson = await settingsRes.json();
          if (settingsJson && Object.prototype.hasOwnProperty.call(settingsJson, 'fetch_counts_on_load')) {
            fetchCountsOnLoad = !!settingsJson.fetch_counts_on_load;
          }
          if (settingsJson && Object.prototype.hasOwnProperty.call(settingsJson, 'preload_vontology_tree')) {
            preloadVontologyTree = !!settingsJson.preload_vontology_tree;
          }
        }
      } catch (e) {
        // Default to true if settings fetch fails (safe fallback)
        fetchCountsOnLoad = true;
        preloadVontologyTree = false;
      }
      // Set a global flag to be read by tree render to decide decoupling
      try { if (typeof window !== 'undefined') window.__VONTOLOGY_DECOUPLE_COUNTS__ = !fetchCountsOnLoad; } catch (_) { }
      try { if (typeof window !== 'undefined') window.__VONTOLOGY_PRELOAD_ENABLED__ = !!preloadVontologyTree; } catch (_) { }
      try { window.dispatchEvent(new CustomEvent('von:settingsLoaded', { detail: { fetchCountsOnLoad, preloadVontologyTree } })); } catch (_) { }

      if (!preloadVontologyTree) {
        console.log('[preloadVontologyData] Preload disabled by settings.');
        return null;
      }

      console.log('[preloadVontologyData] Starting background preload of Vontology tree (and counts if enabled)...');
      preloadTask = startBackgroundTask('preload_vontology_tree', {
        label: 'Preload Vontology tree',
        detail: fetchCountsOnLoad ? 'Tree + entity counts' : 'Tree only'
      });
      try { if (typeof window !== 'undefined') window.__VONTOLOGY_BUSY = true; } catch (_) { }

      const treePromise = fetch('/vontology/api/vontology/tree').then(r => r.ok ? r.json() : Promise.reject(`HTTP error! status: ${r.status}`));
      let countsJson = null;
      if (fetchCountsOnLoad) {
        try {
          countsJson = await fetch('/vontology/api/vontology/entity_counts').then(r => r.ok ? r.json() : Promise.reject(`HTTP error! status: ${r.status}`));
        } catch (e) {
          console.warn('[preloadVontologyData] entity_counts preload failed:', e);
        }
      }
      const tree = await treePromise;
      if (!publishVontologyPreload(preloadGeneration, tree, countsJson?.entity_counts || null)) {
        console.log('[preloadVontologyData] Discarding a preload retired by an explicit refresh.');
        if (preloadTask) {
          finishBackgroundTask(preloadTask, {
            status: 'success',
            detail: 'Preload retired by explicit refresh'
          });
          preloadTask = null;
        }
        return null;
      }
      // Also seed the global state so helpers that rely on stored tree can function sooner
      try { setVontologyTreeData(tree); } catch (_) { }
      console.log('[preloadVontologyData] Preload complete. counts_on_load=', fetchCountsOnLoad);
      if (preloadTask) {
        finishBackgroundTask(preloadTask, {
          status: 'success',
          detail: fetchCountsOnLoad ? 'Tree + counts preloaded' : 'Tree preloaded'
        });
        preloadTask = null;
      }
    } catch (err) {
      if (preloadTask) {
        finishBackgroundTask(preloadTask, {
          status: 'error',
          error: err
        });
      }
      console.warn('[preloadVontologyData] Preload failed:', err);
    }
  })().finally(() => {
    // Allow subsequent preloads if needed (e.g., after a long idle)
    setTimeout(() => { __vontologyPreloadInFlight = null; }, 0);
    try { if (typeof window !== 'undefined') window.__VONTOLOGY_BUSY = false; } catch (_) { }
  });
  return __vontologyPreloadInFlight;
}

// Helper for other modules to query busy status without touching window directly
export function isVontologyBusy() {
  try { return typeof window !== 'undefined' && !!window.__VONTOLOGY_BUSY; } catch (_) { return false; }
}

function retireVontologyPreload() {
  __vontologyPreloadGeneration += 1;
  __vontologyPreloadedTreeData = null;
  __vontologyPreloadedEntityCounts = null;
}

function publishVontologyPreload(generation, tree, entityCounts) {
  if (generation !== __vontologyPreloadGeneration) {
    return false;
  }
  __vontologyPreloadedTreeData = tree;
  __vontologyPreloadedEntityCounts = entityCounts;
  return true;
}

function getCachedPreloadSetting() {
  try {
    if (typeof window !== 'undefined' && typeof window.__VONTOLOGY_PRELOAD_ENABLED__ === 'boolean') {
      return window.__VONTOLOGY_PRELOAD_ENABLED__;
    }
  } catch (_) { }
  return null;
}

async function resolveVontologyPreloadSetting() {
  const cached = getCachedPreloadSetting();
  if (cached !== null) {
    return cached;
  }

  try {
    const settingsRes = await fetch('/api/settings');
    if (settingsRes.ok) {
      const settingsJson = await settingsRes.json();
      const preloadFlag = !!settingsJson?.preload_vontology_tree;
      try { if (typeof window !== 'undefined') window.__VONTOLOGY_PRELOAD_ENABLED__ = preloadFlag; } catch (_) { }
      return preloadFlag;
    }
  } catch (_) { }

  return false;
}

function updateManualLoadUi(enabled) {
  const loadTreeButton = document.getElementById('loadVontologyTreeButton');
  const badge = document.getElementById('vontologyPreloadOffBadge');
  const helper = document.getElementById('vontologyPreloadOffHelp');
  const refreshButton = elements.refreshTreeButton || document.getElementById('refreshTreeButton');
  if (loadTreeButton) {
    loadTreeButton.classList.toggle('hidden', !enabled);
    loadTreeButton.disabled = false;
    loadTreeButton.textContent = 'Load Vontology Tree';
  }
  if (badge) {
    badge.classList.toggle('hidden', !enabled);
  }
  if (helper) {
    helper.classList.toggle('hidden', !enabled);
  }
  if (refreshButton) {
    refreshButton.disabled = !!enabled;
  }

  if (enabled && elements.vontologyTreeContainer) {
    elements.vontologyTreeContainer.innerHTML = '<p>Vontology tree preload is off. Click "Load Vontology Tree" to fetch.</p>';
  }
}

export async function maybeLoadVontologyTree() {
  const autoLoad = await resolveVontologyPreloadSetting();
  if (!autoLoad) {
    updateManualLoadUi(true);
    return false;
  }
  updateManualLoadUi(false);
  if (elements.refreshTreeButton) {
    elements.refreshTreeButton.disabled = true;
  }
  try {
    await fetchAndRenderVontologyTree();
  } finally {
    if (elements.refreshTreeButton) {
      elements.refreshTreeButton.disabled = false;
    }
  }
  return true;
}

export function renderVontologyTree(concepts) {
  if (!elements.vontologyTreeContainer) {
    elements.vontologyTreeContainer = document.getElementById("vontologyTreeContainer");
  }

  if (!elements.vontologyTreeContainer) {
    console.error('Vontology tree container not found');
    return;
  }

  clearContainer(elements.vontologyTreeContainer);

  if (!concepts || concepts.length === 0) {
    return;
  }

  concepts.forEach(concept => {
    const conceptElement = document.createElement('div');
    conceptElement.textContent = concept.name || concept.id;
    conceptElement.setAttribute('data-concept-id', concept.id);
    conceptElement.onclick = () => selectConcept(concept);
    elements.vontologyTreeContainer.appendChild(conceptElement);
  });
}

export function searchVontology(concepts, searchTerm) {
  if (!concepts || !searchTerm) {
    return [];
  }

  const lowerSearchTerm = searchTerm.toLowerCase();
  return concepts.filter(concept =>
    (concept.name && concept.name.toLowerCase().includes(lowerSearchTerm)) ||
    (concept.description && concept.description.toLowerCase().includes(lowerSearchTerm)) ||
    (concept.id && concept.id.toLowerCase().includes(lowerSearchTerm))
  );
}

function selectConcept(concept) {
  // This is a placeholder - in the real implementation this would trigger detailed concept display
  console.log('Concept selected:', concept);
}

// --- Utility functions ---

// Filter redundant nodes from the tree
//
// CRITICAL: This function relies on entity count data that distinguishes between:
// - TYPES (concepts that form the ontology hierarchy)
// - ENTITIES (actual instances of those types)
// See docs/vontology/relationship_authoritative_pathway.md for details.
//
// The filtering removes nodes that have no entity instances associated with them,
// keeping only nodes that either have entities or lead to nodes with entities.
export async function filterRedundantNodes(tree, entityCounts, forcedVisibleIds = new Set()) {
  /**
   * Filter redundant nodes from the tree while preserving complete ontology paths.
   *
   * MODIFIED (JVNAUTOSCI-748): Fixed issue where intermediate nodes were being filtered out,
   * breaking the hierarchy display. The filter now:
   *
   * 1. PRESERVES all nodes that have children (they're part of valid paths)
   * 2. PRESERVES all leaf nodes (they're valid type concepts even without instances)
   * 3. PRESERVES nodes without entity count data (common for pure types)
   * 4. Only filters legacy nodes (non-#V# prefixed)
   *
   * Previous behavior: Would collapse intermediate nodes with no entities and one child,
   * causing chains like Thing -> AI -> ML -> DL to display as only Thing -> DL.
   *
   * New behavior: Maintains complete hierarchy structure, showing all intermediate
   * concepts regardless of entity counts.
   */
  if (!tree) {
    return tree;
  }

  const clone = typeof structuredClone === 'function'
    ? structuredClone(tree)
    : JSON.parse(JSON.stringify(tree));

  function isForcedVisible(node) {
    if (!node || !node.id) return false;
    if (forcedVisibleIds.has(node.id)) return true;
    // Also keep ancestors/descendants if any forced id is in this subtree
    // Quick check: if any descendant is forced, bubble up keep
    if (node.children && node.children.length) {
      for (const child of node.children) {
        if (isForcedVisible(child)) return true;
      }
    }
    return false;
  }

  function hasEntitiesInSubtree(node) {
    // Check if this node or any of its descendants has entities
    if (!node.id) {
      console.log(`[hasEntitiesInSubtree] Node has no ID, keeping: ${node.name}`);
      return true; // No node ID, keep the node to be safe
    }

    // Global safety: if we have no entity count data at all, keep all nodes
    if (!entityCounts || Object.keys(entityCounts).length === 0) {
      return true;
    }

    const entityData = entityCounts[node.id];
    if (entityData) {
      const hasEntities = entityData.has_entities;
      if (hasEntities) {
        return true;
      }
      // This node has no entities, but check its children
    } else {
      // MODIFIED: No entity count data for this specific node
      // This is common for pure type concepts with no instances yet
      // If it has children, we should check them; if it's a leaf, keep it
      // (it's part of the ontology structure even if it has no instances)
      if (!node.children || node.children.length === 0) {
        // Leaf node with no entity data - keep it (it's a valid type concept)
        return true;
      }
      // Has children - check them recursively
    }

    // Check children recursively - CRITICAL: This preserves paths to leaf nodes
    // Even if intermediate nodes have no entities, they're part of valid paths
    if (node.children && node.children.length > 0) {
      return node.children.some(child => hasEntitiesInSubtree(child));
    }

    // Node has no entities and no children with entities
    return false;
  }

  function hasEntities(node) {
    if (!entityCounts || !node.id) return true; // Default to keeping nodes if no entity data
    const entityData = entityCounts[node.id];
    return entityData ? entityData.has_entities : true;
  }

  function isRedundant(node) {
    // MODIFIED: A node is redundant ONLY if it's truly isolated (no children with entities in subtree)
    // We should NOT collapse intermediate nodes that form valid paths in the ontology hierarchy
    //
    // Original logic was too aggressive: it would skip intermediate nodes with no entities and one child
    // This breaks hierarchical paths like: Thing -> AI -> ML -> DL -> Networks -> Transformers
    //
    // NEW RULE: Never consider a node redundant if it has any descendants (directly or indirectly)
    // This preserves the complete ontology structure while still filtering truly empty branches

    if (!node.id || !entityCounts[node.id]) {
      return false; // No entity count data, keep the node to be safe
    }

    // Don't collapse nodes that are part of a path - only filter completely empty branches
    // A node is redundant ONLY if it has no children at all AND no entities
    const hasChildren = node.children && node.children.length > 0;
    if (hasChildren) {
      // If it has children, it's part of a path structure - keep it
      return false;
    }

    // Leaf nodes are kept (even if empty) to show the complete ontology
    return false;
  }

  async function filter(node) {
    // Force-keep node if requested (and thus its path)
    if (isForcedVisible(node)) {
      // Keep this node unconditionally, but filter children normally.
      // This preserves siblings with entities while ensuring the forced path remains visible.
      const keptChildren = [];
      const kids = node.children || [];
      for (let i = 0; i < kids.length; i++) {
        const child = await filter(kids[i]);
        if (child) keptChildren.push(child);
        if ((i + 1) % RENDER_BATCH_SIZE === 0) {
          await yieldThread();
        }
      }
      return { ...node, children: keptChildren };
    }

    // First, check if the node itself is a legacy concept and should be removed.
    if (node.id && !node.id.startsWith('#V#')) {
      debugLog(`[filterRedundantNodes] Filtering out legacy node: ${node.name} (${node.id})`);
      return null; // Remove this node and its entire subtree
    }

    if (!node.children || node.children.length === 0) {
      // Leaf node: always keep (even if empty) to preserve complete ontology structure
      return node;
    }

    // Recursively filter children
    const filteredChildren = [];

    for (let i = 0; i < node.children.length; i++) {
      const child = node.children[i];
      const filteredChild = await filter(child);

      if (filteredChild === null) {
        continue;
      }

      // MODIFIED: Don't apply redundancy collapsing - preserve the hierarchy
      // The old logic would skip intermediate nodes, breaking the tree structure
      filteredChildren.push(filteredChild);

      if ((i + 1) % RENDER_BATCH_SIZE === 0) {
        await yieldThread();
      }
    }

    return {
      ...node,
      children: filteredChildren
    };
  }

  async function filterTree(nodes) {
    if (Array.isArray(nodes)) {
      debugLog(`[filterRedundantNodes] Processing array of ${nodes.length} root nodes:`);
      const finalFiltered = [];
      for (let i = 0; i < nodes.length; i++) {
        const node = nodes[i];
        debugLog(`[filterRedundantNodes] Root node ${i}: ${node.name} (${node.id})`);
        const isLeaf = !node.children || node.children.length === 0;

        // MODIFIED: If a node has children, it's part of the hierarchy - always keep it
        // Only filter root nodes that are truly empty (no children, no entities)
        const hasChildren = node.children && node.children.length > 0;
        const shouldKeep = isForcedVisible(node) || hasChildren || isLeaf || hasEntitiesInSubtree(node);

        if (!shouldKeep) {
          debugLog(`[filterRedundantNodes] FILTERING OUT disconnected root: ${node.name} (${node.id}) - no entities in subtree`);
        } else {
          debugLog(`[filterRedundantNodes] KEEPING root: ${node.name} (${node.id})`);
          const filteredNode = await filter(node);
          if (filteredNode !== null) {
            finalFiltered.push(filteredNode);
          }
        }
        if ((i + 1) % RENDER_BATCH_SIZE === 0) {
          await yieldThread();
        }
      }
      debugLog(`[filterRedundantNodes] Final result: ${nodes.length} -> ${finalFiltered.length} root nodes`);
      return finalFiltered;
    } else if (nodes) {
      const isLeaf = !nodes.children || nodes.children.length === 0;
      const hasChildren = nodes.children && nodes.children.length > 0;

      // MODIFIED: If a node has children, it's part of the hierarchy - keep it
      const shouldKeep = isForcedVisible(nodes) || hasChildren || isLeaf || hasEntitiesInSubtree(nodes);

      if (!shouldKeep) {
        debugLog(`[filterRedundantNodes] Filtering out single disconnected node: ${nodes.name} (${nodes.id}) - no entities in subtree`);
        return null;
      }
      return await filter(nodes);
    }
    return nodes;
  }

  return await filterTree(clone);
}

// --- Test Hooks (JVNAUTOSCI-550) -------------------------------------------------
// Export minimal controlled hooks for Jest tests validating queue & readiness logic.
// These are intentionally lightweight and gated so production behaviour is unaffected.
// Export for testing
export function __test_getTreeReady() { return typeof __vontologyTreeReady !== 'undefined' ? __vontologyTreeReady : false; }
// Export for testing
export function __test_setTreeReady(val) { if (typeof val === 'boolean') { __vontologyTreeReady = val; } }
// Export for testing
export function __test_getPendingSelections() { return Array.isArray(__pendingTreeSelections) ? [...__pendingTreeSelections] : []; }
// Export for testing
export function __test_clearPendingSelections() { if (Array.isArray(__pendingTreeSelections)) { __pendingTreeSelections.length = 0; } }

// Export for testing
export function __test_getVontologyPreloadState() {
  return {
    generation: __vontologyPreloadGeneration,
    tree: __vontologyPreloadedTreeData,
    entityCounts: __vontologyPreloadedEntityCounts
  };
}
// Export for testing
export function __test_publishVontologyPreload(generation, tree, entityCounts) {
  return publishVontologyPreload(generation, tree, entityCounts);
}
// Export for testing
export function __test_retireVontologyPreload() { retireVontologyPreload(); }

// Export for testing
export function __test_selectSearchItem(item) { return selectSearchItem(item); }
export function __test_getUnifiedSearchState() {
  return {
    items: __vontologySearchState.items.map(item => ({ ...item })),
    activeIndex: __vontologySearchState.activeIndex,
    lastQuery: __vontologySearchState.lastQuery,
    mode: __vontologySearchState.mode,
    concepts: { ...__vontologySearchState.concepts, items: __vontologySearchState.concepts.items.map(item => ({ ...item })) },
    conversations: { ...__vontologySearchState.conversations, items: __vontologySearchState.conversations.items.map(item => ({ ...item })) },
    recoveryCount: __vontologySearchState.recoveryCount,
    recoveryCountConfirmedZero: __vontologySearchState.recoveryCountConfirmedZero,
    recoveryCountCoverageComplete: __vontologySearchState.recoveryCountCoverageComplete
  };
}
export function __test_openRemovedConversations() { return openRemovedConversations(); }
export function __test_refreshConversationRecoveryCount() { return refreshConversationRecoveryCount(); }
export function __test_loadMoreConversationSearchResults() { return loadMoreConversationSearchResults(); }
// Export for testing
export function __test_renderSelectedNodePathContent(target, selectedNode, parents = []) {
  renderSelectedNodePathContent(target, selectedNode, parents);
}


// Build a map of how many times each node appears as a child in the tree
export function buildParentCountMap(tree) {
  const parentCount = {};

  function traverse(node) {
    if (node.children && node.children.length > 0) {
      node.children.forEach(child => {
        if (child.id) {
          parentCount[child.id] = (parentCount[child.id] || 0) + 1;
        }
        traverse(child);
      });
    }
  }

  if (Array.isArray(tree)) {
    tree.forEach(traverse);
  } else if (tree) {
    traverse(tree);
  }

  return parentCount;
}

// Collapse linear chains in the tree while respecting multiple parentage and entity counts
export async function filterLinearTreeData(tree, parentCount, entityCounts = null) {
  if (!tree) {
    return tree;
  }

  const clone = typeof structuredClone === 'function'
    ? structuredClone(tree)
    : JSON.parse(JSON.stringify(tree));

  function hasEntitiesInSubtree(node) {
    // Check if this node or any of its descendants has entities
    if (!entityCounts || !node.id) return true; // Default to keeping nodes if no entity data

    // Global safety: if we have no entity count data at all, keep all nodes
    if (Object.keys(entityCounts).length === 0) {
      return true;
    }

    const entityData = entityCounts[node.id];
    if (entityData) {
      const hasEntities = entityData.has_entities;
      if (hasEntities) {
        return true;
      }
      // This node has no entities, but check its children
    } else {
      // No entity count data for this specific node - assume it has no entities
      // but still check children
    }

    // Check children recursively
    if (node.children && node.children.length > 0) {
      return node.children.some(child => hasEntitiesInSubtree(child));
    }

    // Node has no entities and no children with entities
    return false;
  }

  function hasEntities(node) {
    if (!entityCounts || !node.id) return true; // Default to keeping nodes if no entity data
    const entityData = entityCounts[node.id];
    return entityData ? entityData.has_entities : true;
  }

  async function collapse(node) {
    if (!node.children || node.children.length === 0) {
      return node;
    }

    const newChildren = [];
    for (let i = 0; i < node.children.length; i++) {
      const child = node.children[i];
      newChildren.push(await collapse(child));
      if ((i + 1) % RENDER_BATCH_SIZE === 0) {
        await yieldThread();
      }
    }
    node.children = newChildren;

    // Check if this node is part of a linear chain
    if (node.children.length === 1) {
      const child = node.children[0];
      const count = child.id && parentCount ? parentCount[child.id] : 1;

      // Only collapse if the child has only one parent (this node)
      if (count === 1) {
        // Only collapse chains of nodes that have no entities
        if (!hasEntities(node) && !hasEntities(child)) {
          // Find the end of the collapsible chain (only nodes without entities)
          let chainEnd = child;
          let chainPath = [node.name];

          while (chainEnd.children && chainEnd.children.length === 1 && !hasEntities(chainEnd)) {
            const nextChild = chainEnd.children[0];
            const nextCount = nextChild.id && parentCount ? parentCount[nextChild.id] : 1;

            if (nextCount === 1) {
              chainPath.push(chainEnd.name);
              chainEnd = nextChild;
            } else {
              break;
            }
          }

          // If we found a chain of redundant nodes, create a collapsed representation
          if (chainPath.length > 1) {
            // The chain ends at the first node with entities or a branching point
            const finalNode = hasEntities(chainEnd) ? chainEnd : child;
            chainPath.push(finalNode.name);

            // Create a new node that represents the collapsed chain
            const collapsedNode = {
              ...finalNode,  // Use the final node's properties (id, path, etc.)
              name: `${node.name} → ... → ${finalNode.name}`,
              originalName: finalNode.name,
              isCollapsed: true,
              chainPath: chainPath,
              chainStartNode: node,
              children: finalNode.children || []
            };

            return collapsedNode;
          }
        }
      }
    }

    return node;
  }

  async function filterTree(nodes) {
    if (Array.isArray(nodes)) {
      debugLog(`[filterLinearTreeData] Processing array of ${nodes.length} root nodes:`);
      const filtered = [];
      for (let i = 0; i < nodes.length; i++) {
        const node = nodes[i];
        const shouldKeep = hasEntitiesInSubtree(node);
        if (!shouldKeep) {
          debugLog(`[filterLinearTreeData] Filtering out disconnected subtree: ${node.name} (${node.id}) - no entities in subtree`);
        } else {
          filtered.push(await collapse(node));
        }
        if ((i + 1) % RENDER_BATCH_SIZE === 0) {
          await yieldThread();
        }
      }
      return filtered.filter(node => node !== null);
    } else if (nodes) {
      const shouldKeep = hasEntitiesInSubtree(nodes);
      if (!shouldKeep) {
        debugLog(`[filterLinearTreeData] Filtering out single disconnected node: ${nodes.name} (${nodes.id}) - no entities in subtree`);
        return null;
      }
      return await collapse(nodes);
    }
    return nodes;
  }

  return await filterTree(clone);
}
