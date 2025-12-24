// Dynamic Tab Management for Von Application
// Handles creation and management of concept tabs based on vontology selection

// Removed imported populateContentSection to avoid duplicate with local implementation below

import { initializeAnnotationTab } from './annotationTab.js';
import { fetchConceptListWithSuffix, fetchSubtypesWithSuffix, initializeNamesForm, loadConceptAttributes, loadConceptNames, selectConceptWithSuffix } from './conceptTab.js';
import { getCurrentUserConceptId } from './domUtils.js';
import { DEFAULT_LANGUAGE } from './languageConfig.js';
import { detectMarkdown, renderSmartTextAsync } from './markdownUtils.js';
import { destroyPredicateView, initializePredicateView } from './predicateView.js';
import { getConceptTypeDisplayNames, setCurrentConceptType, setCurrentlySelectedConceptId, setSelectedConceptOriginalName } from './state.js';
import { activateTab } from './tabNavigation.js';
import { getKeyConceptIds, updateTabHeaderStarButtons, updateTreeKeyConceptBadge } from './vontology.js';

// Track dynamically created concept tabs
let dynamicConceptTabs = new Map();
let dynamicAnnotationTabs = new Map();
let annotationTabCounter = 0;
// Singleton context menu element for tab operations (created lazily)
let tabContextMenu = null;
let currentContextMenuTarget = null; // The tab button element for which menu opened
let lastContextMenuOpenAt = 0;
let lastContextMenuTriggerEl = null;

// Listen for key concepts being loaded and refresh all star buttons
if (typeof window !== 'undefined') {
    window.addEventListener('keyConceptsLoaded', (event) => {
        console.log('[dynamicTabs] Key concepts loaded event received!');
        const keyConceptIds = getKeyConceptIds();
        console.log('[dynamicTabs] keyConceptIds Set has', keyConceptIds.size, 'items:', Array.from(keyConceptIds));

        // Update all existing star buttons
        const starButtons = document.querySelectorAll('.key-concept-star-button');
        console.log('[dynamicTabs] Found', starButtons.length, 'star buttons to update');

        starButtons.forEach((btn, index) => {
            const tabContent = btn.closest('.tab-content');
            if (!tabContent) {
                console.log('[dynamicTabs] Star button', index, 'has no tab-content parent');
                return;
            }
            const conceptId = tabContent.dataset.conceptId;
            if (!conceptId) {
                console.log('[dynamicTabs] Tab content has no conceptId');
                return;
            }

            const isKey = keyConceptIds.has(conceptId);
            console.log(`[dynamicTabs] Updating star button for ${conceptId}: isKey=${isKey}`);
            btn.textContent = isKey ? '⭐' : '☆';
            btn.title = isKey ? 'Unmark as key concept' : 'Mark as key concept';
            if (isKey) {
                btn.classList.add('marked');
            } else {
                btn.classList.remove('marked');
            }
        });
        console.log('[dynamicTabs] Finished updating all star buttons');
    });
}

document.addEventListener('concept-tab-missing', (event) => {
    if (!event || !event.detail) return;
    const { conceptId, message } = event.detail;
    if (!conceptId) return;
    const info = dynamicConceptTabs.get(conceptId);
    if (!info) return;
    try {
        if (info.content) {
            info.content.classList.add('concept-tab-missing');
            info.content.dataset.conceptMissing = '1';
        }
    } catch (_) { /* ignore */ }
    try {
        if (info.button) {
            info.button.classList.add('tab-button-missing');
            if (message) {
                info.button.title = message;
            }
            info.button.setAttribute('aria-disabled', 'true');
        }
    } catch (_) { /* ignore */ }
});

// Accessible inline SVG icon registry (stroke inherits currentColor)
const ICON_SVGS = {
    // Distinct eye icon for annotate (was pencil duplicate of edit)
    annotate: '<svg aria-hidden="true" class="icon icon-annotate" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12Z"/><circle cx="12" cy="12" r="3"/></svg>',
    edit: '<svg aria-hidden="true" class="icon icon-edit" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
    delete: '<svg aria-hidden="true" class="icon icon-delete" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="M6 6l12 12"/></svg>',
    expand: '<svg aria-hidden="true" class="icon icon-expand" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg>',
    copy: '<svg aria-hidden="true" class="icon icon-copy" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>',
    refresh: '<svg aria-hidden="true" class="icon icon-refresh" viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M1 20v-6h6"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>'
};

function injectIconContent(el, key, label) {
    if (!el) return; const k = key || el.dataset.icon; const svg = ICON_SVGS[k]; if (!svg) return;
    if (el.querySelector('svg')) return; // already injected
    const sr = label || el.getAttribute('aria-label') || k || '';
    el.innerHTML = svg + '<span class="sr-only">' + sr + '</span>';
}
function upgradeActionButtonIcons(scope = document) {
    try { scope.querySelectorAll('.round-icon-button').forEach(btn => injectIconContent(btn)); } catch (_) { }
}// Utility: Copy text to clipboard with modern API and fallback
async function copyToClipboard(text, button) {
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
            showCopyFeedback(button, 'Copied!', 'success');
        } else {
            // Fallback for older browsers or non-secure contexts
            const textArea = document.createElement('textarea');
            textArea.value = text;
            textArea.style.position = 'fixed';
            textArea.style.left = '-999999px';
            textArea.style.top = '-999999px';
            document.body.appendChild(textArea);
            textArea.focus();
            textArea.select();
            const result = document.execCommand('copy');
            document.body.removeChild(textArea);
            if (result) {
                showCopyFeedback(button, 'Copied!', 'success');
            } else {
                throw new Error('Copy command failed');
            }
        }
    } catch (error) {
        console.warn('[copyToClipboard] Failed to copy text:', error);
        showCopyFeedback(button, 'Copy failed', 'error');
    }
}

// Minimal helper (JVNAUTOSCI-571): preserve paragraph boundaries when copying descriptions.
// Extracts block-level elements (p, li, blockquote, pre, headings) and joins with double newlines.
function extractDescriptionPlainText(root) {
    if (!root) return '';
    try { if (root.dataset && root.dataset.rawText) return root.dataset.rawText; } catch (_) { /* ignore */ }
    const selector = 'p,li,blockquote,pre,h1,h2,h3,h4,h5,h6';
    const blocks = Array.from(root.querySelectorAll(selector))
        .map(el => (el.textContent || '').replace(/[ \t\n\r]+/g, ' ').trim())
        .filter(Boolean);
    if (blocks.length === 0) {
        return (root.textContent || '').replace(/[ \t\n\r]+/g, ' ').trim();
    }
    // Join with blank line to avoid sentence run-on across original paragraph boundaries.
    return blocks.join('\n\n');
}
// Expose for tests (side-effect import)
try { Object.assign(globalThis, { __extractDescriptionPlainText: extractDescriptionPlainText }); } catch (_) { }

// Helper: Show visual feedback for copy operation
function showCopyFeedback(button, message, type) {
    const originalTitle = button.title;
    const originalAriaLabel = button.getAttribute('aria-label');

    // Update button appearance
    button.title = message;
    button.setAttribute('aria-label', message);
    button.style.opacity = type === 'success' ? '0.7' : '0.5';

    // Create/update status element for screen readers
    let statusEl = button.parentNode.querySelector('.copy-status');
    if (!statusEl) {
        statusEl = document.createElement('span');
        statusEl.className = 'copy-status sr-only';
        statusEl.setAttribute('aria-live', 'polite');
        button.parentNode.appendChild(statusEl);
    }
    statusEl.textContent = message;

    // Reset after delay
    setTimeout(() => {
        button.title = originalTitle;
        button.setAttribute('aria-label', originalAriaLabel);
        button.style.opacity = '';
        if (statusEl) statusEl.textContent = '';
    }, 2000);
}

// Robustness: observe DOM for late-added round icon buttons (avoids blank circles if section injected after initial upgrade call)
try {
    if (typeof window !== 'undefined' && !window.__roundIconObserverSetup) {
        const setupObserver = () => {
            if (!document.body) return false;
            const obs = new MutationObserver(muts => {
                for (const m of muts) {
                    if (m.type === 'childList') {
                        m.addedNodes.forEach(node => {
                            if (!(node instanceof HTMLElement)) return;
                            if (node.classList && node.classList.contains('round-icon-button')) injectIconContent(node);
                            node.querySelectorAll && node.querySelectorAll('.round-icon-button').forEach(btn => injectIconContent(btn));
                        });
                    } else if (m.type === 'attributes' && m.target instanceof HTMLElement && m.target.classList.contains('round-icon-button')) {
                        injectIconContent(m.target);
                    }
                }
            });
            obs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['data-icon'] });
            window.__roundIconObserverSetup = true;
            return true;
        };
        if (!setupObserver()) {
            document.addEventListener('DOMContentLoaded', setupObserver, { once: true });
        }
    }
} catch (_) { /* no-op */ }

// --- Ontology relationship caches (parents & instance-of types) ---
// parentTypesCache: conceptId -> array of parent type conceptIds (is_a_type_of)
// instanceOfCache: conceptId -> array of type ids this individual is an instance of (is_an_instance_of)
const parentTypesCache = new Map();
const instanceOfCache = new Map();
const namesCache = new Map(); // conceptId -> array of name objects from node_content (with text/abbrev/language)
// Memo for descendant checks: key `${candidate}|${ancestor}` -> boolean
const descendantMemo = new Map();
// Track in-flight relationship/name fetches to deduplicate concurrent requests
const inFlightFetches = new Map();

/** TESTING ONLY helper to inject ontology meta (avoids network in unit tests) */
export function __setTabOntologyMeta(conceptId, { kind, parentTypes, instanceOf }) {
    const info = dynamicConceptTabs.get(conceptId);
    if (info) {
        if (kind) info.kind = kind;
    }
    if (Array.isArray(parentTypes)) parentTypesCache.set(conceptId, parentTypes.slice());
    if (Array.isArray(instanceOf)) instanceOfCache.set(conceptId, instanceOf.slice());
}

// Hoisted utility: ensure note action buttons have glyphs even if icon font not yet applied
function backfillNoteGlyphs(container = document) {
    try {
        const noteActionSelectors = [
            '.note-actions .round-icon-button',
            '.notes-actions .round-icon-button'
        ];
        const buttons = container.querySelectorAll(noteActionSelectors.join(','));
        buttons.forEach(btn => {
            if (btn.innerHTML && btn.innerHTML.trim()) return;
            const icon = btn.dataset.icon || '';
            if (/annotate|note|add/i.test(icon)) btn.innerHTML = '&#9998;';
            else if (/delete|remove|close/i.test(icon)) btn.innerHTML = '&times;';
            else if (/edit|modify/i.test(icon)) btn.innerHTML = '&#128394;';
            else if (/view|open/i.test(icon)) btn.innerHTML = '&#128065;';
            else btn.innerHTML = '&#9998;';
        });
    } catch (e) { console.warn('[dynamicTabs] backfillNoteGlyphs failed', e); }
}

async function fetchNodeRelationships(conceptId) {
    // If we already have parents/instances AND names cached, return directly.
    // Previous logic short-circuited when parent or instance cache existed even if names were missing; that prevented
    // later label refinement from ever seeing names (tab kept long fallback label). We now only short‑circuit if names
    // are also present.
    const haveRel = parentTypesCache.has(conceptId) || instanceOfCache.has(conceptId) || namesCache.has(conceptId);
    const cachedNames = namesCache.get(conceptId);
    const haveNonEmptyNames = Array.isArray(cachedNames) && cachedNames.length > 0;
    if (haveRel && haveNonEmptyNames) {
        return {
            parents: parentTypesCache.get(conceptId) || [],
            instances: instanceOfCache.get(conceptId) || [],
            names: cachedNames || []
        };
    }
    const performFetch = () => (async () => {
        try {
            const resp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(conceptId)}`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            const parents = Array.isArray(data.is_a_type_of) ? data.is_a_type_of.filter(x => typeof x === 'string') : [];

            // (backfillNoteGlyphs hoisted)
            const instOf = Array.isArray(data.is_an_instance_of) ? data.is_an_instance_of.filter(x => typeof x === 'string') : [];
            const names = Array.isArray(data.names) ? data.names : [];
            if (!parentTypesCache.has(conceptId)) parentTypesCache.set(conceptId, parents);
            if (!instanceOfCache.has(conceptId)) instanceOfCache.set(conceptId, instOf);
            if (names.length && (!namesCache.has(conceptId) || (namesCache.get(conceptId) || []).length === 0)) namesCache.set(conceptId, names);
        } catch (e) {
            if (!parentTypesCache.has(conceptId)) parentTypesCache.set(conceptId, []);
            if (!instanceOfCache.has(conceptId)) instanceOfCache.set(conceptId, []);
            if (!namesCache.has(conceptId)) namesCache.set(conceptId, []);
        } finally {
            // Remove from in-flight so future calls can retry if needed
            inFlightFetches.delete(conceptId);
        }
        return {
            parents: parentTypesCache.get(conceptId) || [],
            instances: instanceOfCache.get(conceptId) || [],
            names: namesCache.get(conceptId) || []
        };
    })();

    if (!inFlightFetches.has(conceptId)) {
        inFlightFetches.set(conceptId, performFetch());
    }
    return inFlightFetches.get(conceptId);
}

async function isDescendantType(candidateId, ancestorId) {
    if (candidateId === ancestorId) return false; // a type is not a descendant of itself
    const key = `${candidateId}|${ancestorId}`;
    if (descendantMemo.has(key)) return descendantMemo.get(key);
    const { parents } = await fetchNodeRelationships(candidateId);
    if (!parents.length) { descendantMemo.set(key, false); return false; }
    if (parents.includes(ancestorId)) { descendantMemo.set(key, true); return true; }
    for (const p of parents) {
        if (await isDescendantType(p, ancestorId)) { descendantMemo.set(key, true); return true; }
    }
    descendantMemo.set(key, false); return false;
}

async function getOpenSubtypeTypeIds(rootTypeId) {
    const entries = Array.from(dynamicConceptTabs.values()).filter(t => t.conceptId !== rootTypeId && t.kind === 'type');
    const resultsSet = new Set();
    // Fast path: use already cached parent data if present (avoids async fetch & race overwrites)
    const unresolved = [];
    for (const entry of entries) {
        if (parentTypesCache.has(entry.conceptId)) {
            // BFS using cache only
            let parents = parentTypesCache.get(entry.conceptId) || [];
            const stack = [...parents];
            let isDesc = false;
            while (stack.length) {
                const p = stack.pop();
                if (p === rootTypeId) { isDesc = true; break; }
                if (parentTypesCache.has(p)) {
                    const gp = parentTypesCache.get(p) || [];
                    for (const g of gp) stack.push(g);
                }
            }
            if (isDesc) resultsSet.add(entry.conceptId);
        } else {
            unresolved.push(entry);
        }
    }
    // Fallback async check for unresolved entries
    for (const entry of unresolved) {
        try {
            if (await isDescendantType(entry.conceptId, rootTypeId)) resultsSet.add(entry.conceptId);
        } catch (_) { /* ignore */ }
    }
    return Array.from(resultsSet);
}

async function getOpenInstanceIdsForType(typeId, recursive) {
    const out = [];
    // Precompute descendant types if recursive
    let descendantTypes = null;
    if (recursive) {
        descendantTypes = new Set(await getOpenSubtypeTypeIds(typeId));
    }
    for (const info of dynamicConceptTabs.values()) {
        if (info.kind !== 'individual') continue;
        const { instances } = await fetchNodeRelationships(info.conceptId);
        if (!instances.length) continue;
        const direct = instances.includes(typeId);
        let inherited = false;
        if (!direct && recursive && descendantTypes && instances.some(t => descendantTypes.has(t) || t === typeId)) {
            // If instance is of a subtype (which itself may not have an open tab), fall back to descendant check chain
            for (const instType of instances) {
                if (instType === typeId) { inherited = true; break; }
                if (await isDescendantType(instType, typeId)) { inherited = true; break; }
            }
        }
        if (direct || inherited) out.push(info.conceptId);
    }
    return out;
}

// Exports for tests
export { getOpenInstanceIdsForType, getOpenSubtypeTypeIds };

/**
 * Creates or activates a dynamic concept tab for the given concept
 * @param {string} conceptId - The concept ID (e.g., "#V#Person")
 * @param {string} conceptName - The human readable name (e.g., "Person")
 */
// options: { kind?: 'type' | 'individual' }
export function createOrActivateConceptTab(conceptId, conceptName, activate = true, options = {}) {
    console.log(`[dynamicTabs] Creating/activating concept tab for: ${conceptId} (${conceptName})`);

    // Generate a unique tab ID based on the concept ID
    const tabId = generateTabId(conceptId);

    // Check if tab already exists
    if (dynamicConceptTabs.has(conceptId)) {
        console.log(`[dynamicTabs] Tab already exists for ${conceptId}${activate ? ', activating it' : ', not activating (per flag)'}`);
        if (activate) {
            activateTab(tabId);
        }
        return tabId;
    }

    // Create new dynamic concept tab
    createDynamicConceptTab(conceptId, conceptName, tabId, options.kind, { newlyCreated: options.newlyCreated });

    // Optionally activate the newly created tab
    if (activate) {
        activateTab(tabId);
    }

    return tabId;
}

/**
 * Creates a new dynamic concept tab
 * @param {string} conceptId - The concept ID
 * @param {string} conceptName - The human readable name
 * @param {string} tabId - The generated tab ID
 */
function createDynamicConceptTab(conceptId, conceptName, tabId, kind, opts = {}) {
    console.log(`[dynamicTabs] Creating new tab: ${tabId} for concept ${conceptId}`);

    // Get display names for the concept
    const displayNames = getConceptTypeDisplayNames(conceptId);

    // Create tab button: singular for individual tabs, plural for type tabs
    const buttonText = kind === 'individual' ? displayNames.singular : displayNames.plural;
    const tabButton = createTabButton(tabId, buttonText, conceptId, kind, opts);

    // After initial creation, asynchronously refine label using shortest name in current language
    try { tabButton.dataset.conceptId = conceptId; } catch (_) { }
    try { updateTabLabelWithShortestName(conceptId, tabButton); } catch (_) { /* ignore */ }

    // Asynchronously check if this individual is actually a predicate and update styling
    if (kind === 'individual') {
        (async () => {
            try {
                const nodeResp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(conceptId)}`);
                if (nodeResp.ok) {
                    const nodeJson = await nodeResp.json();
                    if (nodeJson && nodeJson.kind === 'predicate') {
                        // Update tab button styling to predicate
                        tabButton.classList.remove('individual-tab');
                        tabButton.classList.add('predicate-tab');
                        console.log(`[dynamicTabs] Tab ${conceptId} reclassified as predicate on creation`);
                    }
                }
            } catch (err) {
                // Silently ignore - will be detected on content load if this fails
                console.debug('[dynamicTabs] Early predicate detection failed', err);
            }
        })();
    }

    // Create tab content
    const tabContent = createTabContent(tabId, conceptId, kind);

    // Insert tab button in the correct position (after vontology, before import/export)
    insertTabButton(tabButton);

    // Insert tab content in the tab content area
    insertTabContent(tabContent);

    // Store tab information
    dynamicConceptTabs.set(conceptId, {
        tabId: tabId,
        conceptId: conceptId,
        conceptName: conceptName,
        displayNames: displayNames,
        kind: kind,
        button: tabButton,
        content: tabContent
    });

    console.log(`[dynamicTabs] Dynamic concept tab created successfully: ${tabId}`);
}

async function getCurrentLanguageCode() {
    // Try fetching from user settings first
    try {
        const response = await fetch('/api/settings/');
        if (response.ok) {
            const settings = await response.json();
            if (settings.preferred_language) {
                return settings.preferred_language;
            }
        }
    } catch (error) {
        console.warn('Could not load preferred language from settings:', error);
    }

    // Fallback to global, document lang, then default
    const docLang = (document && document.documentElement && document.documentElement.lang) ? document.documentElement.lang : null;
    return (window.currentLanguage || docLang || DEFAULT_LANGUAGE);
}

async function updateTabLabelWithShortestName(conceptId, tabButton, force = false) {
    if (!tabButton) return;
    const currentLang = await getCurrentLanguageCode();
    // Skip if already applied for this language and not forced
    if (!force && tabButton.dataset.shortestLabelApplied === '1' && tabButton.dataset.shortestLabelLanguage === currentLang) return;
    try {
        let { names } = await fetchNodeRelationships(conceptId);
        // If forced and we still have no names (common two-stage case: first fetch populated only relationships)
        // perform a one-shot retry after clearing any empty cache + in-flight entry.
        if (force && (!Array.isArray(names) || names.length === 0)) {
            try { if (namesCache.get(conceptId)?.length === 0) namesCache.delete(conceptId); } catch (_) { }
            try { inFlightFetches.delete(conceptId); } catch (_) { }
            const retry = await fetchNodeRelationships(conceptId);
            names = retry.names;
        }
        if (!Array.isArray(names) || names.length === 0) {
            // Attempt fallback fetch of concept doc (many individual concepts store names only there)
            try {
                const resp = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}`);
                if (resp.ok) {
                    const doc = await resp.json();
                    // Some concept API responses embed names under raw_doc.names, not top-level
                    let rawNames = [];
                    if (Array.isArray(doc.names) && doc.names.length) rawNames = doc.names;
                    else if (doc.raw_doc && Array.isArray(doc.raw_doc.names) && doc.raw_doc.names.length) rawNames = doc.raw_doc.names;
                    if (rawNames.length) {
                        names = rawNames.map(n => ({
                            text: n.text || n.name || n.abbrev || '',
                            language: n.language || n.lang || 'en',
                            abbrev: n.abbrev || (typeof n.name === 'string' && n.name?.length <= 6 ? n.name : undefined),
                            name: n.name,
                            type: n.type // Preserve name type (NL, CODE, ABBR)
                        }));
                        try { namesCache.set(conceptId, names); } catch (_) { /* ignore */ }
                    }
                }
            } catch (_) { /* ignore */ }
        }
        if (!Array.isArray(names) || names.length === 0) return;
        const lang = currentLang;
        // Filter names by language; support region fallbacks (en vs en-NZ)
        let filtered = names.filter(n => n && (n.language === lang));
        if (filtered.length === 0) {
            // Try matching base language part if region variant present
            const langBase = (lang || '').split('-')[0];
            if (langBase) {
                filtered = names.filter(n => n && (n.language === langBase || (n.language || '').startsWith(langBase + '-')));
            }
        }
        if (filtered.length === 0) filtered = names.slice();
        const relevant = filtered; // ensure variable exists
        const candidates = [];
        for (const n of relevant) {
            if (n) {
                if (typeof n.abbrev === 'string' && n.abbrev.trim()) candidates.push(n.abbrev.trim());
                if (typeof n.text === 'string' && n.text.trim()) candidates.push(n.text.trim());
                if (typeof n.name === 'string' && n.name.trim()) candidates.push(n.name.trim()); // fallback alias
            }
        }
        if (!candidates.length) return;
        // CRITICAL: Prioritize names in the user's preferred language before selecting shortest.
        // This ensures "Person" (en-NZ) is chosen over "人" (zh) when user prefers English.
        // Sort by: 1) language match (current lang first), 2) Type (NL > ABBR > CODE), 3) length (shorter first)
        const langBase = (lang || '').split('-')[0];
        candidates.sort((a, b) => {
            // Find corresponding name objects to check language and type
            const aName = relevant.find(n => n.text === a || n.abbrev === a || n.name === a);
            const bName = relevant.find(n => n.text === b || n.abbrev === b || n.name === b);

            const aLang = (aName?.language || '').toLowerCase();
            const bLang = (bName?.language || '').toLowerCase();
            const aType = (aName?.type || '').toUpperCase();
            const bType = (bName?.type || '').toUpperCase();

            const aMatchExact = aLang === lang.toLowerCase();
            const bMatchExact = bLang === lang.toLowerCase();
            const aMatchBase = aLang === langBase || aLang.startsWith(langBase + '-');
            const bMatchBase = bLang === langBase || bLang.startsWith(langBase + '-');

            // 1. Language Preference
            if (aMatchExact && !bMatchExact) return -1;
            if (!aMatchExact && bMatchExact) return 1;
            if (aMatchBase && !bMatchBase) return -1;
            if (!aMatchBase && bMatchBase) return 1;

            // 2. Type Preference (NL > ABBR > CODE)
            // We want NL to come first.
            const typeScore = (t) => {
                if (t === 'NL') return 3;
                if (t === 'ABBR') return 2;
                return 1; // CODE or others
            };
            const scoreA = typeScore(aType);
            const scoreB = typeScore(bType);
            if (scoreA > scoreB) return -1;
            if (scoreB > scoreA) return 1;

            // 3. Length Preference (Shortest first)
            return a.length - b.length || a.localeCompare(b);
        });
        const shortest = candidates[0];
        if (!shortest) return;
        const span = tabButton.querySelector('span');
        if (span && span.textContent !== shortest) {
            tabButton.dataset.originalLabel = span.textContent;
            span.textContent = shortest;
            tabButton.dataset.shortestLabelApplied = '1';
            tabButton.dataset.shortestLabelLanguage = lang;
        }
        // Even if unchanged, record language to avoid redundant future recomputations
        if (!tabButton.dataset.shortestLabelLanguage) tabButton.dataset.shortestLabelLanguage = lang;
    } catch (e) {
        console.warn('[dynamicTabs] Failed to update tab label with shortest name', conceptId, e);
    }
}

function relabelAllDynamicConceptTabs(force = false) {
    for (const info of dynamicConceptTabs.values()) {
        try { updateTabLabelWithShortestName(info.conceptId, info.button, force); } catch (_) { /* ignore */ }
    }
}

/**
 * Creates a tab button element
 * @param {string} tabId - The tab ID
 * @param {string} displayName - The display name for the tab
 * @param {string} conceptId - The concept ID for close functionality
 * @returns {HTMLElement} The tab button element
 */
function createTabButton(tabId, displayName, conceptId, kind, opts = {}) {
    const tabButton = document.createElement('div');
    tabButton.className = 'tab-button closable';
    tabButton.dataset.tab = tabId;
    tabButton.dataset.conceptId = conceptId;
    if (kind === 'type') {
        tabButton.classList.add('type-tab');
    } else if (kind === 'individual') {
        tabButton.classList.add('individual-tab');
    } else if (kind === 'predicate') {
        tabButton.classList.add('predicate-tab');
    } // 'unknown' gets no specific class yet
    if (kind) {
        tabButton.dataset.tabKind = kind;
    }
    // Optional NEW badge for recently created tabs
    if (opts.newlyCreated) {
        const badge = document.createElement('span');
        badge.className = 'new-tab-badge';
        // Color-code badge by tab kind
        if (kind === 'type') badge.classList.add('type');
        if (kind === 'individual') badge.classList.add('individual');
        if (kind === 'predicate') badge.classList.add('predicate');
        badge.textContent = 'NEW';
        badge.title = 'Recently created';
        tabButton.dataset.newlyCreated = 'true';
        // Auto-expire badge after 5 minutes unless user opens the tab first
        const timerId = setTimeout(() => {
            try {
                const b = tabButton.querySelector('.new-tab-badge');
                if (b) b.remove();
                delete tabButton.dataset.newlyCreated;
                delete tabButton.dataset.newBadgeTimer;
            } catch (_) { /* no-op */ }
        }, 5 * 60 * 1000);
        tabButton.dataset.newBadgeTimer = String(timerId);
        // We'll append the badge after creating text span below
        tabButton.__newBadge = badge;
    }

    // Create tab text
    const tabText = document.createElement('span');
    tabText.textContent = displayName;
    tabButton.appendChild(tabText);
    if (tabButton.__newBadge) {
        tabButton.appendChild(tabButton.__newBadge);
        delete tabButton.__newBadge;
    }

    // Create close button
    const closeButton = document.createElement('span');
    closeButton.className = 'close-tab';
    closeButton.textContent = '×';
    closeButton.title = `Close ${displayName} tab`;

    // Close button click handler
    closeButton.addEventListener('click', (e) => {
        e.stopPropagation(); // Prevent tab activation
        closeDynamicConceptTab(conceptId);
    });

    tabButton.appendChild(closeButton);

    // Tab button click handler
    tabButton.addEventListener('click', () => {
        // Remove NEW badge on first activation
        if (tabButton.dataset.newlyCreated === 'true') {
            const badge = tabButton.querySelector('.new-tab-badge');
            if (badge) badge.remove();
            delete tabButton.dataset.newlyCreated;
            // Clear auto-expire timer if present
            const t = tabButton.dataset.newBadgeTimer;
            if (t) {
                clearTimeout(Number(t));
                delete tabButton.dataset.newBadgeTimer;
            }
        }
        activateTab(tabId);
    });

    // Context menu (right-click) handler
    tabButton.addEventListener('contextmenu', (e) => {
        try {
            e.preventDefault();
            openTabContextMenu(e, tabButton, conceptId);
        } catch (err) { console.warn('[dynamicTabs] contextmenu handler failed', err); }
    });

    // Keyboard accessibility: open menu with Shift+F10 or Menu key when focused
    tabButton.setAttribute('tabindex', '0');
    tabButton.addEventListener('keydown', (e) => {
        if ((e.shiftKey && e.key === 'F10') || e.key === 'ContextMenu') {
            e.preventDefault();
            const rect = tabButton.getBoundingClientRect();
            const fakeEvent = { clientX: rect.left + 8, clientY: rect.bottom + 4, preventDefault: () => { } };
            openTabContextMenu(fakeEvent, tabButton, conceptId);
        }
    });

    return tabButton;
}

/**
 * Creates tab content element
 * @param {string} tabId - The tab ID
 * @param {string} conceptId - The concept ID
 * @returns {HTMLElement} The tab content element
 */
function createTabContent(tabId, conceptId, kind) {
    const tabContent = document.createElement('div');
    tabContent.id = tabId;
    tabContent.className = 'tab-content';
    tabContent.dataset.conceptId = conceptId;
    if (kind) {
        tabContent.dataset.tabKind = kind;
    }

    // Add loading state initially
    tabContent.innerHTML = '<div class="loading">Loading concept interface...</div>';

    return tabContent;
}

/**
 * Inserts tab button in the correct position in the tab container
 * @param {HTMLElement} tabButton - The tab button to insert
 */
function insertTabButton(tabButton) {
    const tabContainer = document.getElementById('tabContainer');
    const importExportTab = document.querySelector('.tab-button[data-tab="importExportTab"]');

    if (tabContainer && importExportTab) {
        // Insert before import/export tab
        tabContainer.insertBefore(tabButton, importExportTab);
    } else if (tabContainer) {
        // Fallback: append to end
        tabContainer.appendChild(tabButton);
    } else {
        console.error('[dynamicTabs] Tab container not found');
    }
}

/**
 * Inserts tab content in the tab content area
 * @param {HTMLElement} tabContent - The tab content to insert
 */
function insertTabContent(tabContent) {
    const tabContentArea = document.querySelector('.tab-content-area');

    if (tabContentArea) {
        tabContentArea.appendChild(tabContent);
    } else {
        console.error('[dynamicTabs] Tab content area not found');
    }
}

/**
 * Closes and removes a dynamic concept tab
 * @param {string} conceptId - The concept ID of the tab to close
 */
export function closeDynamicConceptTab(conceptId) {
    console.log(`[dynamicTabs] Closing concept tab for: ${conceptId}`);

    const tabInfo = dynamicConceptTabs.get(conceptId);
    if (!tabInfo) {
        console.warn(`[dynamicTabs] No tab found for concept: ${conceptId}`);
        return;
    }

    // Disconnect MutationObserver (names section) if present to avoid leaks
    try {
        if (tabInfo.namesObserver && typeof tabInfo.namesObserver.disconnect === 'function') {
            tabInfo.namesObserver.disconnect();
        }
    } catch (_) { /* ignore */ }

    // Clean up predicate view if present
    try {
        destroyPredicateView(conceptId);
    } catch (_) { /* ignore */ }

    // Check if this tab is currently active
    const isActive = tabInfo.content.classList.contains('active');

    // Remove DOM elements
    if (tabInfo.button && tabInfo.button.parentNode) {
        // Clear any pending badge timers to avoid leaks
        try {
            const t = tabInfo.button.dataset?.newBadgeTimer;
            if (t) clearTimeout(Number(t));
        } catch (_) { /* no-op */ }
        tabInfo.button.parentNode.removeChild(tabInfo.button);
    }

    if (tabInfo.content && tabInfo.content.parentNode) {
        tabInfo.content.parentNode.removeChild(tabInfo.content);
    }

    // Remove from tracking
    dynamicConceptTabs.delete(conceptId);

    // If the closed tab was active, activate a different tab
    if (isActive) {
        // Try to activate the chat tab as default
        activateTab('chatTab');
    }

    console.log(`[dynamicTabs] Successfully closed tab for concept: ${conceptId}`);
}

// Close a dynamic annotation tab
function closeDynamicAnnotationTab(tabId) {
    const info = dynamicAnnotationTabs.get(tabId);
    if (!info) return;
    const isActive = info.content.classList.contains('active');
    if (info.button && info.button.parentNode) {
        info.button.parentNode.removeChild(info.button);
    }
    if (info.content && info.content.parentNode) {
        info.content.parentNode.removeChild(info.content);
    }
    dynamicAnnotationTabs.delete(tabId);
    if (isActive) {
        try { activateTab('annotationTab'); } catch (_) { }
    }
}

// Create and load a dynamic annotation tab
function createAnnotationTab(text, conceptName, source) {
    const suffix = `ann${++annotationTabCounter}`;
    const tabId = `annotationTab_${suffix}`;
    const icon = source === 'description' ? 'D' : 'N';

    // Create tab button
    const tabButton = document.createElement('div');
    tabButton.className = 'tab-button closable';
    tabButton.dataset.tab = tabId;
    // Build label with preceding eye icon and source letter (D/N). We avoid injecting unsanitized HTML for conceptName.
    try {
        tabButton.innerHTML = `<span class="annotation-tab-icon" aria-hidden="true">${ICON_SVGS.annotate}</span><span class="annotation-source-letter">${icon}</span> `;
        tabButton.appendChild(document.createTextNode(conceptName));
    } catch (_) {
        // Fallback: plain text if innerHTML injection fails for any reason
        tabButton.textContent = `${icon} ${conceptName}`;
    }
    const closeButton = document.createElement('span');
    closeButton.className = 'close-tab';
    closeButton.textContent = '×';
    closeButton.title = 'Close annotation tab';
    closeButton.addEventListener('click', (e) => {
        e.stopPropagation();
        closeDynamicAnnotationTab(tabId);
    });
    tabButton.appendChild(closeButton);
    tabButton.addEventListener('click', () => activateTab(tabId));
    insertTabButton(tabButton);

    // Create content container
    const tabContent = document.createElement('div');
    tabContent.id = tabId;
    tabContent.className = 'tab-content';
    insertTabContent(tabContent);

    dynamicAnnotationTabs.set(tabId, { button: tabButton, content: tabContent });

    loadAnnotationTabContent(tabId, suffix, text);
    activateTab(tabId);
}

async function loadAnnotationTabContent(tabId, suffix, text) {
    const info = dynamicAnnotationTabs.get(tabId);
    if (!info) return;
    try {
        const response = await fetch('/annotation_tab');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        let html = await response.text();
        html = html.replace(/id="([^"]+)"/g, `id="$1_${suffix}"`);
        info.content.innerHTML = html;
        initializeAnnotationTab(suffix);
        const input = document.getElementById(`annotationInput_${suffix}`);
        if (input) input.value = text;
        const runBtn = document.getElementById(`runAnnotationButton_${suffix}`);
        runBtn?.click();
    } catch (e) {
        console.error('[dynamicTabs] Failed to load annotation tab content', e);
        info.content.innerHTML = `<div class="error">Failed to load annotation tab</div>`;
    }
}

/**
 * Loads content for a dynamic concept tab
 * @param {string} tabId - The tab ID
 * @param {string} conceptId - The concept ID
 */
export async function loadDynamicConceptTabContent(tabId, conceptId) {
    console.log(`[dynamicTabs] Loading content for tab: ${tabId}, concept: ${conceptId}`);

    const tabInfo = dynamicConceptTabs.get(conceptId);
    if (!tabInfo) {
        console.error(`[dynamicTabs] No tab info found for concept: ${conceptId}`);
        return;
    }

    // Check if content is already initialized to avoid reloading on tab switch
    if (tabInfo.content && tabInfo.content.dataset.initialized === 'true') {
        console.log(`[dynamicTabs] Tab content for ${conceptId} already initialized, skipping reload`);
        return;
    }

    try {
        // Fetch the concept tab template
        const response = await fetch('/concept_tab');
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }

        const html = await response.text();

        // Make IDs unique for this dynamic tab
        const uniqueIdSuffix = conceptId.replace(/[^a-zA-Z0-9]/g, '_');
        let modifiedHtml = html.replace(/id="([^"]+)"/g, `id="$1_${uniqueIdSuffix}"`);
        // Inject data attribute on the header span without duplicating the suffix
        const headerIdWithSuffix = `conceptTypeDisplayNamePluralElement_${uniqueIdSuffix}`;
        const headerSpanRegex = new RegExp(`(<span[^>]*id="${headerIdWithSuffix}")`);
        modifiedHtml = modifiedHtml.replace(headerSpanRegex, `$1 data-concept-id="${conceptId}"`);

        tabInfo.content.innerHTML = modifiedHtml;

        // Add a small kind badge and an ID copy chip to the header if available.
        // Fallback inference: if tabInfo.kind is absent (regression / legacy dispatch), infer from node content.
        let nodeJson = null; // Store for both badge and predicate detection
        try {
            let { kind } = tabInfo;
            const headerSpan = document.getElementById(`conceptTypeDisplayNamePluralElement_${uniqueIdSuffix}`);
            const headerDiv = headerSpan ? headerSpan.closest('.concept-header') : null;
            // Always attempt inference: even if an initial kind was provided it may be wrong.
            try {
                const nodeResp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(conceptId)}`);
                if (nodeResp.ok) {
                    nodeJson = await nodeResp.json();
                    // Prefer backend-provided computed_kind if available
                    if (nodeJson.computed_kind && nodeJson.computed_kind !== kind) {
                        kind = nodeJson.computed_kind;
                        const updated = dynamicConceptTabs.get(conceptId);
                        if (updated) { updated.kind = kind; }
                    }
                    const rel = (nodeJson && (nodeJson.raw_doc || {}).relationships) || {}; // prefer raw_doc->relationships if present
                    const instanceRel = rel.is_an_instance_of || nodeJson.is_an_instance_of;
                    const typeRel = rel.is_a_type_of || nodeJson.is_a_type_of;
                    const nonEmptyInstance = Array.isArray(instanceRel) ? instanceRel.length > 0 : !!instanceRel;
                    const nonEmptyType = Array.isArray(typeRel) ? typeRel.length > 0 : !!typeRel;
                    let inferred = null;
                    if (nonEmptyInstance && !nonEmptyType) inferred = 'individual';
                    else if (nonEmptyType) inferred = 'type';
                    if (inferred && inferred !== kind) {
                        kind = inferred;
                        const updated = dynamicConceptTabs.get(conceptId);
                        if (updated) { updated.kind = inferred; }
                    }
                }
            } catch (inferErr) {
                console.warn('[dynamicTabs] Kind inference failed', inferErr);
            }

            if (headerDiv && (kind === 'type' || kind === 'individual' || kind === 'predicate') && !headerDiv.querySelector('.kind-badge')) {
                const badge = document.createElement('span');
                // Use backend's three-way classification directly
                const backendKind = nodeJson?.kind || kind;
                const badgeKind = backendKind;
                const badgeText = backendKind === 'predicate' ? 'Predicate' : (backendKind === 'type' ? 'Type' : 'Individual');
                badge.className = `kind-badge ${badgeKind}`;
                badge.textContent = badgeText;
                // Insert as first child for consistent left alignment
                if (headerDiv.firstChild) headerDiv.insertBefore(badge, headerDiv.firstChild); else headerDiv.appendChild(badge);

                // Update tab button styling if this is a predicate
                if (backendKind === 'predicate' && tabInfo.button) {
                    tabInfo.button.classList.remove('individual-tab');
                    tabInfo.button.classList.add('predicate-tab');
                }
            }
            if (headerDiv) {
                attachConceptIdCopyChip(headerDiv, conceptId, kind);
                attachRawDataButton(headerDiv, conceptId, kind, tabInfo.content);
                attachKeyConceptStarButton(headerDiv, conceptId);
                attachAnalysisButtons(headerDiv, conceptId, kind);
            }
        } catch (e) {
            console.warn('[dynamicTabs] Could not attach kind badge:', e);
        }

        // Post-fetch reclassification: if the tab was created with the wrong kind (e.g. assumed type) but
        // data indicates otherwise, reconcile classes, dataset flags, and badge.
        try {
            const currentInfo = dynamicConceptTabs.get(conceptId);
            if (currentInfo) {
                const storedKind = currentInfo.kind; // may have been inferred above
                const contentEl = currentInfo.content;
                const buttonEl = currentInfo.button;
                const datasetKind = contentEl.dataset.tabKind;
                if (storedKind && datasetKind !== storedKind) {
                    // Update dataset
                    contentEl.dataset.tabKind = storedKind;
                    if (buttonEl) buttonEl.dataset.tabKind = storedKind;
                    // Swap CSS classes
                    buttonEl.classList.remove('type-tab', 'individual-tab');
                    contentEl.classList.remove('type-tab', 'individual-tab');
                    if (storedKind === 'type') {
                        buttonEl.classList.add('type-tab');
                        contentEl.classList.add('type-tab');
                    } else if (storedKind === 'individual') {
                        buttonEl.classList.add('individual-tab');
                        contentEl.classList.add('individual-tab');
                    }
                    // Ensure badge reflects updated kind; if existing wrong badge, replace it.
                    const headerSpan2 = document.getElementById(`conceptTypeDisplayNamePluralElement_${uniqueIdSuffix}`);
                    const headerDiv2 = headerSpan2 ? headerSpan2.closest('.concept-header') : null;
                    if (headerDiv2) {
                        let badge = headerDiv2.querySelector('.kind-badge');
                        if (!badge) {
                            badge = document.createElement('span');
                            headerDiv2.insertBefore(badge, headerDiv2.firstChild || null);
                        }
                        // Use backend's three-way classification from node content
                        const backendKind = nodeJson?.kind || storedKind;
                        const badgeKind = backendKind;
                        const badgeText = backendKind === 'predicate' ? 'Predicate' : (backendKind === 'type' ? 'Type' : 'Individual');
                        badge.className = `kind-badge ${badgeKind}`;
                        badge.textContent = badgeText;

                        // Update tab button styling if this is a predicate
                        if (backendKind === 'predicate' && buttonEl) {
                            buttonEl.classList.remove('individual-tab');
                            buttonEl.classList.add('predicate-tab');
                        }
                    }
                    console.log(`[dynamicTabs] Reclassified tab ${conceptId} to kind=${storedKind}`);
                }
            }
        } catch (reclassErr) {
            console.warn('[dynamicTabs] Reclassification step failed', reclassErr);
        }

        // Set the current concept type for this tab only if this is a TYPE tab.
        // Individual tabs should not override the global currentConceptType,
        // otherwise new instance creation may incorrectly inherit an individual's ID as parent.
        const effectiveKind = (dynamicConceptTabs.get(conceptId) || {}).kind;
        if (effectiveKind && effectiveKind !== 'individual' && effectiveKind !== 'unknown') {
            setCurrentConceptType(conceptId);
        } else {
            console.log(`[dynamicTabs] Skipping setCurrentConceptType for individual tab: ${conceptId}`);
        }

        // Initialize the concept tab functionality with unique element IDs
        setTimeout(() => {
            console.log(`[dynamicTabs] Initializing concept tab functionality for ${conceptId}`);
            initializeDynamicConceptTab(conceptId, uniqueIdSuffix);
            // Attach MutationObserver fallback for name changes (only once content likely rendered)
            try { attachNamesObserver(conceptId, uniqueIdSuffix); } catch (_) { }
            // Backfill note glyphs after potential notes render
            try { backfillNoteGlyphs(tabInfo.content); } catch (e) { console.warn('[dynamicTabs] backfillNoteGlyphs post-init failed', e); }
        }, 100);

        // Post-load safety: if legacy description button still present without upgraded actions, trigger ensure
        try {
            const legacyEdit = tabInfo.content.querySelector('button') && Array.from(tabInfo.content.querySelectorAll('button')).find(b => /edit description/i.test(b.textContent || ''));
            const upgraded = tabInfo.content.querySelector('.desc-actions .round-icon-button[data-icon="annotate"]');
            if (legacyEdit && !upgraded) {
                await ensureUnifiedDescriptionSection(conceptId, uniqueIdSuffix);
            }
        } catch (legacyCheckErr) { console.warn('[dynamicTabs] post-load legacy description check failed', legacyCheckErr); }

        // Mark content as initialized to prevent reloading on tab switch
        tabInfo.content.dataset.initialized = 'true';

        console.log(`[dynamicTabs] Successfully loaded content for ${tabId}`);

    } catch (error) {
        console.error(`[dynamicTabs] Error loading content for ${tabId}:`, error);
        tabInfo.content.innerHTML = `<div class="error">Error loading concept tab content. Please try again.</div>`;
    }
}
/**
 * Generates a unique tab ID from a concept ID
 * @param {string} conceptId - The concept ID (e.g., "#V#Person")
 * @returns {string} A safe tab ID (e.g., "conceptTab_Person")
 */
function generateTabId(conceptId) {
    // Remove special characters and create a safe ID
    const safeName = conceptId.replace(/[^a-zA-Z0-9]/g, '_');
    return `conceptTab_${safeName}`;
}

/**
 * Checks if a concept tab exists for the given concept ID
 * @param {string} conceptId - The concept ID to check
 * @returns {boolean} True if tab exists
 */
export function hasConceptTab(conceptId) {
    return dynamicConceptTabs.has(conceptId);
}

/**
 * Gets all active dynamic concept tabs
 * @returns {Array} Array of tab information objects
 */
export function getActiveDynamicTabs() {
    return Array.from(dynamicConceptTabs.values());
}

/**
 * Handles vontology node selection to create/activate concept tabs
 * This function should be called when a vontology node is selected
 * @param {string} conceptId - The selected concept ID
 * @param {string} conceptName - The selected concept name
 */
export function handleVontologyNodeSelection(conceptId, conceptName, activate = true) {
    console.log(`[dynamicTabs] Handling vontology node selection: ${conceptId} (${conceptName})`);

    // Only create tabs for valid concept nodes (not for general tree navigation)
    if (conceptId && conceptId.startsWith('#V#')) {
        // Defer classification until relationships fetched; start as unknown
        createOrActivateConceptTab(conceptId, conceptName, activate, { kind: 'unknown' });
    } else {
        console.log(`[dynamicTabs] Skipping tab creation for non-concept node: ${conceptId}`);
    }
}

/**
 * Closes all dynamic concept tabs
 */
export function closeAllDynamicConceptTabs() {
    console.log('[dynamicTabs] Closing all dynamic concept tabs');

    const conceptIds = Array.from(dynamicConceptTabs.keys());
    conceptIds.forEach(conceptId => {
        closeDynamicConceptTab(conceptId);
    });
}

// Close all tabs except the given conceptId
function closeOtherTabs(conceptId) {
    const ids = Array.from(dynamicConceptTabs.keys());
    ids.forEach(id => { if (id !== conceptId) closeDynamicConceptTab(id); });
}

// Close all tabs to the right of the provided conceptId (based on current DOM order)
function closeTabsToRight(conceptId) {
    const tabContainer = document.getElementById('tabContainer');
    if (!tabContainer) return;
    const buttons = Array.from(tabContainer.querySelectorAll('.tab-button[data-concept-id]'));
    const idx = buttons.findIndex(b => b.dataset.conceptId === conceptId);
    if (idx === -1) return;
    for (let i = buttons.length - 1; i > idx; i--) {
        const cid = buttons[i].dataset.conceptId;
        closeDynamicConceptTab(cid);
    }
}

// Lazy create & show context menu
function openTabContextMenu(evt, tabButton, conceptId) {
    if (!tabContextMenu) {
        tabContextMenu = document.createElement('div');
        tabContextMenu.className = 'tab-context-menu';
        tabContextMenu.style.position = 'fixed';
        tabContextMenu.style.zIndex = '10000';
        tabContextMenu.style.minWidth = '180px';
        tabContextMenu.style.background = '#ffffff';
        tabContextMenu.style.border = '1px solid #d1d5db';
        tabContextMenu.style.borderRadius = '6px';
        tabContextMenu.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
        tabContextMenu.style.padding = '4px 0';
        tabContextMenu.style.fontSize = '14px';
        tabContextMenu.setAttribute('role', 'menu');
        tabContextMenu.setAttribute('aria-label', 'Tab actions');
        document.body.appendChild(tabContextMenu);

        // Global dismissal handlers
        document.addEventListener('click', (e) => {
            // Ignore the synthetic (or immediate) click that may follow a contextmenu invocation
            if (lastContextMenuTriggerEl && e.target === lastContextMenuTriggerEl) {
                if (Date.now() - lastContextMenuOpenAt < 60) {
                    return; // swallow this initial click
                }
            }
            if (tabContextMenu && !tabContextMenu.contains(e.target)) hideTabContextMenu();
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') hideTabContextMenu();
        });
        window.addEventListener('blur', hideTabContextMenu);
    }
    // If the document was reset (e.g., in tests) and the element was removed, re-attach it
    if (tabContextMenu && !document.body.contains(tabContextMenu)) {
        document.body.appendChild(tabContextMenu);
    }

    currentContextMenuTarget = tabButton;
    buildTabContextMenuItems(conceptId);
    lastContextMenuOpenAt = Date.now();
    lastContextMenuTriggerEl = tabButton;

    // Position
    const x = evt.clientX;
    const y = evt.clientY;
    tabContextMenu.style.left = x + 'px';
    tabContextMenu.style.top = y + 'px';
    // Ensure on screen
    requestAnimationFrame(() => {
        const rect = tabContextMenu.getBoundingClientRect();
        let nx = rect.left, ny = rect.top;
        const vw = window.innerWidth, vh = window.innerHeight;
        if (rect.right > vw) nx = Math.max(4, vw - rect.width - 4);
        if (rect.bottom > vh) ny = Math.max(4, vh - rect.height - 4);
        tabContextMenu.style.left = nx + 'px';
        tabContextMenu.style.top = ny + 'px';
    });

    tabContextMenu.style.display = 'block';
    // Focus first item for accessibility
    const firstItem = tabContextMenu.querySelector('[role="menuitem"]');
    if (firstItem) firstItem.focus();
}

function hideTabContextMenu() {
    if (tabContextMenu) {
        tabContextMenu.style.display = 'none';
        tabContextMenu.innerHTML = '';
    }
    currentContextMenuTarget = null;
}

/**
 * Reloads concept data from the backend and refreshes the tab UI
 * @param {string} conceptId - The concept ID to reload
 */
async function reloadConceptTab(conceptId) {
    console.log(`[dynamicTabs] Reloading concept tab: ${conceptId}`);

    try {
        // Calculate the suffix used for this tab's DOM elements
        const suffix = conceptId.replace(/[^a-zA-Z0-9]/g, '_');

        // Show loading state on both tab button and refresh icon
        const tabButton = document.querySelector(`[data-tab-id="${conceptId}"] .tab-name`);
        const originalText = tabButton?.textContent;
        if (tabButton) {
            tabButton.textContent = '🔄 Reloading...';
        }

        // Add loading animation to refresh button icon
        const refreshButton = document.getElementById(`refreshConceptButton_${suffix}`);
        if (refreshButton) {
            refreshButton.classList.add('loading');
            refreshButton.disabled = true;
        }

        // Fetch fresh concept data from backend
        const encodedConceptId = encodeURIComponent(conceptId);
        const response = await fetch(`/api/concepts/${encodedConceptId}`);

        if (!response.ok) {
            throw new Error(`Failed to reload concept: ${response.status} ${response.statusText}`);
        }

        const conceptData = await response.json();

        // Update the tab using the same logic as initial selection
        // This refreshes notes, names, and all UI elements
        selectConceptWithSuffix(conceptData, suffix);

        // Refresh text-relation driven sections (notes/content/description). These sections can stay mounted
        // across reloads, so we explicitly ask them to refresh for the current concept.
        try {
            await ensureUnifiedDescriptionSection(conceptId, suffix);
        } catch (_) { /* ignore */ }
        try {
            await populateNotesSection(conceptId, suffix);
        } catch (_) { /* ignore */ }
        try {
            await populateContentSection(conceptId, suffix);
        } catch (_) { /* ignore */ }

        // Reload names explicitly to ensure they're fresh
        await loadConceptNames(conceptId, suffix);

        // Reload attributes explicitly
        await loadConceptAttributes(conceptId, suffix);

        // Refresh lists for Type tabs (Instances and Subtypes)
        const kind = (dynamicConceptTabs.get(conceptId) || {}).kind;
        if (kind !== 'individual') {
            try {
                await fetchConceptListWithSuffix(conceptId, suffix);
                await fetchSubtypesWithSuffix(conceptId, suffix);
            } catch (listErr) {
                console.warn('[dynamicTabs] Failed to refresh lists during reload', listErr);
            }
        }

        console.log(`[dynamicTabs] Concept tab reloaded successfully: ${conceptId}`);

        // Restore button text
        if (tabButton && originalText) {
            tabButton.textContent = originalText;
        }

        // Remove loading animation from refresh button
        if (refreshButton) {
            refreshButton.classList.remove('loading');
            refreshButton.disabled = false;
        }

    } catch (error) {
        console.error(`[dynamicTabs] Failed to reload concept tab:`, error);

        // Show error in UI
        const tabButton = document.querySelector(`[data-tab-id="${conceptId}"] .tab-name`);
        if (tabButton) {
            const originalText = tabButton.textContent.replace('🔄 Reloading...', '').replace('❌ ', '');
            tabButton.textContent = `❌ ${originalText}`;
            setTimeout(() => {
                if (tabButton) tabButton.textContent = originalText;
            }, 2000);
        }

        // Remove loading animation from refresh button on error
        const suffix = conceptId.replace(/[^a-zA-Z0-9]/g, '_');
        const refreshButton = document.getElementById(`refreshConceptButton_${suffix}`);
        if (refreshButton) {
            refreshButton.classList.remove('loading');
            refreshButton.disabled = false;
        }

        // Show user-friendly error message
        alert(`Failed to reload concept: ${error.message}`);
    }
}

function buildTabContextMenuItems(conceptId) {
    if (!tabContextMenu) return;
    tabContextMenu.innerHTML = '';

    // key: stable identifier used for data-action attribute & tests
    const addItem = (key, label, action, shortcut) => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'tab-context-menu-item';
        item.setAttribute('role', 'menuitem');
        if (key) item.dataset.action = key;
        // Inline minimal resets (full look handled by CSS class in styles.css)
        item.style.display = 'flex';
        item.style.alignItems = 'center';
        item.style.width = '100%';
        item.style.gap = '8px';
        item.style.padding = '6px 10px';
        item.style.background = 'transparent';
        item.style.border = 'none';
        item.style.cursor = 'pointer';
        item.style.textAlign = 'left';
        item.style.font = 'inherit';
        item.style.boxShadow = 'none';
        item.style.color = '#1f2937'; // Override global button white text
        item.addEventListener('click', () => { try { action(); } finally { hideTabContextMenu(); } });
        item.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); action(); hideTabContextMenu(); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); focusSibling(item, 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); focusSibling(item, -1); }
            else if (e.key === 'Home') { e.preventDefault(); focusFirst(); }
            else if (e.key === 'End') { e.preventDefault(); focusLast(); }
            else if (e.key === 'Escape') { hideTabContextMenu(); currentContextMenuTarget?.focus?.(); }
        });
        const span = document.createElement('span');
        span.textContent = label;
        const shortcutSpan = document.createElement('span');
        shortcutSpan.textContent = shortcut || '';
        shortcutSpan.style.marginLeft = 'auto';
        shortcutSpan.style.fontSize = '11px';
        item.appendChild(span);
        item.appendChild(shortcutSpan);
        // Hover styles are handled by CSS for better theme support
        tabContextMenu.appendChild(item);
    };

    const focusSibling = (el, dir) => {
        const items = Array.from(tabContextMenu.querySelectorAll('[role="menuitem"]'));
        const idx = items.indexOf(el);
        if (idx === -1) return;
        let n = idx + dir;
        if (n < 0) n = items.length - 1;
        if (n >= items.length) n = 0;
        items[n].focus();
    };
    const focusFirst = () => { const i = tabContextMenu.querySelector('[role="menuitem"]'); if (i) i.focus(); };
    const focusLast = () => { const items = tabContextMenu.querySelectorAll('[role="menuitem"]'); if (items.length) items[items.length - 1].focus(); };

    addItem('close', 'Close', () => closeDynamicConceptTab(conceptId), 'Alt+W');
    addItem('close-others', 'Close Others', () => closeOtherTabs(conceptId));
    addItem('close-right', 'Close Tabs to Right', () => closeTabsToRight(conceptId));
    addItem('close-all', 'Close All', () => closeAllDynamicConceptTabs());

    // Add reload menu item before bulk close operations
    addItem('reload', '🔄 Reload Concept', () => {
        hideTabContextMenu();
        reloadConceptTab(conceptId);
    });

    // Ontology-aware bulk closing (only for type tabs; any non-individual is treated as type)
    const tabInfo = dynamicConceptTabs.get(conceptId);
    if (tabInfo && tabInfo.kind !== 'individual') {
        addItem('close-subtypes', 'Close Subtype Tabs', () => {
            (async () => {
                try {
                    const subtypeIds = await getOpenSubtypeTypeIds(conceptId);
                    subtypeIds.forEach(id => closeDynamicConceptTab(id));
                } catch (e) {
                    console.warn('[dynamicTabs] close-subtypes failed', e);
                }
            })();
        });
        addItem('close-instances', 'Close Instance Tabs', () => {
            (async () => {
                try {
                    const instanceIds = await getOpenInstanceIdsForType(conceptId, true); // recursive include descendant types
                    instanceIds.forEach(id => closeDynamicConceptTab(id));
                } catch (e) {
                    console.warn('[dynamicTabs] close-instances failed', e);
                }
            })();
        });
    } else if (tabInfo && tabInfo.kind === 'individual') {
        // Bulk close counterpart for instance tabs
        addItem('close-all-instances', 'Close All Instance Tabs', () => {
            try {
                const ids = Array.from(dynamicConceptTabs.values())
                    .filter(t => t.kind === 'individual')
                    .map(t => t.conceptId);
                ids.forEach(id => closeDynamicConceptTab(id));
            } catch (e) {
                console.warn('[dynamicTabs] close-all-instances failed', e);
            }
        });
    }
}

/**
 * Initializes a dynamic concept tab with unique element IDs
 * @param {string} conceptId - The concept ID
 * @param {string} uniqueIdSuffix - The unique suffix for element IDs
 */
async function initializeDynamicConceptTab(conceptId, uniqueIdSuffix) {
    console.log(`[dynamicTabs] Initializing dynamic concept tab for: ${conceptId} with suffix: ${uniqueIdSuffix}`);

    try {
        // Import concept tab functions
        const {
            initializeConceptTab,
            updateConceptTabUI,
            fetchConceptList,
            initializeConceptTabDomElementsWithSuffix,
            fetchConceptListWithSuffix,
            setupConceptTabEventListenersWithSuffix,
            fetchSubtypesWithSuffix,
            fetchInstancesWithSuffix
        } = await import('./conceptTab.js');

        // Get the unique refresh button for this tab
        const refreshButton = document.getElementById(`refreshConceptListButton_${uniqueIdSuffix}`);
        if (refreshButton) {
            // Remove any existing event listeners
            const newRefreshButton = refreshButton.cloneNode(true);
            refreshButton.parentNode.replaceChild(newRefreshButton, refreshButton);

            // Add click event listener for refresh
            newRefreshButton.addEventListener('click', () => {
                console.log(`[dynamicTabs] Refresh button clicked for concept: ${conceptId}`);
                const kind = (dynamicConceptTabs.get(conceptId) || {}).kind;
                // Only update the global current type for Type tabs
                if (kind !== 'individual') {
                    setCurrentConceptType(conceptId);
                }
                // Unified list in Instances section
                fetchConceptListWithSuffix(conceptId, uniqueIdSuffix);
                // If this is a Type tab, refresh subtypes too
                if (kind !== 'individual') {
                    fetchSubtypesWithSuffix(conceptId, uniqueIdSuffix);
                }
            });

            console.log(`[dynamicTabs] Refresh button connected for ${conceptId}`);
        } else {
            console.warn(`[dynamicTabs] Refresh button not found for ${conceptId} (ID: refreshConceptListButton_${uniqueIdSuffix})`);
        }

        // Get the unique concept refresh button (header) for this tab
        const refreshConceptButton = document.getElementById(`refreshConceptButton_${uniqueIdSuffix}`);
        if (refreshConceptButton) {
            const newBtn = refreshConceptButton.cloneNode(true);
            refreshConceptButton.parentNode.replaceChild(newBtn, refreshConceptButton);
            newBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                reloadConceptTab(conceptId);
            });
            // Inject icon if needed (though upgradeActionButtonIcons should handle it)
            injectIconContent(newBtn, 'refresh', 'Refresh concept data');
        }

        // Initialize the concept tab with unique element IDs
        initializeConceptTabDomElementsWithSuffix(uniqueIdSuffix);
        setupConceptTabEventListenersWithSuffix(uniqueIdSuffix);
        updateConceptTabUI();
        // Only populate the unified Instances list for Type tabs
        {
            const kind = (dynamicConceptTabs.get(conceptId) || {}).kind;
            if (kind !== 'individual') {
                fetchConceptListWithSuffix(conceptId, uniqueIdSuffix);
            }
        }

        let tabKind = (dynamicConceptTabs.get(conceptId) || {}).kind;

        // Toggle type-only sections visibility by tab kind
        try {
            tabKind = (dynamicConceptTabs.get(conceptId) || {}).kind;
            const typeSections = document.getElementById(`typeSections_${uniqueIdSuffix}`);
            // Helper to force-hide a section robustly
            const hideEl = (el) => {
                if (!el) return;
                try { el.classList.add('hidden'); } catch (_) { }
                el.setAttribute('aria-hidden', 'true');
                try { el.hidden = true; } catch (_) { }
            };
            const showEl = (el) => {
                if (!el) return;
                try { el.classList.remove('hidden'); } catch (_) { }
                el.removeAttribute('aria-hidden');
                try { el.hidden = false; } catch (_) { }
            };
            if (typeSections) {
                if (tabKind === 'individual') {
                    // For individual tabs, only Names are visible. Remove type-only sections to avoid CSS overrides later.
                    showEl(typeSections);
                    const toRemove = [
                        document.getElementById(`typeDescriptionSection_${uniqueIdSuffix}`),
                        document.getElementById(`subtypesSection_${uniqueIdSuffix}`),
                        document.getElementById(`instancesSection_${uniqueIdSuffix}`)
                    ];
                    for (const el of toRemove) {
                        if (el && el.parentNode) {
                            try { el.parentNode.removeChild(el); } catch (_) { hideEl(el); }
                        }
                    }
                    // Keep Names visible
                    showEl(document.getElementById(`namesSection_${uniqueIdSuffix}`));
                } else {
                    // Type tab: show container and populate subsections
                    showEl(typeSections);
                    fetchSubtypesWithSuffix(conceptId, uniqueIdSuffix);
                    // Ensure description section exists (creates + populates) before adapting UI
                    await ensureUnifiedDescriptionSection(conceptId, uniqueIdSuffix);
                    await adaptTypeConceptTabUI(conceptId, uniqueIdSuffix);
                    await ensureDescriptionActionsIntegrity(conceptId, uniqueIdSuffix);
                }
            }

            // For individuals: adapt UI to show summary/description and hide creation/list sections
            if (tabKind === 'individual') {
                await adaptIndividualConceptTabUI(conceptId, uniqueIdSuffix);
            }
        } catch (e) {
            console.warn('[dynamicTabs] Unable to toggle type sections for tab', conceptId, e);
        }

        let namesOutcome = null;
        try {
            await initializeNamesForm(uniqueIdSuffix);
            namesOutcome = await loadConceptNames(conceptId, uniqueIdSuffix);
            // Load attributes (text relations excluding names)
            await loadConceptAttributes(conceptId, uniqueIdSuffix);
        } catch (e) {
            console.warn('[dynamicTabs] Failed to initialize names form for tab', conceptId, e);
        }

        if (namesOutcome && namesOutcome.status === 'not_found') {
            console.warn(`[dynamicTabs] Concept ${conceptId} is unavailable; skipping further initialisation.`);
            return;
        }

        try {
            await initializeRelationshipsUI(conceptId, uniqueIdSuffix, tabKind);
        } catch (e) {
            console.warn('[dynamicTabs] Failed to initialise relationships UI for tab', conceptId, e);
        }

        console.log(`[dynamicTabs] Dynamic concept tab initialization complete for ${conceptId}`);

        // Initialize predicate view if this concept is a predicate
        try {
            // Fetch concept data to check if it's a predicate
            const nodeResp = await fetch(`/vontology/api/vontology/node_content?identifier=${encodeURIComponent(conceptId)}`);
            if (nodeResp.ok) {
                const conceptData = await nodeResp.json();
                if (conceptData.raw_doc) {
                    await initializePredicateView(conceptId, uniqueIdSuffix, conceptData.raw_doc);
                }
            }
        } catch (predicateErr) {
            console.warn('[dynamicTabs] Failed to initialize predicate view', predicateErr);
        }

        // Post-init verification: ensure description section actually materialized for type tabs
        try {
            const kind = (dynamicConceptTabs.get(conceptId) || {}).kind;
            if (kind !== 'individual') {
                const maybeSection = document.getElementById(`typeDescriptionSection_${uniqueIdSuffix}`);
                const hasEditBtn = document.getElementById(`typeEditDescriptionButton_${uniqueIdSuffix}`);
                if (!maybeSection || !hasEditBtn) {
                    console.warn('[dynamicTabs] Description section missing after init; triggering recovery', { conceptId, uniqueIdSuffix, hasSection: !!maybeSection, hasEditBtn: !!hasEditBtn });
                    // Attempt a single deferred recovery pass
                    setTimeout(() => { try { ensureDescriptionActionsIntegrity(conceptId, uniqueIdSuffix, { silent: true }); } catch (_) { } }, 140);
                }
                // Final integrity verification a bit later to catch slow DOM insertions
                setTimeout(() => { try { ensureDescriptionActionsIntegrity(conceptId, uniqueIdSuffix, { silent: true }); } catch (_) { } }, 420);
            }
        } catch (verifyErr) { console.warn('[dynamicTabs] Post-init description verification failed', verifyErr); }

    } catch (error) {
        console.error(`[dynamicTabs] Error initializing dynamic concept tab for ${conceptId}:`, error);
    }
}

/**
 * Initialize dynamic tabs functionality
 */
export function initializeDynamicTabs() {
    console.log('[dynamicTabs] Initializing dynamic tabs functionality');

    // Clear any existing dynamic tabs on initialization
    closeAllDynamicConceptTabs();

    // Listen for global requests to open concept tabs (avoids circular imports)
    // detail: { conceptId: string, conceptName: string, kind?: 'type'|'individual', activate?: boolean }
    document.addEventListener('open-concept-tab', (evt) => {
        try {
            const detail = evt.detail || {};
            const { conceptId, conceptName, kind, activate, newlyCreated, delayedActivateMs } = detail;
            if (!conceptId || !conceptName) {
                console.warn('[dynamicTabs] open-concept-tab missing conceptId or conceptName');
                return;
            }
            const conceptIdStr = String(conceptId);
            if (!conceptIdStr.startsWith('#V#')) {
                // Older datasets sometimes surface legacy IDs (e.g. GUIDs). Warn but allow them so the user can inspect/delete.
                console.warn('[dynamicTabs] open-concept-tab conceptId does not use canonical #V# prefix:', conceptIdStr);
            }
            // Create or activate without immediate activation if activate is false
            const tabId = createOrActivateConceptTab(conceptIdStr, conceptName, activate !== false, { kind, newlyCreated });

            // If caller requested delayed activation, schedule it here. This keeps the initial open
            // non-activating so the Vontology tree can finish highlighting/scrolling, but will
            // activate the tab after the short delay if the user hasn't already activated it.
            if (delayedActivateMs && Number(delayedActivateMs) > 0) {
                try {
                    const activateLater = () => {
                        // Only activate if the tab still exists and wasn't activated by the user
                        const button = document.querySelector(`.tab-button[data-tab="${tabId}"]`);
                        const isAlreadyActive = button && button.classList.contains('active');
                        if (!isAlreadyActive) {
                            // Re-dispatch a safe activation request
                            try {
                                const evt2 = new CustomEvent('open-concept-tab', { detail: { conceptId: conceptIdStr, conceptName, kind, activate: true } });
                                document.dispatchEvent(evt2);
                            } catch (e2) {
                                // Fallback: directly call activateTab if available
                                try { activateTab(tabId); } catch (_) { /* no-op */ }
                            }
                        }
                    };
                    setTimeout(activateLater, Number(delayedActivateMs));
                } catch (schedErr) {
                    console.warn('[dynamicTabs] Failed to schedule delayed activation', schedErr);
                }
            }
        } catch (e) {
            console.error('[dynamicTabs] Failed handling open-concept-tab event:', e);
        }
    });

    // Listen for requests to open annotation tabs for descriptions or notes
    document.addEventListener('open-annotation-tab', (evt) => {
        try {
            const detail = evt.detail || {};
            const { text, conceptName, source } = detail;
            createAnnotationTab(text || '', conceptName || 'Annotation', source);
        } catch (e) {
            console.error('[dynamicTabs] Failed handling open-annotation-tab event:', e);
        }
    });

    // Global delegation for note annotate buttons (in case buttons injected without direct listeners)
    // Track recent note annotation launches to avoid burst duplicates in tests/dom reflows
    const recentNoteLaunches = new Map(); // key: text|conceptName -> timestamp
    document.addEventListener('click', (e) => {
        try {
            const target = e.target;
            if (!target) return;
            if (target.classList && target.classList.contains('note-annotate-btn')) {
                // Avoid double handling if a direct listener already marked the button
                if (target.dataset.hasDirectAnnotate === '1') return; // direct listener exists
                if (target.dataset.annotationDelegated === '1') return; // already handled once
                const noteItem = target.closest('.note-item');
                const full = noteItem?.getAttribute('data-full') || noteItem?.querySelector('.note-view')?.getAttribute('data-full') || noteItem?.querySelector('.note-view')?.textContent || '';
                // Attempt to derive concept name from an open dynamic concept tab (simplest: any active tab with data-concept-name attr if available)
                let conceptName = 'Concept';
                try {
                    const activeConceptButton = document.querySelector('.tab-button.active[data-concept-name]');
                    if (activeConceptButton) conceptName = activeConceptButton.getAttribute('data-concept-name');
                } catch (_) { /* no-op */ }
                const textVal = (full || '').trim();
                const key = `${conceptName}__${textVal}`;
                const now = Date.now();
                const last = recentNoteLaunches.get(key) || 0;
                if (now - last < 50) {
                    return; // suppress duplicate within 50ms window
                }
                recentNoteLaunches.set(key, now);
                target.dataset.annotationDelegated = '1';
                document.dispatchEvent(new CustomEvent('open-annotation-tab', { detail: { text: textVal, conceptName, source: 'note' } }));
                setTimeout(() => { try { delete target.dataset.annotationDelegated; } catch (_) { } }, 500);
            }
        } catch (delegErr) { console.warn('[dynamicTabs] note annotate delegation error', delegErr); }
    }, true);

    // Delegated handler for description annotate buttons (covers tests/legacy where populateTypeDescription not yet wired)
    document.addEventListener('click', (e) => {
        try {
            const t = e.target;
            if (!t || !t.id || !/^typeAnnotateDescription_/.test(t.id)) return;
            // If a direct listener already exists (populateTypeDescription wired), skip
            if (t.dataset.descAnnotateDirect === '1') return;
            const suffix = t.id.replace('typeAnnotateDescription_', '');
            const display = document.getElementById(`typeDescriptionDisplay_${suffix}`);
            if (!display) return;
            // JVNAUTOSCI-571: use paragraph-preserving extraction for annotation input
            const text = extractDescriptionPlainText(display);
            // Best effort concept name: derive from any active concept tab label or fallback to 'Concept'
            let conceptName = 'Concept';
            try {
                const activeConceptBtn = document.querySelector('.tab-button.active[data-concept-id] span');
                if (activeConceptBtn) conceptName = activeConceptBtn.textContent.trim();
            } catch (_) { /* ignore */ }
            document.dispatchEvent(new CustomEvent('open-annotation-tab', { detail: { text, conceptName, source: 'description' } }));
        } catch (err) { console.warn('[dynamicTabs] delegated description annotate error', err); }
    }, true);

    // Close any open dynamic tab when its concept is deleted elsewhere
    document.addEventListener('concept-deleted', (evt) => {
        try {
            const detail = evt.detail || {}; const cid = detail.conceptId;
            if (cid && dynamicConceptTabs.has(cid)) {
                console.log('[dynamicTabs] concept-deleted received, closing tab', cid);
                closeDynamicConceptTab(cid);
            }
            // Best-effort: trigger tree refresh if available (lazy import to avoid cycle)
            try {
                if (window.fetchAndRenderVontologyTree) {
                    window.fetchAndRenderVontologyTree();
                } else if (typeof document !== 'undefined') {
                    // Attempt dynamic import of module exposing fetchAndRenderVontologyTree if previously exported
                    import('./vontology.js').then(m => {
                        if (m.fetchAndRenderVontologyTree) m.fetchAndRenderVontologyTree();
                    }).catch(() => { });
                }
            } catch (_) { /* no-op */ }
        } catch (e) {
            console.warn('[dynamicTabs] concept-deleted handler failed', e);
        }
    });

    // When instances are updated for a type, refresh any matching Type tabs' unified Instances lists
    document.addEventListener('von:instancesUpdated', async (evt) => {
        try {
            const detail = evt.detail || {};
            const parentTypeId = detail.parentTypeId;
            if (!parentTypeId) return;
            // Find matching dynamic tab info by conceptId
            const tabInfo = dynamicConceptTabs.get(parentTypeId);
            if (!tabInfo) return; // No open tab for this type
            if (tabInfo.kind === 'individual') return; // Only refresh for Type tabs
            const suffix = parentTypeId.replace(/[^a-zA-Z0-9]/g, '_');
            try {
                const { fetchConceptListWithSuffix } = await import('./conceptTab.js');
                fetchConceptListWithSuffix(parentTypeId, suffix);
            } catch (e) {
                console.warn('[dynamicTabs] Failed to refresh unified Instances after update', e);
            }
        } catch (e) {
            console.warn('[dynamicTabs] instancesUpdated handler failed', e);
        }
    });

    // Runtime language change: allow external code to trigger tab relabeling
    try { window.addEventListener('von:language-change', () => { relabelAllDynamicConceptTabs(true); }); } catch (_) { }

    // Names changed event from conceptTab.js -> force relabel specific tabs
    try {
        document.addEventListener('concept-names-changed', (e) => {
            const cid = e?.detail?.conceptId;
            if (!cid) return;
            const info = dynamicConceptTabs.get(cid);
            if (info) {
                // Invalidate cached names so newly added shorter names are considered
                try { namesCache.delete(cid); } catch (_) { }
                try { inFlightFetches.delete(cid); } catch (_) { }
                updateTabLabelWithShortestName(cid, info.button, true);
            }
        });
    } catch (_) { }

    console.log('[dynamicTabs] Dynamic tabs initialization complete');
}

// --- MutationObserver fallback for name changes (in case custom events fail) ---
// Observes the names section DOM inside a concept tab; on mutations, schedules a relabel attempt.
function attachNamesObserver(conceptId, suffix, attempt = 0) {
    try {
        if (typeof MutationObserver === 'undefined') return; // environment lacks support
        const info = dynamicConceptTabs.get(conceptId);
        if (!info) return;
        if (info.namesObserver) return; // already attached
        const sectionId = `namesSection_${suffix}`;
        const sectionEl = document.getElementById(sectionId) || info.content.querySelector(`#${sectionId}`);
        if (!sectionEl) {
            if (attempt < 5) {
                setTimeout(() => attachNamesObserver(conceptId, suffix, attempt + 1), 250 * (attempt + 1));
            }
            return;
        }
        let pending = null;
        const debounce = () => {
            if (pending) clearTimeout(pending);
            pending = setTimeout(() => {
                try {
                    const btn = info.button;
                    if (btn) updateTabLabelWithShortestName(conceptId, btn, true);
                } catch (_) { /* ignore */ }
            }, 120); // small debounce to batch rapid changes
        };
        const mo = new MutationObserver((mutations) => {
            // Only react to substantive changes (child list / character data)
            for (const m of mutations) {
                if (m.type === 'childList' || m.type === 'characterData' || m.type === 'subtree') {
                    debounce();
                    break;
                }
            }
        });
        mo.observe(sectionEl, { subtree: true, childList: true, characterData: true });
        info.namesObserver = mo;
        // Store back updated info (Map values are objects; mutation already visible but assign defensively)
        dynamicConceptTabs.set(conceptId, info);
        console.log(`[dynamicTabs] Names MutationObserver attached for ${conceptId}`);
    } catch (e) {
        console.warn('[dynamicTabs] Failed to attach names MutationObserver', e);
    }
}

// Helper: tailor an Individual concept tab presentation
async function adaptIndividualConceptTabUI(conceptId, suffix) {
    try {
        const encodedId = encodeURIComponent(conceptId);
        // Fetch parents (types) and node content for description
        const [parentsRes, nodeRes, conceptRes] = await Promise.all([
            fetch(`/vontology/api/vontology/parents?identifier=${encodedId}`),
            fetch(`/vontology/api/vontology/node_content?identifier=${encodedId}`),
            fetch(`/api/concepts/${encodedId}`)
        ]);
        const parentsData = parentsRes.ok ? await parentsRes.json() : { parents: [] };
        const nodeData = nodeRes.ok ? await nodeRes.json() : {};
        const conceptData = conceptRes && conceptRes.ok ? await conceptRes.json() : null;

        // Sync concept selection state and original notes using the shared helper
        try {
            const { selectConceptWithSuffix } = await import('./conceptTab.js');
            const conceptObj = {
                id: conceptId,
                _id: conceptData?.id || conceptData?._id,
                concept_id: conceptId,
                name: conceptData?.name || nodeData?.name || conceptId,
                notes: conceptData?.notes || conceptData?.concept_data?.notes || conceptData?.concept_data?.preserved_fields?.notes || ''
            };
            selectConceptWithSuffix(conceptObj, suffix);
        } catch (selErr) {
            console.warn('[dynamicTabs] Unable to pre-select concept in individual view', selErr);
        }

        // ID chip is handled at header attach time; no extra ID text here

        // Replace the Step 1 title text and disable name input
        const titleText = document.getElementById(`conceptFormTitleText_${suffix}`) || document.getElementById('conceptFormTitleText');
        if (titleText) titleText.textContent = 'Concept Details';
        const nameInput = document.getElementById(`conceptName_${suffix}`) || document.getElementById('conceptName');
        if (nameInput) {
            nameInput.disabled = true;
            nameInput.title = 'Name editing is disabled for individual view';
            // Populate with concept name if available
            const conceptName = nodeData?.name || conceptData?.name;
            if (conceptName) nameInput.value = conceptName;
        }

        // Insert a small types summary line under the title
        const step1 = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (step1 && !step1.querySelector('.concept-types-summary')) {
            const types = Array.isArray(parentsData.parents) ? parentsData.parents.map(p => p.name).filter(Boolean) : [];
            const summary = document.createElement('div');
            summary.className = 'concept-types-summary';
            summary.style.margin = '6px 0 10px 0';
            summary.style.color = '#374151';
            summary.style.fontSize = '0.95rem';
            summary.textContent = types.length ? `Instance of: ${types.join(', ')}` : 'No parent type recorded.';
            step1.insertBefore(summary, step1.querySelector('label'));
        }

        // Ensure unified description UI (reuse type description section & logic for individuals)
        await ensureUnifiedDescriptionSection(conceptId, suffix);
        // Multi-note section
        await populateNotesSection(conceptId, suffix);
        // Multi-content section
        await populateContentSection(conceptId, suffix);

        // If pure instance (no type-of parents), hide the concept list entirely
        const isPureInstance = !Array.isArray(parentsData.parents) || parentsData.parents.length === 0;
        if (isPureInstance) {
            const listContainer = document.getElementById(`conceptList_${suffix}`) || document.getElementById('conceptList');
            if (listContainer) listContainer.style.display = 'none';
        }

        // Pre-arm interaction by selecting this concept in state and populating notes
        try {
            setCurrentlySelectedConceptId(conceptId);
            if (conceptData) {
                // Notes now handled exclusively via multi-note relations UI; ensure legacy textarea not populated.
                const name = conceptData.display_name || conceptData.name || nodeData?.display_name || nodeData?.name || conceptId;
                setSelectedConceptOriginalName(name);
            }
            // Ensure title remains neutral for individual view
            const titleText = document.getElementById(`conceptFormTitleText_${suffix}`) || document.getElementById('conceptFormTitleText');
            if (titleText) titleText.textContent = 'Concept Details';
        } catch (stateErr) {
            console.warn('[dynamicTabs] Failed to set selected concept state for individual view', stateErr);
        }

    } catch (e) {
        console.warn('[dynamicTabs] adaptIndividualConceptTabUI failed', e);
    }
}

// Unified description: ensure the typeDescriptionSection exists under Step1 for any concept
// Export for testing: dual-write description updater (JVNAUTOSCI-570)
export async function updateConceptDescription(conceptId, description) {
    try {
        if (!conceptId || typeof description !== 'string') return false;
        const encodedId = encodeURIComponent(conceptId);
        const resp = await fetch(`/api/concepts/${encodedId}/description`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ description })
        });
        if (resp.ok) {
            const data = await resp.json().catch(() => ({}));
            return !data.error;
        }
        // Fallback: legacy endpoint (will eventually be removed)
        if (resp.status === 404 || resp.status === 405) {
            const legacy = await fetch('/vontology/api/vontology/update_description', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ identifier: conceptId, description })
            });
            const legacyData = await legacy.json().catch(() => ({}));
            return legacy.ok && legacyData && legacyData.success;
        }
        return false;
    } catch (e) {
        return false;
    }
}

async function ensureUnifiedDescriptionSection(conceptId, suffix) {
    try {
        let step1 = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (!step1) {
            // Wait for the scaffold to appear before proceeding (avoids resolving early)
            let attempts = 0;
            while (!step1 && attempts < 6) {
                attempts += 1;
                if (attempts === 1) {
                    try { console.debug('[dynamicTabs] ensureUnifiedDescriptionSection: step1 container not yet present, waiting', { conceptId, suffix }); } catch (_) { }
                }
                await new Promise(r => setTimeout(r, 60 * attempts));
                step1 = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
            }
            if (!step1) {
                console.warn('[dynamicTabs] ensureUnifiedDescriptionSection: giving up waiting for step1 container', { conceptId, suffix });
                return;
            }
        }
        // If a per-concept cloned section already exists, refresh contents only
        let descSection = document.getElementById(`typeDescriptionSection_${suffix}`);

        // 1. Detect legacy unsuffixed section (#typeDescriptionSection) and retrofit it in place
        if (!descSection) {
            const legacy = document.getElementById('typeDescriptionSection');
            if (legacy) {
                descSection = legacy;
                // Rename internal IDs so downstream lookups succeed
                try { renameLegacyDescriptionIds(descSection, suffix); } catch (e) { console.warn('[dynamicTabs] legacy desc id rename failed', e); }
                try { upgradeLegacyDescriptionActions(descSection, suffix); } catch (e) { console.warn('[dynamicTabs] legacy desc action upgrade failed', e); }
                if (!window.__legacyDescUpgradeLogged) {
                    try { console.info('[dynamicTabs] Legacy description section upgraded (id renames + new action buttons)', { conceptId, suffix }); } catch (_) { }
                    window.__legacyDescUpgradeLogged = true;
                } else { try { console.debug('[dynamicTabs] Legacy description section re-used for new tab', { conceptId, suffix }); } catch (_) { } }
            }
        }

        // 2. Heuristic: if still no section but we see a legacy rectangular "Edit Description" button somewhere, build a new modern section
        if (!descSection) {
            const legacyEditBtn = Array.from(step1.querySelectorAll('button'))
                .find(b => /edit description/i.test(b.textContent || ''));
            if (legacyEditBtn) {
                descSection = document.createElement('div');
                descSection.id = `typeDescriptionSection_${suffix}`;
                descSection.className = 'concept-list-subsection';
                const existingDisplay = step1.querySelector('.concept-type-description') || step1.querySelector('#typeDescriptionDisplay');
                const initialHtml = existingDisplay ? existingDisplay.innerHTML : '<i>Loading description...</i>';
                descSection.innerHTML = `
                <h3 class="concept-list-title">Description</h3>
                <div class="type-description-wrapper" style="position:relative;">
                  <div id="typeDescriptionDisplay_${suffix}" class="concept-type-description editable-description" tabindex="0">${initialHtml}</div>
                  <textarea id="typeDescriptionTextarea_${suffix}" class="description-editor hidden" placeholder="Enter description..." rows="6"></textarea>
                  <div class="desc-actions text-block-actions">
                    <button id="typeCopyDescription_${suffix}" class="round-icon-button" title="Copy description to clipboard" aria-label="Copy description to clipboard" data-icon="copy" data-keep-title="true"></button>
                    <button id="typeAnnotateDescription_${suffix}" class="round-icon-button" title="Annotate description" aria-label="Annotate description" data-icon="annotate" data-keep-title="true"></button>
                    <button id="typeEditDescriptionButton_${suffix}" class="round-icon-button" title="Edit description" aria-label="Edit description" data-icon="edit" data-keep-title="true"></button>
                    <button id="typeDeleteDescriptionButton_${suffix}" class="round-icon-button" title="Delete description" aria-label="Delete description" data-icon="delete" data-keep-title="true"></button>
                  </div>
                  <div id="typeDescriptionEditActions_${suffix}" class="description-edit-actions hidden">
                    <button id="typeEditDescriptionSave_${suffix}" class="small-btn">Save</button>
                    <button id="typeEditDescriptionCancel_${suffix}" class="small-btn">Cancel</button>
                  </div>
                </div>
                <span id="typeDescriptionStatus_${suffix}" class="concept-status ml-2"></span>`;
                // Insert near where legacy button was
                legacyEditBtn.parentNode.insertBefore(descSection, legacyEditBtn);
                try { legacyEditBtn.remove(); } catch (_) { }
                if (!window.__legacyDescUpgradeLogged) {
                    try { console.info('[dynamicTabs] Heuristic legacy description reconstruction applied (no original section present)', { conceptId, suffix }); } catch (_) { }
                    window.__legacyDescUpgradeLogged = true;
                } else { try { console.debug('[dynamicTabs] Heuristic reconstruction path used again', { conceptId, suffix }); } catch (_) { } }
            }
        }

        // 3. If still no section, create fresh modern one
        if (!descSection) {
            descSection = document.createElement('div');
            descSection.id = `typeDescriptionSection_${suffix}`;
            descSection.className = 'concept-list-subsection';
            descSection.innerHTML = `
                <h3 class="concept-list-title">Description</h3>
                <div class="type-description-wrapper" style="position:relative;">
                  <div id="typeDescriptionDisplay_${suffix}" class="concept-type-description editable-description" tabindex="0"><i>Loading description...</i></div>
                  <textarea id="typeDescriptionTextarea_${suffix}" class="description-editor hidden" placeholder="Enter description..." rows="6"></textarea>
                  <div class="desc-actions text-block-actions">
                    <button id="typeCopyDescription_${suffix}" class="round-icon-button" title="Copy description to clipboard" aria-label="Copy description to clipboard" data-icon="copy" data-keep-title="true"></button>
                    <button id="typeAnnotateDescription_${suffix}" class="round-icon-button" title="Annotate description" aria-label="Annotate description" data-icon="annotate" data-keep-title="true"></button>
                    <button id="typeEditDescriptionButton_${suffix}" class="round-icon-button" title="Edit description" aria-label="Edit description" data-icon="edit" data-keep-title="true"></button>
                    <button id="typeDeleteDescriptionButton_${suffix}" class="round-icon-button" title="Delete description" aria-label="Delete description" data-icon="delete" data-keep-title="true"></button>
                  </div>
                  <div id="typeDescriptionEditActions_${suffix}" class="description-edit-actions hidden">
                    <button id="typeEditDescriptionSave_${suffix}" class="small-btn">Save</button>
                    <button id="typeEditDescriptionCancel_${suffix}" class="small-btn">Cancel</button>
                  </div>
                </div>
                <span id="typeDescriptionStatus_${suffix}" class="concept-status ml-2"></span>`;
            const notesAnchor = document.getElementById(`notesMultiSection_${suffix}`);
            if (notesAnchor && notesAnchor.parentNode) {
                notesAnchor.parentNode.insertBefore(descSection, notesAnchor);
            } else {
                const titleEl = step1.querySelector('#conceptFormTitleText')?.parentElement?.parentElement || step1.firstChild;
                if (titleEl && titleEl.nextSibling) step1.insertBefore(descSection, titleEl.nextSibling); else step1.insertBefore(descSection, step1.firstChild);
            }
            try { console.info('[dynamicTabs] Created fresh description section', { conceptId, suffix }); } catch (_) { }
        } else {
            // If we have an existing (possibly legacy) section ensure action buttons are upgraded
            try { upgradeLegacyDescriptionActions(descSection, suffix); } catch (e) { console.warn('[dynamicTabs] desc upgrade failed', e); }
            try { console.debug('[dynamicTabs] Reusing existing description section (upgrade attempted)', { conceptId, suffix }); } catch (_) { }
        }

        // Ensure glyphs/text for buttons (in case stripped by CSS or fonts)

        // Inject SVG icons into any newly created or upgraded action buttons
        try { upgradeActionButtonIcons(descSection || step1); } catch (_) { }
        // Populate description then emit a deterministic readiness event for tests & other listeners
        await populateTypeDescription(conceptId, suffix);
        try {
            const evt = new CustomEvent('description-populated', { detail: { conceptId, suffix } });
            document.dispatchEvent(evt);
            // Promise gate (one-shot) to allow tests to await without polling
            if (!window.__descriptionReadyResolvers) window.__descriptionReadyResolvers = new Map();
            const key = conceptId + '::' + suffix;
            const pending = window.__descriptionReadyResolvers.get(key);
            if (pending && Array.isArray(pending)) {
                pending.forEach(r => { try { r(); } catch (_) { } });
                window.__descriptionReadyResolvers.delete(key);
            }
        } catch (_) { /* ignore event errors */ }
        try {
            const section = document.getElementById(`typeDescriptionSection_${suffix}`);
            if (section) {
                section.querySelectorAll('.type-description-wrapper').forEach(wrapper => {
                    const actions = wrapper.querySelector('.desc-actions');
                    const display = wrapper.querySelector('.concept-type-description');
                    if (actions && display) setupAdaptiveActionGroup(actions, display);
                });
                try { console.debug('[dynamicTabs] Description section populated', { conceptId, suffix }); } catch (_) { }
            }
        } catch (_) { }
    } catch (e) {
        console.warn('[dynamicTabs] ensureUnifiedDescriptionSection failed', e);
    }
}

// Integrity repair: ensure description section + action buttons exist (race recovery)
async function ensureDescriptionActionsIntegrity(conceptId, suffix, opts = {}) {
    try {
        const sectionId = `typeDescriptionSection_${suffix}`;
        let section = document.getElementById(sectionId);
        const step1 = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (!step1) return false;
        if (!section) {
            // Try unsuffixed ID (for newly moved static template sections)
            section = document.getElementById('typeDescriptionSection');
        }
        if (!section) {
            // Attempt full reconstruction using existing helper
            await ensureUnifiedDescriptionSection(conceptId, suffix);
            section = document.getElementById(sectionId);
        }
        if (!section) return false;

        // Always rename any legacy unsuffixed IDs to suffixed ones (critical for type tabs with moved static template buttons)
        try { renameLegacyDescriptionIds(section, suffix); } catch (_) { }

        // Broaden integrity requirements to include editor controls; if any are missing create them.
        const requiredIds = [
            `typeDescriptionDisplay_${suffix}`,
            `typeCopyDescription_${suffix}`,
            `typeAnnotateDescription_${suffix}`,
            `typeEditDescriptionButton_${suffix}`,
            `typeDeleteDescriptionButton_${suffix}`,
            `typeDescriptionTextarea_${suffix}`,
            `typeDescriptionEditActions_${suffix}`,
            `typeEditDescriptionSave_${suffix}`,
            `typeEditDescriptionCancel_${suffix}`,
            `typeDescriptionStatus_${suffix}`
        ];
        let missing = requiredIds.filter(id => !document.getElementById(id));
        if (missing.length) {
            // First, rename any legacy unsuffixed IDs to suffixed ones
            try { renameLegacyDescriptionIds(section, suffix); } catch (_) { }
            // Re-run upgrade path explicitly
            try { upgradeLegacyDescriptionActions(section, suffix); } catch (_) { }
            // Fresh inject for any still-missing action buttons
            const actions = section.querySelector('.desc-actions') || (() => {
                const a = document.createElement('div'); a.className = 'desc-actions text-block-actions'; section.appendChild(a); return a;
            })();
            const ensureBtn = (id, icon, label) => {
                let btn = document.getElementById(id);
                if (!btn) { btn = document.createElement('button'); btn.id = id; actions.appendChild(btn); }
                btn.classList.add('round-icon-button');
                btn.dataset.icon = icon; btn.title = label; btn.setAttribute('aria-label', label);
            };
            if (!document.getElementById(`typeCopyDescription_${suffix}`)) ensureBtn(`typeCopyDescription_${suffix}`, 'copy', 'Copy description to clipboard');
            if (!document.getElementById(`typeAnnotateDescription_${suffix}`)) ensureBtn(`typeAnnotateDescription_${suffix}`, 'annotate', 'Annotate description');
            if (!document.getElementById(`typeEditDescriptionButton_${suffix}`)) ensureBtn(`typeEditDescriptionButton_${suffix}`, 'edit', 'Edit description');
            if (!document.getElementById(`typeDeleteDescriptionButton_${suffix}`)) ensureBtn(`typeDeleteDescriptionButton_${suffix}`, 'delete', 'Delete description');

            // Ensure display element exists (some legacy markup may only have raw text container)
            let display = document.getElementById(`typeDescriptionDisplay_${suffix}`);
            if (!display) {
                // Attempt to find a plausible paragraph/text container
                display = section.querySelector('.concept-type-description');
                if (!display) {
                    display = document.createElement('div');
                    display.id = `typeDescriptionDisplay_${suffix}`;
                    display.className = 'concept-type-description editable-description';
                    display.innerHTML = '<i>Loading description...</i>';
                    // Insert ahead of actions
                    section.insertBefore(display, actions);
                } else if (!display.id) {
                    display.id = `typeDescriptionDisplay_${suffix}`;
                    display.classList.add('editable-description');
                }
            }

            // Synthesize textarea/editor controls if missing
            let textarea = document.getElementById(`typeDescriptionTextarea_${suffix}`);
            if (!textarea) {
                textarea = document.createElement('textarea');
                textarea.id = `typeDescriptionTextarea_${suffix}`;
                textarea.className = 'description-editor hidden';
                textarea.rows = 6;
                // Place immediately after display for predictable layout
                display.parentNode.insertBefore(textarea, actions);
            }
            let editActions = document.getElementById(`typeDescriptionEditActions_${suffix}`);
            if (!editActions) {
                editActions = document.createElement('div');
                editActions.id = `typeDescriptionEditActions_${suffix}`;
                editActions.className = 'description-edit-actions hidden';
                editActions.innerHTML = `<button id="typeEditDescriptionSave_${suffix}" class="small-btn">Save</button>
<button id="typeEditDescriptionCancel_${suffix}" class="small-btn">Cancel</button>`;
                // Insert after textarea
                textarea.parentNode.insertBefore(editActions, actions.nextSibling);
            } else {
                // Ensure save/cancel present
                if (!document.getElementById(`typeEditDescriptionSave_${suffix}`)) {
                    const save = document.createElement('button');
                    save.id = `typeEditDescriptionSave_${suffix}`; save.className = 'small-btn'; save.textContent = 'Save';
                    editActions.appendChild(save);
                }
                if (!document.getElementById(`typeEditDescriptionCancel_${suffix}`)) {
                    const cancel = document.createElement('button');
                    cancel.id = `typeEditDescriptionCancel_${suffix}`; cancel.className = 'small-btn'; cancel.textContent = 'Cancel';
                    editActions.appendChild(cancel);
                }
            }
            // Ensure status element exists
            if (!document.getElementById(`typeDescriptionStatus_${suffix}`)) {
                const status = document.createElement('span');
                status.id = `typeDescriptionStatus_${suffix}`;
                status.className = 'concept-status ml-2';
                section.appendChild(status);
            }
            missing = requiredIds.filter(id => !document.getElementById(id));
        }
        // Inject icons (idempotent)
        try { upgradeActionButtonIcons(section); } catch (_) { }
        // Rebind / populate if either content not populated OR listeners not yet bound
        const displayEl = document.getElementById(`typeDescriptionDisplay_${suffix}`);
        const needPopulate = document.getElementById(`typeEditDescriptionButton_${suffix}`) && (
            !displayEl?.dataset?.populated || displayEl?.dataset?.descEvents !== '1'
        );
        if (needPopulate) {
            try { await populateTypeDescription(conceptId, suffix); } catch (_) { /* swallow */ }
        }
        if (missing.length === 0) {
            if (!opts.silent) {
                // Enhanced debug to verify button IDs and delegation pattern
                const annotateBtn = document.getElementById(`typeAnnotateDescription_${suffix}`);
                const debugInfo = {
                    conceptId,
                    suffix,
                    annotateButtonExists: !!annotateBtn,
                    annotateButtonId: annotateBtn?.id,
                    delegationPattern: annotateBtn?.id ? /^typeAnnotateDescription_/.test(annotateBtn.id) : false
                };
                console.debug('[dynamicTabs] Description actions integrity verified', debugInfo);
            }
            return true;
        }
        console.warn('[dynamicTabs] Description actions integrity unresolved', { conceptId, suffix, missing });
        return false;
    } catch (e) {
        console.warn('[dynamicTabs] ensureDescriptionActionsIntegrity failed', e);
        return false;
    }
}

// Export for tests
export { ensureDescriptionActionsIntegrity };

// Export for testing: adaptive action bar logic
export function setupAdaptiveActionGroup(actionsEl, contentEl, opts = {}) {
    if (!actionsEl || !contentEl) return;
    const alreadyInit = actionsEl.dataset.adaptiveInit === '1';
    if (!alreadyInit) {
        actionsEl.dataset.adaptiveInit = '1';
        actionsEl.classList.add('actions-edge-align'); // ensure flush left
    }

    const lineHeightPx = parseFloat(getComputedStyle(contentEl).lineHeight) || 20;
    // Support both thresholdLines (current) and legacy linesThreshold (tests may use either)
    const thresholdLines = (opts.thresholdLines || opts.linesThreshold || 3);
    const verticalClass = 'actions-vertical';
    const horizontalClass = 'actions-horizontal';

    const apply = () => {
        try {
            let h = contentEl.clientHeight;
            if (!h) {
                const styleH = parseFloat(contentEl.style.height);
                if (!isNaN(styleH) && styleH > 0) h = styleH;
            }
            const shouldHorizontal = h < lineHeightPx * thresholdLines + 4; // buffer for rounding
            actionsEl.classList.toggle(horizontalClass, shouldHorizontal);
            actionsEl.classList.toggle(verticalClass, !shouldHorizontal);
        } catch (_) { /* ignore measurement issues */ }
    };

    // If observe is explicitly false run synchronously (tests depend on immediate measurement)
    if (opts.observe === false) {
        apply();
    } else if (!alreadyInit) {
        // First-time async measure (layout stabilisation)
        requestAnimationFrame(apply);
    } else {
        // Re-application when reinvoked after height change
        apply();
    }

    // Attach observer only once unless explicitly disabled
    if (opts.observe !== false && !alreadyInit) {
        try {
            const ro = new ResizeObserver(() => apply());
            ro.observe(contentEl);
            actionsEl.__resizeObserver = ro;
        } catch (_) { /* ResizeObserver not available */ }
    }
}

// Upgrade legacy description markup to modern round icon buttons (id suffixed variant required)
function upgradeLegacyDescriptionActions(descSection, suffix) {
    if (!descSection) return;
    const display = descSection.querySelector(`#typeDescriptionDisplay_${suffix}`) || descSection.querySelector('.concept-type-description');
    if (!display) return;
    // Ensure wrapper
    let wrapper = display.closest('.type-description-wrapper');
    if (!wrapper) {
        wrapper = document.createElement('div');
        wrapper.className = 'type-description-wrapper';
        wrapper.style.position = 'relative';
        display.parentNode.insertBefore(wrapper, display);
        wrapper.appendChild(display);
    }
    // Actions container
    let actions = wrapper.querySelector('.desc-actions');
    if (!actions) {
        actions = document.createElement('div');
        actions.className = 'desc-actions text-block-actions';
        wrapper.appendChild(actions);
    }
    const ensureBtn = (id, icon, title) => {
        let btn = descSection.querySelector(`#${id}`);
        if (!btn) {
            btn = document.createElement('button');
            btn.id = id;
            btn.type = 'button';
            actions.appendChild(btn);
        } else if (!actions.contains(btn)) {
            // Move into actions container
            actions.appendChild(btn);
        }
        btn.classList.add('round-icon-button');
        btn.dataset.icon = icon;
        btn.setAttribute('aria-label', title);
        btn.title = title;
        return btn;
    };
    ensureBtn(`typeCopyDescription_${suffix}`, 'copy', 'Copy description to clipboard');
    ensureBtn(`typeAnnotateDescription_${suffix}`, 'annotate', 'Annotate description');
    ensureBtn(`typeEditDescriptionButton_${suffix}`, 'edit', 'Edit description');
    ensureBtn(`typeDeleteDescriptionButton_${suffix}`, 'delete', 'Delete description');

    try {
        const wrapper2 = actions.closest('.type-description-wrapper');
        const display2 = wrapper2 ? wrapper2.querySelector('.concept-type-description') : null;
        if (wrapper2 && display2) {
            setupAdaptiveActionGroup(actions, display2);
        }
    } catch (_) { }
}

export function renameLegacyDescriptionIds(section, suffix) {
    const ids = ['typeDescriptionDisplay', 'typeCopyDescription', 'typeEditDescriptionButton', 'typeDeleteDescriptionButton', 'typeAnnotateDescription', 'typeDescriptionEditor', 'typeDescriptionEditActions', 'typeDescriptionTextarea', 'typeEditDescriptionSave', 'typeEditDescriptionCancel', 'typeDescriptionStatus'];
    let renamedCount = 0;
    ids.forEach(base => {
        const el = section.querySelector(`#${base}`);
        if (el && !el.id.endsWith(`_${suffix}`)) {
            const oldId = el.id;
            el.id = `${base}_${suffix}`;
            console.debug(`[dynamicTabs] Renamed button: ${oldId} → ${el.id}`);
            renamedCount++;
        }
    });
    if (section.id === 'typeDescriptionSection') {
        console.debug(`[dynamicTabs] Renamed section: typeDescriptionSection → typeDescriptionSection_${suffix}`);
        section.id = `typeDescriptionSection_${suffix}`;
    }
    if (renamedCount > 0) {
        console.debug(`[dynamicTabs] renameLegacyDescriptionIds: ${renamedCount} IDs renamed for suffix ${suffix}`);
    }
}

// backfillDescriptionGlyphs removed: SVG icons now injected; textual glyph fallback no longer required.

// Helper: attach a small ID chip with copy-to-clipboard next to the header
function attachConceptIdCopyChip(headerH2, conceptId, kind) {
    try {
        if (!headerH2 || !conceptId) return;

        // Retrofit legacy description markup (pre-icon refactor) to new round icon buttons.
        function upgradeLegacyDescriptionActions(descSection, suffix) {
            if (!descSection) return;
            // If already has the new container with round buttons, nothing to do.
            if (descSection.querySelector('.desc-actions .round-icon-button[data-icon="annotate"]')) return;
            const display = descSection.querySelector(`#typeDescriptionDisplay_${suffix}`) || descSection.querySelector('.concept-type-description');
            if (!display) return;

            // Ensure we have a wrapper for positioning
            let wrapper = display.closest('.type-description-wrapper');
            if (!wrapper) {
                wrapper = document.createElement('div');
                wrapper.className = 'type-description-wrapper';
                wrapper.style.position = 'relative';
                display.parentNode.insertBefore(wrapper, display);
                wrapper.appendChild(display);
            }

            // Create (or reuse) actions container
            let actions = wrapper.querySelector('.desc-actions');
            if (!actions) {
                actions = document.createElement('div');
                actions.className = 'desc-actions text-block-actions';
                wrapper.appendChild(actions);
            }

            // Helper to create or transform a button
            const ensureBtn = (id, icon, title) => {
                let btn = descSection.querySelector(`#${id}`);
                if (!btn) {
                    btn = document.createElement('button');
                    btn.id = id;
                    btn.type = 'button';
                    actions.appendChild(btn);
                } else if (!actions.contains(btn)) {
                    // Move into actions container
                    actions.appendChild(btn);
                }
                btn.classList.add('round-icon-button');
                btn.dataset.icon = icon;
                btn.setAttribute('aria-label', title);
                btn.title = title;
                return btn;
            };
            ensureBtn(`typeCopyDescription_${suffix}`, 'copy', 'Copy description to clipboard');
            ensureBtn(`typeAnnotateDescription_${suffix}`, 'annotate', 'Annotate description');
            ensureBtn(`typeEditDescriptionButton_${suffix}`, 'edit', 'Edit description');
            ensureBtn(`typeDeleteDescriptionButton_${suffix}`, 'delete', 'Delete description');
        }
        // Avoid duplicates
        if (headerH2.querySelector('.concept-id-chip')) return;
        const chip = document.createElement('span');
        chip.className = 'concept-id-chip';
        if (kind) chip.classList.add(kind); // type | individual variant styling

        const text = document.createElement('span');
        text.className = 'concept-id-text';
        text.textContent = conceptId;
        text.title = 'Concept identifier';

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'copy-id-button';
        btn.title = 'Copy ID to clipboard';
        btn.setAttribute('aria-label', 'Copy ID to clipboard');
        btn.textContent = '📋';

        const doCopy = async () => {
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(conceptId);
                } else {
                    // Fallback
                    const ta = document.createElement('textarea');
                    ta.value = conceptId; document.body.appendChild(ta); ta.select();
                    document.execCommand('copy'); document.body.removeChild(ta);
                }
                // Quick feedback
                const original = btn.textContent;
                btn.textContent = '✔';
                btn.title = 'Copied!';
                setTimeout(() => { btn.textContent = original; btn.title = 'Copy ID to clipboard'; }, 1200);
            } catch (e) {
                btn.title = 'Copy failed';
            }
        };

        btn.addEventListener('click', (e) => { e.stopPropagation(); doCopy(); });

        chip.appendChild(text);
        chip.appendChild(btn);
        // spacing before chip from existing header content
        chip.style.marginLeft = '8px';
        headerH2.appendChild(chip);
    } catch (e) {
        console.warn('[dynamicTabs] attachConceptIdCopyChip failed', e);
    }
}

// Helper: add a Raw JSON button and modal to view/copy the raw DB object
// Export for testing raw JSON button behaviour
export function attachRawDataButton(headerH2, conceptId, kind, containerEl) {
    try {
        if (!headerH2 || !conceptId || !containerEl) return;
        // Avoid duplicates
        if (headerH2.querySelector('.raw-json-button')) return;

        const btn = document.createElement('button');
        btn.className = 'raw-json-button';
        // Determine stored preference (default raw). If user previously viewed FULL, clicking will open FULL unless Shift toggled
        const storedPref = (typeof sessionStorage !== 'undefined' && sessionStorage.getItem('rawJsonPreferredMode')) || 'RAW';
        const baseTooltip = () => {
            const pref = (typeof sessionStorage !== 'undefined' && sessionStorage.getItem('rawJsonPreferredMode')) || 'RAW';
            const primary = pref === 'RAW' ? 'Raw Mongo doc' : 'Full payload';
            const toggleHint = 'Shift toggles';
            const alt = pref === 'RAW' ? 'Full payload' : 'Raw Mongo doc';
            return `${primary} (click) / ${alt} (Shift) — ${toggleHint}`;
        };
        btn.title = baseTooltip();
        btn.setAttribute('aria-label', baseTooltip());
        btn.setAttribute('data-keep-title', 'true');
        btn.textContent = '{ }'; // lightweight icon

        // Place it at the far right within the header
        btn.style.marginLeft = 'auto';
        btn.style.fontFamily = 'monospace';
        btn.style.fontSize = '0.9rem';
        btn.style.padding = '2px 6px';
        btn.style.border = '1px solid #d1d5db';
        btn.style.borderRadius = '4px';
        btn.style.background = '#f9fafb';
        btn.style.color = '#374151';
        btn.style.cursor = 'pointer';

        // Ensure headerH2 is flex so the button aligns right without layout jumps
        try {
            headerH2.style.display = 'flex';
            headerH2.style.alignItems = 'center';
            headerH2.style.gap = '8px';
        } catch (_) { /* no-op */ }

        // Robust binding: mark button as bound so future safety rebinds can skip
        btn.addEventListener('click', async (e) => {
            e.stopPropagation(); // Prevent parent click handlers from swallowing the action (regression hardening)
            try {
                const encoded = encodeURIComponent(conceptId);
                // Preference logic: if Shift pressed invert; else use stored preference
                let pref = (typeof sessionStorage !== 'undefined' && sessionStorage.getItem('rawJsonPreferredMode')) || 'RAW';
                const inverted = e.shiftKey === true;
                if (inverted) {
                    pref = (pref === 'RAW') ? 'FULL' : 'RAW';
                    try { sessionStorage.setItem('rawJsonPreferredMode', pref); } catch (_) { /* ignore */ }
                }
                const wantFull = pref === 'FULL';
                const url = `/vontology/api/vontology/node_content?identifier=${encoded}${wantFull ? '' : '&raw_only=1'}`;
                const isRawOnly = !wantFull;
                const t0 = performance.now();
                const res = await fetch(url);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const text = await res.text();
                const latencyMs = performance.now() - t0;
                let data; try { data = JSON.parse(text); } catch (_) { data = { parse_error: true, raw: text }; }
                const byteSize = new Blob([text]).size;
                const jsonText = JSON.stringify(data, null, 2);
                const anchorRect = btn.getBoundingClientRect();
                openRawJsonModal(containerEl, conceptId, jsonText, isRawOnly, { latencyMs, byteSize, pref, anchorRect });
                btn.title = baseTooltip();
                btn.setAttribute('aria-label', baseTooltip());
            } catch (err) {
                const anchorRect = btn.getBoundingClientRect();
                openRawJsonModal(containerEl, conceptId, `{"error": "Failed to load raw object: ${String(err)}"}`, true, { anchorRect });
            }
        }, { passive: true });
        try { btn.dataset.bound = '1'; } catch (_) { /* no-op */ }

        headerH2.appendChild(btn);

        // Add text relations button [ ]
        if (!headerH2.querySelector('.text-relations-button')) {
            const relBtn = document.createElement('button');
            relBtn.className = 'text-relations-button';
            relBtn.textContent = '[ ]';
            relBtn.title = 'list relations';
            relBtn.setAttribute('aria-label', 'list relations');
            relBtn.setAttribute('data-keep-title', 'true');
            relBtn.style.fontFamily = 'monospace';
            relBtn.style.fontSize = '0.9rem';
            relBtn.style.padding = '2px 6px';
            relBtn.style.border = '1px solid #d1d5db';
            relBtn.style.borderRadius = '4px';
            relBtn.style.background = '#f9fafb';
            relBtn.style.color = '#374151';
            relBtn.style.cursor = 'pointer';
            relBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                try {
                    const encoded = encodeURIComponent(conceptId);
                    const url = `/vontology/api/vontology/text_relations?concept_id=${encoded}`;
                    const t0 = performance.now();
                    const res = await fetch(url);
                    if (!res.ok) throw new Error(`HTTP ${res.status}`);
                    const data = await res.json();
                    const latencyMs = performance.now() - t0;
                    const jsonText = JSON.stringify(data, null, 2);
                    const anchorRect = relBtn.getBoundingClientRect();
                    openTextRelationsModal(containerEl, conceptId, jsonText, { latencyMs, anchorRect });
                } catch (err) {
                    const anchorRect = relBtn.getBoundingClientRect();
                    openTextRelationsModal(containerEl, conceptId, `{"error": "Failed to load text relations: ${String(err)}"}`, { anchorRect });
                }
            });
            headerH2.appendChild(relBtn);
        }
    } catch (e) {
        console.warn('[dynamicTabs] attachRawDataButton failed', e);
    }
}

// Helper: add a star button to toggle key concept marking in the header
function attachKeyConceptStarButton(headerH2, conceptId) {
    try {
        if (!headerH2 || !conceptId) return;
        // Avoid duplicates
        if (headerH2.querySelector('.key-concept-star-button')) return;

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'key-concept-star-button';
        btn.setAttribute('aria-label', 'Toggle key concept');
        btn.setAttribute('data-keep-title', 'true');
        btn.style.display = 'inline-block';  // Ensure it's visible

        // Get current key concept state
        const keyConceptIds = getKeyConceptIds();
        const isKey = keyConceptIds.has(conceptId);

        // Set initial state
        btn.textContent = isKey ? '⭐' : '☆';
        btn.title = isKey ? 'Unmark as key concept' : 'Mark as key concept';
        if (isKey) {
            btn.classList.add('marked');
        }

        console.log(`[attachKeyConceptStarButton] Creating star button for ${conceptId}, isKey: ${isKey}`);

        // Click handler
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();

            // Get user ID
            const userConceptId = getCurrentUserConceptId();
            if (!userConceptId) {
                alert('Cannot mark key concepts: No user ID found. Please ensure you are logged in.');
                return;
            }

            const currentlyKey = keyConceptIds.has(conceptId);
            const action = currentlyKey ? 'remove' : 'add';

            try {
                // Call backend
                const response = await fetch('/vontology/api/vontology/concept/key-concept', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        concept_id: conceptId,
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
                if (action === 'add') {
                    keyConceptIds.add(conceptId);
                    btn.textContent = '⭐';
                    btn.title = 'Unmark as key concept';
                    btn.classList.add('marked');
                } else {
                    keyConceptIds.delete(conceptId);
                    btn.textContent = '☆';
                    btn.title = 'Mark as key concept';
                    btn.classList.remove('marked');
                }

                console.log(`[attachKeyConceptStarButton] ${action === 'add' ? 'Marked' : 'Unmarked'} ${conceptId} as key concept`);

                // Update tree badge to reflect the change
                updateTreeKeyConceptBadge(conceptId, action === 'add');

                // Update any other open tab headers for this concept
                updateTabHeaderStarButtons(conceptId, action === 'add');

            } catch (error) {
                console.error('[attachKeyConceptStarButton] Failed to toggle key concept:', error);
                alert(`Failed to ${action} key concept: ${error.message}`);
            }
        });

        headerH2.appendChild(btn);
    } catch (e) {
        console.warn('[dynamicTabs] attachKeyConceptStarButton failed', e);
    }
}

// Safety net: rebind any raw-json buttons that somehow lost their handler (observed regression case)
function ensureRawJsonButtonBindings() {
    try {
        document.querySelectorAll('.raw-json-button').forEach(btn => {
            if (btn.dataset.bound === '1') return; // already bound
            const tabContent = btn.closest('.tab-content');
            const conceptId = tabContent && tabContent.dataset ? tabContent.dataset.conceptId : null;
            if (!conceptId) return;
            // Re-attach by recreating via helper (will skip duplicate guard by removing then re-adding)
            try {
                btn.remove();
                const header = tabContent.querySelector('.concept-header');
                if (header) attachRawDataButton(header, conceptId, (tabContent.dataset && tabContent.dataset.tabKind) || 'unknown', tabContent);
            } catch (rebErr) { console.warn('[dynamicTabs] Failed rebind raw-json-button', rebErr); }
        });
    } catch (e) { /* ignore */ }
}

// Periodic light check (cheap) to ensure buttons stay functional without heavy timers
setInterval(() => { try { ensureRawJsonButtonBindings(); } catch (_) { /* ignore */ } }, 8000);

function openRawJsonModal(containerEl, conceptId, jsonText, isRaw = false, meta = null) {
    // Remove existing modal if present
    const existing = containerEl.querySelector('.raw-json-modal');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.className = 'raw-json-modal';
    const titleLabel = isRaw ? 'Raw Mongo DB object' : 'Full concept payload';
    const modeBadge = `<span class="raw-mode-badge ${isRaw ? 'badge-raw' : 'badge-full'}">${isRaw ? 'RAW' : 'FULL'}</span>`;
    const subNote = isRaw
        ? '<small style="color:#6b7280">This is the stored document (no enrichment). Shift-click { } for full payload.</small>'
        : '<small style="color:#6b7280">Includes derived fields & relationships. Click { } without Shift for raw stored document.</small>';
    overlay.innerHTML = `
            <div class="raw-json-dialog">
                <div class="raw-json-header">
                    <span class="raw-json-title">${modeBadge} ${titleLabel}: ${escapeHtml(conceptId)}</span>
                    <button class="raw-json-close" title="Close">×</button>
                </div>
                <div class="raw-json-body">
                    ${subNote}
                    <pre class="raw-json-pre"><code class="raw-json-code"></code></pre>
                </div>
                <div class="raw-json-footer">
                    <div class="raw-json-stats"></div>
                    <div class="raw-json-actions">
                      <button class="raw-json-copy">Copy JSON</button>
                      <button class="raw-json-close-2">Close</button>
                    </div>
                </div>
            </div>`;

    const codeEl = overlay.querySelector('.raw-json-code');
    if (codeEl) codeEl.textContent = jsonText;

    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) close();
    });
    const closeBtn = overlay.querySelector('.raw-json-close');
    const closeBtn2 = overlay.querySelector('.raw-json-close-2');
    if (closeBtn) closeBtn.addEventListener('click', close);
    if (closeBtn2) closeBtn2.addEventListener('click', close);

    // Populate stats if meta provided
    const statsEl = overlay.querySelector('.raw-json-stats');
    if (statsEl && meta) {
        const { latencyMs, byteSize, pref } = meta;
        const prettySize = humanFileSize(byteSize);
        statsEl.textContent = `${isRaw ? 'RAW' : 'FULL'} • ${prettySize} • ${latencyMs.toFixed(1)} ms`;
        statsEl.style.fontSize = '0.75rem';
        statsEl.style.color = '#6b7280';
        statsEl.style.display = 'flex';
        statsEl.style.alignItems = 'center';
        statsEl.style.gap = '6px';
    }

    const copyBtn = overlay.querySelector('.raw-json-copy');
    if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
            try {
                await navigator.clipboard.writeText(jsonText);
                copyBtn.textContent = 'Copied!';
                setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 1500);
            } catch (e) {
                copyBtn.textContent = 'Copy failed';
                setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 1500);
            }
        });
    }

    containerEl.appendChild(overlay);

    // Anchored positioning (similar to text relations modal) if anchorRect provided
    try {
        const anchorRect = meta && meta.anchorRect;
        if (anchorRect && typeof anchorRect === 'object') {
            overlay.classList.add('anchored');
            const dialog = overlay.querySelector('.raw-json-dialog');
            if (dialog) {
                dialog.style.maxWidth = '640px';
                dialog.style.width = 'min(640px, 70vw)';
                dialog.style.position = 'fixed';
                dialog.style.margin = '0';
                const padding = 8;
                let top = anchorRect.bottom + padding;
                let left = anchorRect.left;
                const vpW = window.innerWidth || 1024; // jsdom fallback
                const vpH = window.innerHeight || 768;  // jsdom fallback
                const rectW = Math.min(640, vpW * 0.7);
                // Use current height measurement (may be 0 in jsdom before layout; fall back)
                const measured = dialog.getBoundingClientRect();
                const rectH = Math.min(measured.height || 400, vpH * 0.8);
                if (left + rectW + 4 > vpW) left = Math.max(4, vpW - rectW - 4);
                if (top + rectH + 4 > vpH) {
                    const altTop = anchorRect.top - rectH - padding;
                    if (altTop >= 4) top = altTop;
                }
                dialog.style.top = `${Math.max(4, top)}px`;
                dialog.style.left = `${Math.max(4, left)}px`;
                dialog.style.maxHeight = '80vh';
                dialog.style.height = 'auto';
                overlay.style.background = 'transparent';
                overlay.style.inset = '0';
                overlay.style.alignItems = 'flex-start';
                overlay.style.justifyContent = 'flex-start';
                overlay.addEventListener('click', (ev) => { if (ev.target === overlay) close(); });
            }
        }
    } catch (posErr) {
        console.warn('[dynamicTabs] Failed anchored positioning for raw json modal', posErr);
    }
}

function openTextRelationsModal(containerEl, conceptId, jsonText, meta = null) {
    const existing = containerEl.querySelector('.text-relations-modal');
    if (existing) existing.remove();
    const overlay = document.createElement('div');
    overlay.className = 'text-relations-modal';
    overlay.innerHTML = `
            <div class="raw-json-dialog">
                <div class="raw-json-header">
                    <span class="raw-json-title">Text Relations: ${escapeHtml(conceptId)}</span>
                    <button class="text-relations-close" title="Close">×</button>
                </div>
                <div class="raw-json-body">
                    <small style="color:#6b7280">All text relations for this concept (read-only)</small>
                    <pre class="raw-json-pre"><code class="text-relations-code"></code></pre>
                </div>
                <div class="raw-json-footer">
                    <div class="raw-json-stats"></div>
                    <div class="raw-json-actions">
                        <button class="text-relations-copy">Copy JSON</button>
                        <button class="text-relations-close-2">Close</button>
                    </div>
                </div>
            </div>`;
    const codeEl = overlay.querySelector('.text-relations-code');
    if (codeEl) codeEl.textContent = jsonText;
    const close = () => overlay.remove();
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    const c1 = overlay.querySelector('.text-relations-close');
    const c2 = overlay.querySelector('.text-relations-close-2');
    if (c1) c1.addEventListener('click', close); if (c2) c2.addEventListener('click', close);
    const copyBtn = overlay.querySelector('.text-relations-copy');
    if (copyBtn) {
        copyBtn.addEventListener('click', async () => {
            try { await navigator.clipboard.writeText(jsonText); copyBtn.textContent = 'Copied!'; setTimeout(() => copyBtn.textContent = 'Copy JSON', 1500); } catch (_) { copyBtn.textContent = 'Copy failed'; setTimeout(() => copyBtn.textContent = 'Copy JSON', 1500); }
        });
    }
    containerEl.appendChild(overlay);
    // Anchored positioning (convert full-screen overlay into anchored panel) if anchorRect provided
    try {
        const anchorRect = meta && meta.anchorRect;
        if (anchorRect && typeof anchorRect === 'object') {
            // Use absolute positioning within viewport (fixed) similar to a context panel
            overlay.classList.add('anchored');
            const dialog = overlay.querySelector('.raw-json-dialog');
            if (dialog) {
                // Temporarily ensure width for measurement
                dialog.style.maxWidth = '640px';
                dialog.style.width = 'min(640px, 70vw)';
                dialog.style.position = 'fixed';
                dialog.style.margin = '0';
                // Calculate preferred top/left (below the button, left-align) with viewport clamping
                const padding = 8;
                let top = anchorRect.bottom + padding;
                let left = anchorRect.left;
                const vpW = window.innerWidth;
                const vpH = window.innerHeight;
                const rectW = Math.min(640, vpW * 0.7);
                const rectH = Math.min(dialog.getBoundingClientRect().height || 400, vpH * 0.8);
                // Clamp horizontal so it stays in viewport
                if (left + rectW + 4 > vpW) left = Math.max(4, vpW - rectW - 4);
                // If not enough space below, try placing above
                if (top + rectH + 4 > vpH) {
                    const altTop = anchorRect.top - rectH - padding;
                    if (altTop >= 4) top = altTop; // else keep below; will overflow slightly
                }
                dialog.style.top = `${Math.max(4, top)}px`;
                dialog.style.left = `${Math.max(4, left)}px`;
                dialog.style.maxHeight = '80vh';
                dialog.style.height = 'auto';
                // Make overlay background minimal (transparent) for anchored mode
                overlay.style.background = 'transparent';
                overlay.style.inset = '0';
                overlay.style.alignItems = 'flex-start';
                overlay.style.justifyContent = 'flex-start';
                // Dismiss when clicking outside dialog (since transparent background)
                overlay.addEventListener('click', (ev) => {
                    if (ev.target === overlay) close();
                });
            }
        }
    } catch (posErr) {
        console.warn('[dynamicTabs] Failed anchored positioning for text relations modal', posErr);
    }
}

// Human-readable file size
function humanFileSize(bytes) {
    try {
        const thresh = 1024;
        if (Math.abs(bytes) < thresh) return bytes + ' B';
        const units = ['KB', 'MB', 'GB', 'TB'];
        let u = -1;
        let b = bytes;
        do { b /= thresh; ++u; } while (Math.abs(b) >= thresh && u < units.length - 1);
        return b.toFixed(1) + ' ' + units[u];
    } catch (_) { return bytes + ' B'; }
}

function escapeHtml(s) {
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// Helper: show description for Type tabs and attach edit/save/cancel
async function populateTypeDescription(conceptId, suffix) {
    try {
        const descSection = document.getElementById(`typeDescriptionSection_${suffix}`) || document.getElementById('typeDescriptionSection');
        const encodedId = encodeURIComponent(conceptId);
        // Guard: ensure the primary step container exists to avoid stray fetch work before UI scaffold
        const step1Container = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (!step1Container) return;
        const display = document.getElementById(`typeDescriptionDisplay_${suffix}`);
        const editActions = document.getElementById(`typeDescriptionEditActions_${suffix}`) ||
            document.getElementById(`typeDescriptionEditor_${suffix}`);
        const textarea = document.getElementById(`typeDescriptionTextarea_${suffix}`);
        const editBtn = document.getElementById(`typeEditDescriptionButton_${suffix}`);
        const annotateBtn = document.getElementById(`typeAnnotateDescription_${suffix}`);
        const delBtn = document.getElementById(`typeDeleteDescriptionButton_${suffix}`);
        const saveBtn = document.getElementById(`typeEditDescriptionSave_${suffix}`);
        const cancelBtn = document.getElementById(`typeEditDescriptionCancel_${suffix}`);
        const statusEl = document.getElementById(`typeDescriptionStatus_${suffix}`);
        // Capture missing buttons before potential reconstruction (so we can force rebind if we add them)
        const preMissingButtons = [];
        ['Copy', 'Annotate', 'Edit', 'Delete'].forEach(kind => {
            const id = `type${kind}Description_${suffix}`.replace('EditDescription_', 'EditDescriptionButton_').replace('DeleteDescription_', 'DeleteDescriptionButton_');
            if (!document.getElementById(id)) preMissingButtons.push(id);
        });
        // Reconstruct missing action buttons even if listeners previously bound (race where buttons removed or never injected icons)
        const ensureActionButtonsExist = () => {
            if (!display) return false;
            // Action container may have been removed by external DOM ops; rebuild idempotently
            let actionsContainer = document.getElementById(`typeCopyDescription_${suffix}`)?.parentElement;
            if (!actionsContainer || !actionsContainer.classList.contains('desc-actions')) {
                // Attempt to locate wrapper then inject a fresh actions container
                const wrapper = display.closest('.type-description-wrapper') || display.parentElement;
                actionsContainer = document.createElement('div');
                actionsContainer.className = 'desc-actions text-block-actions';
                if (wrapper && wrapper.appendChild) wrapper.appendChild(actionsContainer); else display.parentNode.appendChild(actionsContainer);
            }
            const mk = (id, icon, label) => {
                let btn = descSection.querySelector(`#${id}`);
                if (!btn) {
                    btn = document.createElement('button');
                    btn.id = id; btn.className = 'round-icon-button'; btn.dataset.icon = icon;
                    if (label) { btn.title = label; btn.setAttribute('aria-label', label); }
                    actionsContainer.appendChild(btn);
                }
            };
            mk(`typeCopyDescription_${suffix}`, 'copy', 'Copy description to clipboard');
            mk(`typeAnnotateDescription_${suffix}`, 'annotate', 'Annotate description');
            mk(`typeEditDescriptionButton_${suffix}`, 'edit', 'Edit description');
            mk(`typeDeleteDescriptionButton_${suffix}`, 'delete', 'Delete description');
            try { upgradeActionButtonIcons(actionsContainer); } catch (_) { /* ignore */ }
            return true;
        };
        const reconstructed = ensureActionButtonsExist();

        // Fast-path safety: only skip if content populated, listeners bound, AND all required buttons still present
        const listenersBound = display && display.dataset.descEvents === '1';
        const requiredBtnIds = [
            `typeCopyDescription_${suffix}`,
            `typeAnnotateDescription_${suffix}`,
            `typeEditDescriptionButton_${suffix}`,
            `typeDeleteDescriptionButton_${suffix}`
        ];
        const missingBtns = requiredBtnIds.filter(id => !document.getElementById(id));
        if (display && display.dataset.populated === '1' && textarea && typeof textarea.value === 'string' && listenersBound && missingBtns.length === 0) {
            // If we reconstructed buttons (some were missing before) we need to force rebind even though listenersBound is true.
            if (reconstructed && preMissingButtons.length) {
                try { delete display.dataset.descEvents; } catch (_) { /* force rebind below */ }
            } else {
                return; // Fully initialised & buttons intact
            }
        }
        if (listenersBound && (missingBtns.length || preMissingButtons.length)) {
            // Buttons disappeared after initial binding; clear descEvents flag to force rebind path
            try { delete display.dataset.descEvents; } catch (_) { }
        }

        // Only the core elements are required to proceed; delete button may be absent in older markup
        if (!(display && editActions && textarea && editBtn && saveBtn && cancelBtn)) {
            const isTestEnv = (typeof jest !== 'undefined') || (typeof process !== 'undefined' && process?.env?.JEST_WORKER_ID);
            // One-time synthesis attempt for runtime (not only test) if host section exists or can be created.
            let synthesized = false;
            let host = document.getElementById(`typeDescriptionSection_${suffix}`);
            if (!host) {
                host = document.createElement('div');
                host.id = `typeDescriptionSection_${suffix}`;
                step1Container.appendChild(host);
            }
            if (!host.innerHTML) {
                host.innerHTML = `
                                                <h3 class="concept-list-title">Description</h3>
                                                <div class="type-description-wrapper" style="position:relative;">
                                                    <div id="typeDescriptionDisplay_${suffix}" class="concept-type-description editable-description" tabindex="0"><i>Loading description...</i></div>
                                                    <textarea id="typeDescriptionTextarea_${suffix}" class="description-editor hidden" rows="6"></textarea>
                                                    <div class="desc-actions text-block-actions">
                                                        <button id="typeCopyDescription_${suffix}" class="round-icon-button" data-icon="copy"></button>
                                                        <button id="typeAnnotateDescription_${suffix}" class="round-icon-button" data-icon="annotate"></button>
                                                        <button id="typeEditDescriptionButton_${suffix}" class="round-icon-button" data-icon="edit"></button>
                                                        <button id="typeDeleteDescriptionButton_${suffix}" class="round-icon-button" data-icon="delete"></button>
                                                    </div>
                                                    <div id="typeDescriptionEditActions_${suffix}" class="description-edit-actions hidden">
                                                        <button id="typeEditDescriptionSave_${suffix}" class="small-btn">Save</button>
                                                        <button id="typeEditDescriptionCancel_${suffix}" class="small-btn">Cancel</button>
                                                    </div>
                                                </div>
                                                <span id="typeDescriptionStatus_${suffix}" class="concept-status ml-2"></span>`;
                synthesized = true;
            }
            if (synthesized || isTestEnv) {
                return populateTypeDescription(conceptId, suffix);
            }
            console.warn('[dynamicTabs] populateTypeDescription: missing essential elements – will retry shortly', { hasDisplay: !!display, hasEditActions: !!editActions, hasTextarea: !!textarea, hasEdit: !!editBtn, hasSave: !!saveBtn, hasCancel: !!cancelBtn, suffix, synthesized });
            // Light retry strategy (non-test only): attempt a few times with backoff; store attempt count on window-scoped map
            try {
                if (!window.__descPopulateRetries) window.__descPopulateRetries = new Map();
                const key = conceptId + '::' + suffix;
                const rec = window.__descPopulateRetries.get(key) || { attempts: 0 };
                if (rec.attempts < 5) {
                    rec.attempts += 1;
                    window.__descPopulateRetries.set(key, rec);
                    const delay = 80 * rec.attempts; // incremental backoff
                    setTimeout(() => { try { populateTypeDescription(conceptId, suffix); } catch (_) { /* swallow */ } }, delay);
                } else {
                    console.warn('[dynamicTabs] populateTypeDescription: giving up after retries', { conceptId, suffix });
                }
            } catch (_) { /* ignore retry errors */ }
            return;
        }

        let relationId = null;

        // Helper to consistently render and store the raw description text (pre-render)
        const applyRawDescription = (rawText) => {
            const text = rawText || '';
            // Persist original raw text (including markdown / angle brackets) for future edits
            try { display.dataset.rawText = text; } catch (_) { }
            // Reset markdown-rendered class then render
            display.className = (display.className || '').replace(/\bmarkdown-rendered\b/g, '').trim();
            if (text) {
                const isMarkdown = detectMarkdown(text);
                if (isMarkdown) display.classList.add('markdown-rendered');
                display.textContent = text;
                void renderSmartTextAsync(text, true).then((html) => {
                    display.innerHTML = html;
                }).catch(() => {
                    display.textContent = text;
                });
            } else {
                display.innerHTML = '<i>No description available.</i>';
            }
        };

        // Fetch current description via text relation API with layered fallbacks & diagnostics
        const loadDescription = async () => {
            const setEmpty = () => {
                relationId = null;
                display.innerHTML = '<i>No description available.</i>';
                textarea.value = '';
            };
            try {
                const primaryUrl = `/api/concepts/${encodedId}/texts?predicate=hasDescription&limit=1`;
                let res = await fetch(primaryUrl);
                let data = await res.json().catch(() => ({}));
                if (res.ok && Array.isArray(data.texts) && data.texts.length) {
                    const desc = data.texts[0];
                    relationId = desc.relation_id || null;
                    applyRawDescription(desc.text || '');
                    textarea.value = desc.text || '';
                    console.debug('[dynamicTabs] Description loaded (primary API)', { conceptId, relationId });
                    return;
                }
                console.debug('[dynamicTabs] Primary description API returned no data – attempting legacy fallback', { status: res.status, bodyKeys: Object.keys(data || {}) });
                const legacyUrl = `/vontology/api/vontology/node_content?identifier=${encodedId}`;
                res = await fetch(legacyUrl);
                if (res.ok) {
                    data = await res.json().catch(() => ({}));
                    const legacyDesc = data.description || data.content_html;
                    if (legacyDesc) {
                        // If only HTML (legacy) provided, preserve a text-only raw variant for editing
                        if (data.content_html && !data.description) {
                            // Strip tags for the raw editable form
                            const plain = legacyDesc.replace(/<[^>]+>/g, '');
                            applyRawDescription(plain);
                            textarea.value = plain;
                        } else {
                            applyRawDescription(legacyDesc);
                            textarea.value = legacyDesc || '';
                        }
                        relationId = null;
                        console.debug('[dynamicTabs] Description loaded (legacy node_content)', { conceptId });
                        return;
                    }
                }
                const relUrl = `/vontology/api/vontology/text_relations?concept_id=${encodedId}&limit=500`;
                res = await fetch(relUrl);
                if (res.ok) {
                    data = await res.json().catch(() => ({}));
                    const list = data.text_relations || data.texts || data.relations || [];
                    const found = Array.isArray(list) ? list.find(t => t && t.predicate === 'hasDescription') : null;
                    if (found) {
                        relationId = found.relation_id || null;
                        applyRawDescription(found.text || '');
                        textarea.value = found.text || '';
                        console.debug('[dynamicTabs] Description loaded (relations list fallback)', { conceptId, relationId });
                        return;
                    }
                }
                console.debug('[dynamicTabs] No description found after all fallbacks', { conceptId });
                setEmpty();
            } catch (err) {
                console.warn('[dynamicTabs] loadDescription error', err);
                setEmpty();
            }
        };

        await loadDescription();
        try { if (display) display.dataset.populated = '1'; } catch (_) { }

        // Remove previous listeners by cloning buttons to avoid duplicate handlers on re-init
        const copyBtn = document.getElementById(`typeCopyDescription_${suffix}`);
        const newEdit = editBtn.cloneNode(true); editBtn.parentNode.replaceChild(newEdit, editBtn);
        const newSave = saveBtn.cloneNode(true); saveBtn.parentNode.replaceChild(newSave, saveBtn);
        const newCancel = cancelBtn.cloneNode(true); cancelBtn.parentNode.replaceChild(newCancel, cancelBtn);
        let newCopy = null;
        if (copyBtn) {
            newCopy = copyBtn.cloneNode(true);
            copyBtn.parentNode.replaceChild(newCopy, copyBtn);
        }
        let newDel = null;
        if (delBtn) {
            newDel = delBtn.cloneNode(true);
            delBtn.parentNode.replaceChild(newDel, delBtn);
        }
        // Clone annotate button too (previously omitted) to avoid stacking listeners on re-init
        let newAnnotate = null;
        if (annotateBtn) {
            newAnnotate = annotateBtn.cloneNode(true);
            annotateBtn.parentNode.replaceChild(newAnnotate, annotateBtn);
            // Mark as having a direct handler so delegated global listener skips it
            newAnnotate.dataset.descAnnotateDirect = '1';
        }

        newEdit.addEventListener('click', () => {
            // Store original content for cancel functionality
            const originalContent = display.innerHTML;
            const originalText = display.dataset.rawText || display.textContent || '';

            // Hide display and show textarea with current content
            display.classList.add('hidden');
            textarea.classList.remove('hidden');
            textarea.value = originalText;
            textarea.focus();

            // Show save/cancel actions
            editActions.classList.remove('hidden');

            // Disable edit button
            newEdit.disabled = true;
            statusEl.textContent = '';

            // Store original content for cancel
            textarea.dataset.originalContent = originalContent;
        });

        newCancel.addEventListener('click', () => {
            // Restore original content
            const originalContent = textarea.dataset.originalContent || '';
            display.innerHTML = originalContent;

            // Hide textarea and show display
            textarea.classList.add('hidden');
            display.classList.remove('hidden');
            statusEl.textContent = '';
        });

        if (newCopy) {
            newCopy.addEventListener('click', async () => {
                const text = extractDescriptionPlainText(display);
                if (!text.trim()) {
                    showCopyFeedback(newCopy, 'No description to copy', false);
                    return;
                }
                try {
                    await copyToClipboard(text);
                    showCopyFeedback(newCopy, 'Description copied!', true);
                } catch (e) {
                    showCopyFeedback(newCopy, 'Copy failed', false);
                }
            });
        }
        newSave.addEventListener('click', async () => {
            const newText = textarea.value.trim();
            if (!newText) {
                statusEl.textContent = 'Cannot save empty description.';
                return;
            }
            statusEl.textContent = 'Saving...';
            try {
                // JVNAUTOSCI-570 regression fix: use dedicated dual-write description endpoint
                const ok = await updateConceptDescription(conceptId, newText);
                if (!ok) throw new Error('Failed to persist description');

                applyRawDescription(newText);
                statusEl.textContent = 'Saved';
                textarea.classList.add('hidden');
                display.classList.remove('hidden');
                editActions.classList.add('hidden');
                newEdit.disabled = false;
            } catch (e) {
                statusEl.textContent = `Error: ${e.message}`;
            }
        });

        if (newAnnotate) {
            newAnnotate.addEventListener('click', () => {
                // JVNAUTOSCI-571: paragraph-preserving extraction (was raw textContent causing '.It')
                const text = extractDescriptionPlainText(display);
                const info = dynamicConceptTabs.get(conceptId);
                const conceptName = info?.conceptName || conceptId;
                if (newAnnotate.dataset.lastDispatchTs) {
                    const last = Number(newAnnotate.dataset.lastDispatchTs) || 0;
                    if (Date.now() - last < 40) return;
                }
                newAnnotate.dataset.lastDispatchTs = String(Date.now());
                document.dispatchEvent(new CustomEvent('open-annotation-tab', {
                    detail: { text, conceptName, source: 'description' }
                }));
            });
        }

        if (newDel) {
            let armed = false; let timer = null;
            const reset = () => {
                armed = false;
                newDel.classList.remove('danger', 'confirming');
                newDel.removeAttribute('aria-pressed');
                newDel.title = 'Delete description';
                newDel.setAttribute('aria-label', 'Delete description');
                if (timer) { clearTimeout(timer); timer = null; }
            };
            newDel.addEventListener('click', async () => {
                if (!armed) {
                    armed = true;
                    newDel.classList.add('danger', 'confirming');
                    newDel.setAttribute('aria-pressed', 'true');
                    newDel.title = 'Confirm delete';
                    newDel.setAttribute('aria-label', 'Confirm delete');
                    timer = setTimeout(() => reset(), 4000);
                    return;
                }
                statusEl.textContent = 'Deleting...'; newDel.disabled = true;
                try {
                    if (relationId) {
                        const resp = await fetch(`/api/concepts/${encodedId}/texts/${encodeURIComponent(relationId)}`, { method: 'DELETE' });
                        const data = await resp.json().catch(() => ({}));
                        if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    }
                    relationId = null;
                    applyRawDescription('');
                    textarea.value = '';
                    statusEl.textContent = 'Description deleted';
                } catch (err) {
                    statusEl.textContent = `Error: ${err.message}`; newDel.disabled = false; reset();
                    return;
                }
                newDel.disabled = false;
                reset();
            });
        }
        try { if (display) display.dataset.descEvents = '1'; } catch (_) { }
    } catch (e) {
        console.warn('[dynamicTabs] populateTypeDescription failed', e);
    }
}

// Helper: Multi-note section (list/add/edit/delete) using text relations predicate hasNote
async function populateNotesSection(conceptId, suffix) {
    try {
        const containerParent = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (!containerParent) return;
        const existing = document.getElementById(`notesMultiSection_${suffix}`);
        if (existing) {
            try {
                if (typeof existing.__refreshForConcept === 'function') {
                    await existing.__refreshForConcept(conceptId);
                    return;
                }
            } catch (_) { /* ignore */ }
            // Older DOM from previous versions: remove and rebuild to avoid stale concept binding.
            try { existing.remove(); } catch (_) { /* ignore */ }
        }

        let currentConceptId = conceptId;

        const section = document.createElement('div');
        section.id = `notesMultiSection_${suffix}`;
        section.className = 'concept-notes-multi-section';
        section.style.margin = '8px 0 16px 0';
        section.innerHTML = `
                    <div class="notes-multi-header" style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                        <h3 id="notesHeading_${suffix}" style="margin:0;font-size:1rem;color:#374151;">Notes</h3>
                        <button type="button" id="addNoteButton_${suffix}" class="circular-add-button" aria-describedby="notesStatus_${suffix}" aria-label="Add a new note"></button>
                    </div>
                    <div id="notesList_${suffix}" class="notes-list" role="list" aria-labelledby="notesHeading_${suffix}" style="display:flex;flex-direction:column;gap:10px;"></div>
                    <div id="notesStatus_${suffix}" class="notes-status" role="status" aria-live="polite" style="font-size:0.75rem;color:#6b7280;margin-top:4px;"></div>`;
        // Insert near description (after description sections if present, before other fields)
        const anchor = document.getElementById(`typeDescriptionSection_${suffix}`);
        if (anchor && anchor.parentNode) {
            anchor.parentNode.insertBefore(section, anchor.nextSibling);
        } else {
            containerParent.insertBefore(section, containerParent.firstChild);
        }

        const listEl = section.querySelector(`#notesList_${suffix}`);
        const statusEl = section.querySelector(`#notesStatus_${suffix}`);
        const addBtn = section.querySelector(`#addNoteButton_${suffix}`);

        const fetchNotes = async () => {
            statusEl.textContent = 'Loading notes...';
            try {
                const res = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts?predicate=hasNote&limit=200`, {
                    cache: 'no-store',
                    headers: { 'Cache-Control': 'no-cache' }
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
                renderNotes(Array.isArray(data.texts) ? data.texts : []);
                statusEl.textContent = data.count ? `${data.count} note${data.count === 1 ? '' : 's'}` : 'No notes yet.';
            } catch (e) {
                statusEl.textContent = `Failed to load notes: ${e.message}`;
            }
        };

        // Allow callers to reuse this section for another concept without remounting.
        section.__refreshForConcept = async (newConceptId) => {
            if (typeof newConceptId === 'string' && newConceptId.trim()) {
                currentConceptId = newConceptId;
            }
            await fetchNotes();
        };

        const renderNotes = (notes) => {
            listEl.innerHTML = '';
            if (!notes.length) return;
            for (const n of notes) {
                const item = document.createElement('div');
                item.className = 'note-item';
                item.setAttribute('role', 'listitem');
                item.style.border = '1px solid #e5e7eb';
                item.style.background = '#f9fafb';
                item.style.padding = '8px 10px';
                item.style.position = 'relative';
                item.dataset.relationId = n.relation_id;

                // Smart render note text (detect markdown)
                const noteText = n.text || '';
                const isMarkdown = detectMarkdown(noteText);
                let displayHtml; let safeText; let truncated = false;
                safeText = escapeHtml(noteText);
                truncated = safeText.length > 800;
                displayHtml = truncated ? safeText.slice(0, 800) + '…' : safeText || '<i>(empty)</i>';
                const viewClasses = isMarkdown ? 'note-view markdown-rendered' : 'note-view';
                const viewStyle = isMarkdown
                    ? 'white-space:normal;font-size:0.85rem;line-height:1.25;max-height:220px;overflow:auto;'
                    : 'white-space:pre-wrap;font-size:0.85rem;line-height:1.25;max-height:220px;overflow:auto;';
                item.innerHTML = `
                                                 <div class="${viewClasses}" data-full="${safeText}" data-truncated="${truncated ? '1' : '0'}" style="${viewStyle}">${displayHtml}</div>
                   <div class="note-edit hidden" style="margin-top:4px;">
                      <textarea class="note-textarea" style="width:100%;min-height:120px;font-family:monospace;font-size:0.8rem;padding:6px;">${escapeHtml(n.text || '')}</textarea>
                      <div style="margin-top:4px;display:flex;gap:8px;align-items:center;">
                        <button type="button" class="small-btn primary note-save">Save</button>
                        <button type="button" class="small-btn note-cancel">Cancel</button>
                        <span class="note-status" style="font-size:0.7rem;color:#6b7280;"></span>
                      </div>
                   </div>
                   <div class="note-actions text-block-actions">
                             <button type="button" class="round-icon-button note-copy-btn" title="Copy note to clipboard" aria-label="Copy note to clipboard" data-icon="copy" data-keep-title="true"></button>
                             <button type="button" class="round-icon-button note-expand-btn" title="Show more" aria-label="Show full note" data-icon="expand" data-keep-title="true" ${truncated ? '' : 'style="display:none;"'}></button>
                             <button type="button" class="round-icon-button note-annotate-btn" title="Annotate note" aria-label="Annotate note" data-icon="annotate" data-keep-title="true"></button>
                             <button type="button" class="round-icon-button note-edit-btn" title="Edit note" aria-label="Edit note" data-icon="edit" data-keep-title="true"></button>
                             <button type="button" class="round-icon-button note-delete-btn" title="Delete note" aria-label="Delete note" data-icon="delete" data-keep-title="true"></button>
                   </div>`;
                listEl.appendChild(item);

                if (isMarkdown) {
                    const viewEl = item.querySelector('.note-view');
                    if (viewEl) {
                        void renderSmartTextAsync(noteText, true).then((html) => {
                            viewEl.innerHTML = html || '<i>(empty)</i>';
                            // Markdown is injected as HTML; do not preserve whitespace formatting.
                            viewEl.style.whiteSpace = 'normal';
                        }).catch(() => {
                            // Leave plaintext fallback.
                        });
                    }
                }

                wireNoteItem(item, n);
                try {
                    // Adaptive layout: align edge + orientation switch for note actions (mirror description actions)
                    const actions = item.querySelector('.note-actions');
                    const display = item.querySelector('.note-view');
                    if (actions && display && typeof setupAdaptiveActionGroup === 'function') {
                        setupAdaptiveActionGroup(actions, display);
                    }
                } catch (_) { /* ignore individual note adaptive errors */ }
            }
            try { upgradeActionButtonIcons(listEl); } catch (_) { }
        };

        const wireNoteItem = (item, note) => {
            const viewEl = item.querySelector('.note-view');
            const editWrap = item.querySelector('.note-edit');
            const textarea = item.querySelector('.note-textarea');
            const saveBtn = item.querySelector('.note-save');
            const cancelBtn = item.querySelector('.note-cancel');
            const status = item.querySelector('.note-status');
            const copyBtn = item.querySelector('.note-copy-btn');
            const editBtn = item.querySelector('.note-edit-btn');
            const delBtn = item.querySelector('.note-delete-btn');
            const expandBtn = item.querySelector('.note-expand-btn');
            const annotateBtn = item.querySelector('.note-annotate-btn');
            let deleteArmed = false; let deleteTimer = null;
            const resetDelete = () => {
                deleteArmed = false;
                delBtn.classList.remove('danger', 'confirming');
                delBtn.removeAttribute('aria-pressed');
                delBtn.title = 'Delete note';
                delBtn.setAttribute('aria-label', 'Delete note');
                if (deleteTimer) { clearTimeout(deleteTimer); deleteTimer = null; }
            };
            if (expandBtn) {
                expandBtn.addEventListener('click', () => {
                    if (expandBtn.dataset.expanded === '1') {
                        // collapse
                        const full = viewEl.getAttribute('data-full') || '';
                        const truncated = full.length > 800;
                        viewEl.innerHTML = truncated ? escapeHtml(full.slice(0, 800)) + '…' : escapeHtml(full);
                        viewEl.style.maxHeight = '220px';
                        viewEl.style.overflow = 'auto';
                        expandBtn.dataset.icon = 'expand';
                        expandBtn.title = 'Show more';
                        expandBtn.setAttribute('aria-label', 'Show full note');
                        expandBtn.dataset.expanded = '0';
                    } else {
                        viewEl.innerHTML = escapeHtml(viewEl.getAttribute('data-full') || '');
                        viewEl.style.maxHeight = 'none';
                        viewEl.style.overflow = 'visible';
                        expandBtn.dataset.icon = 'collapse';
                        expandBtn.title = 'Show less';
                        expandBtn.setAttribute('aria-label', 'Show less of note');
                        expandBtn.dataset.expanded = '1';
                    }
                });
            }

            if (copyBtn) {
                copyBtn.addEventListener('click', async () => {
                    const text = viewEl.getAttribute('data-full') || viewEl.textContent || '';
                    if (!text.trim()) {
                        showCopyFeedback(copyBtn, 'No note to copy', false);
                        return;
                    }
                    try {
                        await copyToClipboard(text);
                        showCopyFeedback(copyBtn, 'Note copied!', true);
                    } catch (e) {
                        showCopyFeedback(copyBtn, 'Copy failed', false);
                    }
                });
            }

            editBtn.addEventListener('click', () => {
                editWrap.classList.remove('hidden'); editWrap.style.removeProperty('display');
                viewEl.classList.add('hidden'); editBtn.disabled = true; status.textContent = ''; textarea.focus();
            });
            cancelBtn.addEventListener('click', () => {
                editWrap.classList.add('hidden'); viewEl.classList.remove('hidden'); editBtn.disabled = false; status.textContent = ''; textarea.value = note.text || ''; resetDelete();
                if (expandBtn) expandBtn.focus();
            });
            saveBtn.addEventListener('click', async () => {
                const newText = textarea.value.trim();
                if (!newText) { status.textContent = 'Cannot save empty note.'; return; }
                if (newText === (note.text || '')) { status.textContent = 'No changes.'; return; }
                // Simple duplicate warning (same text as another existing note)
                try {
                    const otherTexts = Array.from(document.querySelectorAll(`#notesList_${suffix} .note-item`))
                        .filter(el => el !== item)
                        .map(el => (el.querySelector('.note-view')?.getAttribute('data-full') || '').trim());
                    if (otherTexts.includes(newText.trim())) {
                        status.textContent = 'Warning: duplicate of another note (saving anyway)';
                    }
                } catch (_) { /* ignore */ }
                status.textContent = 'Saving...'; saveBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts/${encodeURIComponent(note.relation_id)}`, {
                        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: newText })
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    note.text = newText; // optimistic sync
                    const safeFull = escapeHtml(newText);
                    viewEl.setAttribute('data-full', safeFull);
                    const truncated = safeFull.length > 800;
                    viewEl.setAttribute('data-truncated', truncated ? '1' : '0');
                    if (truncated) {
                        viewEl.innerHTML = safeFull.slice(0, 800) + '…';
                        viewEl.style.maxHeight = '220px';
                        if (expandBtn) { expandBtn.style.display = 'flex'; expandBtn.dataset.icon = 'expand'; expandBtn.dataset.expanded = '0'; }
                    } else {
                        viewEl.innerHTML = safeFull;
                        if (expandBtn) { expandBtn.style.display = 'none'; }
                    }
                    status.textContent = 'Saved';
                    editWrap.classList.add('hidden'); viewEl.classList.remove('hidden'); editBtn.disabled = false;
                    if (expandBtn) expandBtn.focus(); else editBtn.focus();
                } catch (e) {
                    status.textContent = `Error: ${e.message}`;
                } finally {
                    saveBtn.disabled = false;
                }
            });
            delBtn.addEventListener('click', async () => {
                if (!deleteArmed) {
                    deleteArmed = true;
                    delBtn.classList.add('danger', 'confirming');
                    delBtn.setAttribute('aria-pressed', 'true');
                    delBtn.title = 'Confirm delete';
                    delBtn.setAttribute('aria-label', 'Confirm delete');
                    deleteTimer = setTimeout(() => resetDelete(), 4000);
                    return;
                }
                status.textContent = 'Deleting...'; delBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts/${encodeURIComponent(note.relation_id)}`, { method: 'DELETE' });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    item.remove();
                    statusEl.textContent = 'Note deleted';
                    // Move focus to heading for context after deletion
                    try { document.getElementById(`notesHeading_${suffix}`)?.focus?.(); } catch (_) { }
                } catch (e) {
                    status.textContent = `Error: ${e.message}`; delBtn.disabled = false; resetDelete();
                    return;
                }
                resetDelete();
            });

            if (annotateBtn) {
                annotateBtn.dataset.hasDirectAnnotate = '1';
                annotateBtn.addEventListener('click', () => {
                    const info = dynamicConceptTabs.get(currentConceptId);
                    const conceptName = info?.conceptName || currentConceptId;
                    document.dispatchEvent(new CustomEvent('open-annotation-tab', {
                        detail: { text: note.text || '', conceptName, source: 'note' }
                    }));
                });
            }
        };

        const addNewNoteEditor = () => {
            // Prevent multiple new editors
            if (listEl.querySelector('.note-item.new-note')) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'note-item new-note';
            wrapper.style.border = '1px dashed #9ca3af';
            wrapper.style.padding = '8px 10px';
            wrapper.innerHTML = `
                            <div style="font-size:0.75rem;color:#6b7280;margin-bottom:4px;">New Note</div>
                            <textarea class="new-note-text" aria-label="New note text" style="width:100%;min-height:120px;font-family:monospace;font-size:0.8rem;padding:6px;"></textarea>
                            <div style="margin-top:6px;display:flex;gap:8px;align-items:center;">
                                <button type="button" class="small-btn primary create-note" aria-label="Create note">Create</button>
                                <button type="button" class="small-btn cancel-note" aria-label="Cancel new note">Cancel</button>
                                <span class="create-status" role="status" aria-live="polite" style="font-size:0.7rem;color:#6b7280;"></span>
                            </div>`;
            listEl.insertBefore(wrapper, listEl.firstChild);
            const ta = wrapper.querySelector('.new-note-text');
            const createBtn = wrapper.querySelector('.create-note');
            const cancelBtn = wrapper.querySelector('.cancel-note');
            const cStatus = wrapper.querySelector('.create-status');
            ta.focus();
            cancelBtn.addEventListener('click', () => wrapper.remove());
            createBtn.addEventListener('click', async () => {
                const txt = (ta.value || '').trim();
                if (!txt) { cStatus.textContent = 'Enter text'; return; }
                // Duplicate warning across existing full note texts
                try {
                    const existingFulls = Array.from(document.querySelectorAll(`#notesList_${suffix} .note-item .note-view`)).map(el => (el.getAttribute('data-full') || '').trim());
                    if (existingFulls.includes(txt)) {
                        cStatus.textContent = 'Warning: duplicate of existing note (creating anyway)';
                    }
                } catch (_) { /* ignore */ }
                cStatus.textContent = 'Creating...'; createBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ predicate: 'hasNote', text: txt })
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    wrapper.remove();
                    await fetchNotes();
                    statusEl.textContent = 'Note added';
                } catch (e) {
                    cStatus.textContent = `Error: ${e.message}`; createBtn.disabled = false;
                }
            });
        };

        // Wire add button (replace listener by cloning to avoid duplicates)
        if (addBtn) {
            const cloned = addBtn.cloneNode(true); addBtn.parentNode.replaceChild(cloned, addBtn);
            cloned.addEventListener('click', () => addNewNoteEditor());
        }

        await fetchNotes();
    } catch (e) {
        console.warn('[dynamicTabs] populateNotesSection failed', e);
    }
}

// Helper: Multi-content section (list/add/edit/delete) using text relations predicate hasContent
async function populateContentSection(conceptId, suffix) {
    try {
        const containerParent = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (!containerParent) return;
        const existing = document.getElementById(`contentMultiSection_${suffix}`);
        if (existing) {
            try {
                if (typeof existing.__refreshForConcept === 'function') {
                    await existing.__refreshForConcept(conceptId);
                    return;
                }
            } catch (_) { /* ignore */ }
            // Older DOM from previous versions: remove and rebuild to avoid stale concept binding.
            try { existing.remove(); } catch (_) { /* ignore */ }
        }

        let currentConceptId = conceptId;

        const section = document.createElement('div');
        section.id = `contentMultiSection_${suffix}`;
        section.className = 'concept-content-multi-section';
        section.style.margin = '8px 0 16px 0';
        section.innerHTML = `
                    <div class="content-multi-header" style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                        <h3 id="contentHeading_${suffix}" style="margin:0;font-size:1rem;color:#374151;">Content</h3>
                        <button type="button" id="addContentButton_${suffix}" class="circular-add-button" aria-describedby="contentStatus_${suffix}" aria-label="Add new content"></button>
                    </div>
                    <div id="contentList_${suffix}" class="content-list" role="list" aria-labelledby="contentHeading_${suffix}" style="display:flex;flex-direction:column;gap:10px;"></div>
                    <div id="contentStatus_${suffix}" class="content-status" role="status" aria-live="polite" style="font-size:0.75rem;color:#6b7280;margin-top:4px;"></div>`;
        // Insert after notes section if present, otherwise after description sections
        const notesAnchor = document.getElementById(`notesMultiSection_${suffix}`);
        const descAnchor = document.getElementById(`typeDescriptionSection_${suffix}`);
        if (notesAnchor && notesAnchor.parentNode) {
            notesAnchor.parentNode.insertBefore(section, notesAnchor.nextSibling);
        } else if (descAnchor && descAnchor.parentNode) {
            descAnchor.parentNode.insertBefore(section, descAnchor.nextSibling);
        } else {
            containerParent.insertBefore(section, containerParent.firstChild);
        }

        const listEl = section.querySelector(`#contentList_${suffix}`);
        const statusEl = section.querySelector(`#contentStatus_${suffix}`);
        const addBtn = section.querySelector(`#addContentButton_${suffix}`);

        const fetchContent = async () => {
            statusEl.textContent = 'Loading content...';
            try {
                const res = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts?predicate=hasContent&limit=200`, {
                    cache: 'no-store',
                    headers: { 'Cache-Control': 'no-cache' }
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
                renderContent(Array.isArray(data.texts) ? data.texts : []);
                statusEl.textContent = data.count ? `${data.count} content item${data.count === 1 ? '' : 's'}` : 'No content yet.';
            } catch (e) {
                statusEl.textContent = `Failed to load content: ${e.message}`;
            }
        };

        // Allow callers to reuse this section for another concept without remounting.
        section.__refreshForConcept = async (newConceptId) => {
            if (typeof newConceptId === 'string' && newConceptId.trim()) {
                currentConceptId = newConceptId;
            }
            await fetchContent();
        };

        const renderContent = (contentItems) => {
            listEl.innerHTML = '';
            if (!contentItems.length) return;
            for (const c of contentItems) {
                const item = document.createElement('div');
                item.className = 'content-item';
                item.setAttribute('role', 'listitem');
                item.style.border = '1px solid #e5e7eb';
                item.style.background = '#f8fafc';
                item.style.padding = '8px 10px';
                item.style.position = 'relative';
                item.dataset.relationId = c.relation_id;

                // Smart render content text (detect markdown)
                const contentText = c.text || '';
                const isMarkdown = detectMarkdown(contentText);
                let displayHtml; let safeText; let truncated = false;
                safeText = escapeHtml(contentText);
                truncated = safeText.length > 1000;
                displayHtml = truncated ? safeText.slice(0, 1000) + '…' : safeText || '<i>(empty)</i>';
                const viewClasses = isMarkdown ? 'content-view markdown-rendered' : 'content-view';
                const viewStyle = isMarkdown
                    ? 'white-space:normal;font-size:0.85rem;line-height:1.25;max-height:250px;overflow:auto;'
                    : 'white-space:pre-wrap;font-size:0.85rem;line-height:1.25;max-height:250px;overflow:auto;';
                item.innerHTML = `
                                                 <div class="${viewClasses}" data-full="${safeText}" data-truncated="${truncated ? '1' : '0'}" style="${viewStyle}">${displayHtml}</div>
                   <div class="content-edit hidden" style="margin-top:4px;">
                      <textarea class="content-textarea" style="width:100%;min-height:150px;font-family:monospace;font-size:0.8rem;padding:6px;">${escapeHtml(c.text || '')}</textarea>
                      <div style="margin-top:4px;display:flex;gap:8px;align-items:center;">
                        <button type="button" class="small-btn primary content-save">Save</button>
                        <button type="button" class="small-btn content-cancel">Cancel</button>
                        <span class="content-status" style="font-size:0.7rem;color:#6b7280;"></span>
                      </div>
                   </div>
                   <div class="content-actions text-block-actions">
                             <button type="button" class="round-icon-button content-copy-btn" title="Copy content to clipboard" aria-label="Copy content to clipboard" data-icon="copy"></button>
                             <button type="button" class="round-icon-button content-expand-btn" title="Show more" aria-label="Show full content" data-icon="expand" ${truncated ? '' : 'style="display:none;"'}></button>
                             <button type="button" class="round-icon-button content-annotate-btn" title="Annotate content" aria-label="Annotate content" data-icon="annotate"></button>
                             <button type="button" class="round-icon-button content-edit-btn" title="Edit content" aria-label="Edit content" data-icon="edit"></button>
                             <button type="button" class="round-icon-button content-delete-btn" title="Delete content" aria-label="Delete content" data-icon="delete"></button>
                   </div>`;
                listEl.appendChild(item);

                if (isMarkdown) {
                    const viewEl = item.querySelector('.content-view');
                    if (viewEl) {
                        void renderSmartTextAsync(contentText, true).then((html) => {
                            viewEl.innerHTML = html || '<i>(empty)</i>';
                            // Markdown is injected as HTML; do not preserve whitespace formatting.
                            viewEl.style.whiteSpace = 'normal';
                        }).catch(() => {
                            // Leave plaintext fallback.
                        });
                    }
                }

                wireContentItem(item, c);
                try {
                    // Adaptive layout: align edge + orientation switch for content actions
                    const actions = item.querySelector('.content-actions');
                    const display = item.querySelector('.content-view');
                    if (actions && display && typeof setupAdaptiveActionGroup === 'function') {
                        setupAdaptiveActionGroup(actions, display);
                    }
                } catch (_) { /* ignore individual content adaptive errors */ }
            }
            try { upgradeActionButtonIcons(listEl); } catch (_) { }
        };

        const wireContentItem = (item, content) => {
            const viewEl = item.querySelector('.content-view');
            const editWrap = item.querySelector('.content-edit');
            const textarea = item.querySelector('.content-textarea');
            const saveBtn = item.querySelector('.content-save');
            const cancelBtn = item.querySelector('.content-cancel');
            const status = item.querySelector('.content-status');
            const copyBtn = item.querySelector('.content-copy-btn');
            const editBtn = item.querySelector('.content-edit-btn');
            const delBtn = item.querySelector('.content-delete-btn');
            const expandBtn = item.querySelector('.content-expand-btn');
            const annotateBtn = item.querySelector('.content-annotate-btn');
            let deleteArmed = false; let deleteTimer = null;
            const resetDelete = () => {
                deleteArmed = false;
                delBtn.classList.remove('danger', 'confirming');
                delBtn.removeAttribute('aria-pressed');
                delBtn.title = 'Delete content';
                delBtn.setAttribute('aria-label', 'Delete content');
                if (deleteTimer) { clearTimeout(deleteTimer); deleteTimer = null; }
            };
            if (expandBtn) {
                expandBtn.addEventListener('click', () => {
                    if (expandBtn.dataset.expanded === '1') {
                        // collapse
                        const full = viewEl.getAttribute('data-full') || '';
                        const truncated = full.length > 1000;
                        viewEl.innerHTML = truncated ? escapeHtml(full.slice(0, 1000)) + '…' : escapeHtml(full);
                        viewEl.style.maxHeight = '250px';
                        viewEl.style.overflow = 'auto';
                        expandBtn.dataset.icon = 'expand';
                        expandBtn.title = 'Show more';
                        expandBtn.setAttribute('aria-label', 'Show full content');
                        expandBtn.dataset.expanded = '0';
                    } else {
                        viewEl.innerHTML = escapeHtml(viewEl.getAttribute('data-full') || '');
                        viewEl.style.maxHeight = 'none';
                        viewEl.style.overflow = 'visible';
                        expandBtn.dataset.icon = 'collapse';
                        expandBtn.title = 'Show less';
                        expandBtn.setAttribute('aria-label', 'Show less of content');
                        expandBtn.dataset.expanded = '1';
                    }
                });
            }

            if (copyBtn) {
                copyBtn.addEventListener('click', async () => {
                    const text = viewEl.getAttribute('data-full') || viewEl.textContent || '';
                    if (!text.trim()) {
                        showCopyFeedback(copyBtn, 'No content to copy', false);
                        return;
                    }
                    try {
                        await copyToClipboard(text);
                        showCopyFeedback(copyBtn, 'Content copied!', true);
                    } catch (e) {
                        showCopyFeedback(copyBtn, 'Copy failed', false);
                    }
                });
            }

            editBtn.addEventListener('click', () => {
                editWrap.classList.remove('hidden'); editWrap.style.removeProperty('display');
                viewEl.classList.add('hidden'); editBtn.disabled = true; status.textContent = ''; textarea.focus();
            });
            cancelBtn.addEventListener('click', () => {
                editWrap.classList.add('hidden'); viewEl.classList.remove('hidden'); editBtn.disabled = false; status.textContent = ''; textarea.value = content.text || ''; resetDelete();
                if (expandBtn) expandBtn.focus();
            });
            saveBtn.addEventListener('click', async () => {
                const newText = textarea.value.trim();
                if (!newText) { status.textContent = 'Cannot save empty content.'; return; }
                if (newText === (content.text || '')) { status.textContent = 'No changes.'; return; }
                // Simple duplicate warning (same text as another existing content item)
                try {
                    const otherTexts = Array.from(document.querySelectorAll(`#contentList_${suffix} .content-item`))
                        .filter(el => el !== item)
                        .map(el => (el.querySelector('.content-view')?.getAttribute('data-full') || '').trim());
                    if (otherTexts.includes(newText.trim())) {
                        status.textContent = 'Warning: duplicate of another content item (saving anyway)';
                    }
                } catch (_) { /* ignore */ }
                status.textContent = 'Saving...'; saveBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts/${encodeURIComponent(content.relation_id)}`, {
                        method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: newText })
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    content.text = newText; // optimistic sync
                    const safeFull = escapeHtml(newText);
                    viewEl.setAttribute('data-full', safeFull);
                    const truncated = safeFull.length > 1000;
                    viewEl.setAttribute('data-truncated', truncated ? '1' : '0');
                    if (truncated) {
                        viewEl.innerHTML = safeFull.slice(0, 1000) + '…';
                        viewEl.style.maxHeight = '250px';
                        if (expandBtn) { expandBtn.style.display = 'flex'; expandBtn.dataset.icon = 'expand'; expandBtn.dataset.expanded = '0'; }
                    } else {
                        viewEl.innerHTML = safeFull;
                        if (expandBtn) { expandBtn.style.display = 'none'; }
                    }
                    status.textContent = 'Saved';
                    editWrap.classList.add('hidden'); viewEl.classList.remove('hidden'); editBtn.disabled = false;
                    if (expandBtn) expandBtn.focus(); else editBtn.focus();
                } catch (e) {
                    status.textContent = `Error: ${e.message}`;
                } finally {
                    saveBtn.disabled = false;
                }
            });
            delBtn.addEventListener('click', async () => {
                if (!deleteArmed) {
                    deleteArmed = true;
                    delBtn.classList.add('danger', 'confirming');
                    delBtn.setAttribute('aria-pressed', 'true');
                    delBtn.title = 'Confirm delete';
                    delBtn.setAttribute('aria-label', 'Confirm delete');
                    deleteTimer = setTimeout(() => resetDelete(), 4000);
                    return;
                }
                status.textContent = 'Deleting...'; delBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts/${encodeURIComponent(content.relation_id)}`, { method: 'DELETE' });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    item.remove();
                    statusEl.textContent = 'Content deleted';
                    // Move focus to heading for context after deletion
                    try { document.getElementById(`contentHeading_${suffix}`)?.focus?.(); } catch (_) { }
                } catch (e) {
                    status.textContent = `Error: ${e.message}`; delBtn.disabled = false; resetDelete();
                    return;
                }
                resetDelete();
            });

            if (annotateBtn) {
                annotateBtn.dataset.hasDirectAnnotate = '1';
                annotateBtn.addEventListener('click', () => {
                    const info = dynamicConceptTabs.get(currentConceptId);
                    const conceptName = info?.conceptName || currentConceptId;
                    document.dispatchEvent(new CustomEvent('open-annotation-tab', {
                        detail: { text: content.text || '', conceptName, source: 'content' }
                    }));
                });
            }
        };

        const addNewContentEditor = () => {
            // Prevent multiple new editors
            if (listEl.querySelector('.content-item.new-content')) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'content-item new-content';
            wrapper.style.border = '1px dashed #9ca3af';
            wrapper.style.padding = '8px 10px';
            wrapper.innerHTML = `
                            <div style="font-size:0.75rem;color:#6b7280;margin-bottom:4px;">New Content</div>
                            <textarea class="new-content-text" aria-label="New content text" style="width:100%;min-height:150px;font-family:monospace;font-size:0.8rem;padding:6px;" placeholder="Enter content text (e.g., grant application, call for papers)..."></textarea>
                            <div style="margin-top:6px;display:flex;gap:8px;align-items:center;">
                                <button type="button" class="small-btn primary create-content" aria-label="Create content">Create</button>
                                <button type="button" class="small-btn cancel-content" aria-label="Cancel new content">Cancel</button>
                                <span class="create-status" role="status" aria-live="polite" style="font-size:0.7rem;color:#6b7280;"></span>
                            </div>`;
            listEl.insertBefore(wrapper, listEl.firstChild);
            const ta = wrapper.querySelector('.new-content-text');
            const createBtn = wrapper.querySelector('.create-content');
            const cancelBtn = wrapper.querySelector('.cancel-content');
            const cStatus = wrapper.querySelector('.create-status');
            ta.focus();
            cancelBtn.addEventListener('click', () => wrapper.remove());
            createBtn.addEventListener('click', async () => {
                const txt = (ta.value || '').trim();
                if (!txt) { cStatus.textContent = 'Enter text'; return; }
                // Duplicate warning across existing full content texts
                try {
                    const existingFulls = Array.from(document.querySelectorAll(`#contentList_${suffix} .content-item .content-view`)).map(el => (el.getAttribute('data-full') || '').trim());
                    if (existingFulls.includes(txt)) {
                        cStatus.textContent = 'Warning: duplicate of existing content (creating anyway)';
                    }
                } catch (_) { /* ignore */ }
                cStatus.textContent = 'Creating...'; createBtn.disabled = true;
                try {
                    const resp = await fetch(`/api/concepts/${encodeURIComponent(currentConceptId)}/texts`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ predicate: 'hasContent', text: txt })
                    });
                    const data = await resp.json().catch(() => ({}));
                    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
                    wrapper.remove();
                    await fetchContent();
                    statusEl.textContent = 'Content added';
                } catch (e) {
                    cStatus.textContent = `Error: ${e.message}`; createBtn.disabled = false;
                }
            });
        };

        // Wire add button (replace listener by cloning to avoid duplicates)
        if (addBtn) {
            const cloned = addBtn.cloneNode(true); addBtn.parentNode.replaceChild(cloned, addBtn);
            cloned.addEventListener('click', () => addNewContentEditor());
        }

        await fetchContent();
    } catch (e) {
        console.warn('[dynamicTabs] populateContentSection failed', e);
    }
}

// Export for testing
export { ensureUnifiedDescriptionSection, populateContentSection, populateNotesSection, populateTypeDescription };

// Exported helper for tests: await description population deterministically instead of timing guesses
export async function waitForDescriptionPopulated(conceptId, suffix, timeoutMs = 1500) {
    const display = () => document.getElementById(`typeDescriptionDisplay_${suffix}`);
    if (display() && display().dataset && display().dataset.populated === '1') return true;
    const key = conceptId + '::' + suffix;
    if (!window.__descriptionReadyResolvers) window.__descriptionReadyResolvers = new Map();
    return await new Promise((resolve) => {
        const arr = window.__descriptionReadyResolvers.get(key) || [];
        arr.push(() => resolve(true));
        window.__descriptionReadyResolvers.set(key, arr);
        // Fallback timeout
        setTimeout(() => resolve(false), timeoutMs);
    });
}

// Helper: tailor a Type concept tab presentation
async function adaptTypeConceptTabUI(conceptId, suffix) {
    try {
        // Ensure Start Interaction will operate on the Type concept
        try {
            setCurrentlySelectedConceptId(conceptId);
            setSelectedConceptOriginalName?.(conceptId);
        } catch (_) { /* no-op */ }

        // Update the title to reflect type refinement
        const titleText = document.getElementById(`conceptFormTitleText_${suffix}`) || document.getElementById('conceptFormTitleText');
        if (titleText) titleText.textContent = 'Type Details';

        const step1 = document.getElementById(`conceptStep1_${suffix}`) || document.getElementById('conceptStep1');
        if (step1) {
            // Move the Type Description section to the top of Step 1 for a description-centric layout
            const descSection = document.getElementById(`typeDescriptionSection_${suffix}`) || document.getElementById('typeDescriptionSection');
            if (descSection && descSection.parentNode !== step1) {
                step1.insertBefore(descSection, step1.firstChild);
            }

            // Hide name and notes inputs and their labels
            const nameInput = document.getElementById(`conceptName_${suffix}`) || document.getElementById('conceptName');
            if (nameInput) {
                // Hide its preceding label if present
                const prev = nameInput.previousElementSibling;
                if (prev && prev.tagName === 'LABEL') prev.style.display = 'none';
                nameInput.style.display = 'none';
            }

            // Hide action buttons that are for create/update/delete flows
            const removeBtn = document.getElementById(`removeCurrentConceptButton_${suffix}`) || document.getElementById('removeCurrentConceptButton');
            if (removeBtn) removeBtn.style.display = 'none';
            const saveBtn = document.getElementById(`saveNotesButton_${suffix}`) || document.getElementById('saveNotesButton');
            if (saveBtn) saveBtn.style.display = 'none';
            const addBtn = document.getElementById(`addNewConceptButton_${suffix}`) || document.getElementById('addNewConceptButton');
            if (addBtn) addBtn.style.display = 'none';

            // Ensure Start Interaction remains visible
            const startBtn = document.getElementById(`startInteractionButton_${suffix}`) || document.getElementById('startInteractionButton');
            if (startBtn) startBtn.style.display = 'inline-block';
        }

        // Ensure Instances section is visible for Type tabs (so concept list can be grouped there)
        const instancesSection = document.getElementById(`instancesSection_${suffix}`) || document.getElementById('instancesSection');
        if (instancesSection) instancesSection.style.removeProperty('display');

        // Ensure multi-note section present for Type tabs
        await populateNotesSection(conceptId, suffix);
        // Ensure multi-content section present for Type tabs
        await populateContentSection(conceptId, suffix);

        // Optionally default-render description if toggle button exists (unify behavior)
        try {
            const toggleBtn = document.getElementById(`toggleNotesRenderButton_${suffix}`);
            const descEl = document.getElementById(`conceptDescription_${suffix}`);
            if (toggleBtn && descEl && window.ensureDescriptionRendered) {
                window.ensureDescriptionRendered(suffix);
            }
        } catch (_) { /* no-op */ }
    } catch (e) {
        console.warn('[dynamicTabs] adaptTypeConceptTabUI failed', e);
    }
}
// --- Relationships UI ---
async function initializeRelationshipsUI(conceptId, suffix, kind) {
    try {
        // Show loading indicator immediately
        const content = document.getElementById(`relationshipsContent_${suffix}`) || document.getElementById('relationshipsContent');
        if (content) {
            content.innerHTML = '<div style="color: #999; padding: 12px;">Loading relationships...</div>';
        }

        const container = document.getElementById(`relationshipsSection_${suffix}`) || document.getElementById('relationshipsSection');
        if (!container) return;

        // Simple in-memory caches (module scope) – create if not already
        if (!window.__dynamicTabsCaches) {
            window.__dynamicTabsCaches = { salientPredicates: new Map(), elicitationPredicates: null, elicitationFetchedAt: 0 };
        }
        const caches = window.__dynamicTabsCaches;

        // Populate kind selector based on tab kind
        const kindSelect = document.getElementById(`relationshipKindSelect_${suffix}`) || document.getElementById('relationshipKindSelect');
        const kindsForType = [
            { val: 'is_a_type_of', label: 'is a type of (parent type)' },
            { val: 'has_subtype', label: 'has subtype (child type)' },
            { val: 'related_to', label: 'related to' },
        ];
        const kindsForIndividual = [
            { val: 'is_an_instance_of', label: 'is an instance of (type)' },
            { val: 'related_to', label: 'related to' },
        ];
        const options = (kind === 'individual' || kind === 'unknown') ? kindsForIndividual : kindsForType;
        if (kindSelect) {
            if (!kindSelect.getAttribute('aria-label')) {
                kindSelect.setAttribute('aria-label', 'Relationship kind');
            }
            kindSelect.innerHTML = '';
            for (const opt of options) {
                const o = document.createElement('option');
                o.value = opt.val; o.textContent = opt.label; kindSelect.appendChild(o);
            }
            const appendOptionsWithScopeHeaders = (entry) => {
                if (!kindSelect) return;
                const groups = entry?.predicatesByScope;
                const scopeMeta = {
                    instance: { label: 'Instance scope', title: 'Applies only to this individual (or direct descendants).' },
                    type: { label: 'Type scope', title: 'Inherited from the concept\'s type definition.' },
                    unclassified: { label: 'Unclassified scope', title: 'Scope not yet classified; review inheritance.' }
                };

                if (groups && Object.values(groups).some((items) => Array.isArray(items) && items.length)) {
                    Object.entries(groups).forEach(([scope, items]) => {
                        if (!Array.isArray(items) || items.length === 0) {
                            return;
                        }
                        const meta = scopeMeta[scope] || { label: scope, title: '' };
                        const group = document.createElement('optgroup');
                        group.label = meta.label;
                        if (meta.title) {
                            group.title = meta.title;
                        }
                        items.forEach((item) => {
                            if (!item || !item.id) {
                                return;
                            }
                            const option = document.createElement('option');
                            option.value = item.id;
                            option.textContent = item.label || item.id;
                            option.dataset.isTextPredicate = item.is_text_predicate ? 'true' : 'false';
                            option.dataset.salientScope = scope;
                            const originTypes = Array.isArray(item.origin_types) ? item.origin_types : [];
                            const originLabel = originTypes.length ? `Origin types: ${originTypes.join(', ')}` : 'Origin types unknown';
                            const scopeTitle = meta.title || meta.label;
                            option.title = `${scopeTitle} - ${originLabel}`;
                            group.appendChild(option);
                        });
                        if (group.children.length) {
                            kindSelect.appendChild(group);
                        }
                    });
                    return true;
                }

                const flatList = Array.isArray(entry?.predicates) ? entry.predicates : [];
                if (flatList.length) {
                    const sep = document.createElement('option');
                    sep.disabled = true; sep.textContent = '--- salient predicates ---';
                    kindSelect.appendChild(sep);
                    flatList.forEach((p) => {
                        const option = document.createElement('option');
                        option.value = p.id;
                        option.textContent = p.label || p.id;
                        option.dataset.isTextPredicate = p.is_text_predicate ? 'true' : 'false';
                        option.title = 'Legacy salient predicate (scope split disabled)';
                        kindSelect.appendChild(option);
                    });
                    return true;
                }
                return false;
            };
            const buildCacheEntry = (data) => {
                const entry = {
                    predicates: Array.isArray(data?.predicates) ? data.predicates : [],
                    predicatesByScope: data?.predicates_by_scope || null,
                    rawScopeMap: data?.raw_scope_map || null,
                    fetchedAt: Date.now()
                };
                return entry;
            };
            // If this is a type tab, append dynamic elicitation predicates
            if (kind !== 'individual') {
                // Cache elicitation predicates globally for session (refresh every 5 minutes)
                const now = Date.now();
                const stale = (now - caches.elicitationFetchedAt) > 5 * 60 * 1000;
                if (!caches.elicitationPredicates || stale) {
                    try {
                        const resp = await fetch('/vontology/api/vontology/predicates/elicitation');
                        if (resp.ok) {
                            const data = await resp.json();
                            if (data && data.success && Array.isArray(data.predicates)) {
                                caches.elicitationPredicates = data.predicates;
                                caches.elicitationFetchedAt = now;
                            }
                        }
                    } catch (e) {
                        console.warn('Failed to load elicitation predicates', e);
                    }
                }
                const preds = caches.elicitationPredicates || [];
                if (preds.length) {
                    const sep = document.createElement('option');
                    sep.disabled = true; sep.textContent = '--- elicitation predicates ---';
                    kindSelect.appendChild(sep);
                    for (const p of preds) {
                        const po = document.createElement('option');
                        po.value = p.id;
                        po.textContent = p.label || p.id;
                        // Store metadata about whether this is a text predicate
                        po.dataset.isTextPredicate = p.is_text_predicate ? 'true' : 'false';
                        kindSelect.appendChild(po);
                    }
                }
            } else {
                // Individual tab: append salient predicates aggregated from all its types with caching
                const cached = caches.salientPredicates.get(conceptId);
                if (cached) {
                    if (!appendOptionsWithScopeHeaders(cached)) {
                        const sep = document.createElement('option');
                        sep.disabled = true; sep.textContent = 'No salient predicates';
                        kindSelect.appendChild(sep);
                    }
                } else {
                    // Placeholder while loading
                    const loadingOpt = document.createElement('option');
                    loadingOpt.disabled = true; loadingOpt.textContent = 'Loading salient predicates…';
                    kindSelect.appendChild(loadingOpt);
                    try {
                        const resp = await fetch(`/vontology/api/vontology/predicates/salient?instance_id=${encodeURIComponent(conceptId)}`);
                        if (resp.ok) {
                            const data = await resp.json();
                            if (data && data.success) {
                                const entry = buildCacheEntry(data);
                                if (entry.predicates.length || (entry.predicatesByScope && Object.values(entry.predicatesByScope).some((items) => Array.isArray(items) && items.length))) {
                                    caches.salientPredicates.set(conceptId, entry);
                                }
                                // Remove placeholder
                                kindSelect.removeChild(loadingOpt);
                                if (!appendOptionsWithScopeHeaders(entry)) {
                                    const sep = document.createElement('option');
                                    sep.disabled = true; sep.textContent = 'No salient predicates';
                                    kindSelect.appendChild(sep);
                                }
                            } else {
                                loadingOpt.textContent = 'No salient predicates';
                            }
                        } else {
                            loadingOpt.textContent = 'Salient predicates failed';
                        }
                    } catch (e) {
                        console.warn('Failed to load salient predicates', e);
                        loadingOpt.textContent = 'Salient predicates error';
                    }
                }
            }
        }

        // Wire Add button
        const addBtn = document.getElementById(`relationshipAddButton_${suffix}`) || document.getElementById('relationshipAddButton');
        let targetInput = document.getElementById(`relationshipTargetInput_${suffix}`) || document.getElementById('relationshipTargetInput');
        const statusEl = document.getElementById(`relationshipsStatus_${suffix}`) || document.getElementById('relationshipsStatus');
        if (addBtn && targetInput && kindSelect) {

            // Function to determine if current selection is a text predicate
            const isCurrentSelectionTextPredicate = () => {
                const selectedOption = kindSelect.options[kindSelect.selectedIndex];
                return selectedOption && selectedOption.dataset.isTextPredicate === 'true';
            };

            // Create inline editor for text predicates (similar to names editing)
            const createInlineTextEditor = (chipElement, textItem, predicate, conceptId, suffix) => {
                const originalText = textItem.text || textItem;
                const originalLang = textItem.lang || 'en';

                // Create editing form
                const editForm = document.createElement('div');
                editForm.style.cssText = `
                    display: inline-flex;
                    gap: 4px;
                    align-items: center;
                    background: #fff;
                    padding: 4px 6px;
                    border: 1px solid #3b82f6;
                    border-radius: 4px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                `;

                // Text input
                const textInput = document.createElement('input');
                textInput.type = 'text';
                textInput.value = originalText;
                textInput.style.cssText = `
                    border: none;
                    outline: none;
                    min-width: 120px;
                    font-family: monospace;
                    font-size: inherit;
                    background: transparent;
                `;

                // Language select
                const langSelect = document.createElement('select');
                langSelect.style.cssText = `
                    border: none;
                    outline: none;
                    font-size: 11px;
                    background: transparent;
                `;

                // Populate language options
                const langOptions = [
                    { value: 'en', label: 'en' },
                    { value: 'en-NZ', label: 'en-NZ' },
                    { value: 'en-US', label: 'en-US' },
                    { value: 'fr', label: 'fr' },
                    { value: 'de', label: 'de' },
                    { value: 'es', label: 'es' }
                ];

                for (const lang of langOptions) {
                    const option = document.createElement('option');
                    option.value = lang.value;
                    option.textContent = lang.label;
                    langSelect.appendChild(option);
                }
                langSelect.value = originalLang;

                // Save button
                const saveBtn = document.createElement('button');
                saveBtn.textContent = '✓';
                saveBtn.style.cssText = `
                    border: none;
                    background: #10b981;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 3px;
                    cursor: pointer;
                    font-size: 12px;
                `;

                // Cancel button
                const cancelBtn = document.createElement('button');
                cancelBtn.textContent = '✕';
                cancelBtn.style.cssText = `
                    border: none;
                    background: #ef4444;
                    color: white;
                    padding: 2px 6px;
                    border-radius: 3px;
                    cursor: pointer;
                    font-size: 12px;
                `;

                editForm.appendChild(textInput);
                editForm.appendChild(langSelect);
                editForm.appendChild(saveBtn);
                editForm.appendChild(cancelBtn);

                // Replace chip content with edit form
                const originalContent = chipElement.innerHTML;
                chipElement.innerHTML = '';
                chipElement.appendChild(editForm);

                // Focus the text input
                textInput.focus();
                textInput.select();

                // Save function
                const saveEdit = async () => {
                    const newText = textInput.value.trim();
                    const newLang = langSelect.value;

                    if (!newText) {
                        alert('Text value cannot be empty');
                        return;
                    }

                    try {
                        // First remove the old text relation
                        await fetch(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
                            method: 'DELETE',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ predicate, text: originalText })
                        });

                        // Then add the new text relation
                        const resp = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                predicate,
                                text: newText,
                                lang: newLang,
                                provenance: { source: 'inline_edit' }
                            })
                        });

                        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

                        // Refresh the relationships display
                        await renderRelationships(conceptId, suffix);
                    } catch (e) {
                        alert(`Error saving changes: ${e.message}`);
                        // Restore original content on error
                        chipElement.innerHTML = originalContent;
                    }
                };

                // Cancel function
                const cancelEdit = () => {
                    chipElement.innerHTML = originalContent;
                };

                // Event listeners
                saveBtn.addEventListener('click', saveEdit);
                cancelBtn.addEventListener('click', cancelEdit);

                // Save on Enter, cancel on Escape
                textInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        saveEdit();
                    } else if (e.key === 'Escape') {
                        e.preventDefault();
                        cancelEdit();
                    }
                });

                // Cancel on blur (after a delay to allow button clicks)
                textInput.addEventListener('blur', () => {
                    setTimeout(() => {
                        if (document.activeElement !== saveBtn && document.activeElement !== cancelBtn && document.activeElement !== langSelect) {
                            cancelEdit();
                        }
                    }, 100);
                });
            };

            // Create rich form interface for binary text predicates
            const createTextPredicateForm = () => {
                const formContainer = document.createElement('div');
                formContainer.className = 'text-predicate-form';
                formContainer.style.cssText = `
                    display: inline-flex;
                    gap: 4px;
                    align-items: center;
                    margin-left: 4px;
                    vertical-align: middle;
                `;

                // Text input
                const textInput = document.createElement('input');
                textInput.type = 'text';
                textInput.placeholder = 'Text value (e.g., "example@email.com")';
                textInput.title = 'Enter the text value';
                textInput.style.cssText = `
                    width: 180px;
                    padding: 4px 8px;
                    border: 1px solid #ccc;
                    border-radius: 3px;
                    font-size: 13px;
                    font-family: inherit;
                `;

                // Language select
                const languageSelect = document.createElement('select');
                languageSelect.title = 'Select language';
                languageSelect.style.cssText = `
                    width: 80px;
                    padding: 4px 6px;
                    border: 1px solid #ccc;
                    border-radius: 3px;
                    font-size: 13px;
                    background: white;
                `;

                // Type select
                const typeSelect = document.createElement('select');
                typeSelect.title = 'Select text type';
                typeSelect.style.cssText = `
                    width: 70px;
                    padding: 4px 6px;
                    border: 1px solid #ccc;
                    border-radius: 3px;
                    font-size: 13px;
                    background: white;
                `;                // Populate language options (similar to names form)
                const languageOptions = [
                    { value: 'en', label: 'English' },
                    { value: 'en-NZ', label: 'English (New Zealand)' },
                    { value: 'en-US', label: 'English (US)' },
                    { value: 'fr', label: 'French' },
                    { value: 'de', label: 'German' },
                    { value: 'es', label: 'Spanish' }
                ];

                for (const lang of languageOptions) {
                    const option = document.createElement('option');
                    option.value = lang.value;
                    option.textContent = lang.label;
                    languageSelect.appendChild(option);
                }
                languageSelect.value = 'en'; // Default

                // Populate type options
                const typeOptions = [
                    { value: 'TERM', label: 'Term' },
                    { value: 'EMAIL', label: 'Email' },
                    { value: 'URL', label: 'URL' },
                    { value: 'CODE', label: 'Code' },
                    { value: 'NOTE', label: 'Note' }
                ];

                for (const type of typeOptions) {
                    const option = document.createElement('option');
                    option.value = type.value;
                    option.textContent = type.label;
                    typeSelect.appendChild(option);
                }
                typeSelect.value = 'TERM'; // Default

                formContainer.appendChild(textInput);
                formContainer.appendChild(languageSelect);
                formContainer.appendChild(typeSelect);

                // Store references for easy access
                formContainer._textInput = textInput;
                formContainer._languageSelect = languageSelect;
                formContainer._typeSelect = typeSelect;

                return formContainer;
            };

            // Function to update input interface based on predicate type
            const updateInputForPredicateType = () => {
                const inputContainer = targetInput.parentNode;
                const isTextPredicate = isCurrentSelectionTextPredicate();

                if (isTextPredicate) {
                    // Replace simple input with inline text predicate controls
                    if (!inputContainer.querySelector('.text-predicate-form')) {
                        targetInput.style.display = 'none';
                        const textForm = createTextPredicateForm();
                        // Insert the form right after the target input (inline)
                        targetInput.parentNode.insertBefore(textForm, targetInput.nextSibling);
                    }
                    // Clear any existing search results since they don't apply to text predicates
                    clearResults();
                } else {
                    // Show simple concept input, hide text form
                    targetInput.style.display = 'block';
                    targetInput.placeholder = '#V#target_concept_id';
                    targetInput.title = 'Enter or search for a concept ID';
                    const textForm = inputContainer.querySelector('.text-predicate-form');
                    if (textForm) {
                        textForm.remove();
                    }
                }
            };            // Add event listener to kind select to update input behavior
            kindSelect.addEventListener('change', updateInputForPredicateType);
            // Initialize with current selection
            updateInputForPredicateType();
            // Replace listeners by cloning
            const newBtn = addBtn.cloneNode(true); addBtn.parentNode.replaceChild(newBtn, addBtn);
            newBtn.addEventListener('click', async () => {
                const k = kindSelect.value;
                let tgt, lang, textType;

                statusEl.textContent = '';
                if (!k) { statusEl.textContent = 'Select a relationship kind'; return; }

                if (isCurrentSelectionTextPredicate()) {
                    // Extract data from rich text form
                    const textForm = targetInput.parentNode.querySelector('.text-predicate-form');
                    if (!textForm) { statusEl.textContent = 'Text form not found'; return; }

                    tgt = (textForm._textInput.value || '').trim();
                    lang = textForm._languageSelect.value || 'en';
                    textType = textForm._typeSelect.value || 'TERM';

                    if (!tgt) { statusEl.textContent = 'Enter a text value'; return; }
                } else {
                    // Extract data from simple concept input
                    tgt = (targetInput.value || '').trim();
                    if (!tgt) { statusEl.textContent = 'Select kind and target id'; return; }
                    if (tgt === conceptId) { statusEl.textContent = 'Target cannot equal source'; return; }
                }

                try {
                    statusEl.textContent = 'Adding...';

                    if (isCurrentSelectionTextPredicate()) {
                        // Use text relations API for binary text predicates with rich form data
                        const resp = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                predicate: k,
                                text: tgt,
                                lang: lang,
                                provenance: {
                                    source: 'relationship_form',
                                    text_type: textType
                                }
                            })
                        });
                        const data = await resp.json().catch(() => ({}));
                        console.log('[addRelationship] Text relations API response:', { status: resp.status, data });
                        if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);

                        // Clear the rich form
                        const textForm = targetInput.parentNode.querySelector('.text-predicate-form');
                        if (textForm) {
                            textForm._textInput.value = '';
                            textForm._languageSelect.value = 'en';
                            textForm._typeSelect.value = 'TERM';
                        }
                    } else {
                        // Use traditional relationships API for concept predicates
                        const resp = await fetch('/vontology/api/vontology/relationships/add', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ source_id: conceptId, kind: k, target_id: tgt })
                        });
                        const data = await resp.json().catch(() => ({}));
                        if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);

                        // Clear the simple input
                        targetInput.value = '';
                    }

                    statusEl.textContent = 'Added';
                    await renderRelationships(conceptId, suffix, kind);
                } catch (e) {
                    statusEl.textContent = `Error: ${e.message}`;
                }
            });

            // Enhance the target input with a search dropdown (reuse Vontology search)
            // Clear any prior wiring by cloning the input to avoid duplicate listeners on re-init
            const clonedInput = targetInput.cloneNode(true);
            if (targetInput.parentNode) targetInput.parentNode.replaceChild(clonedInput, targetInput);
            targetInput = clonedInput;

            // Create/ensure a results container next to the input
            const existingResults = document.getElementById(`relationshipSearchResults_${suffix}`) || document.getElementById('relationshipSearchResults');
            if (existingResults && existingResults.parentNode) existingResults.parentNode.removeChild(existingResults);
            const results = document.createElement('div');
            results.id = `relationshipSearchResults_${suffix}`;
            results.className = 'vontology-search-results';
            // Position under the input
            results.style.position = 'absolute';
            results.style.zIndex = '1000';
            // Wrap input in a relatively positioned container to anchor dropdown
            if (!targetInput.parentElement.style.position) {
                targetInput.parentElement.style.position = 'relative';
            }
            targetInput.parentElement.appendChild(results);

            // Local state
            const state = { items: [], activeIndex: -1, ac: null };

            const debounce = (fn, wait) => {
                let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
            };

            const clearResults = () => {
                state.items = []; state.activeIndex = -1; results.classList.remove('open'); results.innerHTML = '';
            };

            const setActive = (idx) => {
                state.activeIndex = idx;
                results.querySelectorAll('.vontology-search-item').forEach((el, i) => {
                    if (i === idx) el.classList.add('active'); else el.classList.remove('active');
                });
            };

            const render = (items) => {
                results.innerHTML = '';
                if (!items.length) { results.classList.remove('open'); return; }
                const list = document.createElement('div');
                list.setAttribute('role', 'listbox');
                items.forEach((it, idx) => {
                    const row = document.createElement('div');
                    row.className = 'vontology-search-item'; row.setAttribute('role', 'option'); row.dataset.index = String(idx);
                    const name = document.createElement('span'); name.className = 'vontology-search-item-name'; name.textContent = it.name || it.id; name.title = `${it.name || it.id} — ${it.id}`;
                    const kindBadge = document.createElement('span');
                    // Use backend's three-way classification directly
                    const badgeKind = it.kind; // 'type', 'predicate', or 'individual' from backend
                    const badgeText = it.kind === 'predicate' ? 'Predicate' : (it.kind === 'individual' ? 'Individual' : 'Type');
                    kindBadge.className = `vontology-search-item-kind ${badgeKind}`;
                    kindBadge.textContent = badgeText;
                    row.appendChild(name); row.appendChild(kindBadge);
                    row.addEventListener('mouseenter', () => setActive(idx));
                    row.addEventListener('mouseleave', () => setActive(-1));
                    row.addEventListener('click', () => {
                        targetInput.value = it.id; clearResults(); targetInput.focus();
                    });
                    list.appendChild(row);
                });
                results.appendChild(list);
                if (items.length) results.classList.add('open');
            };

            const performSearch = async (q) => {
                try { state.ac?.abort?.(); } catch (_) { }
                const ac = new AbortController(); state.ac = ac;
                const k = kindSelect.value;
                // Determine whether to include individuals in autocomplete results.
                // Requirement: On individual tabs, relation target autocomplete should allow other individuals as well as types
                // (except for strictly type-target structural relations like is_an_instance_of).
                // We also always allow individuals for 'related_to' and dynamic predicate kinds (#V#...).
                const includeIndividuals = (() => {
                    if (k === 'related_to') return true; // symmetric free-form relation
                    if (k.startsWith('#V#')) return true; // dynamic predicate (salient / elicitation)
                    if (kind === 'individual') {
                        // On an individual tab, allow individual targets for most predicates except those that must point to types.
                        if (k === 'is_an_instance_of') return false; // must target types
                        return true; // default allow
                    }
                    return false; // type tabs keep prior conservative behavior
                })();
                const url = `/vontology/api/vontology/search?q=${encodeURIComponent(q)}&limit=12&fallback_substring=true${includeIndividuals ? '&include_individuals=true' : ''}`;
                try {
                    const resp = await fetch(url, { signal: ac.signal });
                    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                    const data = await resp.json();
                    let items = Array.isArray(data?.results) ? data.results : [];
                    // Reorder to prefer exact match and shorter names
                    const qc = q ? q.toLowerCase() : '';
                    if (qc && items.length) {
                        items = items.slice();
                        items.sort((a, b) => {
                            const aName = (a.name || a.id || '').toLowerCase();
                            const bName = (b.name || b.id || '').toLowerCase();
                            const aExact = (aName === qc) ? 0 : 1;
                            const bExact = (bName === qc) ? 0 : 1;
                            if (aExact !== bExact) return aExact - bExact;
                            if (aName.length !== bName.length) return aName.length - bName.length;
                            const aPrefix = aName.startsWith(qc) ? 0 : 1;
                            const bPrefix = bName.startsWith(qc) ? 0 : 1;
                            if (aPrefix !== bPrefix) return aPrefix - bPrefix;
                            return 0;
                        });
                    }
                    state.items = items;
                    render(state.items);
                } catch (e) {
                    if (e?.name === 'AbortError') return; clearResults();
                }
            };

            const debounced = debounce(async () => {
                // Only perform search if current selection is not a text predicate
                if (isCurrentSelectionTextPredicate()) return;
                const q = targetInput.value.trim(); if (!q) { clearResults(); return; } await performSearch(q);
            }, 200);

            targetInput.addEventListener('input', (e) => {
                // Clear results immediately if this is a text predicate
                if (isCurrentSelectionTextPredicate()) {
                    clearResults();
                } else {
                    debounced(e);
                }
            });
            targetInput.addEventListener('focus', () => {
                // Only show results if not a text predicate and we have results
                if (!isCurrentSelectionTextPredicate() && state.items.length) {
                    results.classList.add('open');
                }
            });
            targetInput.addEventListener('keydown', (e) => {
                // Concept search navigation only applies to non-text predicates
                if (isCurrentSelectionTextPredicate() || !state.items.length) return;
                if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(state.activeIndex + 1, state.items.length - 1)); }
                else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(state.activeIndex - 1, 0)); }
                else if (e.key === 'Enter') {
                    e.preventDefault();
                    const q = (targetInput.value || '').trim().toLowerCase();
                    if (q && state.items && state.items.length) {
                        const exact = state.items.find(it => ((it.name || it.id || '').toLowerCase() === q));
                        if (exact) { targetInput.value = exact.id; clearResults(); return; }
                    }
                    // No exact match: pick the top-ranked result (state.items[0])
                    if (state.items && state.items.length) {
                        targetInput.value = state.items[0].id; clearResults(); return;
                    }
                }
                else if (e.key === 'Escape') { e.preventDefault(); clearResults(); targetInput.blur(); }
            });

            // Close on outside click
            const onDocClick = (ev) => { if (!results.contains(ev.target) && ev.target !== targetInput) clearResults(); };
            document.addEventListener('click', onDocClick);

            // Clear results when kind changes (filter set changes)
            kindSelect.addEventListener('change', () => clearResults());
        }

        // Initial render
        await renderRelationships(conceptId, suffix, kind);
    } catch (e) {
        console.warn('[dynamicTabs] initializeRelationshipsUI failed', e);
    }
}

// Metadata cache for concept kind and display names (relationships section)
const relationshipsConceptMetadataCache = new Map();
async function getConceptMetadata(conceptId) {
    if (relationshipsConceptMetadataCache.has(conceptId)) {
        return relationshipsConceptMetadataCache.get(conceptId);
    }
    try {
        const resp = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}`);
        if (resp.ok) {
            const data = await resp.json();
            const metadata = {
                kind: data.kind || 'individual',
                name: data.display_name || conceptId
            };
            relationshipsConceptMetadataCache.set(conceptId, metadata);
            return metadata;
        }
    } catch (err) {
        console.warn('[dynamicTabs] Failed to fetch metadata for', conceptId, err);
    }
    return { kind: 'individual', name: conceptId };
}

async function renderRelationships(conceptId, suffix, kind) {
    const content = document.getElementById(`relationshipsContent_${suffix}`) || document.getElementById('relationshipsContent');
    if (!content) return;
    content.innerHTML = '<div>Loading relationships...</div>';
    try {
        // Fetch both concept relationships and available predicates to determine which are text predicates
        const [relRes, elicitationRes, salientRes] = await Promise.all([
            fetch(`/vontology/api/vontology/relationships?identifier=${encodeURIComponent(conceptId)}`),
            fetch(`/vontology/api/vontology/predicates/elicitation?instance_id=${encodeURIComponent(conceptId)}`),
            fetch(`/vontology/api/vontology/predicates/salient?instance_id=${encodeURIComponent(conceptId)}`)
        ]);

        if (!relRes.ok) throw new Error(`HTTP ${relRes.status}`);
        const relData = await relRes.json();
        const rel = relData && relData.relationships ? relData.relationships : {};

        // Build a map of predicates to their text/concept type
        const predicateTypeMap = new Map();

        console.log('[renderRelationships] Building predicateTypeMap...');
        if (elicitationRes.ok) {
            const elicitationData = await elicitationRes.json();
            console.log('[renderRelationships] elicitationData:', elicitationData);
            if (elicitationData.predicates) {
                elicitationData.predicates.forEach((pred, index) => {
                    console.log(`[renderRelationships] elicitation predicate ${index}:`, pred);
                    console.log('[renderRelationships] elicitation predicate:', pred.id, 'is_text:', pred.is_text_predicate);
                    predicateTypeMap.set(pred.id, pred.is_text_predicate || false);
                });
            }
        }

        if (salientRes.ok) {
            const salientData = await salientRes.json();
            console.log('[renderRelationships] salientData:', salientData);
            if (salientData.predicates) {
                salientData.predicates.forEach((pred, index) => {
                    console.log(`[renderRelationships] salient predicate ${index}:`, pred);
                    console.log('[renderRelationships] salient predicate:', pred.id, 'is_text:', pred.is_text_predicate);
                    predicateTypeMap.set(pred.id, pred.is_text_predicate || false);
                });
            }
        }

        console.log('[renderRelationships] Final predicateTypeMap:', predicateTypeMap);

        const sections = [
            { key: 'is_a_type_of', title: 'Is a type of' },
            { key: 'has_subtype', title: 'Has subtype' },
            { key: 'is_an_instance_of', title: 'Is an instance of' },
            // { key: 'has_instance', title: 'Has instance' }, // handled elsewhere
            { key: 'related_to', title: 'Related to' },
        ];
        const structuralKeys = new Set(sections.map(s => s.key).concat(['has_instance']));

        const wrapper = document.createElement('div');
        wrapper.className = 'relationships-wrapper';

        for (const s of sections) {
            const arr = Array.isArray(rel[s.key]) ? rel[s.key] : [];
            if (!arr.length) continue;
            const div = document.createElement('div');
            div.className = 'relationship-group';

            // Create predicate header as a clickable cartouche
            const h = document.createElement('h4');
            h.style.margin = '6px 0';
            h.style.display = 'inline-block';

            const predicateChip = document.createElement('span');
            predicateChip.className = 'concept-cartouche predicate';
            predicateChip.textContent = s.title;
            predicateChip.style.cursor = 'pointer';
            predicateChip.style.fontSize = '0.875rem';
            predicateChip.style.fontWeight = '600';
            predicateChip.title = `Open ${s.key}`;

            // Make predicate header clickable to open concept tab
            predicateChip.addEventListener('click', () => {
                try {
                    const evt = new CustomEvent('open-concept-tab', {
                        detail: { conceptId: s.key, conceptName: s.title, kind: 'predicate', activate: true }
                    });
                    document.dispatchEvent(evt);
                } catch (e) {
                    console.warn('[dynamicTabs] Failed to open predicate tab', e);
                }
            });

            h.appendChild(predicateChip);
            const list = document.createElement('div'); list.className = 'relationship-list'; list.style.display = 'flex'; list.style.flexWrap = 'wrap'; list.style.gap = '6px';

            // Backend now provides kind alongside name, eliminating N metadata fetches
            // Use backend-provided metadata with fallback to cache for backward compatibility
            for (let i = 0; i < arr.length; i++) {
                const item = arr[i];
                // Prefer backend-provided kind, fall back to fetching if not present (backward compatibility)
                const metadata = item.kind
                    ? { kind: item.kind, name: item.name || item.id }
                    : await getConceptMetadata(item.id);

                const chip = document.createElement('span');
                chip.className = 'relationship-chip';
                chip.style.display = 'inline-flex';
                chip.style.alignItems = 'center';
                chip.style.gap = '4px';
                chip.style.padding = '2px 8px';
                chip.style.border = '1px solid #ddd';
                chip.style.borderRadius = '12px';
                chip.style.background = '#f9fafb';
                chip.title = item.id;

                // Clickable name with cartouche styling based on kind
                const name = document.createElement('span');
                name.className = `concept-cartouche ${metadata.kind}`;
                name.textContent = item.name || item.id;
                name.style.cursor = 'pointer';
                name.title = `Open ${item.name || item.id}`;

                // Bold the most salient type for "is a type of" relationships
                if (s.key === 'is_a_type_of' && item.is_most_salient) {
                    name.style.fontWeight = 'bold';
                    name.style.color = '#059669'; // Slightly different green color for emphasis
                    name.title += ' (Most Salient Type)';
                    chip.style.border = '2px solid #059669'; // Thicker border for salient type
                    chip.style.background = '#ecfdf5'; // Light green background
                }
                name.addEventListener('click', () => {
                    try {
                        // Use backend's kind from metadata
                        const evt = new CustomEvent('open-concept-tab', {
                            detail: { conceptId: item.id, conceptName: item.name || item.id, kind: metadata.kind, activate: true }
                        });
                        document.dispatchEvent(evt);
                    } catch (e) {
                        console.warn('[dynamicTabs] Failed to open related concept tab', e);
                    }
                });
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.className = 'chip-remove';
                remove.textContent = '×';
                remove.title = `Remove ${s.title} → ${item.name || item.id}`;
                // Inline fallbacks to neutralize global button styles
                remove.style.border = 'none';
                remove.style.background = 'transparent';
                remove.style.cursor = 'pointer';
                remove.style.color = '#b91c1c';
                remove.style.boxShadow = 'none';
                remove.style.transform = 'none';
                remove.addEventListener('click', async () => {
                    try {
                        const resp = await fetch('/vontology/api/vontology/relationships/remove', {
                            method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ source_id: conceptId, kind: s.key, target_id: item.id })
                        });
                        const d = await resp.json().catch(() => ({}));
                        if (!resp.ok || d.error) throw new Error(d.error || `HTTP ${resp.status}`);
                        await renderRelationships(conceptId, suffix, kind);
                    } catch (e) {
                        console.warn('[dynamicTabs] remove relationship failed', e);
                        alert(`Failed to remove relationship: ${e.message}`);
                    }
                });
                chip.appendChild(name); chip.appendChild(remove); list.appendChild(chip);
            }
            div.appendChild(h); div.appendChild(list); wrapper.appendChild(div);
        }

        // Handle dynamic predicates - both concept relationships and text predicates
        const dynamicConceptKeys = Object.keys(rel)
            .filter(k => !structuralKeys.has(k) && Array.isArray(rel[k]) && rel[k].length && !predicateTypeMap.get(k));
        console.log('[renderRelationships] dynamicConceptKeys:', dynamicConceptKeys);

        // Fetch text predicates that we know are text predicates
        const textPredicatePromises = [];
        const textPredicateKeys = [];

        for (const [predicate, isText] of predicateTypeMap.entries()) {
            if (isText) {
                textPredicateKeys.push(predicate);
                textPredicatePromises.push(
                    fetch(`/api/concepts/${encodeURIComponent(conceptId)}/texts?predicate=${encodeURIComponent(predicate)}&limit=200`)
                        .then(res => res.ok ? res.json() : { texts: [] })
                        .then(data => ({ predicate, texts: data.texts || [] }))
                        .catch(() => ({ predicate, texts: [] }))
                );
            }
        }

        const textPredicateResults = await Promise.all(textPredicatePromises);

        // Debug: Log what we got from text relations API
        console.log('[renderRelationships] textPredicateResults:', textPredicateResults);

        // Combine all dynamic predicates (concept + text)
        const allDynamicKeys = [...dynamicConceptKeys, ...textPredicateKeys].sort();
        console.log('[renderRelationships] allDynamicKeys:', allDynamicKeys);

        for (const dk of allDynamicKeys) {
            const isTextPredicate = predicateTypeMap.get(dk) || false;
            let arr = [];

            if (isTextPredicate) {
                // Get full text objects from text relations API result
                // textPredicateResults contains the API responses which have {texts: [...], count: N} structure
                const textResultIndex = textPredicateKeys.indexOf(dk);
                const textResult = textPredicateResults[textResultIndex];
                console.log(`[renderRelationships] Text result for ${dk}:`, textResult);
                arr = textResult && textResult.texts ? textResult.texts : [];
            } else {
                // Get concept relationships from relationships API
                arr = rel[dk] || [];
            }

            if (!arr.length) continue;

            const div = document.createElement('div');
            div.className = 'relationship-group';
            const titleText = dk.startsWith('#V#') ? dk.replace('#V#', '').replace(/_/g, ' ') : dk;

            // Create dynamic predicate header as a clickable cartouche
            const h = document.createElement('h4');
            h.style.margin = '6px 0';
            h.style.display = 'inline-block';

            const predicateChip = document.createElement('span');
            predicateChip.className = 'concept-cartouche predicate';
            predicateChip.textContent = titleText;
            predicateChip.style.cursor = 'pointer';
            predicateChip.style.fontSize = '0.875rem';
            predicateChip.style.fontWeight = '600';
            predicateChip.title = `Open ${dk}`;

            // Make dynamic predicate header clickable
            predicateChip.addEventListener('click', () => {
                try {
                    const evt = new CustomEvent('open-concept-tab', {
                        detail: { conceptId: dk, conceptName: titleText, kind: 'predicate', activate: true }
                    });
                    document.dispatchEvent(evt);
                } catch (e) {
                    console.warn('[dynamicTabs] Failed to open predicate tab', e);
                }
            });

            h.appendChild(predicateChip);
            const list = document.createElement('div'); list.className = 'relationship-list'; list.style.display = 'flex'; list.style.flexWrap = 'wrap'; list.style.gap = '6px';

            // Backend now provides kind alongside name for concept predicates, eliminating N metadata fetches
            // For text predicates, no metadata needed
            for (let itemIdx = 0; itemIdx < arr.length; itemIdx++) {
                const item = arr[itemIdx];
                // For concept predicates: prefer backend-provided kind, fall back to fetching if not present
                // For text predicates: metadata is null
                const metadata = !isTextPredicate
                    ? (item.kind
                        ? { kind: item.kind, name: item.name || item.id }
                        : await getConceptMetadata(item.id))
                    : null;

                const chip = document.createElement('span');
                chip.className = 'relationship-chip';
                chip.style.display = 'inline-flex'; chip.style.alignItems = 'center'; chip.style.gap = '4px'; chip.style.padding = '2px 8px';
                chip.style.border = '1px solid #ddd'; chip.style.borderRadius = '12px'; chip.style.background = '#f9fafb';

                const name = document.createElement('span');

                if (isTextPredicate) {
                    // Handle text predicate: item is a text object with text, lang, predicate, etc.
                    console.log('[renderRelationships] Processing text predicate item:', item);

                    const textValue = item.text || item;
                    const language = item.lang || 'en';

                    chip.title = `Text value: ${textValue} (${language})`;

                    // Create main text span - clickable for editing
                    name.textContent = textValue;
                    name.style.color = '#374151';
                    name.style.cursor = 'pointer';
                    name.style.fontFamily = 'monospace';
                    name.title = 'Click to edit text value';

                    // Add click-to-edit functionality
                    name.addEventListener('click', () => {
                        createInlineTextEditor(chip, item, dk, conceptId, suffix);
                    });

                    // Add language badge similar to names display
                    const badge = document.createElement('span');
                    badge.textContent = language;
                    badge.style.fontSize = '0.75rem';
                    badge.style.color = '#6b7280';
                    badge.style.backgroundColor = '#f3f4f6';
                    badge.style.padding = '1px 4px';
                    badge.style.borderRadius = '4px';
                    badge.style.marginLeft = '4px';
                    badge.style.fontFamily = 'monospace';

                    // Add both text and badge to the chip
                    chip.appendChild(name);
                    chip.appendChild(badge);
                } else {
                    // Handle concept predicate: item has id and name properties
                    chip.title = item.id;
                    name.className = `concept-cartouche ${metadata.kind}`;
                    name.textContent = item.name || item.id;
                    name.style.cursor = 'pointer';
                    name.addEventListener('click', () => {
                        // Use backend's kind from metadata
                        const evt = new CustomEvent('open-concept-tab', { detail: { conceptId: item.id, conceptName: item.name || item.id, kind: metadata.kind, activate: true } });
                        document.dispatchEvent(evt);
                    });
                }

                const remove = document.createElement('button');
                remove.type = 'button'; remove.className = 'chip-remove'; remove.textContent = '×';
                remove.title = `Remove ${titleText} → ${isTextPredicate ? (item.text || item) : (item.name || item.id)}`;
                remove.style.border = 'none'; remove.style.background = 'transparent'; remove.style.cursor = 'pointer'; remove.style.color = '#b91c1c';
                remove.addEventListener('click', async () => {
                    try {
                        if (isTextPredicate) {
                            // Use text relations API for binary text predicates
                            const textValue = item.text || item;
                            const resp = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}/texts`, {
                                method: 'DELETE', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ predicate: dk, text: textValue })
                            });
                            const d = await resp.json().catch(() => ({}));
                            if (!resp.ok || d.error) throw new Error(d.error || `HTTP ${resp.status}`);
                        } else {
                            // Use relationships API for concept predicates
                            const resp = await fetch('/vontology/api/vontology/relationships/remove', {
                                method: 'POST', headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ source_id: conceptId, kind: dk, target_id: item.id })
                            });
                            const d = await resp.json().catch(() => ({}));
                            if (!resp.ok || d.error) throw new Error(d.error || `HTTP ${resp.status}`);
                        }
                        await renderRelationships(conceptId, suffix, kind);
                    } catch (e) {
                        console.warn('[dynamicTabs] remove dynamic relationship failed', e);
                        alert(`Failed to remove relationship: ${e.message}`);
                    }
                });

                // For concept predicates, append name; for text predicates, name and badge are already appended
                if (!isTextPredicate) {
                    chip.appendChild(name);
                }
                chip.appendChild(remove);
                list.appendChild(chip);
            }
            div.appendChild(h); div.appendChild(list); wrapper.appendChild(div);
        }

        content.innerHTML = '';
        if (wrapper.children.length === 0) content.innerHTML = '<i>No relationships yet.</i>'; else content.appendChild(wrapper);
    } catch (e) {
        content.innerHTML = `<span style="color:red">Failed to load relationships (${e.message})</span>`;
    }
}

// Helper: Add analysis and relationship buttons next to JSON button
function attachAnalysisButtons(headerDiv, conceptId, kind) {
    try {
        if (!headerDiv || !conceptId) return;

        // Avoid duplicates
        if (headerDiv.querySelector('.analysis-buttons-group')) return;

        // Create container for the new buttons
        const buttonGroup = document.createElement('div');
        buttonGroup.className = 'analysis-buttons-group';
        buttonGroup.style.display = 'flex';
        buttonGroup.style.gap = '4px';
        buttonGroup.style.marginLeft = '8px';

        // 1. Flag Toggle Button
        const flagBtn = document.createElement('button');
        flagBtn.className = 'analysis-flag-button';
        flagBtn.title = 'Toggle analysis flag';
        flagBtn.setAttribute('aria-label', 'Toggle analysis flag for later analysis');
        flagBtn.setAttribute('data-keep-title', 'true');
        flagBtn.innerHTML = '🏴'; // Flag emoji
        flagBtn.style.padding = '2px 6px';
        flagBtn.style.border = '1px solid #d1d5db';
        flagBtn.style.borderRadius = '4px';
        flagBtn.style.background = '#f9fafb';
        flagBtn.style.color = '#374151';
        flagBtn.style.cursor = 'pointer';
        flagBtn.style.fontSize = '0.9rem';

        // 2. Organization Relation Button
        const orgBtn = document.createElement('button');
        orgBtn.className = 'org-relation-button';
        orgBtn.title = 'Toggle organization relation';
        orgBtn.setAttribute('aria-label', 'Toggle specific_to_organisation relationship');
        orgBtn.setAttribute('data-keep-title', 'true');
        orgBtn.innerHTML = '🏢'; // Building emoji
        orgBtn.style.padding = '2px 6px';
        orgBtn.style.border = '1px solid #d1d5db';
        orgBtn.style.borderRadius = '4px';
        orgBtn.style.background = '#f9fafb';
        orgBtn.style.color = '#374151';
        orgBtn.style.cursor = 'pointer';
        orgBtn.style.fontSize = '0.9rem';

        // 3. User Relation Button
        const userBtn = document.createElement('button');
        userBtn.className = 'user-relation-button';
        userBtn.title = 'Toggle user relation';
        userBtn.setAttribute('aria-label', 'Toggle specific_to_user relationship');
        userBtn.setAttribute('data-keep-title', 'true');
        userBtn.innerHTML = '👤'; // Person emoji
        userBtn.style.padding = '2px 6px';
        userBtn.style.border = '1px solid #d1d5db';
        userBtn.style.borderRadius = '4px';
        userBtn.style.background = '#f9fafb';
        userBtn.style.color = '#374151';
        userBtn.style.cursor = 'pointer';
        userBtn.style.fontSize = '0.9rem';

        // Load initial states
        loadButtonStates(conceptId, flagBtn, orgBtn, userBtn);

        // Event listeners
        flagBtn.addEventListener('click', () => toggleConceptFlag(conceptId, flagBtn));
        orgBtn.addEventListener('click', () => toggleOrganizationRelation(conceptId, orgBtn));
        userBtn.addEventListener('click', () => toggleUserRelation(conceptId, userBtn));

        // Add buttons to group
        buttonGroup.appendChild(flagBtn);
        buttonGroup.appendChild(orgBtn);
        buttonGroup.appendChild(userBtn);

        // Add to header
        headerDiv.appendChild(buttonGroup);

        // After analysis buttons, attach delete concept button (unified endpoint)
        attachDeleteConceptButton(headerDiv, conceptId, kind);

    } catch (e) {
        console.warn('[dynamicTabs] attachAnalysisButtons failed', e);
    }
}

// Helper: attach delete concept button to header
function attachDeleteConceptButton(headerDiv, conceptId, kind) {
    try {
        if (!headerDiv || !conceptId) return;
        if (headerDiv.querySelector('.delete-concept-button')) return; // already added
        const btn = document.createElement('button');
        btn.className = 'delete-concept-button';
        btn.title = 'Delete this concept';
        btn.setAttribute('aria-label', 'Delete this concept');
        btn.setAttribute('data-keep-title', 'true');
        btn.textContent = '🗑️';
        btn.style.padding = '2px 6px';
        btn.style.border = '1px solid #d1d5db';
        btn.style.borderRadius = '4px';
        btn.style.background = '#fef2f2';
        btn.style.color = '#991b1b';
        btn.style.cursor = 'pointer';
        btn.style.fontSize = '0.9rem';
        btn.style.marginLeft = '4px';
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm('Permanently delete this concept? This cannot be undone.')) return;
            const originalText = btn.textContent;
            btn.disabled = true; btn.textContent = '…';
            try {
                const url = `/vontology/api/vontology/node?concept_id=${encodeURIComponent(conceptId)}&simulate=0`;
                const resp = await fetch(url, { method: 'DELETE' });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok || !data.success) throw new Error(data.error || data.message || `HTTP ${resp.status}`);
                // Dispatch global event so other modules (tree, tabs) react
                // Defer dispatch to next microtask to ensure any late listeners attach
                Promise.resolve().then(() => {
                    try {
                        const evt = new CustomEvent('concept-deleted', { detail: { conceptId, kind, correlation_id: data.correlation_id } });
                        document.dispatchEvent(evt);
                    } catch (_) { /* no-op */ }
                });
                alert(`Deleted concept ${conceptId}`);
            } catch (err) {
                console.warn('[dynamicTabs] delete concept failed', err);
                alert(`Delete failed: ${err.message}`);
            } finally {
                btn.disabled = false; btn.textContent = originalText;
            }
        });
        headerDiv.appendChild(btn);
    } catch (e) {
        console.warn('[dynamicTabs] attachDeleteConceptButton failed', e);
    }
}

// Expose for tests (harmless in production; allows manual invocation in integration tests)
if (typeof window !== 'undefined') {
    try { window.attachDeleteConceptButton = attachDeleteConceptButton; } catch (_) { /* no-op */ }
}

// Load initial button states from concept data
async function loadButtonStates(conceptId, flagBtn, orgBtn, userBtn) {
    try {
        const encoded = encodeURIComponent(conceptId);
        const response = await fetch(`/vontology/api/vontology/node_content?identifier=${encoded}`);
        if (!response.ok) return;

        const data = await response.json();
        const concept = data.raw_doc || data;

        // Update flag button state
        if (flagBtn) {
            const flagged = concept.flagged || false;
            updateFlagButtonState(flagBtn, flagged);
        }

        // Update organization button state
        if (orgBtn) {
            const orgRelations = concept.relationships?.specific_to_organisation || [];
            const hasOrgRelation = Array.isArray(orgRelations) ? orgRelations.length > 0 : !!orgRelations;
            updateOrgButtonState(orgBtn, hasOrgRelation);
        }

        // Update user button state
        if (userBtn) {
            const userRelations = concept.relationships?.specific_to_user || [];
            const hasUserRelation = Array.isArray(userRelations) ? userRelations.length > 0 : !!userRelations;
            updateUserButtonState(userBtn, hasUserRelation);
        }

    } catch (e) {
        console.warn('[dynamicTabs] Failed to load button states:', e);
    }
}

// Toggle concept flag
async function toggleConceptFlag(conceptId, flagBtn) {
    try {
        const response = await fetch('/vontology/api/vontology/concept/flag', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                concept_id: conceptId,
                flagged: !flagBtn.dataset.flagged || flagBtn.dataset.flagged === 'false'
            })
        });

        const result = await response.json();
        if (result.success) {
            updateFlagButtonState(flagBtn, result.flagged);
            showToast(`Concept ${result.flagged ? 'flagged' : 'unflagged'} for analysis`);
        } else {
            showToast(`Error: ${result.error}`, 'error');
        }
    } catch (e) {
        console.error('Failed to toggle flag:', e);
        showToast('Failed to toggle flag', 'error');
    }
}

// Toggle organization relation
async function toggleOrganizationRelation(conceptId, orgBtn) {
    try {
        const hasRelation = orgBtn.dataset.hasRelation === 'true';
        const action = hasRelation ? 'remove' : 'add';
        orgBtn.disabled = true;
        // Derive current organisation concept id from localStorage (authoritative client context).
        let orgId = (
            window.localStorage.getItem('von_current_org') ||
            window.localStorage.getItem('von_current_organisation') ||
            window.localStorage.getItem('vonCurrentOrg') ||
            null
        );
        // Extract concept_id if localStorage contains JSON object
        if (orgId && typeof orgId === 'string' && orgId.startsWith('{')) {
            try {
                const orgObj = JSON.parse(orgId);
                orgId = orgObj.concept_id || orgId;
            } catch (e) {
                // Keep original string if JSON parse fails
            }
        }
        if (action === 'add' && !orgId) {
            showToast('Organisation context unavailable', 'error');
            return;
        }
        const response = await fetch('/vontology/api/vontology/concept/organization-relation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            // Use NZ spelling primary key; backend tolerates multiple synonyms.
            body: JSON.stringify({ concept_id: conceptId, action, organisation_concept_id: orgId })
        });

        const result = await response.json();
        if (!result.success && !result.changed) {
            showToast(result.error || 'No change applied', 'error');
            // Force removal still available for cleanup scenarios, but no context mismatch logic needed
        } else if (result.success) {
            const newHasRelation = Array.isArray(result.specific_to_organisation) && result.specific_to_organisation.length > 0;
            updateOrgButtonState(orgBtn, newHasRelation);
            showToast(`Organization relation ${action === 'add' ? 'added' : 'removed'}`);
        } else if (result.changed === false) {
            showToast('No organization relation updated', 'error');
        }
        // Context mismatch no longer relevant - client localStorage is sole authority
        // Refresh authoritative state from server doc
        await loadButtonStates(conceptId, null, orgBtn, null);
    } catch (e) {
        console.error('Failed to toggle organization relation:', e);
        showToast('Failed to toggle organization relation', 'error');
    } finally { orgBtn.disabled = false; }
}

// Toggle user relation
async function toggleUserRelation(conceptId, userBtn) {
    try {
        const hasRelation = userBtn.dataset.hasRelation === 'true';
        const action = hasRelation ? 'remove' : 'add';
        userBtn.disabled = true;
        // Get current user concept id from localStorage (client authoritative identity)
        let userId = (
            window.localStorage.getItem('von_current_user') ||
            window.localStorage.getItem('vonCurrentUser') ||
            null
        );
        // Extract concept_id if localStorage contains JSON object
        if (userId && typeof userId === 'string' && userId.startsWith('{')) {
            try {
                const userObj = JSON.parse(userId);
                userId = userObj.concept_id || userId;
            } catch (e) {
                // Keep original string if JSON parse fails
            }
        }
        if (action === 'add' && !userId) {
            showToast('User context unavailable', 'error');
            return;
        }
        const response = await fetch('/vontology/api/vontology/concept/user-relation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ concept_id: conceptId, action, user_concept_id: userId })
        });

        const result = await response.json();
        if (!result.success && !result.changed) {
            showToast(result.error || 'No change applied', 'error');
            // Force removal still available for cleanup scenarios, but no context mismatch logic needed
        } else if (result.success) {
            const newHasRelation = Array.isArray(result.specific_to_user) && result.specific_to_user.length > 0;
            updateUserButtonState(userBtn, newHasRelation);
            showToast(`User relation ${action === 'add' ? 'added' : 'removed'}`);
        } else if (result.changed === false) {
            showToast('No user relation updated', 'error');
        }
        // Context mismatch no longer relevant - client localStorage is sole authority
        await loadButtonStates(conceptId, null, null, userBtn);
    } catch (e) {
        console.error('Failed to toggle user relation:', e);
        showToast('Failed to toggle user relation', 'error');
    } finally { userBtn.disabled = false; }
}

// Update button states visually
function updateFlagButtonState(flagBtn, flagged) {
    flagBtn.dataset.flagged = flagged.toString();
    if (flagged) {
        flagBtn.style.background = '#fef3c7'; // Yellow background when flagged
        flagBtn.style.borderColor = '#f59e0b';
        flagBtn.title = 'Remove analysis flag';
    } else {
        flagBtn.style.background = '#f9fafb';
        flagBtn.style.borderColor = '#d1d5db';
        flagBtn.title = 'Add analysis flag';
    }
}

function updateOrgButtonState(orgBtn, hasRelation) {
    orgBtn.dataset.hasRelation = hasRelation.toString();
    orgBtn.setAttribute('aria-pressed', hasRelation ? 'true' : 'false');
    if (hasRelation) {
        orgBtn.style.background = '#dbeafe'; // Blue background when related
        orgBtn.style.borderColor = '#3b82f6';
        orgBtn.title = 'Remove organization relation';
    } else {
        orgBtn.style.background = '#f9fafb';
        orgBtn.style.borderColor = '#d1d5db';
        orgBtn.title = 'Add organization relation';
    }
}

function updateUserButtonState(userBtn, hasRelation) {
    userBtn.dataset.hasRelation = hasRelation.toString();
    userBtn.setAttribute('aria-pressed', hasRelation ? 'true' : 'false');
    if (hasRelation) {
        userBtn.style.background = '#dcfce7'; // Green background when related
        userBtn.style.borderColor = '#10b981';
        userBtn.title = 'Remove user relation';
    } else {
        userBtn.style.background = '#f9fafb';
        userBtn.style.borderColor = '#d1d5db';
        userBtn.title = 'Add user relation';
    }
}

// Helper functions to get current user/org info
async function getCurrentUserOrganization() {
    try {
        const response = await fetch('/api/settings/user/current');
        if (!response.ok) throw new Error('Failed to get user info');
        const user = await response.json();
        if (!user.organization_id) throw new Error('Missing organization context');
        return user.organization_id;
    } catch (e) {
        console.warn('Could not get current user organization');
        showToast('Organisation context unavailable', 'error');
        return null;
    }
}

async function getCurrentUserId() {
    try {
        const response = await fetch('/api/settings/user/current');
        if (!response.ok) throw new Error('Failed to get user info');
        const user = await response.json();
        if (!user.user_id) throw new Error('Missing user context');
        return user.user_id;
    } catch (e) {
        console.warn('Could not get current user ID');
        showToast('User context unavailable', 'error');
        return null;
    }
}

// Simple toast notification helper
function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    toast.style.cssText = `
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 12px 20px;
        border-radius: 4px;
        color: white;
        font-weight: 500;
        z-index: 10000;
        opacity: 0;
        transition: opacity 0.3s ease;
    `;

    if (type === 'error') {
        toast.style.backgroundColor = '#ef4444';
    } else {
        toast.style.backgroundColor = '#10b981';
    }

    document.body.appendChild(toast);

    // Fade in
    setTimeout(() => { toast.style.opacity = '1'; }, 10);

    // Auto remove
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => document.body.removeChild(toast), 300);
    }, 3000);
}

// Explicit exports for tests / external modules that need to force relabeling
export { initializeRelationshipsUI, relabelAllDynamicConceptTabs, reloadConceptTab, updateTabLabelWithShortestName };


