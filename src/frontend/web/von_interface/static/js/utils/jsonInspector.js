const MATCH_CLASS = 'json-inspector-match';
const CURRENT_MATCH_CLASS = 'json-inspector-match-current';
const SEARCH_TARGET_SELECTOR = '.json-inspector-search-target';
const NODE_SELECTOR = 'details.json-inspector-node';

function isCompositeValue(value) {
    return value !== null && typeof value === 'object';
}

function formatPrimitive(value) {
    if (typeof value === 'string') {
        return JSON.stringify(value);
    }
    if (value === null) {
        return 'null';
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
        return String(value);
    }
    if (typeof value === 'undefined') {
        return 'undefined';
    }
    return JSON.stringify(value);
}

function formatPreviewValue(value) {
    if (typeof value === 'string') {
        if (value.length > 24) {
            return JSON.stringify(`${value.slice(0, 21)}...`);
        }
        return JSON.stringify(value);
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
        return String(value);
    }
    if (value === null) {
        return 'null';
    }
    if (Array.isArray(value)) {
        return `[${value.length}]`;
    }
    if (isCompositeValue(value)) {
        return '{...}';
    }
    return String(value);
}

function buildCompositePreview(value) {
    if (Array.isArray(value)) {
        if (!value.length) return '[]';
        const sample = value.slice(0, 3).map(formatPreviewValue);
        const suffix = value.length > 3 ? ', ...' : '';
        return `[${sample.join(', ')}${suffix}]`;
    }

    const entries = Object.entries(value || {});
    if (!entries.length) return '{}';
    const sample = entries
        .slice(0, 3)
        .map(([key, entryValue]) => `${key}: ${formatPreviewValue(entryValue)}`);
    const suffix = entries.length > 3 ? ', ...' : '';
    return `{${sample.join(', ')}${suffix}}`;
}

function createSearchTarget(text, className) {
    const span = document.createElement('span');
    span.className = `${className} json-inspector-search-target`.trim();
    span.dataset.rawText = String(text ?? '');
    span.textContent = span.dataset.rawText;
    return span;
}

function createCompositeNode(value, depth, keyLabel = null) {
    const details = document.createElement('details');
    details.className = 'json-inspector-node';
    details.open = depth <= 1;

    const summary = document.createElement('summary');
    summary.className = 'json-inspector-summary';

    if (keyLabel !== null) {
        summary.appendChild(createSearchTarget(JSON.stringify(String(keyLabel)), 'json-inspector-key'));
        const keySeparator = document.createElement('span');
        keySeparator.className = 'json-inspector-separator';
        keySeparator.textContent = ': ';
        summary.appendChild(keySeparator);
    }

    const kind = Array.isArray(value) ? 'Array' : 'Object';
    const size = Array.isArray(value) ? value.length : Object.keys(value || {}).length;
    const typeEl = document.createElement('span');
    typeEl.className = 'json-inspector-type';
    typeEl.textContent = `${kind}(${size})`;
    summary.appendChild(typeEl);

    const previewEl = document.createElement('span');
    previewEl.className = 'json-inspector-preview';
    previewEl.textContent = buildCompositePreview(value);
    summary.appendChild(previewEl);

    details.appendChild(summary);

    const children = document.createElement('div');
    children.className = 'json-inspector-children';
    const entries = Array.isArray(value)
        ? value.map((item, index) => [String(index), item])
        : Object.entries(value || {});

    if (!entries.length) {
        const empty = document.createElement('div');
        empty.className = 'json-inspector-empty';
        empty.textContent = '(empty)';
        children.appendChild(empty);
    } else {
        entries.forEach(([entryKey, entryValue]) => {
            children.appendChild(createJsonNode(entryValue, depth + 1, entryKey));
        });
    }

    details.appendChild(children);
    return details;
}

function createPrimitiveNode(value, keyLabel = null) {
    const row = document.createElement('div');
    row.className = 'json-inspector-row';

    if (keyLabel !== null) {
        row.appendChild(createSearchTarget(JSON.stringify(String(keyLabel)), 'json-inspector-key'));
        const separator = document.createElement('span');
        separator.className = 'json-inspector-separator';
        separator.textContent = ': ';
        row.appendChild(separator);
    }

    row.appendChild(createSearchTarget(formatPrimitive(value), 'json-inspector-value'));
    return row;
}

function createJsonNode(value, depth, keyLabel = null) {
    if (isCompositeValue(value)) {
        return createCompositeNode(value, depth, keyLabel);
    }
    return createPrimitiveNode(value, keyLabel);
}

function restoreSearchTargets(root) {
    root.querySelectorAll(SEARCH_TARGET_SELECTOR).forEach((target) => {
        const rawText = target.dataset.rawText || '';
        target.textContent = rawText;
    });
}

function openAncestorsForMatch(node, root) {
    let current = node instanceof Element ? node.parentElement : null;
    while (current && current !== root) {
        if (current.tagName === 'DETAILS') {
            current.open = true;
        }
        current = current.parentElement;
    }
}

function highlightSearchTarget(target, queryLower) {
    const rawText = target.dataset.rawText || '';
    if (!queryLower) {
        target.textContent = rawText;
        return [];
    }

    const haystack = rawText.toLowerCase();
    if (!haystack.includes(queryLower)) {
        target.textContent = rawText;
        return [];
    }

    const fragment = document.createDocumentFragment();
    const marks = [];
    let cursor = 0;

    while (cursor < rawText.length) {
        const next = haystack.indexOf(queryLower, cursor);
        if (next < 0) {
            fragment.appendChild(document.createTextNode(rawText.slice(cursor)));
            break;
        }
        if (next > cursor) {
            fragment.appendChild(document.createTextNode(rawText.slice(cursor, next)));
        }
        const mark = document.createElement('mark');
        mark.className = MATCH_CLASS;
        mark.textContent = rawText.slice(next, next + queryLower.length);
        marks.push(mark);
        fragment.appendChild(mark);
        cursor = next + queryLower.length;
    }

    target.textContent = '';
    target.appendChild(fragment);
    return marks;
}

function setCurrentMatch(matches, index) {
    matches.forEach((mark) => {
        mark.classList.remove(CURRENT_MATCH_CLASS);
    });
    if (!matches.length || index < 0 || index >= matches.length) {
        return;
    }
    const current = matches[index];
    current.classList.add(CURRENT_MATCH_CLASS);
    if (typeof current.scrollIntoView === 'function') {
        current.scrollIntoView({ block: 'center', inline: 'nearest' });
    }
}

function parsePayload(payload) {
    if (typeof payload === 'string') {
        try {
            return { value: JSON.parse(payload), parseError: false };
        } catch (_) {
            return { value: payload, parseError: true };
        }
    }
    return { value: payload, parseError: false };
}

function setMatchCountLabel(labelEl, query, count) {
    if (!labelEl) return;
    if (!query) {
        labelEl.textContent = 'Type to filter';
        return;
    }
    if (!count) {
        labelEl.textContent = 'No matches';
        return;
    }
    labelEl.textContent = `${count} match${count === 1 ? '' : 'es'}`;
}

function setStepButtonsEnabled(prevButton, nextButton, enabled) {
    if (prevButton) prevButton.disabled = !enabled;
    if (nextButton) nextButton.disabled = !enabled;
}

/**
 * Mount a JSON inspector (expand/collapse + in-panel search) into a container.
 */
export function mountJsonInspector(container, payload, options = {}) {
    if (!(container instanceof Element)) {
        return null;
    }

    const parsed = parsePayload(payload);
    container.innerHTML = '';

    const inspector = document.createElement('div');
    inspector.className = 'json-inspector';

    const toolbar = document.createElement('div');
    toolbar.className = 'json-inspector-toolbar';

    const searchInput = document.createElement('input');
    searchInput.className = 'json-inspector-search';
    searchInput.type = 'search';
    searchInput.placeholder = options.searchPlaceholder || 'Filter JSON...';
    searchInput.setAttribute('aria-label', 'Filter JSON fields and values');

    const matchCount = document.createElement('span');
    matchCount.className = 'json-inspector-match-count';
    matchCount.textContent = 'Type to filter';

    const prevButton = document.createElement('button');
    prevButton.type = 'button';
    prevButton.className = 'btn-mini json-inspector-step';
    prevButton.textContent = 'Prev';
    prevButton.disabled = true;

    const nextButton = document.createElement('button');
    nextButton.type = 'button';
    nextButton.className = 'btn-mini json-inspector-step';
    nextButton.textContent = 'Next';
    nextButton.disabled = true;

    const expandButton = document.createElement('button');
    expandButton.type = 'button';
    expandButton.className = 'btn-mini json-inspector-expand';
    expandButton.textContent = 'Expand all';

    const collapseButton = document.createElement('button');
    collapseButton.type = 'button';
    collapseButton.className = 'btn-mini json-inspector-collapse';
    collapseButton.textContent = 'Collapse all';

    toolbar.appendChild(searchInput);
    toolbar.appendChild(matchCount);
    toolbar.appendChild(prevButton);
    toolbar.appendChild(nextButton);
    toolbar.appendChild(expandButton);
    toolbar.appendChild(collapseButton);
    inspector.appendChild(toolbar);

    const viewport = document.createElement('div');
    viewport.className = 'json-inspector-viewport';

    if (parsed.parseError) {
        const fallback = document.createElement('pre');
        fallback.className = 'json-inspector-fallback';
        fallback.textContent = String(parsed.value ?? '');
        viewport.appendChild(fallback);
        searchInput.disabled = true;
        expandButton.disabled = true;
        collapseButton.disabled = true;
        matchCount.textContent = 'Invalid JSON';
    } else {
        const rootNode = createJsonNode(parsed.value, 0, null);
        rootNode.classList.add('json-inspector-root');
        rootNode.open = true;
        viewport.appendChild(rootNode);
    }

    inspector.appendChild(viewport);
    container.appendChild(inspector);

    const state = {
        matches: [],
        currentMatchIndex: -1
    };

    const advanceMatch = (direction) => {
        if (!state.matches.length) return;
        const total = state.matches.length;
        state.currentMatchIndex = (state.currentMatchIndex + direction + total) % total;
        setCurrentMatch(state.matches, state.currentMatchIndex);
    };

    const applySearch = (query) => {
        if (searchInput.disabled) {
            return;
        }
        const trimmed = String(query || '').trim();
        const lowered = trimmed.toLowerCase();

        restoreSearchTargets(viewport);
        state.matches = [];
        state.currentMatchIndex = -1;

        viewport.querySelectorAll(SEARCH_TARGET_SELECTOR).forEach((target) => {
            const marks = highlightSearchTarget(target, lowered);
            if (marks.length) {
                marks.forEach((mark) => {
                    openAncestorsForMatch(mark, viewport);
                    state.matches.push(mark);
                });
            }
        });

        if (state.matches.length) {
            state.currentMatchIndex = 0;
            setCurrentMatch(state.matches, state.currentMatchIndex);
        }
        setMatchCountLabel(matchCount, trimmed, state.matches.length);
        setStepButtonsEnabled(prevButton, nextButton, state.matches.length > 0);
    };

    const expandAll = () => {
        viewport.querySelectorAll(NODE_SELECTOR).forEach((node) => {
            node.open = true;
        });
    };

    const collapseAll = () => {
        viewport.querySelectorAll(NODE_SELECTOR).forEach((node) => {
            if (node.classList.contains('json-inspector-root')) {
                node.open = true;
            } else {
                node.open = false;
            }
        });
    };

    searchInput.addEventListener('input', () => {
        applySearch(searchInput.value);
    });
    searchInput.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        advanceMatch(event.shiftKey ? -1 : 1);
    });
    prevButton.addEventListener('click', () => advanceMatch(-1));
    nextButton.addEventListener('click', () => advanceMatch(1));
    expandButton.addEventListener('click', expandAll);
    collapseButton.addEventListener('click', collapseAll);

    return {
        rootElement: inspector,
        focusSearch() {
            searchInput.focus();
            searchInput.select();
        },
        setSearchQuery(query) {
            searchInput.value = String(query || '');
            applySearch(searchInput.value);
        },
        expandAll,
        collapseAll
    };
}
