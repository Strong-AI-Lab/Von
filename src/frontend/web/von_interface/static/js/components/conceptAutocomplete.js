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

let autocompleteState = {
    isOpen: false,
    selectedIndex: -1,
    results: [],
    triggerPos: null,
    textarea: null,
    searchTimeout: null,
    dropdownPointerDown: false,
};

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
        autocompleteState.dropdownPointerDown = true;
    });
    dropdown.addEventListener('pointerup', () => {
        // Release after click dispatches
        setTimeout(() => {
            autocompleteState.dropdownPointerDown = false;
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
    autocompleteState.isOpen = false;
    autocompleteState.selectedIndex = -1;
    autocompleteState.results = [];
}

/**
 * Search for concepts matching the query
 */
async function searchConcepts(query) {
    try {
        if (!query || (typeof query === 'string' && query.length < 1)) {
            closeAutocomplete();
            return;
        }

        const response = await fetch(
            `${SEARCH_API}?${SEARCH_PARAM}=${encodeURIComponent(query)}&limit=${MAX_RESULTS}`
        );

        if (!response.ok) {
            console.warn('Concept search failed:', response.status);
            closeAutocomplete();
            return;
        }

        const data = await response.json();
        const results = Array.isArray(data.results) ? data.results : [];

        // Update state
        autocompleteState.results = results;
        autocompleteState.selectedIndex = -1;

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
            kindBadge.style.backgroundColor = '#fff3cd';
            kindBadge.style.color = '#997404';
        }

        item.appendChild(nameSpan);
        item.appendChild(kindBadge);

        // Hover effect
        item.addEventListener('mouseenter', () => {
            item.style.backgroundColor = '#f5f5f5';
            autocompleteState.selectedIndex = index;
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
    if (autocompleteState.textarea) {
        const rect = autocompleteState.textarea.getBoundingClientRect();
        dropdown.style.top = `${rect.bottom + window.scrollY}px`;
        dropdown.style.left = `${rect.left + window.scrollX}px`;
        dropdown.style.width = `${Math.max(rect.width, 250)}px`;
    }

    dropdown.style.display = 'block';
    autocompleteState.isOpen = true;
}

/**
 * Update visual selection of items
 */
function updateItemSelection() {
    const items = document.querySelectorAll('.concept-autocomplete-item');
    items.forEach((item, index) => {
        if (index === autocompleteState.selectedIndex) {
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
    const ta = autocompleteState.textarea;
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
    ta.value = before + insertedId + after;

    // Move cursor after inserted concept ID
    const newPos = triggerIdx + insertedId.length;
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
    if (!autocompleteState.isOpen || autocompleteState.results.length === 0) {
        return;
    }

    const itemCount = autocompleteState.results.length;

    switch (event.key) {
        case 'ArrowDown':
            event.preventDefault();
            autocompleteState.selectedIndex = Math.min(
                autocompleteState.selectedIndex + 1,
                itemCount - 1
            );
            updateItemSelection();
            break;

        case 'ArrowUp':
            event.preventDefault();
            autocompleteState.selectedIndex = Math.max(
                autocompleteState.selectedIndex - 1,
                0
            );
            updateItemSelection();
            break;

        case 'Enter':
            event.preventDefault();
            if (autocompleteState.selectedIndex >= 0) {
                const result = autocompleteState.results[autocompleteState.selectedIndex];
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

    // Debounce search
    clearTimeout(autocompleteState.searchTimeout);
    autocompleteState.searchTimeout = setTimeout(() => {
        searchConcepts(searchText);
    }, DEBOUNCE_MS);
}

/**
 * Initialize autocomplete on a textarea element
 */
export function initializeConceptAutocomplete(textareaElement) {
    if (!textareaElement) return;

    // Store reference
    autocompleteState.textarea = textareaElement;

    // Attach event listeners
    textareaElement.addEventListener('input', handleInput);
    textareaElement.addEventListener('keydown', handleKeydown);

    // Close on blur
    textareaElement.addEventListener('blur', () => {
        // Delay to allow click on dropdown items
        setTimeout(() => {
            if (!autocompleteState.dropdownPointerDown) {
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
    clearTimeout(autocompleteState.searchTimeout);
}
