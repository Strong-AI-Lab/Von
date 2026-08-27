/**
 * Organisation Selector Component
 *
 * Manages organisation context switching and role display for Phase 2.
 * Integrates with session management to switch RAG namespace on org change.
 *
 * JVNAUTOSCI-1011: Now uses sessionStorage (window-scoped) instead of localStorage
 * for org context, enabling different organisation contexts in different browser
 * windows without interference.
 */

import { getJsonDetailed, postJson } from '../apiService.js';
import {
    armRetryableLoadState,
    clearRetryableLoadState,
    describeRetryableLoadFailure,
} from '../utils/retryableLoadState.js';
import {
    getSessionScopedOrgContext,
    hasSessionPersonalOrgContext,
    setSessionScopedOrgContext,
} from '../utils/sessionScopedStorage.js';

// JVNAUTOSCI-1011: Switch to sessionStorage for window-scoped org context
const SS_ORG_CONTEXT = 'von_org_context';
const SS_ORG_ROLE = 'von_org_role';
const SS_CURRENT_ORG = 'von_current_org';
const SS_CURRENT_NAMESPACE = 'current_user_namespace';

// Keep legacy localStorage keys for backward compatibility during transition
const LS_ORG_CONTEXT = 'von_org_context';
const LS_ORG_ROLE = 'von_org_role';

export function normaliseOrganisationDisplayName(label) {
    const text = String(label || '').trim();
    if (!text) return null;
    return text.replace(/\s*\([^()]+\)\s*$/, '').trim() || text;
}

/**
 * Load user's available organisations from backend
 */
export async function loadMyOrganisations() {
    const { data } = await getJsonDetailed('/von/api/organisations/my_organisations');
    return data?.organisations || [];
}

/**
 * Get current session context (user, org, role, namespace)
 */
export async function getSessionContext() {
    const { data } = await getJsonDetailed('/von/api/session/context');
    return data;
}

function renderRetryableOrgSelectorFailure(container, containerId, error) {
    if (!container) return;

    const failure = describeRetryableLoadFailure(
        error,
        'Organisations are temporarily unavailable.'
    );

    container.innerHTML = '';

    const message = document.createElement('span');
    message.className = 'org-error';
    message.textContent = failure.retryable
        ? `${failure.message} Click to retry.`
        : failure.message;
    container.appendChild(message);
    container.title = failure.message;

    if (failure.retryable) {
        const retryButton = document.createElement('button');
        retryButton.type = 'button';
        retryButton.className = 'org-retry-button';
        retryButton.textContent = 'Retry';
        retryButton.style.marginLeft = '8px';
        retryButton.addEventListener('click', () => {
            renderOrgSelector(containerId);
        });
        container.appendChild(retryButton);

        armRetryableLoadState(container, () => renderOrgSelector(containerId), {
            backgroundDelayMs: Math.max(0, failure.retryAfterSeconds) * 1000
        });
    } else {
        clearRetryableLoadState(container);
    }
}

/**
 * Switch to a different organisation
 * JVNAUTOSCI-1011: Now stores in sessionStorage for window-scoped contexts,
 * but also writes to localStorage for persistence across restarts/new windows.
 * @param {string|null} orgConceptId - The organisation concept ID, or null for Personal
 * @param {string} [orgName] - Optional organisation name for event detail
 */
export async function switchOrganisation(orgConceptId, orgName = null) {
    try {
        const targetOrganisationId = orgConceptId || null;
        const response = await postJson('/von/api/session/set_organisation', {
            organisation_concept_id: targetOrganisationId
        });

        if (response.status === 'updated') {
            if (response.organisation_id) {
                const orgData = {
                    concept_id: response.organisation_id,
                    namespace: response.namespace
                };
                const currentOrgData = {
                    id: null,
                    concept_id: response.organisation_id,
                    name: orgName || null
                };

                // JVNAUTOSCI-1011: Store in sessionStorage for window-scoped context
                sessionStorage.setItem(SS_ORG_CONTEXT, JSON.stringify(orgData));
                sessionStorage.setItem(SS_ORG_ROLE, response.role || 'member');
                setSessionScopedOrgContext(currentOrgData);

                // Also store in localStorage for persistence across restarts/new windows
                localStorage.setItem(LS_ORG_CONTEXT, JSON.stringify(orgData));
                localStorage.setItem(LS_ORG_ROLE, response.role || 'member');
            } else {
                sessionStorage.removeItem(SS_ORG_CONTEXT);
                sessionStorage.removeItem(SS_ORG_ROLE);
                localStorage.removeItem(LS_ORG_CONTEXT);
                localStorage.removeItem(LS_ORG_ROLE);
                setSessionScopedOrgContext(null);
            }

            if (response.namespace) {
                sessionStorage.setItem(SS_CURRENT_NAMESPACE, response.namespace);
                localStorage.setItem(SS_CURRENT_NAMESPACE, response.namespace);
            }
            sessionStorage.removeItem('von_org_switching');

            // Dispatch event so other components can react to org change
            const event = new CustomEvent('orgSwitched', {
                detail: {
                    organisation_id: response.organisation_id,
                    organisation_name: orgName,
                    role: response.role,
                    namespace: response.namespace,
                    window_session_id: response.window_session_id
                }
            });
            document.dispatchEvent(event);

            return response;
        }
        throw new Error(response.error || 'Failed to switch organisation');
    } catch (err) {
        console.error('Error switching organisation:', err);
        throw err;
    }
}

/**
 * Render organisation selector dropdown
 * JVNAUTOSCI-1011: Now uses sessionStorage for window-scoped org context
 */
export async function renderOrgSelector(containerId) {
    const container = document.getElementById(containerId);
    if (!container) {
        console.warn(`Container ${containerId} not found`);
        return;
    }

    try {
        // Load organisations and current context
        const [organisations, context] = await Promise.all([
            loadMyOrganisations(),
            getSessionContext()
        ]);

        if (organisations.length === 0) {
            container.innerHTML = '<span class="org-info">No organisations</span>';
            return;
        }

        clearRetryableLoadState(container);
        container.title = '';

        // Build dropdown HTML
        let html = '<div class="org-selector-wrapper">';
        html += '<label for="orgSelect" class="org-label">Organisation:</label>';
        html += '<select id="orgSelect" class="org-dropdown">';

        // Option for user-only (no org)
        html += '<option value="">Personal (No Org)</option>';

        // Organisation options
        organisations.forEach(org => {
            const selected = context.organisation_id === org.concept_id ? 'selected' : '';
            html += `<option value="${org.concept_id}" ${selected} data-role="${org.role}" data-concept-id="${org.concept_id}">`;
            html += `${org.name} (${org.role})`;
            html += `</option>`;
        });

        html += '</select>';

        // Show current role if org selected
        if (context.organisation_id && context.role) {
            html += `<span class="org-role"> • Role: ${context.role}</span>`;
        }

        html += '</div>';

        container.innerHTML = html;

        // Attach event listener
        const select = document.getElementById('orgSelect');
        if (select) {
            select.addEventListener('change', async (e) => {
                const orgId = e.target.value || null;
                try {
                    try {
                        const selected = e.target.selectedOptions?.[0];
                        const name = normaliseOrganisationDisplayName(selected?.textContent);
                        // JVNAUTOSCI-1011: Use sessionStorage for switching indicator
                        sessionStorage.setItem('von_org_switching', JSON.stringify({
                            concept_id: orgId || null,
                            name: name || null
                        }));
                        if (window.parent?.updateModelInfoFooterDisplay) {
                            window.parent.updateModelInfoFooterDisplay();
                        } else if (window.updateModelInfoFooterDisplay) {
                            window.updateModelInfoFooterDisplay();
                        }
                    } catch { }
                    const selected = e.target.selectedOptions?.[0];
                    const orgDisplayName = orgId
                        ? normaliseOrganisationDisplayName(selected?.textContent)
                        : null;
                    await switchOrganisation(orgId, orgDisplayName || null);

                    // Refresh the selector to show updated state
                    await renderOrgSelector(containerId);
                } catch (err) {
                    console.error('Error switching organisation:', err);
                    alert('Failed to switch organisation: ' + err.message);
                    try { sessionStorage.removeItem('von_org_switching'); } catch { }
                    // Revert selection
                    await renderOrgSelector(containerId);
                }
            });
        }

    } catch (err) {
        console.error('Error rendering org selector:', err);
        renderRetryableOrgSelectorFailure(container, containerId, err);
    }
}

/**
 * Get stored organisation context (for UI state restoration)
 * JVNAUTOSCI-1011: Now reads from sessionStorage first (window-scoped),
 * falling back to localStorage for backward compatibility
 */
export function getStoredOrgContext() {
    return getSessionScopedOrgContext();
}

/**
 * Get stored organisation role
 * JVNAUTOSCI-1011: Now reads from sessionStorage first
 */
export function getStoredOrgRole() {
    if (hasSessionPersonalOrgContext()) {
        return null;
    }
    // Try sessionStorage first
    const sessionRole = sessionStorage.getItem(SS_ORG_ROLE);
    if (sessionRole) {
        return sessionRole;
    }
    // Fallback to localStorage
    return localStorage.getItem(LS_ORG_ROLE);
}

/**
 * Clear stored organisation context (used on logout)
 * JVNAUTOSCI-1011: Now clears both sessionStorage and localStorage
 */
export function clearStoredOrgContext() {
    // Clear sessionStorage
    sessionStorage.removeItem(SS_ORG_CONTEXT);
    sessionStorage.removeItem(SS_ORG_ROLE);
    sessionStorage.removeItem(SS_CURRENT_ORG);
    sessionStorage.removeItem(SS_CURRENT_NAMESPACE);
    // Clear localStorage for complete cleanup
    localStorage.removeItem(LS_ORG_CONTEXT);
    localStorage.removeItem(LS_ORG_ROLE);
}

/**
 * Display org context in navigation/header
 */
export function displayContextIndicator(containerId) {
    const container = document.getElementById(containerId);
    if (!container) return;

    getSessionContext().then(context => {
        if (!context.authenticated) {
            container.textContent = 'Not authenticated';
            return;
        }

        const userDisplay = context.user_id ? context.user_id.replace(/_/g, ' ') : 'Unknown';

        if (context.organisation_id) {
            const orgDisplay = context.organisation_id.replace(/_/g, ' ');
            const roleDisplay = context.role ? ` (${context.role})` : '';
            container.textContent = `${userDisplay} @ ${orgDisplay}${roleDisplay}`;
            container.className = 'context-indicator org-context';
        } else {
            container.textContent = `${userDisplay}`;
            container.className = 'context-indicator personal-context';
        }
    }).catch(err => {
        console.error('Error displaying context:', err);
        container.textContent = 'Error loading context';
    });
}

/**
 * Listen for organisation switches and trigger RAG namespace updates
 */
export function setupOrgSwitchListener(callback) {
    document.addEventListener('orgSwitched', (e) => {
        const { organisation_id, namespace } = e.detail;
        console.log(`Organisation switched to: ${organisation_id || 'personal'}, namespace: ${namespace}`);
        if (callback) {
            callback(organisation_id, namespace);
        }
    });
}
