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

const TRIGGER_PATTERN = /#[Vv]#/;
const DEBOUNCE_MS = 200;
const MAX_RESULTS = 8;
const SEARCH_API = '/vontology/api/search';

let autocompleteState = {
    isOpen: false,
    selectedIndex: -1,
    results: [],
    triggerPos: null,
    textarea: null,
    searchTimeout: null,
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
        if (!query || query.length < 1) {
            closeAutocomplete();
            return;
        }

        const response = await fetch(
            `${SEARCH_API}?q=${encodeURIComponent(query)}&limit=${MAX_RESULTS}`
        );

        if (!response.ok) {
            console.warn('Concept search failed:', response.status);
            closeAutocomplete();
            return;
        }

        const data = await response.json();
        const results = data.results || [];

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

        // Create display: "name (id)" with badge for kind
        const kindBadge = document.createElement('span');
        kindBadge.textContent = result.kind;
        kindBadge.style.display = 'inline-block';
        kindBadge.style.fontSize = '10px';
        kindBadge.style.padding = '2px 6px';
        kindBadge.style.marginLeft = '8px';
        kindBadge.style.backgroundColor = '#e8e8e8';
        kindBadge.style.borderRadius = '3px';
        kindBadge.style.fontWeight = 'bold';
        kindBadge.style.color = '#333';

        const nameSpan = document.createElement('span');
        nameSpan.textContent = result.name;
        nameSpan.style.fontWeight = '500';

        const idSpan = document.createElement('span');
        idSpan.textContent = ` (${result.id})`;
        idSpan.style.color = '#666';
        idSpan.style.fontSize = '12px';
        idSpan.style.marginLeft = '4px';

        item.appendChild(nameSpan);
        item.appendChild(idSpan);
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
    if (!ta) return;

    const text = ta.value;
    const cursorPos = ta.selectionStart;

    // Find the #V# or #v# trigger position
    let triggerIdx = -1;
    let searchStart = Math.max(0, cursorPos - 50);
    let substring = text.substring(searchStart, cursorPos);
    let match = substring.match(/#[Vv]#$/);

    if (match) {
        triggerIdx = searchStart + substring.length - match[0].length;
    }

    if (triggerIdx === -1) return;

    // Replace from trigger to cursor position
    const before = text.substring(0, triggerIdx);
    const after = text.substring(cursorPos);
    ta.value = before + conceptId + after;

    // Move cursor after inserted concept ID
    const newPos = triggerIdx + conceptId.length;
    ta.selectionStart = newPos;
    ta.selectionEnd = newPos;
    ta.focus();

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

    // Look for #V# or #v# pattern before cursor
    let searchStart = Math.max(0, cursorPos - 100);
    let substring = text.substring(searchStart, cursorPos);

    // Find last #V# or #v#
    let lastTriggerIdx = -1;
    let triggerMatch = null;

    // Search backwards from cursor
    for (let i = substring.length - 1; i >= 0; i--) {
        if (
            substring[i] === '#' &&
            i > 0 &&
            (substring[i - 1] === 'V' || substring[i - 1] === 'v')
        ) {
            lastTriggerIdx = searchStart + i - 1;
            triggerMatch = substring[i - 1];
            break;
        }
    }

    // If no trigger found, close autocomplete
    if (lastTriggerIdx === -1) {
        closeAutocomplete();
        return;
    }

    // Get search text after trigger
    const searchText = text.substring(lastTriggerIdx + 3, cursorPos).trim();

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
        setTimeout(closeAutocomplete, 150);
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
