/**
 * Predicate extent display component.
 *
 * This module provides a reusable component for displaying the extent of a predicate
 * (all the instances/triples where the predicate is used).
 */

import { createOrActivateConceptTab } from './dynamicTabs.js';
import { fetchPredicateExtent } from './predicateUtils.js';
import { selectBestNameForContext, selectShortestNameForContext } from './utils/nameSelection.js';
import { getCartoucheAppearanceSettings } from './utils/textDecorator.js';
import { showToast } from './utils/toast.js';

const CONCEPT_SEARCH_API = '/vontology/api/vontology/search';
const CONCEPT_SEARCH_LIMIT = 8;
const CONCEPT_SEARCH_DEBOUNCE_MS = 200;

async function copyConceptIdToClipboard(conceptId) {
    const value = String(conceptId ?? '').trim();
    if (!value) return;

    try {
        if (navigator?.clipboard?.writeText) {
            await navigator.clipboard.writeText(value);
            showToast('Concept ID copied');
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
        showToast('Concept ID copied');
    } catch (_) {
        showToast('Copy failed', 'error');
    } finally {
        try { textarea.remove(); } catch (_) { }
    }
}

/**
 * Cache for concept display names to avoid repeated fetches.
 */
const conceptNameCache = new Map();

/**
 * Cache for concept metadata (kind: type/predicate/individual).
 * Structure: { displayName: string, kind: 'type'|'predicate'|'individual'|'unknown' }
 */
const conceptMetadataCache = new Map();

/**
 * Fetch the display name and metadata for a concept.
 * Prefers shortest name in the current language.
 *
 * @param {string} conceptId - The concept ID
 * @returns {Promise<{displayName: string, kind: string}>}
 */
async function getConceptMetadata(conceptId) {
    if (!conceptId || !conceptId.startsWith('#V#')) {
        return { displayName: conceptId, kind: 'unknown' };
    }

    // Check cache first
    if (conceptMetadataCache.has(conceptId)) {
        return conceptMetadataCache.get(conceptId);
    }

    try {
        // Fetch concept data
        const resp = await fetch(`/api/concepts/${encodeURIComponent(conceptId)}`);
        if (!resp.ok) {
            const fallback = { displayName: conceptId, kind: 'unknown' };
            conceptMetadataCache.set(conceptId, fallback);
            return fallback;
        }

        const doc = await resp.json();

        // Use kind from backend (type, predicate, or individual)
        const kind = doc.kind || 'unknown';

        // Try to get names from the concept
        let names = [];
        if (Array.isArray(doc.names) && doc.names.length) {
            names = doc.names;
        } else if (doc.raw_doc && Array.isArray(doc.raw_doc.names) && doc.raw_doc.names.length) {
            names = doc.raw_doc.names;
        }

        if (names.length === 0) {
            // Fallback to concept name field or ID
            const displayName = doc.name || doc.display_name || conceptId;
            const metadata = { displayName, kind };
            conceptMetadataCache.set(conceptId, metadata);
            return metadata;
        }

        const bestName = selectBestNameForContext(names);
        const shortestName = selectShortestNameForContext(names);
        const prefs = getCartoucheAppearanceSettings();
        const displayName = (prefs?.useShortestName ? (shortestName || bestName) : (bestName || shortestName)) || doc.name || doc.display_name || conceptId;
        const metadata = { displayName, kind };
        conceptMetadataCache.set(conceptId, metadata);
        return metadata;
    } catch (error) {
        console.debug('[predicateExtentDisplay] Failed to fetch metadata for', conceptId, error);
        const fallback = { displayName: conceptId, kind: 'unknown' };
        conceptMetadataCache.set(conceptId, fallback);
        return fallback;
    }
}

/**
 * Create a predicate extent display component.
 *
 * @param {string} conceptId - The concept ID of the predicate
 * @param {HTMLElement} container - The container element to render into
 * @returns {Object} Component controller with methods to update and refresh
 */
export function createPredicateExtentDisplay(conceptId, container) {
    // Component state
    const state = {
        conceptId,
        currentPage: 0,
        pageSize: 50,
        sortBy: 'timestamp',
        sortOrder: 'desc',
        filters: {
            subjectType: null,
            objectType: null,
            source: null
        },
        data: null,
        loading: false,
        error: null
    };

    // DOM elements
    let rootElement = null;
    let tableElement = null;
    let filtersElement = null;
    let paginationElement = null;
    let errorElement = null;
    let loadingElement = null;
    let addStatusElement = null;

    /**
     * Initialise the component.
     */
    async function init() {
        render();
        await loadData();
    }

    /**
     * Create the initial DOM structure.
     */
    function render() {
        rootElement = document.createElement('div');
        rootElement.className = 'predicate-extent-display';

        // Error container
        errorElement = document.createElement('div');
        errorElement.className = 'predicate-extent-error';
        errorElement.style.display = 'none';
        rootElement.appendChild(errorElement);

        // Loading indicator
        loadingElement = document.createElement('div');
        loadingElement.className = 'predicate-extent-loading';
        loadingElement.textContent = 'Loading extent data...';
        loadingElement.style.display = 'none';
        rootElement.appendChild(loadingElement);

        // Filters section
        filtersElement = document.createElement('div');
        filtersElement.className = 'predicate-extent-filters';
        renderFilters();
        rootElement.appendChild(filtersElement);

        // Table section
        tableElement = document.createElement('div');
        tableElement.className = 'predicate-extent-table-container';
        rootElement.appendChild(tableElement);

        addStatusElement = document.createElement('div');
        addStatusElement.className = 'predicate-extent-add-status';
        rootElement.appendChild(addStatusElement);

        // Pagination section
        paginationElement = document.createElement('div');
        paginationElement.className = 'predicate-extent-pagination';
        rootElement.appendChild(paginationElement);

        container.appendChild(rootElement);
    }

    function setAddStatus(message, tone = 'info') {
        if (!addStatusElement) return;
        addStatusElement.textContent = message || '';
        addStatusElement.dataset.tone = tone;
    }

    function buildConceptSearchUrl(query) {
        const q = String(query ?? '').trim();
        const params = new URLSearchParams();
        params.set('q', q);
        params.set('limit', String(CONCEPT_SEARCH_LIMIT));
        params.set('include_individuals', 'true');
        params.set('fallback_substring', 'true');
        return `${CONCEPT_SEARCH_API}?${params.toString()}`;
    }

    async function searchConcepts(query) {
        if (!query || query.trim().length < 1) return [];
        const resp = await fetch(buildConceptSearchUrl(query));
        if (!resp.ok) return [];
        const data = await resp.json();
        return Array.isArray(data?.results) ? data.results : [];
    }

    function attachConceptSearch(inputEl, resultsEl) {
        if (!inputEl || !resultsEl) return;
        let debounceId = null;
        let active = false;

        const closeResults = () => {
            resultsEl.innerHTML = '';
            resultsEl.classList.remove('open');
            active = false;
        };

        const renderResults = (items) => {
            resultsEl.innerHTML = '';
            if (!items.length) {
                closeResults();
                return;
            }
            resultsEl.classList.add('open');
            active = true;
            items.forEach((item) => {
                const row = document.createElement('div');
                row.className = 'predicate-extent-search-item';

                const name = document.createElement('span');
                name.textContent = item.name || item.id;
                name.title = `${item.name || item.id} — ${item.id}`;

                const kind = document.createElement('span');
                kind.className = `predicate-extent-search-kind ${item.kind || 'type'}`;
                kind.textContent = item.kind === 'predicate' ? 'Predicate'
                    : (item.kind === 'individual' ? 'Individual' : 'Type');

                row.appendChild(name);
                row.appendChild(kind);

                row.addEventListener('click', () => {
                    inputEl.value = item.id;
                    closeResults();
                });

                resultsEl.appendChild(row);
            });
        };

        inputEl.addEventListener('input', () => {
            const query = inputEl.value || '';
            if (debounceId) {
                clearTimeout(debounceId);
            }
            debounceId = setTimeout(async () => {
                const results = await searchConcepts(query);
                renderResults(results);
            }, CONCEPT_SEARCH_DEBOUNCE_MS);
        });

        inputEl.addEventListener('blur', () => {
            setTimeout(() => {
                if (active) closeResults();
            }, 150);
        });
    }

    /**
     * Render the filter controls.
     */
    function renderFilters() {
        filtersElement.innerHTML = '';

        const filtersContainer = document.createElement('div');
        filtersContainer.className = 'predicate-filters-row';

        // Subject type filter
        const subjectTypeGroup = document.createElement('div');
        subjectTypeGroup.className = 'filter-group';
        subjectTypeGroup.innerHTML = `
            <label for="subjectTypeFilter">Subject Type:</label>
            <input type="text" id="subjectTypeFilter" placeholder="e.g. #V#person"
                   value="${state.filters.subjectType || ''}" />
        `;
        filtersContainer.appendChild(subjectTypeGroup);

        // Object type filter
        const objectTypeGroup = document.createElement('div');
        objectTypeGroup.className = 'filter-group';
        objectTypeGroup.innerHTML = `
            <label for="objectTypeFilter">Object Type:</label>
            <input type="text" id="objectTypeFilter" placeholder="e.g. #V#organisation"
                   value="${state.filters.objectType || ''}" />
        `;
        filtersContainer.appendChild(objectTypeGroup);

        // Source filter
        const sourceGroup = document.createElement('div');
        sourceGroup.className = 'filter-group';
        sourceGroup.innerHTML = `
            <label for="sourceFilter">Source:</label>
            <select id="sourceFilter">
                <option value="">All sources</option>
                <option value="text_relations" ${state.filters.source === 'text_relations' ? 'selected' : ''}>Text Relations</option>
                <option value="structured" ${state.filters.source === 'structured' ? 'selected' : ''}>Structured Relations</option>
            </select>
        `;
        filtersContainer.appendChild(sourceGroup);

        // Apply button
        const applyButton = document.createElement('button');
        applyButton.className = 'btn-primary';
        applyButton.textContent = 'Apply Filters';
        applyButton.onclick = handleFilterApply;
        filtersContainer.appendChild(applyButton);

        // Reset button
        const resetButton = document.createElement('button');
        resetButton.className = 'btn-secondary';
        resetButton.textContent = 'Reset';
        resetButton.onclick = handleFilterReset;
        filtersContainer.appendChild(resetButton);

        filtersElement.appendChild(filtersContainer);
    }

    /**
     * Render the extent data table.
     */
    async function renderTable() {
        if (!state.data || !state.data.extent || state.data.extent.length === 0) {
            const total = (state.data && state.data.total) || 0;
            if (total === 0) {
                tableElement.innerHTML = '<p class="info-message">No instances of this predicate found.</p>';
            } else {
                tableElement.innerHTML = '<p class="info-message">No data available for the current page.</p>';
            }
            return;
        }

        const table = document.createElement('table');
        table.className = 'predicate-extent-table';

        // Table header
        const thead = document.createElement('thead');
        const headerRow = document.createElement('tr');

        const headers = [
            { key: 'subject', label: 'Subject', sortable: true },
            { key: 'predicate', label: 'Predicate', sortable: false },
            { key: 'object', label: 'Object(s)', sortable: true },
            { key: 'source', label: 'Source', sortable: true },
            { key: 'timestamp', label: 'Timestamp', sortable: true }
        ];

        headers.forEach(header => {
            const th = document.createElement('th');
            th.textContent = header.label;

            if (header.sortable) {
                th.className = 'sortable';
                th.onclick = () => handleSort(header.key);

                // Add sort indicator
                if (state.sortBy === header.key) {
                    const indicator = document.createElement('span');
                    indicator.className = 'sort-indicator';
                    indicator.textContent = state.sortOrder === 'asc' ? ' ▲' : ' ▼';
                    th.appendChild(indicator);
                }
            }

            headerRow.appendChild(th);
        });

        thead.appendChild(headerRow);
        table.appendChild(thead);

        // Table body
        const tbody = document.createElement('tbody');

        // Fetch display names for all concepts in parallel
        const conceptIds = new Set();
        state.data.extent.forEach(item => {
            if (item.subject && item.subject.startsWith('#V#')) {
                conceptIds.add(item.subject);
            }
            if (item.predicate && item.predicate.startsWith('#V#')) {
                conceptIds.add(item.predicate);
            }
            // Handle objects/arguments
            const objects = Array.isArray(item.object) ? item.object :
                (item.arguments && Array.isArray(item.arguments) ? item.arguments :
                    (item.object ? [item.object] : []));
            objects.forEach(obj => {
                if (obj && obj.startsWith('#V#')) {
                    conceptIds.add(obj);
                }
            });
        });

        // Fetch all metadata (display names, kinds, predicate status)
        const metadataPromises = Array.from(conceptIds).map(id =>
            getConceptMetadata(id).then(metadata => ({ id, metadata }))
        );
        const metadataResults = await Promise.all(metadataPromises);
        const conceptMetadata = new Map(metadataResults.map(r => [r.id, r.metadata]));

        // Helper to create a clickable concept cell styled as a cartouche
        const createConceptCell = (conceptId) => {
            const cell = document.createElement('td');

            if (conceptId && conceptId.startsWith('#V#')) {
                const metadata = conceptMetadata.get(conceptId) || { displayName: conceptId, kind: 'unknown' };
                const displayName = metadata.displayName;

                const link = document.createElement('a');
                link.href = '#';
                // Add cartouche styling based on kind (backend determines type/predicate/individual)
                if (metadata.kind === 'predicate') {
                    link.className = 'concept-link concept-cartouche predicate';
                } else if (metadata.kind === 'type') {
                    link.className = 'concept-link concept-cartouche type';
                } else if (metadata.kind === 'individual') {
                    link.className = 'concept-link concept-cartouche individual';
                } else {
                    link.className = 'concept-link concept-cartouche';
                }
                link.textContent = displayName;
                link.title = conceptId; // Show full ID on hover
                link.onclick = (e) => {
                    e.preventDefault();
                    e.stopPropagation(); // Prevent row selection
                    createOrActivateConceptTab(conceptId, displayName, true);
                };
                link.addEventListener('contextmenu', (e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    void copyConceptIdToClipboard(conceptId);
                });

                cell.appendChild(link);
            } else {
                cell.textContent = conceptId || 'N/A';
                cell.title = conceptId || '';
            }

            return cell;
        };

        state.data.extent.forEach(item => {
            const row = document.createElement('tr');

            // Subject - clickable if it's a concept
            const subjectCell = createConceptCell(item.subject);
            row.appendChild(subjectCell);

            // Predicate - clickable if it's a concept
            const predicateCell = createConceptCell(item.predicate || state.conceptId);
            row.appendChild(predicateCell);

            // Object(s) - handle n-ary predicates, make each object clickable if it's a concept
            const objectCell = document.createElement('td');
            const objects = Array.isArray(item.object) ? item.object :
                (item.arguments && Array.isArray(item.arguments) ? item.arguments :
                    (item.object ? [item.object] : []));

            if (objects.length > 0) {
                objects.forEach((obj, idx) => {
                    if (idx > 0) {
                        objectCell.appendChild(document.createTextNode(', '));
                    }

                    if (obj && obj.startsWith('#V#')) {
                        const metadata = conceptMetadata.get(obj) || { displayName: obj, kind: 'unknown' };
                        const displayName = metadata.displayName;

                        const link = document.createElement('a');
                        link.href = '#';
                        // Add cartouche styling based on kind (backend determines type/predicate/individual)
                        if (metadata.kind === 'predicate') {
                            link.className = 'concept-link concept-cartouche predicate';
                        } else if (metadata.kind === 'type') {
                            link.className = 'concept-link concept-cartouche type';
                        } else if (metadata.kind === 'individual') {
                            link.className = 'concept-link concept-cartouche individual';
                        } else {
                            link.className = 'concept-link concept-cartouche';
                        }
                        link.textContent = displayName;
                        link.title = obj; // Show full ID on hover
                        link.onclick = (e) => {
                            e.preventDefault();
                            e.stopPropagation(); // Prevent row selection
                            createOrActivateConceptTab(obj, displayName, true);
                        };
                        link.addEventListener('contextmenu', (e) => {
                            e.preventDefault();
                            e.stopPropagation();
                            void copyConceptIdToClipboard(obj);
                        });

                        objectCell.appendChild(link);
                    } else {
                        objectCell.appendChild(document.createTextNode(obj || 'N/A'));
                    }
                });
            } else {
                objectCell.textContent = 'N/A';
            }
            row.appendChild(objectCell);

            // Source
            const sourceCell = document.createElement('td');
            sourceCell.textContent = item.source || 'structured';
            sourceCell.className = `source-${item.source || 'structured'}`;
            row.appendChild(sourceCell);

            // Timestamp
            const timestampCell = document.createElement('td');
            if (item.timestamp) {
                const date = new Date(item.timestamp);
                timestampCell.textContent = date.toLocaleDateString('en-NZ', {
                    year: 'numeric',
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit'
                });
                timestampCell.title = date.toISOString();
            } else {
                timestampCell.textContent = 'N/A';
            }
            row.appendChild(timestampCell);

            tbody.appendChild(row);
        });

        table.appendChild(tbody);

        // Add grounding row
        const tfoot = document.createElement('tfoot');
        const addRow = document.createElement('tr');
        addRow.className = 'predicate-extent-add-row';

        const buildSearchCell = (placeholder) => {
            const cell = document.createElement('td');
            const wrap = document.createElement('div');
            wrap.className = 'predicate-extent-search-wrap';

            const input = document.createElement('input');
            input.type = 'text';
            input.className = 'predicate-extent-input';
            input.placeholder = placeholder;

            const results = document.createElement('div');
            results.className = 'predicate-extent-search-results';

            wrap.appendChild(input);
            wrap.appendChild(results);
            cell.appendChild(wrap);

            attachConceptSearch(input, results);
            return { cell, input };
        };

        const subject = buildSearchCell('Subject concept…');
        const object = buildSearchCell('Object concept…');

        addRow.appendChild(subject.cell);
        addRow.appendChild(createConceptCell(state.conceptId));
        addRow.appendChild(object.cell);

        const sourceCell = document.createElement('td');
        sourceCell.textContent = 'structured';
        addRow.appendChild(sourceCell);

        const actionCell = document.createElement('td');
        const addButton = document.createElement('button');
        addButton.className = 'btn-primary';
        addButton.textContent = 'Add grounding';
        addButton.addEventListener('click', async () => {
            const subjectId = (subject.input.value || '').trim();
            const objectId = (object.input.value || '').trim();
            if (!subjectId || !subjectId.startsWith('#V#')) {
                setAddStatus('Select a valid subject concept ID.', 'error');
                return;
            }
            if (!objectId || !objectId.startsWith('#V#')) {
                setAddStatus('Select a valid object concept ID.', 'error');
                return;
            }
            if (subjectId === objectId) {
                setAddStatus('Subject and object must differ.', 'error');
                return;
            }
            setAddStatus('Adding grounding…', 'info');
            try {
                const resp = await fetch('/vontology/api/vontology/relationships/add', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        source_id: subjectId,
                        kind: state.conceptId,
                        target_id: objectId
                    })
                });
                const data = await resp.json().catch(() => ({}));
                if (!resp.ok || data.error) {
                    throw new Error(data.error || `HTTP ${resp.status}`);
                }
                subject.input.value = '';
                object.input.value = '';
                setAddStatus('Grounding added.', 'success');
                await loadData();
            } catch (err) {
                setAddStatus(`Failed to add grounding: ${err.message}`, 'error');
            }
        });
        actionCell.appendChild(addButton);
        addRow.appendChild(actionCell);

        tfoot.appendChild(addRow);
        table.appendChild(tfoot);
        tableElement.innerHTML = '';
        tableElement.appendChild(table);
    }

    /**
     * Render pagination controls.
     */
    function renderPagination() {
        if (!state.data) {
            paginationElement.innerHTML = '';
            return;
        }

        const total = state.data.total || 0;
        const extent = state.data.extent || [];

        // Hide pagination if there are no results or only one page worth
        if (total === 0 || total <= state.pageSize) {
            paginationElement.innerHTML = '';
            return;
        }

        paginationElement.innerHTML = '';

        const paginationContainer = document.createElement('div');
        paginationContainer.className = 'pagination-controls';

        // Summary text
        const summary = document.createElement('span');
        summary.className = 'pagination-summary';
        const start = Math.min(state.currentPage * state.pageSize + 1, total);
        const end = Math.min((state.currentPage + 1) * state.pageSize, total);
        summary.textContent = `Showing ${start}-${end} of ${total}`;
        paginationContainer.appendChild(summary);

        // Previous button
        const prevButton = document.createElement('button');
        prevButton.textContent = '← Previous';
        prevButton.className = 'btn-secondary';
        prevButton.disabled = state.currentPage === 0;
        prevButton.onclick = handlePrevPage;
        paginationContainer.appendChild(prevButton);

        // Page size selector
        const pageSizeGroup = document.createElement('div');
        pageSizeGroup.className = 'page-size-group';
        pageSizeGroup.innerHTML = `
            <label for="pageSizeSelect">Per page:</label>
            <select id="pageSizeSelect">
                <option value="25" ${state.pageSize === 25 ? 'selected' : ''}>25</option>
                <option value="50" ${state.pageSize === 50 ? 'selected' : ''}>50</option>
                <option value="100" ${state.pageSize === 100 ? 'selected' : ''}>100</option>
                <option value="200" ${state.pageSize === 200 ? 'selected' : ''}>200</option>
            </select>
        `;
        pageSizeGroup.querySelector('select').onchange = handlePageSizeChange;
        paginationContainer.appendChild(pageSizeGroup);

        // Next button
        const nextButton = document.createElement('button');
        nextButton.textContent = 'Next →';
        nextButton.className = 'btn-secondary';
        nextButton.disabled = (state.currentPage + 1) * state.pageSize >= total;
        nextButton.onclick = handleNextPage;
        paginationContainer.appendChild(nextButton);

        paginationElement.appendChild(paginationContainer);
    }

    /**
     * Load extent data from the API.
     */
    async function loadData() {
        state.loading = true;
        state.error = null;

        showLoading();
        hideError();

        try {
            const options = {
                limit: state.pageSize,
                offset: state.currentPage * state.pageSize,
                sort_by: state.sortBy,
                sort_order: state.sortOrder
            };

            if (state.filters.subjectType) {
                options.subject_type = state.filters.subjectType;
            }

            if (state.filters.objectType) {
                options.object_type = state.filters.objectType;
            }

            if (state.filters.source) {
                options.source = state.filters.source;
            }

            state.data = await fetchPredicateExtent(state.conceptId, options);

            renderTable();
            renderPagination();
        } catch (error) {
            state.error = error.message || 'Failed to load extent data';
            showError(state.error);
            console.error('Error loading predicate extent:', error);
        } finally {
            state.loading = false;
            hideLoading();
        }
    }

    /**
     * Handle filter apply button click.
     */
    function handleFilterApply() {
        const subjectTypeInput = document.getElementById('subjectTypeFilter');
        const objectTypeInput = document.getElementById('objectTypeFilter');
        const sourceSelect = document.getElementById('sourceFilter');

        state.filters.subjectType = subjectTypeInput.value.trim() || null;
        state.filters.objectType = objectTypeInput.value.trim() || null;
        state.filters.source = sourceSelect.value || null;

        // Reset to first page when applying filters
        state.currentPage = 0;

        loadData();
    }

    /**
     * Handle filter reset button click.
     */
    function handleFilterReset() {
        state.filters = {
            subjectType: null,
            objectType: null,
            source: null
        };
        state.currentPage = 0;

        renderFilters();
        loadData();
    }

    /**
     * Handle column sort click.
     */
    function handleSort(column) {
        if (state.sortBy === column) {
            // Toggle sort order
            state.sortOrder = state.sortOrder === 'asc' ? 'desc' : 'asc';
        } else {
            // New column, default to descending
            state.sortBy = column;
            state.sortOrder = 'desc';
        }

        loadData();
    }

    /**
     * Handle previous page button click.
     */
    function handlePrevPage() {
        if (state.currentPage > 0) {
            state.currentPage--;
            loadData();
        }
    }

    /**
     * Handle next page button click.
     */
    function handleNextPage() {
        const maxPage = Math.ceil(state.data.total / state.pageSize) - 1;
        if (state.currentPage < maxPage) {
            state.currentPage++;
            loadData();
        }
    }

    /**
     * Handle page size change.
     */
    function handlePageSizeChange(event) {
        state.pageSize = parseInt(event.target.value, 10);
        state.currentPage = 0; // Reset to first page
        loadData();
    }

    /**
     * Show loading indicator.
     */
    function showLoading() {
        if (loadingElement) {
            loadingElement.style.display = 'block';
        }
    }

    /**
     * Hide loading indicator.
     */
    function hideLoading() {
        if (loadingElement) {
            loadingElement.style.display = 'none';
        }
    }

    /**
     * Show error message.
     */
    function showError(message) {
        if (errorElement) {
            errorElement.textContent = `Error: ${message}`;
            errorElement.style.display = 'block';
        }
    }

    /**
     * Hide error message.
     */
    function hideError() {
        if (errorElement) {
            errorElement.style.display = 'none';
        }
    }

    /**
     * Refresh the component with current state.
     */
    function refresh() {
        return loadData();
    }

    /**
     * Update filters and refresh.
     */
    function updateFilters(newFilters) {
        state.filters = { ...state.filters, ...newFilters };
        state.currentPage = 0;
        return loadData();
    }

    /**
     * Destroy the component and clean up.
     */
    function destroy() {
        if (rootElement && rootElement.parentNode) {
            rootElement.parentNode.removeChild(rootElement);
        }
    }

    // Initialise the component
    init();

    // Return controller interface
    return {
        refresh,
        updateFilters,
        destroy,
        getState: () => ({ ...state })
    };
}
