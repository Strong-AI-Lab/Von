/**
 * Predicate metadata display panel component.
 *
 * This module provides a component for displaying predicate-specific metadata
 * including arity, domain/range constraints, documentation, and usage statistics.
 */

import { fetchPredicateMetadata } from './predicateUtils.js';

/**
 * Create a predicate metadata display panel.
 *
 * @param {string} conceptId - The concept ID of the predicate
 * @param {HTMLElement} container - The container element to render into
 * @returns {Object} Component controller with methods to update and refresh
 */
export function createPredicateMetadataPanel(conceptId, container) {
    // Component state
    const state = {
        conceptId,
        metadata: null,
        loading: false,
        error: null
    };

    // DOM elements
    let rootElement = null;
    let contentElement = null;
    let errorElement = null;
    let loadingElement = null;

    /**
     * Initialise the component.
     */
    async function init() {
        render();
        await loadMetadata();
    }

    /**
     * Create the initial DOM structure.
     */
    function render() {
        rootElement = document.createElement('div');
        rootElement.className = 'predicate-metadata-panel';

        // Header
        const header = document.createElement('div');
        header.className = 'predicate-metadata-header';
        header.innerHTML = '<h3>Predicate Metadata</h3>';
        rootElement.appendChild(header);

        // Error container
        errorElement = document.createElement('div');
        errorElement.className = 'predicate-metadata-error';
        errorElement.style.display = 'none';
        rootElement.appendChild(errorElement);

        // Loading indicator
        loadingElement = document.createElement('div');
        loadingElement.className = 'predicate-metadata-loading';
        loadingElement.textContent = 'Loading metadata...';
        loadingElement.style.display = 'none';
        rootElement.appendChild(loadingElement);

        // Content area
        contentElement = document.createElement('div');
        contentElement.className = 'predicate-metadata-content';
        rootElement.appendChild(contentElement);

        container.appendChild(rootElement);
    }

    /**
     * Render the metadata content.
     */
    function renderMetadata() {
        if (!state.metadata) {
            contentElement.innerHTML = '<p class="no-metadata">No metadata available for this predicate.</p>';
            return;
        }

        contentElement.innerHTML = '';

        // Basic properties section
        const basicSection = createSection('Basic Properties');

        // Arity
        if (state.metadata.arity !== undefined && state.metadata.arity !== null) {
            addProperty(basicSection, 'Arity', state.metadata.arity.toString());
        }

        // Predicate type
        if (state.metadata.predicate_type) {
            addProperty(basicSection, 'Type', formatPredicateType(state.metadata.predicate_type));
        }

        contentElement.appendChild(basicSection);

        // Domain and range constraints
        if (state.metadata.domain_constraints || state.metadata.range_constraints || state.metadata.argument_types) {
            const constraintsSection = createSection('Domain & Range Constraints');

            // Domain constraints (subject types)
            if (state.metadata.domain_constraints && state.metadata.domain_constraints.length > 0) {
                const domainList = document.createElement('div');
                domainList.className = 'constraint-list';
                domainList.innerHTML = `
                    <strong>Domain (Subject Types):</strong>
                    <ul>
                        ${state.metadata.domain_constraints.map(c => `<li>${formatConcept(c)}</li>`).join('')}
                    </ul>
                `;
                constraintsSection.appendChild(domainList);
            }

            // Range constraints (object types) - for binary predicates
            if (state.metadata.range_constraints && state.metadata.range_constraints.length > 0) {
                const rangeList = document.createElement('div');
                rangeList.className = 'constraint-list';
                rangeList.innerHTML = `
                    <strong>Range (Object Types):</strong>
                    <ul>
                        ${state.metadata.range_constraints.map(c => `<li>${formatConcept(c)}</li>`).join('')}
                    </ul>
                `;
                constraintsSection.appendChild(rangeList);
            }

            // Argument types - for n-ary predicates
            if (state.metadata.argument_types && Object.keys(state.metadata.argument_types).length > 0) {
                const argsDiv = document.createElement('div');
                argsDiv.className = 'argument-types';
                argsDiv.innerHTML = '<strong>Argument Types:</strong>';

                const argsList = document.createElement('ul');
                Object.entries(state.metadata.argument_types).sort(([a], [b]) => parseInt(a) - parseInt(b)).forEach(([argNum, types]) => {
                    const li = document.createElement('li');
                    li.innerHTML = `<strong>Argument ${argNum}:</strong> ${types.map(t => formatConcept(t)).join(', ')}`;
                    argsList.appendChild(li);
                });
                argsDiv.appendChild(argsList);
                constraintsSection.appendChild(argsDiv);
            }

            contentElement.appendChild(constraintsSection);
        }

        // Documentation section
        if (state.metadata.documentation || state.metadata.text_value_documentation) {
            const docsSection = createSection('Documentation');

            if (state.metadata.documentation) {
                const docDiv = document.createElement('div');
                docDiv.className = 'documentation-content';
                docDiv.textContent = state.metadata.documentation;
                docsSection.appendChild(docDiv);
            }

            if (state.metadata.text_value_documentation && state.metadata.text_value_documentation.length > 0) {
                const textDocsDiv = document.createElement('div');
                textDocsDiv.className = 'text-documentation';
                textDocsDiv.innerHTML = '<strong>Text Value Documentation:</strong>';

                const docsList = document.createElement('ul');
                state.metadata.text_value_documentation.forEach(doc => {
                    const li = document.createElement('li');
                    li.textContent = doc;
                    docsList.appendChild(li);
                });
                textDocsDiv.appendChild(docsList);
                docsSection.appendChild(textDocsDiv);
            }

            contentElement.appendChild(docsSection);
        }

        // Usage statistics section
        if (state.metadata.statistics) {
            const statsSection = createSection('Usage Statistics');
            const stats = state.metadata.statistics;

            if (stats.total_uses !== undefined) {
                addProperty(statsSection, 'Total Uses', stats.total_uses.toString());
            }

            if (stats.unique_subjects !== undefined) {
                addProperty(statsSection, 'Unique Subjects', stats.unique_subjects.toString());
            }

            if (stats.unique_objects !== undefined) {
                addProperty(statsSection, 'Unique Objects', stats.unique_objects.toString());
            }

            if (stats.text_relations_count !== undefined) {
                addProperty(statsSection, 'Text Relations', stats.text_relations_count.toString());
            }

            if (stats.structured_relations_count !== undefined) {
                addProperty(statsSection, 'Structured Relations', stats.structured_relations_count.toString());
            }

            contentElement.appendChild(statsSection);
        }

        // Custom metadata properties (knowledge-driven extensibility)
        if (state.metadata.custom_properties && Object.keys(state.metadata.custom_properties).length > 0) {
            const customSection = createSection('Additional Properties');

            Object.entries(state.metadata.custom_properties).forEach(([key, value]) => {
                addProperty(customSection, formatPropertyName(key), formatPropertyValue(value));
            });

            contentElement.appendChild(customSection);
        }
    }

    /**
     * Create a metadata section with a title.
     *
     * @param {string} title - Section title
     * @returns {HTMLElement} The section element
     */
    function createSection(title) {
        const section = document.createElement('div');
        section.className = 'metadata-section';

        const titleElement = document.createElement('h4');
        titleElement.className = 'metadata-section-title';
        titleElement.textContent = title;
        section.appendChild(titleElement);

        return section;
    }

    /**
     * Add a property row to a section.
     *
     * @param {HTMLElement} section - The section to add to
     * @param {string} label - Property label
     * @param {string} value - Property value
     */
    function addProperty(section, label, value) {
        const row = document.createElement('div');
        row.className = 'metadata-property-row';

        const labelElement = document.createElement('span');
        labelElement.className = 'property-label';
        labelElement.textContent = label + ':';

        const valueElement = document.createElement('span');
        valueElement.className = 'property-value';
        valueElement.textContent = value;

        row.appendChild(labelElement);
        row.appendChild(valueElement);
        section.appendChild(row);
    }

    /**
     * Format a predicate type string for display.
     *
     * @param {string} type - The predicate type
     * @returns {string} Formatted type
     */
    function formatPredicateType(type) {
        if (!type) return 'Unknown';

        // Remove #V# prefix and convert underscores to spaces
        return type.replace(/^#V#/, '').replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
    }

    /**
     * Format a concept ID for display.
     *
     * @param {string} concept - The concept ID
     * @returns {string} Formatted concept
     */
    function formatConcept(concept) {
        if (!concept) return 'N/A';

        // Remove #V# prefix for cleaner display
        return concept.replace(/^#V#/, '');
    }

    /**
     * Format a property name from snake_case to Title Case.
     *
     * @param {string} name - The property name
     * @returns {string} Formatted name
     */
    function formatPropertyName(name) {
        return name.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
    }

    /**
     * Format a property value for display.
     *
     * @param {*} value - The property value
     * @returns {string} Formatted value
     */
    function formatPropertyValue(value) {
        if (value === null || value === undefined) {
            return 'N/A';
        }

        if (Array.isArray(value)) {
            return value.map(v => formatConcept(String(v))).join(', ');
        }

        if (typeof value === 'object') {
            return JSON.stringify(value, null, 2);
        }

        return String(value);
    }

    /**
     * Load metadata from the API.
     */
    async function loadMetadata() {
        state.loading = true;
        state.error = null;

        showLoading();
        hideError();

        try {
            state.metadata = await fetchPredicateMetadata(state.conceptId);
            renderMetadata();
        } catch (error) {
            state.error = error.message || 'Failed to load predicate metadata';
            showError(state.error);
            console.error('Error loading predicate metadata:', error);
        } finally {
            state.loading = false;
            hideLoading();
        }
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
     * Refresh the component.
     */
    function refresh() {
        return loadMetadata();
    }

    /**
     * Update the concept ID and reload.
     */
    function updateConceptId(newConceptId) {
        state.conceptId = newConceptId;
        return loadMetadata();
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
        updateConceptId,
        destroy,
        getState: () => ({ ...state }),
        getMetadata: () => state.metadata
    };
}
