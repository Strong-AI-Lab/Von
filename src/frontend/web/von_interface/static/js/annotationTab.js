import { acceptAnnotation, annotateTurn, createInstance, createType, revokeAnnotation, searchTypes } from './apiService.js';

// Sample preset text for quick demo usage
const SAMPLE_TEXT = `The hippocampus plays a crucial role in spatial memory consolidation. Recent work by O'Keefe and colleagues suggests place cells form a cognitive map. Disruption of NMDA receptor signaling impairs LTP and downstream memory encoding pathways.`;

// Ephemeral per-page session state for each annotation tab (resets on full page reload)
const sessionStates = new Map();

// Cache of concept_id -> kind ("type" | "individual" | "unknown") to avoid repeated node_content fetches
export const conceptKindCache = new Map(); // Export for testing
// Track in-flight fetch promises to deduplicate concurrent classification lookups
const inFlightKindFetch = new Map();

async function fetchConceptKind(conceptId) {
  if (!conceptId || typeof conceptId !== 'string') return 'unknown';
  if (conceptKindCache.has(conceptId)) return conceptKindCache.get(conceptId);
  if (inFlightKindFetch.has(conceptId)) return inFlightKindFetch.get(conceptId);
  const p = (async () => {
    try {
      const resp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(conceptId)}`);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      const kind = (data && data.computed_kind) || 'unknown';
      conceptKindCache.set(conceptId, kind);
      return kind;
    } catch (e) {
      conceptKindCache.set(conceptId, 'unknown');
      return 'unknown';
    } finally {
      inFlightKindFetch.delete(conceptId);
    }
  })();
  inFlightKindFetch.set(conceptId, p);
  return p;
}

function classifySuggestion(suggestion) {
  if (!suggestion || !Array.isArray(suggestion.candidates) || !suggestion.candidates.length) return 'hypothesis';
  // Use first candidate with concept_id
  const cand = suggestion.candidates.find(c => c && (c.concept_id || c.id || c.conceptId));
  if (!cand) return 'hypothesis';
  const cid = cand.concept_id || cand.id || cand.conceptId;
  if (!cid) return 'hypothesis';
  const kind = conceptKindCache.get(cid);
  if (kind === 'individual') return 'individual';
  if (kind === 'type') return 'type';
  return 'unknown'; // unresolved yet
}

function applySpanClassification(resultsContainer, highlightPane, suggestions) {
  if (!resultsContainer || !highlightPane || !Array.isArray(suggestions)) return;
  suggestions.forEach((s, idx) => {
    const block = resultsContainer.querySelector(`.annotation-span-block[data-span-index="${idx}"]`);
    const hl = highlightPane.querySelector(`.hl-span[data-span-index="${idx}"]`);
    if (!block && !hl) return;
    const cls = classifySuggestion(s);
    const classListTargets = [block, hl].filter(Boolean);
    classListTargets.forEach(el => {
      el.classList.remove('kind-type', 'kind-individual', 'kind-hypothesis');
      if (cls === 'type') el.classList.add('kind-type');
      else if (cls === 'individual') el.classList.add('kind-individual');
      else if (cls === 'hypothesis') el.classList.add('kind-hypothesis');
    });
  });
}

function scheduleKindResolution(resultsContainer, highlightPane, suggestions) {
  if (!Array.isArray(suggestions)) return;
  const toResolve = new Set();
  suggestions.forEach(s => {
    if (!s || !Array.isArray(s.candidates)) return;
    const cand = s.candidates.find(c => c && (c.concept_id || c.id || c.conceptId));
    if (!cand) return;
    const cid = cand.concept_id || cand.id || cand.conceptId;
    if (cid && !conceptKindCache.has(cid)) toResolve.add(cid);
  });
  if (!toResolve.size) return;
  // Fetch all concept kinds (fire & forget); upon resolution re-apply classification
  Promise.all(Array.from(toResolve).map(cid => fetchConceptKind(cid))).then(() => {
    try { applySpanClassification(resultsContainer, highlightPane, suggestions); } catch (_) { /* ignore */ }
  });
}

function getSpanSources(s) {
  // Prefer embedded span.source if present; fall back to s.span.source -> s.source(s)
  if (s && s.span && s.span.source) return Array.isArray(s.span.source) ? s.span.source : [s.span.source];
  if (s && s.sources) return Array.isArray(s.sources) ? s.sources : [s.sources];
  if (s && s.source) return Array.isArray(s.source) ? s.source : [s.source];
  return [];
}

const _typeIdCache = new Map();

// Export for testing
export async function resolveSuggestedTypes(suggestions) {
  if (!Array.isArray(suggestions)) return;
  const names = new Set();
  suggestions.forEach(s => {
    const t = s && s.span && typeof s.span.type === 'string' ? s.span.type.trim() : '';
    if (t) names.add(t);
  });
  const map = new Map();
  for (const name of names) {
    let cid = _typeIdCache.get(name.toLowerCase());
    if (!cid) {
      try {
        const res = await searchTypes(name, 1);
        const item = res && res[0];
        cid = item && (item.concept_id || item.id);
        if (cid && !cid.startsWith('#V#')) cid = `#V#${cid}`;
        if (cid) _typeIdCache.set(name.toLowerCase(), cid);
      } catch (_) { /* ignore */ }
    }
    if (cid) map.set(name, cid);
  }
  suggestions.forEach(s => {
    const t = s && s.span && s.span.type;
    if (t && map.has(t)) {
      s.suggested_type_id = map.get(t);
    }
  });
}

// Utility to add a listener only once per element+type+namespace
function attachOnce(el, type, ns, handler) {
  if (!el) return;
  const key = `__bound_${type}_${ns}`;
  if (el[key]) return; // already bound
  el.addEventListener(type, handler);
  el[key] = true;
}

// Export for testing
export function buildAnnotationRecord(s, idx, turnId) {
  const span = s && s.span ? s.span : {};
  const record = {
    id: s && s.id ? s.id : `ann-${turnId}-${idx}`,
    turn_id: turnId,
    span: {
      start: span.start,
      end: span.end,
      text: span.text || ''
    },
    sources: getSpanSources(s),
    status: 'suggested'
  };
  let kind = classifySuggestion(s);
  if (kind === 'individual') kind = 'instance';
  if (kind === 'type') {
    const c = (s.candidates && s.candidates[0]) || {};
    record.kind = 'type';
    record.target = {
      concept_id: c.concept_id || c.id || c.conceptId || '',
      name: c.name || ''
    };
    if (c.confidence !== undefined) record.confidence_score = c.confidence;
  } else if (kind === 'instance') {
    const c = (s.candidates && s.candidates[0]) || {};
    record.kind = 'instance';
    record.target = {
      instance_id: c.concept_id || c.id || c.conceptId || '',
      name: c.name || '',
      type_id: c.type_id || c.typeId || ''
    };
    if (c.confidence !== undefined) record.confidence_score = c.confidence;
  } else {
    record.kind = 'hypothesis';
    record.target = {
      proposed_name: span.text || '',
      suggested_type_id: s && s.suggested_type_id ? s.suggested_type_id : '',
      value: span.text || ''
    };
    if (s && s.confidence_score !== undefined) record.confidence_score = s.confidence_score;
  }
  return record;
}

// Export for testing
export function openAnnotationJsonModal(record, rect) {
  if (!record) return;
  const existing = document.querySelector('.annotation-json-modal');
  if (existing) existing.remove();
  const modal = document.createElement('div');
  modal.className = 'annotation-json-modal';
  // Use fixed positioning so panel stays anchored to viewport even if page scrolls
  if (rect && typeof rect === 'object') {
    modal.style.position = 'fixed';
    const padding = 6;
    let left = rect.left;
    let top = rect.bottom + padding;
    const vpW = window.innerWidth || 1024;
    const vpH = window.innerHeight || 768;
    // Clamp width after creation (CSS sets max-width); adjust for overflow
    if (left + 420 > vpW) left = Math.max(4, vpW - 420 - 4);
    // If not enough space below show above
    if (top + 320 > vpH) {
      const altTop = rect.top - 320 - padding;
      if (altTop >= 4) top = altTop;
    }
    modal.style.left = `${Math.max(4, left)}px`;
    modal.style.top = `${Math.max(4, top)}px`;
  }
  // Inner structure (header, body, footer) mirrors raw-json modal patterns for consistency
  modal.innerHTML = `
    <div class="annotation-json-header">
      <span class="annotation-json-title">Annotation JSON</span>
      <button class="annotation-json-close btn-mini" title="Close" aria-label="Close dialog">×</button>
    </div>
    <div class="annotation-json-body">
      <pre class="annotation-json-content"></pre>
    </div>
    <div class="annotation-json-footer">
      <div class="annotation-json-actions">
        <button class="annotation-json-copy btn-mini" title="Copy JSON">Copy JSON</button>
      </div>
    </div>`;
  const pre = modal.querySelector('.annotation-json-content');
  if (pre) pre.textContent = JSON.stringify(record, null, 2);
  // Close handlers
  const doClose = () => modal.remove();
  const c1 = modal.querySelector('.annotation-json-close');
  if (c1) c1.addEventListener('click', doClose);
  // Copy handler
  const copyBtn = modal.querySelector('.annotation-json-copy');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      try {
        navigator.clipboard.writeText(pre.textContent || '');
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 1500);
      } catch (e) {
        copyBtn.textContent = 'Copy failed';
        setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 1500);
      }
    });
  }
  document.body.appendChild(modal);
}

// Export for testing (mirrors JSON modal pattern)
export function openLlmIoModal(io, rect) {
  if (!io) return;
  const existing = document.querySelector('.annotation-llmio-modal');
  if (existing) existing.remove();
  const modal = document.createElement('div');
  modal.className = 'annotation-llmio-modal';
  if (rect) {
    modal.style.position = 'fixed';
    const padding = 6;
    let left = rect.left;
    let top = rect.bottom + padding;
    const vpW = window.innerWidth || 1024;
    const vpH = window.innerHeight || 768;
    if (left + 640 > vpW) left = Math.max(4, vpW - 640 - 4);
    if (top + 480 > vpH) {
      const altTop = rect.top - 480 - padding;
      if (altTop >= 4) top = altTop;
    }
    modal.style.left = `${Math.max(4, left)}px`;
    modal.style.top = `${Math.max(4, top)}px`;
  }
  modal.innerHTML = `
    <div class="annotation-llmio-header">
      <span class="annotation-llmio-title">LLM Prompt & Output</span>
      <button class="annotation-llmio-close btn-mini" title="Close" aria-label="Close dialog">×</button>
    </div>
    <div class="annotation-llmio-body">
      <div class="annotation-llmio-section">
        <div class="annotation-llmio-section-title">Prompt</div>
        <pre class="annotation-llmio-pre annotation-llmio-pre-prompt"></pre>
      </div>
      <div class="annotation-llmio-section">
        <div class="annotation-llmio-section-title">Output</div>
        <pre class="annotation-llmio-pre annotation-llmio-pre-output"></pre>
      </div>
    </div>
    <div class="annotation-llmio-footer">
      <div class="annotation-llmio-actions">
        <button class="annotation-llmio-copy btn-mini" title="Copy both as JSON">Copy JSON</button>
      </div>
    </div>`;
  const promptPre = modal.querySelector('.annotation-llmio-pre-prompt');
  const outputPre = modal.querySelector('.annotation-llmio-pre-output');
  if (promptPre) promptPre.textContent = io.prompt || '(none)';
  if (outputPre) outputPre.textContent = io.output || '(none)';
  const doClose = () => modal.remove();
  const c1 = modal.querySelector('.annotation-llmio-close');
  if (c1) c1.addEventListener('click', doClose);
  const copyBtn = modal.querySelector('.annotation-llmio-copy');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      try {
        const data = { prompt: io.prompt || '', output: io.output || '', truncated: !!io.truncated };
        navigator.clipboard.writeText(JSON.stringify(data, null, 2));
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 1500);
      } catch (e) {
        copyBtn.textContent = 'Copy failed';
        setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 1500);
      }
    });
  }
  document.body.appendChild(modal);
}

function formatCandidates(spanObj, candidates, turnId, suggestion) {
  if (!candidates || !candidates.length) {
    // Enhanced empty state: allow manual instance creation (JVNAUTOSCI-552)
    // Container elements are wired later (search + create flow). Keep minimal to avoid layout shift.
    const suggestedTypeId = suggestion && suggestion.suggested_type_id;
    const suggestedTypeName = suggestedTypeId ? suggestedTypeId.replace('#V#', '').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/^\w/, c => c.toUpperCase()) : '';

    const suggestedTypeButton = suggestedTypeId ?
      `<button type="button" class="btn-mini manual-instance-start" data-mode="suggested-type" data-type-id="${suggestedTypeId}" title="Create ${suggestedTypeName} instance for this span">Create ${suggestedTypeName}…</button>` : '';

    return `<div class="annotation-no-candidates-wrap">
      <em class="annotation-no-candidates">No candidates</em>
      <div class="manual-instance-create" data-turn-id="${turnId || ''}">
        <div class="manual-create-start-buttons">
          ${suggestedTypeButton}
          <button type="button" class="btn-mini manual-instance-start" data-mode="instance" title="Create a new instance for this span">Create instance…</button>
          <button type="button" class="btn-mini manual-instance-start" data-mode="type" title="Create a new subtype (Type) for this span">Create type…</button>
        </div>
        <div class="manual-instance-panel hidden" data-mode="instance">
          <div class="manual-instance-row">
            <label class="sr-only" for="manualTypeSearch-${turnId}">Type search</label>
            <input id="manualTypeSearch-${turnId}" class="manual-type-search" type="text" placeholder="Search type (e.g. Person)">
            <div class="manual-type-results" aria-live="polite"></div>
          </div>
          <div class="manual-instance-row">
            <label class="sr-only" for="manualInstanceName-${turnId}">Instance name</label>
            <input id="manualInstanceName-${turnId}" class="manual-instance-name" type="text" placeholder="Name (default: span text)">
            <button type="button" class="btn-mini manual-instance-create-btn" disabled>Create</button>
            <button type="button" class="btn-mini manual-instance-cancel">Cancel</button>
          </div>
          <div class="manual-instance-status" aria-live="polite"></div>
        </div>
      </div>
    </div>`;
  }
  return '<ul class="annotation-candidate-list">' + candidates.map((c, idx) => {
    const name = c.name || c.concept_id || '(unnamed)';
    const cid = c.concept_id
      ? (() => {
        const fullId = c.concept_id.startsWith('#V#') ? c.concept_id : `#V#${c.concept_id}`;
        return `<a href="javascript:void(0)" class="vontology-token annotation-candidate-concept" data-concept-id="${fullId}" title="Open concept tab">${fullId}</a>`;
      })()
      : (c.proposed ? '<span class="candidate-proposed">(proposed)</span>' : '');
    const reason = c.reason ? ` <span class="candidate-reason">${c.reason}</span>` : '';
    const accepted = c.__accepted;
    const acceptBtn = `<button class="candidate-action btn-mini" data-action="accept" data-cand-index="${idx}" ${accepted ? 'disabled' : ''}>${accepted ? 'Accepted' : 'Accept'}</button>`;
    const undoBtn = accepted ? `<button class="candidate-action btn-mini" data-action="undo" data-cand-index="${idx}">Undo</button>` : '';
    return `<li class="candidate-item ${accepted ? 'accepted' : ''}" data-cand-index="${idx}">${name} ${cid}${reason} <span class="candidate-actions">${acceptBtn} ${undoBtn}</span></li>`;
  }).join('') + '</ul>';
}

// Export for testing
export function renderResults(container, suggestions, turnId) {
  // Ensure LLM I/O button (even if there are zero suggestions) when metadata present
  try {
    if (container && container.__llm_io) {
      const parent = container.parentElement || container;
      let headerBar = parent.querySelector('.annotation-llmio-bar');
      if (!headerBar) {
        headerBar = document.createElement('div');
        headerBar.className = 'annotation-llmio-bar';
        parent.insertBefore(headerBar, parent.firstChild);
      }
      if (!headerBar.querySelector('.annotation-llmio-btn')) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn-mini annotation-llmio-btn';
        btn.textContent = 'LLM I/O';
        btn.title = 'Show prompt & raw output used for this run';
        attachOnce(btn, 'click', 'llmIoOpen', () => {
          try { openLlmIoModal(container.__llm_io, btn.getBoundingClientRect()); } catch (e) { console.warn('openLlmIoModal failed', e); }
        });
        headerBar.appendChild(btn);
      }
    }
  } catch (_) { /* ignore */ }
  if (!suggestions || !suggestions.length) {
    // Preserve existing header bar (LLM button). Manage a dedicated content wrapper.
    let content = container.querySelector('.annotation-results-content');
    if (!content) {
      content = document.createElement('div');
      content.className = 'annotation-results-content';
      container.appendChild(content);
    }
    content.innerHTML = '<p class="annotation-empty">No suggestions returned.</p>';
    return;
  }
  function normalizeSourceName(raw) {
    if (!raw) return '';
    const r = raw.toLowerCase();
    if (r.includes('match')) return 'match';
    if (r.includes('llm')) return 'llm';
    if (r.includes('ner') || r.includes('spacy')) return ''; // NER removed
    return raw.toLowerCase();
  }
  function sourceBadgeMarkup(kind) {
    const map = {
      llm: { label: 'LLM', icon: '🤖' },
      match: { label: 'MATCH', icon: '🔎' },
      // NER removed
    };
    const meta = map[kind] || { label: kind.toUpperCase(), icon: '•' };
    return `<span class="annotation-source-badge ${kind}"><span class="annotation-source-icon">${meta.icon}</span>${meta.label}</span>`;
  }
  function formatSourceLabel(srcArr) {
    const kinds = [...new Set(srcArr.map(normalizeSourceName).filter(Boolean))];
    if (!kinds.length) return '';
    return ` <span class="annotation-span-sources">${kinds.map(sourceBadgeMarkup).join('')}</span>`;
  }
  // Ensure dedicated content wrapper so header bar persists across renders
  let content = container.querySelector('.annotation-results-content');
  if (!content) {
    content = document.createElement('div');
    content.className = 'annotation-results-content';
    container.appendChild(content);
  }
  content.innerHTML = suggestions.map((s, sIdx) => {
    const span = s.span || {};
    const text = span.text || '(?)';
    const start = span.start;
    const end = span.end;
    const srcArr = getSpanSources(s);
    const sources = srcArr.join(' ');
    return `<div class="annotation-span-block" data-span-index="${sIdx}" data-sources="${sources}" data-start="${start}" data-end="${end}" data-text="${encodeURIComponent(text)}">
      <div class="annotation-span-header">Span: <span class="annotation-span-text">${text}</span> <span class="annotation-span-range">[${start}, ${end}]</span>${formatSourceLabel(srcArr)} <button class="btn-mini annotation-json-btn" title="Show JSON">JSON</button></div>
      <div class="annotation-candidates-wrap">${formatCandidates(span, s.candidates, turnId, s)}</div>
    </div>`;
  }).join('');
  // Wire JSON buttons
  Array.from(container.querySelectorAll('.annotation-json-btn')).forEach(btn => {
    attachOnce(btn, 'click', 'showJson', () => {
      const block = btn.closest('.annotation-span-block');
      if (!block) return;
      const idx = Number(block.getAttribute('data-span-index'));
      const rec = buildAnnotationRecord(suggestions[idx], idx, turnId);
      try {
        openAnnotationJsonModal(rec, btn.getBoundingClientRect());
      } catch (_) {
        alert(JSON.stringify(rec, null, 2));
      }
    });
  });
  // After rendering, attempt to apply cached classification immediately if suggestions exist
  try {
    const suffix = container.id.startsWith('annotationResults') ? container.id.slice('annotationResults'.length) : '';
    const highlightPane = document.getElementById(`annotationHighlights${suffix}`);
    applySpanClassification(container, highlightPane, suggestions);
  } catch (_) { /* ignore */ }
}

function renderHighlights(highlightPane, fullText, suggestions) {
  if (!highlightPane) return;
  const columns = document.querySelector('.annotation-columns');
  if (!suggestions || !suggestions.length) {
    highlightPane.classList.add('empty');
    highlightPane.innerHTML = '';
    if (columns) columns.classList.add('no-annotations');
    return;
  }
  if (columns) columns.classList.remove('no-annotations');
  highlightPane.classList.remove('empty');
  const pieces = [];
  let cursor = 0;
  // Helper: find nearest occurrence of needle around approxStart
  function findNearest(original, needle, approxStart, searchRadius = 120) {
    if (!needle) return null;
    const lowNeedle = needle.toLowerCase();
    let best = null; let bestDist = Infinity;
    const lo = Math.max(0, approxStart - searchRadius);
    const hi = Math.min(original.length, approxStart + searchRadius + needle.length + 2);
    const window = original.slice(lo, hi);
    let idx = window.toLowerCase().indexOf(lowNeedle);
    while (idx !== -1) {
      const abs = lo + idx;
      const dist = Math.abs(abs - approxStart);
      if (dist < bestDist) { bestDist = dist; best = abs; }
      if (bestDist === 0) break;
      idx = window.toLowerCase().indexOf(lowNeedle, idx + 1);
    }
    return best != null ? { start: best, end: best + needle.length } : null;
  }
  // Sort spans by start then prefer longer first so we keep broader coverage and skip overlaps cleanly
  const ordered = suggestions
    .map((s, i) => ({ s, i }))
    .filter(x => x.s && x.s.span && typeof x.s.span.start === 'number' && typeof x.s.span.end === 'number')
    .sort((a, b) => (a.s.span.start - b.s.span.start) || (b.s.span.end - b.s.span.start) - (a.s.span.end - a.s.span.start));
  ordered.forEach(({ s, i }) => {
    const origStart = s.span.start; const origEnd = s.span.end; const txt = s.span.text || '';
    if (!txt) return;
    let start = origStart; let end = origEnd;
    // Bounds clamp
    if (start < 0) start = 0;
    if (end > fullText.length) end = fullText.length;
    if (end <= start) return;
    // Verify substring; if mismatch attempt nearest search
    const substr = fullText.slice(start, end);
    if (substr.toLowerCase() !== txt.toLowerCase()) {
      const found = findNearest(fullText, txt, start);
      if (found) { start = found.start; end = found.end; }
    }
    // Skip if still mismatch after search to avoid corrupt highlight
    if (fullText.slice(start, end).toLowerCase() !== txt.toLowerCase()) return;
    // Enforce non-overlap: if span starts before cursor, skip entirely (do NOT trim mid-word)
    if (start < cursor) return;
    if (start > cursor) pieces.push(escapeHtml(fullText.slice(cursor, start)));
    const sources = getSpanSources(s).join(' ');
    pieces.push(`<span class=\"hl-span\" data-span-index=\"${i}\" data-source=\"${sources}\">${escapeHtml(fullText.slice(start, end))}</span>`);
    cursor = end;
  });
  if (cursor < fullText.length) pieces.push(escapeHtml(fullText.slice(cursor)));
  highlightPane.innerHTML = pieces.join('');
}

function escapeHtml(str) {
  return str.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\'': '&#39;' }[c]));
}

function updateCounts(countsEl, suggestions) {
  if (!countsEl) return;
  if (!suggestions || !suggestions.length) { countsEl.textContent = '0 spans'; return; }
  const accepted = suggestions.reduce((acc, s) => acc + (s.candidates || []).filter(c => c.__accepted).length, 0);
  const sourceCounts = suggestions.reduce((acc, s) => {
    getSpanSources(s).forEach(src => { if (!src) return; acc[src] = (acc[src] || 0) + 1; });
    return acc;
  }, {});
  const parts = Object.entries(sourceCounts).map(([k, v]) => `${k}:${v}`);
  countsEl.textContent = `${suggestions.length} spans (${parts.join(' ')}) ${accepted} accepted`;
}

function copyJson(suggestions) {
  try { navigator.clipboard.writeText(JSON.stringify(suggestions, null, 2)); } catch (e) { console.warn('Clipboard copy failed', e); }
}

export function initializeAnnotationTab(suffix = '') {
  const idSuffix = suffix ? `_${suffix}` : '';
  const container = document.getElementById(`annotationTab${idSuffix}`);
  if (!container) return;
  console.log('[annotationTab] initializeAnnotationTab: start (first run)');
  let state = sessionStates.get(suffix);
  if (!state) {
    state = { text: '', suggestions: [], lastTurnId: null, modes: { llm: true, match: true }, lastManualType: null, recentManualTypes: [] };
    // Load MRU types from localStorage (session scope key) if present
    try {
      const raw = localStorage.getItem('annotation_recent_manual_types');
      if (raw) {
        const arr = JSON.parse(raw);
        if (Array.isArray(arr)) state.recentManualTypes = arr.slice(0, 5);
        if (!state.lastManualType && state.recentManualTypes.length) state.lastManualType = state.recentManualTypes[0];
      }
    } catch (e) { /* ignore parse/storage errors */ }
    sessionStates.set(suffix, state);
  }
  const input = document.getElementById(`annotationInput${idSuffix}`);
  const button = document.getElementById(`runAnnotationButton${idSuffix}`);
  const llmToggle = document.getElementById(`llmEnrichToggle${idSuffix}`);
  const matchToggle = document.getElementById(`matchToggle${idSuffix}`);
  const nerToggle = null; // removed
  const status = document.getElementById(`annotationStatus${idSuffix}`);
  const results = document.getElementById(`annotationResults${idSuffix}`);
  const highlightPane = document.getElementById(`annotationHighlights${idSuffix}`);
  const countsEl = document.getElementById(`annotationCounts${idSuffix}`);
  const copyBtn = document.getElementById(`copyJsonButton${idSuffix}`);
  const sampleBtn = document.getElementById(`sampleTextButton${idSuffix}`);
  const clearBtn = document.getElementById(`clearTextButton${idSuffix}`);
  const spinner = document.getElementById(`annotationSpinner${idSuffix}`);
  let lastLlmInteraction = { prompt: '', output: '' };
  let lastAuxLlmCalls = [];
  // Fallback: attempt to recover last LLM I/O (history endpoint) if page reloaded after a run.
  (async () => {
    try {
      if (!container.__llm_io) {
        const resp = await fetch('/api/annotations/llm_history');
        if (resp.ok) {
          const data = await resp.json();
          if (data && data.status === 'ok' && data.llm_io && data.llm_io.prompt) {
            container.__llm_io = data.llm_io;
            try { if (results && !results.__llm_io) results.__llm_io = data.llm_io; } catch (_) { /* ignore */ }
            try { if (Array.isArray(data.llm_io.aux_llm_calls)) lastAuxLlmCalls = data.llm_io.aux_llm_calls; } catch (_) { /* ignore */ }
            // Force header bar render even before any new run
            try { renderResults(results, container.__suggestions || [], container.__lastTurnId); } catch (_) { /* ignore */ }
          }
        }
      }
    } catch (_) { /* silent */ }
  })();
  // Legacy inline prompt preview removed (replaced by popup)
  // LLM interaction popup elements
  const llmInfoBtn = document.getElementById(`showLlminfoButton${idSuffix}`);
  const llmPopup = document.getElementById(`llmInteractionPopup${idSuffix}`);
  const llmCloseBtn = document.getElementById(`closeLlminfoButton${idSuffix}`);
  const llmRefreshBtn = document.getElementById(`refreshLlminfoButton${idSuffix}`);
  const llmCopyBtn = document.getElementById(`copyLlmJsonButton${idSuffix}`);
  const llmPromptPre = document.getElementById(`llmInteractionPrompt${idSuffix}`);
  const llmOutputPre = document.getElementById(`llmInteractionOutput${idSuffix}`);
  const llmMetaDiv = document.getElementById(`llmInteractionMeta${idSuffix}`);
  const llmAuxSection = document.getElementById(`llmInteractionAuxSection${idSuffix}`);
  const llmAuxPre = document.getElementById(`llmInteractionAux${idSuffix}`);
  const promptConceptBtn = document.getElementById(`promptConceptButton${idSuffix}`);

  // Insert classification legend (idempotent) near counts area
  try {
    if (countsEl && !document.getElementById(`annotationLegend${idSuffix}`)) {
      const legend = document.createElement('div');
      legend.id = `annotationLegend${idSuffix}`;
      legend.className = 'annotation-legend';
      legend.innerHTML = '<span class="legend-title">Legend:</span>' +
        '<span class="legend-chip chip-type" title="Span with at least one type concept candidate">Type</span>' +
        '<span class="legend-chip chip-individual" title="Span resolved to an individual concept">Individual</span>' +
        '<span class="legend-chip chip-hypothesis" title="Span with no candidates yet (hypothesis)">Hypothesis</span>';
      const parent = countsEl.parentElement;
      if (parent) parent.insertBefore(legend, countsEl.nextSibling);
    }
  } catch (_) { /* ignore legend insertion issues */ }


  async function fetchFullLlmInteraction(text) {
    if (!text) {
      if (llmPromptPre) llmPromptPre.textContent = '(no text)';
      if (llmOutputPre) llmOutputPre.textContent = '(no text)';
      if (llmMetaDiv) llmMetaDiv.textContent = '';
      return;
    }
    if (llmPromptPre) llmPromptPre.textContent = 'Loading prompt...';
    if (llmOutputPre) llmOutputPre.textContent = 'Loading output...';
    if (llmMetaDiv) llmMetaDiv.textContent = '';
    try {
      const resp = await fetch('/api/annotations/prompt_preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, include_output: true })
      });
      const data = await resp.json();
      if (!resp.ok || data.status !== 'ok') {
        const errMsg = 'Error: ' + (data.error || resp.status);
        if (llmPromptPre) llmPromptPre.textContent = errMsg;
        if (llmOutputPre) llmOutputPre.textContent = errMsg;
        return;
      }
      const preview = data.preview || {};
      const output = data.output || {};
      if (llmPromptPre) {
        const maxLen = 8000;
        const full = preview.prompt || '';
        llmPromptPre.textContent = full.length > maxLen ? full.slice(0, maxLen) + '\n... [truncated]' : full;
      }
      if (llmOutputPre) {
        if (output.error) {
          llmOutputPre.textContent = 'Output error: ' + output.error;
        } else if (output.raw) {
          llmOutputPre.textContent = output.raw;
        } else {
          llmOutputPre.textContent = '(no output)';
        }
      }
      lastLlmInteraction.prompt = preview.prompt || '';
      // Prefer raw output, fall back to serialized output object or blank
      if (output.raw) {
        lastLlmInteraction.output = output.raw;
      } else if (output.error) {
        lastLlmInteraction.output = 'Error: ' + output.error;
      } else {
        try { lastLlmInteraction.output = typeof output === 'string' ? output : JSON.stringify(output); } catch { lastLlmInteraction.output = ''; }
      }
      if (llmMetaDiv) {
        const metaParts = [];
        if (output.model) metaParts.push('model: ' + output.model);
        if (typeof output.length === 'number') metaParts.push('chars: ' + output.length);
        if (typeof output.timing_ms === 'number') metaParts.push('time: ' + output.timing_ms + ' ms');
        if (output.truncated) metaParts.push('truncated: true');
        llmMetaDiv.textContent = metaParts.join(' | ');
      }
    } catch (e) {
      console.warn('LLM interaction fetch failed', e);
      if (llmPromptPre) llmPromptPre.textContent = 'Error loading LLM interaction';
      if (llmOutputPre) llmOutputPre.textContent = 'Error loading LLM interaction';
    }
  }

  function renderAuxLlmCalls() {
    if (!llmAuxSection || !llmAuxPre) return;
    const hasAux = Array.isArray(lastAuxLlmCalls) && lastAuxLlmCalls.length > 0;
    if (hasAux) {
      llmAuxSection.classList.remove('hidden');
      try {
        llmAuxPre.textContent = JSON.stringify(lastAuxLlmCalls, null, 2);
      } catch (e) {
        llmAuxPre.textContent = 'Error formatting auxiliary LLM calls';
      }
    } else {
      llmAuxSection.classList.add('hidden');
      llmAuxPre.textContent = '(no auxiliary LLM calls)';
    }
  }

  function openLlmPopup() {
    if (!llmPopup) return;
    llmPopup.classList.remove('hidden');
    llmPopup.setAttribute('aria-hidden', 'false');
    renderAuxLlmCalls();
    fetchFullLlmInteraction(input.value.trim());
  }
  function closeLlmPopup() {
    if (!llmPopup) return;
    llmPopup.classList.add('hidden');
    llmPopup.setAttribute('aria-hidden', 'true');
  }

  if (llmInfoBtn) {
    attachOnce(llmInfoBtn, 'click', 'llmInfoOpen', () => openLlmPopup());
  }
  if (llmCloseBtn) {
    attachOnce(llmCloseBtn, 'click', 'llmInfoClose', () => closeLlmPopup());
  }
  if (llmRefreshBtn) {
    attachOnce(llmRefreshBtn, 'click', 'llmInfoRefresh', () => fetchFullLlmInteraction(input.value.trim()));
  }
  if (promptConceptBtn) {
    attachOnce(promptConceptBtn, 'click', 'promptConceptNav', (e) => {
      e.stopPropagation();
      const conceptId = promptConceptBtn.dataset.conceptId || '#V#find_concepts_in_text_prompt';
      try {
        document.dispatchEvent(new CustomEvent('von:selectConceptById', { detail: { conceptId, createConceptTab: true } }));
      } catch (err) { console.warn('Prompt concept navigation failed', err); }
    });
  }
  if (llmCopyBtn) {
    attachOnce(llmCopyBtn, 'click', 'llmCopyJson', () => {
      const promptConceptId = (promptConceptBtn && (promptConceptBtn.dataset.conceptId || promptConceptBtn.textContent)) || '#V#find_concepts_in_text_prompt';
      const record = { prompt: lastLlmInteraction.prompt || '', output: lastLlmInteraction.output || '', prompt_concept: promptConceptId, aux_llm_calls: Array.isArray(lastAuxLlmCalls) ? lastAuxLlmCalls : [] };
      try { navigator.clipboard.writeText(JSON.stringify(record, null, 2)); llmCopyBtn.textContent = 'Copied'; setTimeout(() => { llmCopyBtn.textContent = 'Copy JSON'; }, 1500); } catch (e) { console.warn('Clipboard write failed', e); }
    });
  }

  // Removed old auto-refresh of inline prompt preview.

  // Always attempt to attach listeners (attachOnce prevents duplicates) so race on dataset.initialized flag cannot block wiring.
  {
    attachOnce(button, 'click', 'annotateRun', async () => {
      const text = input.value.trim();
      if (!text) { status.textContent = 'Enter some text first.'; return; }
      status.textContent = 'Annotating...'; spinner.classList.remove('hidden');
      // Inline prompt preview removed (popup fetches on demand).
      const turnId = 'ann-' + Date.now();
      try {
        const start = performance.now();
        const resp = await annotateTurn({
          conversation_id: 'annotation-demo',
          turn_id: turnId,
          speaker: 'user',
          text,
          metadata: { llm_enrich: !!(llmToggle && llmToggle.checked), match: matchToggle ? !!matchToggle.checked : true }
        });
        const ms = Math.round(performance.now() - start);
        if (resp && resp.suggestions) {
          // Attach last LLM I/O (backend already truncates if large). Needed for LLM I/O button.
          try { if (resp.llm_io) { results.__llm_io = resp.llm_io; } } catch (_) { /* ignore */ }
          try { if (resp.llm_debug) { results.__llm_debug = resp.llm_debug; } } catch (_) { /* ignore */ }
          const auxCalls = (resp && resp.llm_debug && resp.llm_debug.aux_llm_calls) || (resp && resp.llm_io && resp.llm_io.aux_llm_calls) || [];
          lastAuxLlmCalls = Array.isArray(auxCalls) ? auxCalls : [];
          try { await resolveSuggestedTypes(resp.suggestions); } catch (_) { /* ignore */ }
          container.__lastTurnId = turnId;
          container.__suggestions = resp.suggestions;
          // Backward compatibility: some earlier code inspected container.__llm_io
          try { if (resp.llm_io) { container.__llm_io = resp.llm_io; } } catch (_) { /* ignore */ }
          renderAuxLlmCalls();
          renderResults(results, resp.suggestions, turnId);
          renderHighlights(highlightPane, text, resp.suggestions);
          updateCounts(countsEl, resp.suggestions);
          applySpanClassification(results, highlightPane, resp.suggestions);
          scheduleKindResolution(results, highlightPane, resp.suggestions);
          const parts = [];
          if (llmToggle && llmToggle.checked) parts.push('LLM');
          if (matchToggle && matchToggle.checked) parts.push('Match');
          // NER removed
          // Per-source counts for quick validation
          const srcCountMap = resp.suggestions.reduce((m, s) => { getSpanSources(s).forEach(src => { if (!src) return; m[src] = (m[src] || 0) + 1; }); return m; }, {});
          const srcParts = Object.entries(srcCountMap).map(([k, v]) => `${k}:${v}`);
          const modeInfo = parts.length ? ` (${parts.join('+')})` : '';
          // Timings
          let timingPart = '';
          if (resp.timings) {
            const t = resp.timings;
            const segs = [];
            if (t.match_ms !== undefined) segs.push(`match:${t.match_ms}`);
            if (t.llm_ms !== undefined) segs.push(`llm:${t.llm_ms}`);
            // ner_ms removed
            if (t.enrich_ms !== undefined) segs.push(`enr:${t.enrich_ms}`);
            if (t.total_ms !== undefined) segs.push(`total:${t.total_ms}`);
            if (segs.length) timingPart = ' {' + segs.join(' | ') + ' ms}';
          }
          status.textContent = `Got ${resp.suggestions.length} spans in ${ms} ms${modeInfo}${srcParts.length ? ' [' + srcParts.join(', ') + ']' : ''}${timingPart}.`;
        } else {
          lastAuxLlmCalls = [];
          renderAuxLlmCalls();
          status.textContent = `No suggestions (took ${ms} ms)`;
          results.innerHTML = '';
          renderHighlights(highlightPane, text, []);
          updateCounts(countsEl, []);
        }
        state.text = text;
        state.suggestions = container.__suggestions || [];
        state.lastTurnId = container.__lastTurnId;
        state.modes = { llm: !!(llmToggle && llmToggle.checked), match: !!(matchToggle && matchToggle.checked) };
      } catch (e) {
        console.error('[annotationTab] error', e);
        status.textContent = 'Error running annotation';
      } finally {
        spinner.classList.add('hidden');
      }
    });

    // Event delegation for candidate accept / undo
    attachOnce(results, 'click', 'candidateActions', async (e) => {
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      // Manual instance create flow buttons
      if (target.classList.contains('manual-instance-start')) {
        const wrap = target.closest('.manual-instance-create');
        if (!wrap) return;
        const mode = target.dataset.mode || 'instance';
        const panel = wrap.querySelector('.manual-instance-panel');
        if (panel) {
          panel.dataset.mode = mode === 'suggested-type' ? 'instance' : mode; // treat suggested-type as instance mode
          panel.classList.remove('hidden');

          // Shared references used for both suggested-type and standard flows
          const searchInput = panel.querySelector('.manual-type-search');
          const resultsBox = panel.querySelector('.manual-type-results');
          const createBtn = panel.querySelector('.manual-instance-create-btn');
          const nameInput = panel.querySelector('.manual-instance-name');

          // For suggested-type mode, prefill with the suggested type
          if (mode === 'suggested-type') {
            const typeId = target.dataset.typeId;
            const typeName = target.textContent.replace('Create ', '').replace('…', '');
            if (typeId && typeName) {
              if (searchInput) {
                searchInput.value = typeName;
              }
              panel.dataset.selectedTypeId = typeId;
              panel.dataset.selectedTypeName = typeName;
              if (resultsBox) {
                resultsBox.innerHTML = `<div class="type-option selected" data-id="${typeId}">${typeName}</div>`;
              }
            }
            if (createBtn) createBtn.disabled = false;
            if (nameInput) {
              if (!nameInput.value) {
                try {
                  const block = wrap.closest('.annotation-span-block');
                  const encoded = block && block.getAttribute('data-text');
                  if (encoded) nameInput.value = decodeURIComponent(encoded);
                } catch (_) { /* ignore decode issues */ }
              }
              nameInput.focus();
            }
          } else {
            // Prefill last selected type if available (existing behavior)
            try {
              const sess = sessionStates.get(suffix);
              if (sess && sess.lastManualType && panel) {
                if (searchInput && !searchInput.value) {
                  searchInput.value = sess.lastManualType.name || sess.lastManualType.id;
                  // Trigger synthetic keyup debounce path
                  const ev = new Event('keyup');
                  searchInput.dispatchEvent(ev);
                }
              }
            } catch (prefillErr) { /* ignore */ }
          }
          // Render recent types shortlist if available and not already rendered
          try {
            const sess2 = sessionStates.get(suffix);
            if (sess2 && Array.isArray(sess2.recentManualTypes) && sess2.recentManualTypes.length) {
              const existing = panel.querySelector('.manual-type-recent-row');
              if (!existing) {
                const recentRow = document.createElement('div');
                recentRow.className = 'manual-instance-row manual-type-recent-row';
                recentRow.innerHTML = '<span class="manual-type-recent-label">Recent:</span>' + sess2.recentManualTypes.map(t => `<button type="button" class="btn-mini manual-type-recent-btn" data-id="${t.id}" title="Reuse type ${t.name || t.id}">${t.name || t.id}</button>`).join('');
                // Insert after first row (type search row)
                const firstRow = panel.querySelector('.manual-instance-row');
                if (firstRow && firstRow.nextSibling) {
                  panel.insertBefore(recentRow, firstRow.nextSibling);
                } else if (firstRow) {
                  panel.appendChild(recentRow);
                } else {
                  panel.appendChild(recentRow);
                }
              }
            }
          } catch (recentErr) { /* ignore */ }
        }
        // Hide both start buttons container while panel open
        const btnContainer = wrap.querySelector('.manual-create-start-buttons');
        if (btnContainer) btnContainer.classList.add('hidden');
        return;
      }
      if (target.classList.contains('manual-instance-cancel')) {
        const panel = target.closest('.manual-instance-panel');
        if (panel) {
          panel.classList.add('hidden');
          const btnContainer = panel.parentElement && panel.parentElement.querySelector('.manual-create-start-buttons');
          if (btnContainer) btnContainer.classList.remove('hidden');
        }
        return;
      }
      if (target.classList.contains('manual-instance-create-btn')) {
        const panel = target.closest('.manual-instance-panel');
        if (!panel) return;
        const statusEl = panel.querySelector('.manual-instance-status');
        const nameInput = panel.querySelector('.manual-instance-name');
        const searchInput = panel.querySelector('.manual-type-search');
        const resultsBox = panel.querySelector('.manual-type-results');
        const mode = panel.dataset.mode || 'instance';
        let selectedType = resultsBox && resultsBox.querySelector('.type-option.selected');
        // Fallback: use panel dataset if user clicked a recent button before results populated
        if (!selectedType) {
          const dsId = panel && panel.dataset && panel.dataset.selectedTypeId;
          const dsName = panel && panel.dataset && panel.dataset.selectedTypeName;
          if (dsId) {
            // Minimal shim to mimic needed DOM API surface
            class SelectedTypeShim {
              constructor(id, name) { this._id = id; this.textContent = name || id; }
              getAttribute(attr) { return attr === 'data-id' ? this._id : null; }
            }
            selectedType = new SelectedTypeShim(dsId, dsName || dsId);
          }
        }
        if (!selectedType) { if (statusEl) statusEl.textContent = 'Select a type first.'; return; }
        const parentId = selectedType.getAttribute('data-id');
        // Locate owning span to derive text default
        const spanBlock = target.closest('.annotation-span-block');
        const spanIndex = spanBlock ? parseInt(spanBlock.dataset.spanIndex, 10) : -1;
        const suggestions = container.__suggestions || [];
        const suggestion = suggestions[spanIndex];
        if (!suggestion) return;
        const span = suggestion.span;
        const baseName = (nameInput && nameInput.value.trim()) || span.text || 'Unnamed';
        target.disabled = true;
        if (statusEl) statusEl.textContent = mode === 'type' ? 'Creating type…' : 'Creating…';
        try {
          const created = mode === 'type' ? await createType(parentId, baseName) : await createInstance(parentId, baseName);
          if (created && created.concept_id) {
            // Inject as candidate + mark accepted
            const cand = { name: created.name || baseName, concept_id: created.concept_id, __accepted: true, reason: mode === 'type' ? '(manual type)' : '(manual)' };
            suggestion.candidates = suggestion.candidates || [];
            if (mode === 'type') {
              // Ensure classification reflects new type (make it first & cache kind)
              try { conceptKindCache.set(created.concept_id, 'type'); } catch (_) { /* ignore */ }
              suggestion.candidates.unshift(cand);
            } else {
              suggestion.candidates.push(cand);
            }
            renderResults(results, suggestions, container.__lastTurnId);
            renderHighlights(highlightPane, input.value, suggestions);
            updateCounts(countsEl, suggestions);
            await acceptAnnotation({
              turn_id: container.__lastTurnId,
              span: { start: span.start, end: span.end, text: span.text },
              candidate: cand
            });
            // Update recent manual types MRU if a new type was created (reused by instance creation flow)
            if (mode === 'type') {
              try {
                const sess = sessionStates.get(suffix);
                if (sess) {
                  if (!Array.isArray(sess.recentManualTypes)) sess.recentManualTypes = [];
                  const entry = { id: created.concept_id, name: created.name || baseName };
                  sess.lastManualType = entry;
                  sess.recentManualTypes = sess.recentManualTypes.filter(t => t.id !== entry.id);
                  sess.recentManualTypes.unshift(entry);
                  if (sess.recentManualTypes.length > 5) sess.recentManualTypes.length = 5;
                  try { localStorage.setItem('annotation_recent_manual_types', JSON.stringify(sess.recentManualTypes)); } catch (_) { /* ignore */ }
                }
              } catch (_) { /* ignore */ }
            }
            // Fire-and-forget telemetry log (non-blocking)
            try {
              const telemetryEndpoint = mode === 'type' ? '/api/annotations/manual_type' : '/api/annotations/manual_instance';
              fetch(telemetryEndpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                  span_text: span.text,
                  instance_concept_id: mode === 'type' ? undefined : created.concept_id,
                  parent_type_id: parentId,
                  type_concept_id: mode === 'type' ? created.concept_id : undefined,
                  turn_id: container.__lastTurnId
                })
              });
            } catch (teleErr) { /* swallow */ }
            if (statusEl) statusEl.textContent = mode === 'type' ? 'Type created & accepted' : 'Created & accepted';
            // Locate the newly added candidate list item and apply flash class
            try {
              const spanBlockUpdated = results.querySelector(`.annotation-span-block[data-span-index="${spanIndex}"]`);
              if (spanBlockUpdated) {
                const candItems = spanBlockUpdated.querySelectorAll('.candidate-item');
                const lastItem = candItems[candItems.length - 1];
                if (lastItem && lastItem.classList.contains('accepted')) {
                  lastItem.classList.add('flash-new');
                  // Move focus for accessibility
                  lastItem.setAttribute('tabindex', '-1');
                  lastItem.focus({ preventScroll: false });
                }
              }
            } catch (focusErr) { /* ignore focus issues */ }
            // Auto-collapse panel & reset inputs
            try {
              if (panel) {
                panel.classList.add('hidden');
                const btnContainer = panel.parentElement && panel.parentElement.querySelector('.manual-create-start-buttons');
                if (btnContainer) btnContainer.classList.remove('hidden');
                if (nameInput) nameInput.value = '';
                if (searchInput) searchInput.value = '';
                if (resultsBox) resultsBox.innerHTML = '';
                const createBtn = panel.querySelector('.manual-instance-create-btn');
                if (createBtn) createBtn.disabled = true;
              }
            } catch (collapseErr) { /* swallow */ }
            // Toast message (ephemeral) – only one at a time
            try {
              const existingToast = document.querySelector('.annotation-toast');
              if (existingToast) existingToast.remove();
              const toast = document.createElement('div');
              toast.className = 'annotation-toast';
              toast.textContent = mode === 'type' ? `Type created: ${created.concept_id}` : `Instance created: ${created.concept_id}`;
              document.body.appendChild(toast);
              setTimeout(() => { try { toast.remove(); } catch (_) { /* ignore */ } }, 4000);
            } catch (toastErr) { /* ignore */ }
            try { applySpanClassification(results, highlightPane, suggestions); } catch (_) { /* ignore */ }
          } else {
            if (statusEl) statusEl.textContent = 'Create returned no concept';
          }
        } catch (err) {
          console.warn('Manual create failed', err);
          if (statusEl) statusEl.textContent = 'Create failed';
        } finally {
          target.disabled = false;
        }
        return;
      }
      // Recent type quick-select button
      if (target.classList.contains('manual-type-recent-btn')) {
        const panel = target.closest('.manual-instance-panel');
        if (!panel) return;
        const searchInput = panel.querySelector('.manual-type-search');
        const typeId = target.getAttribute('data-id');
        if (searchInput && typeId) {
          const typeName = target.textContent || typeId;
          searchInput.value = typeName;
          // Update session state immediately
          try {
            const sess = sessionStates.get(suffix);
            if (sess) {
              const entry = { id: typeId, name: typeName };
              sess.lastManualType = entry;
              if (!Array.isArray(sess.recentManualTypes)) sess.recentManualTypes = [];
              sess.recentManualTypes = sess.recentManualTypes.filter(t => t.id !== typeId);
              sess.recentManualTypes.unshift(entry);
              if (sess.recentManualTypes.length > 5) sess.recentManualTypes.length = 5;
              try { localStorage.setItem('annotation_recent_manual_types', JSON.stringify(sess.recentManualTypes)); } catch (e) { /* ignore */ }
            }
          } catch (e2) { /* ignore */ }
          // Set panel dataset fallback so create can proceed even if search not yet rendered options
          try { panel.dataset.selectedTypeId = typeId; panel.dataset.selectedTypeName = typeName; } catch (e3) { /* ignore */ }
          // Enable create immediately (user can override once list arrives)
          const createBtn = panel.querySelector('.manual-instance-create-btn');
          if (createBtn) createBtn.disabled = false;
          // Trigger search (debounced handler will auto-select if present)
          const ev = new Event('keyup');
          searchInput.dispatchEvent(ev);
        }
        return;
      }
      // Concept link click
      if (target.classList.contains('annotation-candidate-concept')) {
        const conceptId = target.dataset.conceptId;
        if (conceptId) {
          try {
            document.dispatchEvent(new CustomEvent('von:selectConceptById', { detail: { conceptId, createConceptTab: true } }));
          } catch (err) { console.warn('Concept link navigation failed', err); }
        }
        e.preventDefault();
        return;
      }
      if (!target.classList.contains('candidate-action')) return;
      const action = target.dataset.action;
      const spanBlock = target.closest('.annotation-span-block');
      if (!spanBlock) return;
      const spanIndex = parseInt(spanBlock.dataset.spanIndex, 10);
      const suggestions = container.__suggestions || [];
      const suggestion = suggestions[spanIndex];
      if (!suggestion) return;
      const span = suggestion.span;
      const candIndex = parseInt(target.dataset.candIndex, 10);
      const candidate = suggestion.candidates[candIndex];
      if (!candidate) return;
      try {
        if (action === 'accept' && !candidate.__accepted) {
          candidate.__accepted = true;
          renderResults(results, suggestions, container.__lastTurnId);
          renderHighlights(highlightPane, input.value, suggestions);
          updateCounts(countsEl, suggestions);
          applySpanClassification(results, highlightPane, suggestions);
          scheduleKindResolution(results, highlightPane, suggestions);
          await acceptAnnotation({
            turn_id: container.__lastTurnId,
            span: { start: span.start, end: span.end, text: span.text },
            candidate: candidate
          });
          state.suggestions = suggestions;
        } else if (action === 'undo' && candidate.__accepted) {
          candidate.__accepted = false;
          renderResults(results, suggestions, container.__lastTurnId);
          renderHighlights(highlightPane, input.value, suggestions);
          updateCounts(countsEl, suggestions);
          applySpanClassification(results, highlightPane, suggestions);
          scheduleKindResolution(results, highlightPane, suggestions);
          const candidateId = candidate.concept_id || candidate.id || candidate.conceptId;
          await revokeAnnotation(candidateId ? { candidate_id: candidateId } : { object_text: span.text });
        }
      } catch (err) {
        console.warn('Annotation candidate action failed', err);
        // Re-render to reflect any optimistic state revert if needed
        if (action === 'accept') candidate.__accepted = false;
        if (action === 'undo') candidate.__accepted = true;
        renderResults(results, suggestions, container.__lastTurnId);
        renderHighlights(highlightPane, input.value, suggestions);
        updateCounts(countsEl, suggestions);
        applySpanClassification(results, highlightPane, suggestions);
        scheduleKindResolution(results, highlightPane, suggestions);
      }
    });

    // Highlight click -> scroll to candidate block
    attachOnce(highlightPane, 'click', 'highlightSelect', (e) => {
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.classList.contains('hl-span')) return;
      const idx = target.dataset.spanIndex;
      const block = results.querySelector(`.annotation-span-block[data-span-index="${idx}"]`);
      if (block) {
        results.querySelectorAll('.annotation-span-block.active').forEach(b => b.classList.remove('active'));
        block.classList.add('active');
        block.scrollIntoView({ behavior: 'smooth', block: 'center' });
        highlightPane.querySelectorAll('.hl-span.active').forEach(s => s.classList.remove('active'));
        target.classList.add('active');
      }
    });
    // Results hover -> highlight span
    attachOnce(results, 'mouseover', 'spanHoverOn', (e) => {
      const block = e.target.closest('.annotation-span-block');
      if (!block) return;
      const idx = block.dataset.spanIndex;
      const spanEl = highlightPane.querySelector(`.hl-span[data-span-index="${idx}"]`);
      if (spanEl) spanEl.classList.add('active');
    });
    attachOnce(results, 'mouseout', 'spanHoverOff', (e) => {
      const block = e.target.closest('.annotation-span-block');
      if (!block) return;
      const idx = block.dataset.spanIndex;
      const spanEl = highlightPane.querySelector(`.hl-span[data-span-index="${idx}"]`);
      if (spanEl) spanEl.classList.remove('active');
    });

    // Type search (delegated keyup) with simple debounce per panel
    let typeSearchTimer = null;
    attachOnce(results, 'keyup', 'manualTypeSearch', async (e) => {
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.classList.contains('manual-type-search')) return;
      const panel = target.closest('.manual-instance-panel');
      const resultsBox = panel && panel.querySelector('.manual-type-results');
      const createBtn = panel && panel.querySelector('.manual-instance-create-btn');
      if (!panel || !resultsBox) return;
      resultsBox.classList.remove('empty');
      resultsBox.classList.add('loading');
      resultsBox.innerHTML = '<div class="mini-spinner" aria-hidden="true"></div><span class="sr-only">Searching</span>';
      if (typeSearchTimer) clearTimeout(typeSearchTimer);
      typeSearchTimer = setTimeout(async () => {
        const q = target.value.trim();
        if (!q) {
          resultsBox.classList.remove('loading');
          resultsBox.innerHTML = '<span class="manual-type-hint">Type at least 1 character to search types.</span>';
          if (createBtn) createBtn.disabled = true; return;
        }
        const list = await searchTypes(q, 6);
        resultsBox.classList.remove('loading');
        if (!list.length) {
          resultsBox.classList.add('empty');
          resultsBox.innerHTML = '<span class="manual-type-empty">No matching types – try a broader fragment.</span>';
          if (createBtn) createBtn.disabled = true; return;
        }
        resultsBox.innerHTML = '<ul class="manual-type-options" role="listbox">' + list.map((t, i) => `<li class="type-option" role="option" aria-selected="false" data-index="${i}" data-id="${t.id}">${t.name || t.id}</li>`).join('') + '</ul>';
        if (createBtn) createBtn.disabled = true;
        // Auto-select previously used type if it appears in result set
        try {
          const sess = sessionStates.get(suffix);
          if (sess && sess.lastManualType) {
            const matchLi = resultsBox.querySelector(`.type-option[data-id="${sess.lastManualType.id}"]`);
            if (matchLi) {
              matchLi.classList.add('selected');
              matchLi.setAttribute('aria-selected', 'true');
              if (createBtn) createBtn.disabled = false;
              // Focus name input for rapid entry
              const panel = resultsBox.closest('.manual-instance-panel');
              const nameInput = panel && panel.querySelector('.manual-instance-name');
              if (nameInput) nameInput.focus();
            }
          }
        } catch (autoSelErr) { /* ignore */ }
      }, 220);
    });
    // Keyboard navigation inside type search results (Up/Down/Enter)
    attachOnce(results, 'keydown', 'manualTypeSearchNav', (e) => {
      const target = e.target;
      if (!(target instanceof HTMLElement)) return;
      if (!target.classList.contains('manual-type-search') && !target.classList.contains('manual-instance-name')) return;
      const panel = target.closest('.manual-instance-panel');
      if (!panel) return;
      const resultsBox = panel.querySelector('.manual-type-results');
      const optionsList = resultsBox && resultsBox.querySelector('.manual-type-options');
      const createBtn = panel.querySelector('.manual-instance-create-btn');
      if (target.classList.contains('manual-type-search')) {
        if (!optionsList) return;
        const options = Array.from(optionsList.querySelectorAll('.type-option'));
        if (!options.length) return;
        let currentIndex = options.findIndex(o => o.classList.contains('selected'));
        // Up / Down move selection
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          currentIndex = (currentIndex + 1) % options.length;
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          currentIndex = (currentIndex - 1 + options.length) % options.length;
        } else if (e.key === 'Enter') {
          if (currentIndex === -1) currentIndex = 0; // choose first if none
        } else {
          return; // other keys ignored
        }
        options.forEach((o, i) => {
          const sel = i === currentIndex;
          o.classList.toggle('selected', sel);
          o.setAttribute('aria-selected', sel ? 'true' : 'false');
        });
        if (createBtn) createBtn.disabled = currentIndex === -1;
      } else if (target.classList.contains('manual-instance-name')) {
        if (e.key === 'Enter') {
          // Trigger create if enabled
          if (createBtn && !createBtn.disabled) {
            e.preventDefault();
            createBtn.click();
          }
        }
      }
    });
    // Click type option select
    attachOnce(results, 'click', 'manualTypeOptionSelect', (e) => {
      const li = e.target.closest && e.target.closest('.type-option');
      if (!li) return;
      const list = li.parentElement;
      if (list) list.querySelectorAll('.type-option.selected').forEach(x => x.classList.remove('selected'));
      li.classList.add('selected');
      li.setAttribute('aria-selected', 'true');
      const panel = li.closest('.manual-instance-panel');
      const createBtn = panel && panel.querySelector('.manual-instance-create-btn');
      if (createBtn) createBtn.disabled = false;
      // Clear any fallback dataset selection (now we have an explicit list selection)
      try { if (panel && panel.dataset) { delete panel.dataset.selectedTypeId; delete panel.dataset.selectedTypeName; } } catch (clrErr) { /* ignore */ }
      // Focus name input for rapid entry
      const nameInput = panel && panel.querySelector('.manual-instance-name');
      if (nameInput) nameInput.focus();
      // Persist last chosen type into session state for reuse
      try {
        const typeId = li.getAttribute('data-id');
        const typeName = li.textContent || typeId;
        const sess = sessionStates.get(suffix);
        if (sess && typeId) {
          const entry = { id: typeId, name: typeName };
          sess.lastManualType = entry;
          if (!Array.isArray(sess.recentManualTypes)) sess.recentManualTypes = [];
          // Remove existing occurrence
          sess.recentManualTypes = sess.recentManualTypes.filter(t => t.id !== typeId);
          // Add to front
          sess.recentManualTypes.unshift(entry);
          // Cap at 5
          if (sess.recentManualTypes.length > 5) sess.recentManualTypes.length = 5;
          try { localStorage.setItem('annotation_recent_manual_types', JSON.stringify(sess.recentManualTypes)); } catch (e) { /* ignore */ }
        }
      } catch (persistErr) { /* ignore */ }
    });
    attachOnce(copyBtn, 'click', 'copyJson', () => { copyJson(container.__suggestions || []); });
    attachOnce(sampleBtn, 'click', 'sampleFill', () => { input.value = SAMPLE_TEXT; input.focus(); });
    attachOnce(clearBtn, 'click', 'clearAll', () => {
      input.value = '';
      container.__suggestions = [];
      state.text = '';
      state.suggestions = [];
      state.lastTurnId = null;
      results.innerHTML = '';
      renderHighlights(highlightPane, '', []);
      updateCounts(countsEl, []);
      status.textContent = '';
    });

    // Defer marking initialized; tabNavigation now sets flag after successful init
    console.log('[annotationTab] initializeAnnotationTab: listeners attached (idempotent).');
  }

  // Rehydrate session state when coming back to the tab inside same page lifetime
  if (state.text && !input.value) input.value = state.text;
  // Restore toggle states
  if (llmToggle) llmToggle.checked = state.modes.llm;
  if (matchToggle) matchToggle.checked = state.modes.match;
  // NER toggle removed
  if (state.suggestions.length && (!container.__suggestions || !container.__suggestions.length)) {
    container.__suggestions = state.suggestions;
    container.__lastTurnId = state.lastTurnId;
    renderResults(results, state.suggestions, state.lastTurnId);
    renderHighlights(highlightPane, input.value, state.suggestions);
    updateCounts(countsEl, state.suggestions);
    applySpanClassification(results, highlightPane, state.suggestions);
    scheduleKindResolution(results, highlightPane, state.suggestions);
    // Show source counts on restore
    const srcCountMap = state.suggestions.reduce((m, s) => { getSpanSources(s).forEach(src => { if (!src) return; m[src] = (m[src] || 0) + 1; }); return m; }, {});
    const srcParts = Object.entries(srcCountMap).map(([k, v]) => `${k}:${v}`);
    status.textContent = `Restored ${state.suggestions.length} spans (session)${srcParts.length ? ' [' + srcParts.join(', ') + ']' : ''}.`;
  }
}

// Auto init if tab content already present
if (document.getElementById('annotationTab')) {
  try { initializeAnnotationTab(); } catch (e) { console.error(e); }
}

// Observe DOM for dynamic insertion of annotation tab (guard for jsdom teardown)
try {
  if (typeof MutationObserver !== 'undefined' && typeof document !== 'undefined' && document.body) {
    const mo = new MutationObserver(() => {
      let el;
      try { el = document.getElementById && document.getElementById('annotationTab'); } catch (e) { return; }
      if (el && !el.__observerInitialized) {
        try { initializeAnnotationTab(); el.__observerInitialized = true; } catch (e) { console.error(e); }
      }
    });
    try { mo.observe(document.body, { childList: true, subtree: true }); } catch (e) { /* ignore */ }
  }
} catch (e) { /* swallow */ }
