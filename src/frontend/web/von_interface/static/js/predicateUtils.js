/**
 * Predicate detection and utility functions.
 *
 * This module provides logic for detecting when a concept is a predicate
 * and displaying predicate-specific information.
 */

import { getJson } from './apiService.js';

/**
 * Check if a concept is a predicate by examining its instance-of relationships.
 *
 * A concept is considered a predicate if it's an instance of:
 * - #V#predicate
 * - #V#binary_predicate
 * - #V#unary_predicate
 * - #V#n_ary_predicate
 * - Or any type containing "predicate" in its name (case-insensitive)
 * - Or any other predicate-related type
 *
 * @param {Object} conceptData - The concept data object with relationships
 * @returns {boolean} True if the concept is a predicate
 */
export function isPredicate(conceptData) {
    if (!conceptData || !conceptData.relationships) {
        return false;
    }

    const instanceOf = conceptData.relationships.is_an_instance_of;
    if (!instanceOf) {
        return false;
    }

    // Normalize to array
    const types = Array.isArray(instanceOf) ? instanceOf : [instanceOf];

    // Check if any of the types indicate this is a predicate
    const predicateTypes = [
        '#V#predicate',
        '#V#binary_predicate',
        '#V#unary_predicate',
        '#V#n_ary_predicate',
        '#V#ternary_predicate',
        '#V#relation',
        '#V#property'
    ];

    return types.some(type => {
        // Direct match with known predicate types
        if (predicateTypes.some(predicateType =>
            type === predicateType || type?.startsWith?.(predicateType)
        )) {
            return true;
        }

        // Check if the type name contains "predicate" (case-insensitive)
        // This catches instances like "nearly Functional Binary Predicate on Person"
        if (typeof type === 'string' && type.toLowerCase().includes('predicate')) {
            return true;
        }

        return false;
    });
}

/**
 * Get the predicate type (unary, binary, n-ary, etc.) from concept data.
 *
 * @param {Object} conceptData - The concept data object with relationships
 * @returns {string|null} The predicate type or null if not a predicate
 */
export function getPredicateType(conceptData) {
    if (!isPredicate(conceptData)) {
        return null;
    }

    const instanceOf = conceptData.relationships.is_an_instance_of;
    const types = Array.isArray(instanceOf) ? instanceOf : [instanceOf];

    // Check for specific predicate types in priority order
    if (types.includes('#V#unary_predicate')) return 'unary';
    if (types.includes('#V#binary_predicate')) return 'binary';
    if (types.includes('#V#ternary_predicate')) return 'ternary';
    if (types.includes('#V#n_ary_predicate')) return 'n-ary';

    // Generic predicate
    if (types.includes('#V#predicate')) return 'predicate';

    return 'predicate'; // Default
}

/**
 * Fetch predicate extent from the backend.
 *
 * @param {string} conceptId - The concept ID of the predicate
 * @param {Object} options - Query options (limit, offset, sort_by, etc.)
 * @returns {Promise<Object>} The extent data
 */
export async function fetchPredicateExtent(conceptId, options = {}) {
    const {
        limit = 100,
        offset = 0,
        sort_by = 'created_at',
        sort_order = 'desc',
        subject_type = null,
        object_type = null,
        source = 'all'
    } = options;

    const params = new URLSearchParams({
        limit: limit.toString(),
        offset: offset.toString(),
        sort_by,
        sort_order,
        source
    });

    if (subject_type) params.append('subject_type', subject_type);
    if (object_type) params.append('object_type', object_type);

    try {
        const data = await getJson(`/api/predicates/${encodeURIComponent(conceptId)}/extent?${params}`);
        return data;
    } catch (error) {
        console.error('Error fetching predicate extent:', error);
        throw error;
    }
}

/**
 * Fetch predicate metadata from the backend.
 *
 * @param {string} conceptId - The concept ID of the predicate
 * @returns {Promise<Object>} The metadata
 */
export async function fetchPredicateMetadata(conceptId) {
    try {
        const data = await getJson(`/api/predicates/${encodeURIComponent(conceptId)}/metadata`);
        return data;
    } catch (error) {
        console.error('Error fetching predicate metadata:', error);
        throw error;
    }
}

/**
 * Create a visual badge indicating a concept is a predicate.
 *
 * @param {string} predicateType - The type of predicate (binary, unary, etc.)
 * @returns {HTMLElement} The badge element
 */
export function createPredicateBadge(predicateType) {
    const badge = document.createElement('span');
    badge.className = 'predicate-badge';
    badge.textContent = predicateType ? `${predicateType} predicate` : 'predicate';
    badge.title = 'This concept is a predicate';
    badge.style.cssText = `
        display: inline-block;
        background-color: #4CAF50;
        color: white;
        padding: 2px 8px;
        border-radius: 3px;
        font-size: 0.85em;
        font-weight: bold;
        margin-left: 8px;
        vertical-align: middle;
    `;
    return badge;
}

/**
 * Check if the predicate view is currently enabled for a concept.
 * This can be used to toggle between standard and predicate views.
 *
 * @param {string} conceptId - The concept ID
 * @returns {boolean} True if predicate view should be shown
 */
export function shouldShowPredicateView(conceptId) {
    // Check localStorage or session storage for user preference
    const key = `predicate_view_${conceptId}`;
    const stored = sessionStorage.getItem(key);

    // Default to true if the concept is a predicate
    return stored !== 'false';
}

/**
 * Toggle the predicate view preference for a concept.
 *
 * @param {string} conceptId - The concept ID
 * @param {boolean} enabled - Whether to enable the predicate view
 */
export function setPredicateViewPreference(conceptId, enabled) {
    const key = `predicate_view_${conceptId}`;
    sessionStorage.setItem(key, enabled.toString());
}

/**
 * Format extent item for display.
 *
 * @param {Object} item - An extent item from the API
 * @returns {Object} Formatted item with display-ready properties
 */
export function formatExtentItem(item) {
    return {
        subject: item.subject,
        subjectDisplay: item.subject_name || item.subject,
        predicate: item.predicate,
        object: item.object,
        objectDisplay: item.object_name || item.object,
        source: item.source,
        language: item.object_language,
        createdAt: item.created_at ? new Date(item.created_at).toLocaleDateString() : null,
        updatedAt: item.updated_at ? new Date(item.updated_at).toLocaleDateString() : null
    };
}

/**
 * Export functions for testing
 */
export const __test__ = {
    isPredicate,
    getPredicateType,
    formatExtentItem
};
