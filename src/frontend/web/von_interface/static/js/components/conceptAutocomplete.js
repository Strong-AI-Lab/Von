/**
 * Concept Autocomplete Component
 *
 * Provides inline #V# (or #v#) trigger-based concept search with autocomplete dropdown.
 * Searches concepts as user types after #V# trigger and inserts selected concept ID.
 *
 * Features:
 * - Triggers on #V# or #v# prefix
 * - Live search as user types
 * - Keyboard navigation (up/down/enter/esc)
 * - Click to select
 * - Debounced search requests
 * - Graceful fallback if search unavailable
 */

import { makeNonTriggerVontologyId } from './promptCartoucheOverlay.js';

const TRIGGER_PATTERN = /#[Vv]#/;
const DEBOUNCE_MS = 200;
const MAX_RESULTS = 8;
// Use the same search implementation as the global search UI to avoid drift.
const SEARCH_API = '/vontology/api/vontology/search';
const SEARCH_PARAM = 'q';
const MAX_QUERY_CHARS = 80;
const INCLUDE_INDIVIDUALS = true;
const FALLBACK_SUBSTRING = true;

// Per-textarea state to avoid race conditions when multiple textareas exist (e.g. multiple chat tabs)
const textareaStates = new WeakMap();

function getOrCreateState(textarea) {
    if (!textareaStates.has(textarea)) {
        textareaStates.set(textarea, {
            isOpen: false,
            selectedIndex: -1,
            results: [],
            triggerPos: null,
            searchTimeout: null,
            dropdownPointerDown: false,
        });
    }
    return textareaStates.get(textarea);
}

// Track the currently active textarea for the dropdown
let activeTextarea = null;

/**
 * Create autocomplete dropdown element
 */
function createDropdown() {
    const dropdown = document.createElement('div');
    dropdown.className = 'concept-autocomplete-dropdown';
    dropdown.style.display = 'none';
    dropdown.style.position = 'absolute';
    dropdown.style.zIndex = '1000';
    dropdown.style.background = 'white';
    dropdown.style.border = '1px solid #ccc';
    dropdown.style.borderRadius = '4px';
    dropdown.style.boxShadow = '0 2px 8px rgba(0,0,0,0.1)';
    dropdown.style.maxHeight = '250px';
    dropdown.style.overflowY = 'auto';
    dropdown.style.minWidth = '200px';

    // Track pointer state so blur on the textarea does not close before click handlers run
    dropdown.addEventListener('pointerdown', () => {
        if (activeTextarea) {
            const state = getOrCreateState(activeTextarea);
            state.dropdownPointerDown = true;
        }
    });
    dropdown.addEventListener('pointerup', () => {
        // Release after click dispatches
        setTimeout(() => {
            if (activeTextarea) {
                const state = getOrCreateState(activeTextarea);
                state.dropdownPointerDown = false;
            }
        }, 0);
    });
    return dropdown;
}

/**
 * Close autocomplete dropdown
 */
export function closeAutocomplete() {
    const dropdown = document.querySelector('.concept-autocomplete-dropdown');
    if (dropdown) {
        dropdown.style.display = 'none';
    }
    if (activeTextarea) {
        const state = getOrCreateState(activeTextarea);
        state.isOpen = false;
        state.selectedIndex = -1;
        state.results = [];
    }
}

// Export for testing
export function buildConceptSearchUrl(query) {
    const q = String(query ?? '').trim();
    const params = new URLSearchParams();
    params.set(SEARCH_PARAM, q);
    params.set('limit', String(MAX_RESULTS));
    if (INCLUDE_INDIVIDUALS) params.set('include_individuals', 'true');
    if (FALLBACK_SUBSTRING) params.set('fallback_substring', 'true');
    return `${SEARCH_API}?${params.toString()}`;
}

/**
 * Search for concepts matching the query
 * @param {string} query - Search query
 * @param {HTMLElement} originTextarea - The textarea that initiated the search (for race condition protection)
 */
async function searchConcepts(query, originTextarea) {
    try {
        if (!query || (typeof query === 'string' && query.length < 1)) {
            closeAutocomplete();
            return;
        }

        const response = await fetch(buildConceptSearchUrl(query));

        // Race condition guard: if the active textarea changed while we were fetching, abort
        if (activeTextarea !== originTextarea) {
            console.log('[conceptAutocomplete] Discarding stale search results (textarea changed)');
            return;
        }

        if (!response.ok) {
            console.warn('Concept search failed:', response.status);
            closeAutocomplete();
            return;
        }

        const data = await response.json();
        const results = Array.isArray(data.results) ? data.results : [];

        // Race condition guard again after parsing
        if (activeTextarea !== originTextarea) {
            console.log('[conceptAutocomplete] Discarding stale search results (textarea changed)');
            return;
        }

        // Update state for this textarea
        const state = getOrCreateState(originTextarea);
        state.results = results;
        state.selectedIndex = -1;

        if (results.length === 0) {
            closeAutocomplete();
            return;
        }

        // Render dropdown
        renderDropdown(results);
    } catch (err) {
        console.error('Concept search error:', err);
        closeAutocomplete();
    }
}

// Export for testing
export function getTriggerSearchText(text, cursorPos, maxLookback = 200) {
    const input = String(text ?? '');
    const pos = typeof cursorPos === 'number' ? cursorPos : 0;

    const safeCursorPos = Math.max(0, Math.min(pos, input.length));
    const searchStart = Math.max(0, safeCursorPos - Math.max(0, maxLookback));
    const substring = input.substring(searchStart, safeCursorPos);

    // Find last #V# or #v# before cursor.
    let lastTriggerIdx = -1;
    for (let i = substring.length - 3; i >= 0; i--) {
        if (
            substring[i] === '#' &&
            (substring[i + 1] === 'V' || substring[i + 1] === 'v') &&
            substring[i + 2] === '#'
        ) {
            lastTriggerIdx = searchStart + i;
            break;
        }
    }

    if (lastTriggerIdx === -1) {
        return null;
    }

    const searchText = input.substring(lastTriggerIdx + 3, safeCursorPos);
    return { triggerIdx: lastTriggerIdx, searchText };
}

/**
 * Render the autocomplete dropdown
 */
function renderDropdown(results) {
    let dropdown = document.querySelector('.concept-autocomplete-dropdown');
    if (!dropdown) {
        dropdown = createDropdown();
        document.body.appendChild(dropdown);
    }

    // Clear previous items
    dropdown.innerHTML = '';

    // Get state for active textarea
    const state = activeTextarea ? getOrCreateState(activeTextarea) : null;

    // Create result items
    results.forEach((result, index) => {
        const item = document.createElement('div');
        item.className = 'concept-autocomplete-item';
        item.style.padding = '8px 12px';
        item.style.cursor = 'pointer';
        item.style.borderBottom = '1px solid #f0f0f0';
        item.style.fontSize = '14px';
        item.style.display = 'flex';
        item.style.alignItems = 'center';
        item.style.justifyContent = 'space-between';
        item.style.gap = '8px';

        // Left side: name with tooltip
        const nameSpan = document.createElement('span');
        nameSpan.className = 'concept-autocomplete-item-name';
        nameSpan.textContent = result.name || result.id;
        nameSpan.style.fontWeight = '500';
        nameSpan.style.flex = '1';
        nameSpan.title = `${result.name || result.id} — ${result.id}`;

        // Right side: kind badge (matching vontology.js style)
        const badgeText = result.kind === 'predicate' ? 'Predicate' :
            (result.kind === 'individual' ? 'Individual' : 'Type');
        const kindBadge = document.createElement('span');
        kindBadge.className = `concept-autocomplete-item-kind ${result.kind}`;
        kindBadge.textContent = badgeText;
        kindBadge.style.display = 'inline-block';
        kindBadge.style.fontSize = '11px';
        kindBadge.style.padding = '3px 8px';
        kindBadge.style.borderRadius = '3px';
        kindBadge.style.fontWeight = '500';
        kindBadge.style.whiteSpace = 'nowrap';

        // Set badge colors to match vontology.js
        if (result.kind === 'individual') {
            kindBadge.style.backgroundColor = '#d4edda';
            kindBadge.style.color = '#155724';
        } else if (result.kind === 'type') {
            kindBadge.style.backgroundColor = '#cfe2ff';
            kindBadge.style.color = '#084298';
        } else if (result.kind === 'predicate') {
            kindBadge.style.backgroundColor = '#f3e8ff';
            kindBadge.style.color = '#6b21a8';
        }

        item.appendChild(nameSpan);
        item.appendChild(kindBadge);

        // Hover effect
        item.addEventListener('mouseenter', () => {
            item.style.backgroundColor = '#f5f5f5';
            if (state) state.selectedIndex = index;
            updateItemSelection();
        });

        item.addEventListener('mouseleave', () => {
            item.style.backgroundColor = 'transparent';
        });

        // Click to select
        item.addEventListener('click', () => {
            insertConcept(result.id);
            closeAutocomplete();
        });

        // Data attribute for testing
        item.setAttribute('data-concept-id', result.id);

        dropdown.appendChild(item);
    });

    // Position dropdown below the textarea
    if (activeTextarea) {
        const rect = activeTextarea.getBoundingClientRect();
        dropdown.style.top = `${rect.bottom + window.scrollY}px`;
        dropdown.style.left = `${rect.left + window.scrollX}px`;
        dropdown.style.width = `${Math.max(rect.width, 250)}px`;
    }

    dropdown.style.display = 'block';
    if (state) state.isOpen = true;
}

/**
 * Update visual selection of items
 */
function updateItemSelection() {
    const state = activeTextarea ? getOrCreateState(activeTextarea) : null;
    const selectedIndex = state ? state.selectedIndex : -1;
    const items = document.querySelectorAll('.concept-autocomplete-item');
    items.forEach((item, index) => {
        if (index === selectedIndex) {
            item.style.backgroundColor = '#e3f2fd';
            item.style.fontWeight = '600';
        } else {
            item.style.backgroundColor = 'transparent';
            item.style.fontWeight = '500';
        }
    });
}

/**
 * Insert selected concept ID into textarea
 */
function insertConcept(conceptId) {
    const ta = activeTextarea;
    if (!ta || !ta.value) {
        console.warn('[conceptAutocomplete] insertConcept called with invalid textarea');
        return;
    }

    const insertedId = makeNonTriggerVontologyId(conceptId);

    const text = ta.value;
    const cursorPos = ta.selectionStart;

    // Find the #V# or #v# trigger position (search backwards for the pattern)
    let triggerIdx = -1;
    let searchStart = Math.max(0, cursorPos - 100);
    let substring = text.substring(searchStart, cursorPos);

    // Search backwards for #V# or #v#
    for (let i = substring.length - 3; i >= 0; i--) {
        if (
            substring[i] === '#' &&
            (substring[i + 1] === 'V' || substring[i + 1] === 'v') &&
            substring[i + 2] === '#'
        ) {
            triggerIdx = searchStart + i;
            break;
        }
    }

    if (triggerIdx === -1) {
        console.warn('[conceptAutocomplete] No #V# trigger found when inserting concept');
        return;
    }

    // Replace from trigger to cursor position
    const before = text.substring(0, triggerIdx);
    const after = text.substring(cursorPos);

    // Ensure a separator after the inserted concept token, otherwise subsequent typing can
    // accidentally extend the hidden token (e.g., #V\u200B#personabc), which breaks hydration.
    let separator = '';
    const nextChar = after[0] || '';
    if (!nextChar) {
        separator = ' ';
    } else if (!/\s/.test(nextChar) && !/[\.,;:!\?\)\]\}]/.test(nextChar) && nextChar !== '"' && nextChar !== "'" && nextChar !== '`') {
        separator = ' ';
    }

    ta.value = before + insertedId + separator + after;

    // Move cursor after inserted concept ID
    const newPos = triggerIdx + insertedId.length + separator.length;
    ta.selectionStart = newPos;
    ta.selectionEnd = newPos;
    ta.focus();

    console.log(`[conceptAutocomplete] Inserted ${insertedId} at position ${triggerIdx}`);

    // Trigger input event for any listeners
    ta.dispatchEvent(new Event('input', { bubbles: true }));
}

/**
 * Handle keyboard navigation in dropdown
 */
function handleKeydown(event) {
    const ta = event.target;
    const state = getOrCreateState(ta);

    if (!state.isOpen || state.results.length === 0) {
        return;
    }

    const itemCount = state.results.length;

    switch (event.key) {
        case 'ArrowDown':
            event.preventDefault();
            state.selectedIndex = Math.min(
                state.selectedIndex + 1,
                itemCount - 1
            );
            updateItemSelection();
            break;

        case 'ArrowUp':
            event.preventDefault();
            state.selectedIndex = Math.max(
                state.selectedIndex - 1,
                0
            );
            updateItemSelection();
            break;

        case 'Enter':
            event.preventDefault();
            if (state.selectedIndex >= 0) {
                const result = state.results[state.selectedIndex];
                insertConcept(result.id);
                closeAutocomplete();
            }
            break;

        case 'Escape':
            event.preventDefault();
            closeAutocomplete();
            break;
    }
}

/**
 * Handle input in textarea
 */
function handleInput(event) {
    const ta = event.target;
    const text = ta.value;
    const cursorPos = ta.selectionStart;

    // Set this textarea as the active one for autocomplete
    activeTextarea = ta;
    const state = getOrCreateState(ta);

    const trigger = getTriggerSearchText(text, cursorPos);
    if (!trigger) {
        closeAutocomplete();
        return;
    }

    const { triggerIdx: lastTriggerIdx, searchText } = trigger;
    console.log(`[conceptAutocomplete] Found trigger at position ${lastTriggerIdx}`);

    console.log(`[conceptAutocomplete] Search text: "${searchText}"`);

    // Stop if the user has moved on to normal text entry.
    if (/\s/.test(searchText)) {
        closeAutocomplete();
        return;
    }

    // Safety cap: avoid keeping an ancient trigger active.
    if (searchText.length > MAX_QUERY_CHARS) {
        closeAutocomplete();
        return;
    }

    // Debounce search - capture the textarea for race condition protection
    clearTimeout(state.searchTimeout);
    const originTextarea = ta;
    state.searchTimeout = setTimeout(() => {
        searchConcepts(searchText, originTextarea);
    }, DEBOUNCE_MS);
}

/**
 * Initialize autocomplete on a textarea element
 */
export function initializeConceptAutocomplete(textareaElement) {
    if (!textareaElement) return;

    // Create per-textarea state (will be retrieved by handlers via getOrCreateState)
    getOrCreateState(textareaElement);

    // Attach event listeners
    textareaElement.addEventListener('input', handleInput);
    textareaElement.addEventListener('keydown', handleKeydown);

    // Set as active on focus to handle tab switches
    textareaElement.addEventListener('focus', () => {
        activeTextarea = textareaElement;
    });

    // Close on blur
    textareaElement.addEventListener('blur', () => {
        const state = getOrCreateState(textareaElement);
        // Delay to allow click on dropdown items
        setTimeout(() => {
            if (!state.dropdownPointerDown) {
                closeAutocomplete();
            }
        }, 150);
    });

    console.log(`[conceptAutocomplete] Initialized on ${textareaElement.id}`);
}

/**
 * Initialize on multiple textareas by selector
 */
export function initializeConceptAutocompleteOnAll(selector) {
    const elements = document.querySelectorAll(selector);
    elements.forEach((el) => {
        initializeConceptAutocomplete(el);
    });
    console.log(
        `[conceptAutocomplete] Initialized on ${elements.length} elements matching ${selector}`
    );
}

/**
 * Clean up autocomplete (for page unload)
 */
export function cleanupConceptAutocomplete() {
    closeAutocomplete();
    if (activeTextarea) {
        const state = getOrCreateState(activeTextarea);
        clearTimeout(state.searchTimeout);
    }
}
