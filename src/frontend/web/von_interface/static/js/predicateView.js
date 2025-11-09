/**
 * Predicate view integration module.
 *
 * This module handles the integration of predicate-specific UI components
 * (extent display and metadata panel) into the concept tab.
 */

import { createPredicateExtentDisplay } from './predicateExtentDisplay.js';
import { createPredicateMetadataPanel } from './predicateMetadataPanel.js';
import { isPredicate } from './predicateUtils.js';

// Track active predicate view components
const activePredicateViews = new Map();

/**
 * Check if predicate view is currently active for a concept.
 *
 * @param {string} conceptId - The concept ID
 * @returns {boolean} True if predicate view is active
 */
export function isPredicateViewActive(conceptId) {
    return activePredicateViews.has(conceptId);
}

/**
 * Initialize predicate view for a concept tab if the concept is a predicate.
 *
 * @param {string} conceptId - The concept ID
 * @param {string} uniqueIdSuffix - The unique suffix for dynamic tab elements
 * @param {Object} conceptData - The concept data (must include relationships)
 * @returns {Promise<boolean>} True if predicate view was initialized
 */
export async function initializePredicateView(conceptId, uniqueIdSuffix, conceptData) {
    console.log(`[predicateView] Checking if ${conceptId} is a predicate`);

    // Check if this concept is a predicate
    if (!conceptData || !isPredicate(conceptData)) {
        console.log(`[predicateView] ${conceptId} is not a predicate, skipping predicate view`);
        return false;
    }

    console.log(`[predicateView] ${conceptId} is a predicate, initializing predicate view`);

    // Find the container to inject predicate-specific sections
    const tabContent = document.getElementById(`conceptTab_${uniqueIdSuffix}`);
    if (!tabContent) {
        console.warn(`[predicateView] Tab content not found for ${conceptId}`);
        return false;
    }

    // Find or create a container for predicate sections
    let predicateContainer = tabContent.querySelector('.predicate-view-container');
    if (!predicateContainer) {
        predicateContainer = document.createElement('div');
        predicateContainer.className = 'predicate-view-container';
        predicateContainer.id = `predicate-view-container-${uniqueIdSuffix}`;

        // Insert after the names section or at the beginning of type sections
        const namesSection = document.getElementById(`namesSection_${uniqueIdSuffix}`);
        const typeSections = document.getElementById(`typeSections_${uniqueIdSuffix}`);

        if (namesSection && namesSection.nextSibling) {
            namesSection.parentNode.insertBefore(predicateContainer, namesSection.nextSibling);
        } else if (typeSections) {
            typeSections.insertBefore(predicateContainer, typeSections.firstChild);
        } else {
            // Fallback: append to tab content
            tabContent.appendChild(predicateContainer);
        }
    }

    // Clear any existing predicate view components
    predicateContainer.innerHTML = '';

    try {
        // Create metadata panel
        const metadataContainer = document.createElement('div');
        metadataContainer.className = 'predicate-metadata-container';
        predicateContainer.appendChild(metadataContainer);

        const metadataPanel = createPredicateMetadataPanel(conceptId, metadataContainer);

        // Create extent display
        const extentContainer = document.createElement('div');
        extentContainer.className = 'predicate-extent-container';
        predicateContainer.appendChild(extentContainer);

        const extentDisplay = createPredicateExtentDisplay(conceptId, extentContainer);

        // Store component references for cleanup
        activePredicateViews.set(conceptId, {
            conceptId,
            uniqueIdSuffix,
            predicateContainer,
            metadataPanel,
            extentDisplay
        });

        console.log(`[predicateView] Predicate view initialized successfully for ${conceptId}`);
        return true;

    } catch (error) {
        console.error(`[predicateView] Error initializing predicate view for ${conceptId}:`, error);
        return false;
    }
}

/**
 * Update predicate view when concept data changes.
 *
 * @param {string} conceptId - The concept ID
 * @returns {Promise<boolean>} True if update was successful
 */
export async function updatePredicateView(conceptId) {
    const view = activePredicateViews.get(conceptId);
    if (!view) {
        console.log(`[predicateView] No active predicate view for ${conceptId}`);
        return false;
    }

    try {
        // Refresh both components
        await Promise.all([
            view.metadataPanel.refresh(),
            view.extentDisplay.refresh()
        ]);

        console.log(`[predicateView] Predicate view updated successfully for ${conceptId}`);
        return true;

    } catch (error) {
        console.error(`[predicateView] Error updating predicate view for ${conceptId}:`, error);
        return false;
    }
}

/**
 * Destroy predicate view for a concept.
 *
 * @param {string} conceptId - The concept ID
 */
export function destroyPredicateView(conceptId) {
    const view = activePredicateViews.get(conceptId);
    if (!view) {
        return;
    }

    try {
        // Destroy components
        if (view.metadataPanel) {
            view.metadataPanel.destroy();
        }

        if (view.extentDisplay) {
            view.extentDisplay.destroy();
        }

        // Remove container from DOM
        if (view.predicateContainer && view.predicateContainer.parentNode) {
            view.predicateContainer.parentNode.removeChild(view.predicateContainer);
        }

        // Remove from tracking
        activePredicateViews.delete(conceptId);

        console.log(`[predicateView] Predicate view destroyed for ${conceptId}`);

    } catch (error) {
        console.error(`[predicateView] Error destroying predicate view for ${conceptId}:`, error);
    }
}

/**
 * Clean up all active predicate views.
 */
export function destroyAllPredicateViews() {
    for (const conceptId of activePredicateViews.keys()) {
        destroyPredicateView(conceptId);
    }
}
