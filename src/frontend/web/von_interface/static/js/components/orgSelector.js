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

import { getJson, getUserContext, postJson } from '../apiService.js';

// JVNAUTOSCI-1011: Switch to sessionStorage for window-scoped org context
const SS_ORG_CONTEXT = 'von_org_context';
const SS_ORG_ROLE = 'von_org_role';
const SS_CURRENT_ORG = 'von_current_org';
const SS_CURRENT_NAMESPACE = 'current_user_namespace';

// Keep legacy localStorage keys for backward compatibility during transition
const LS_ORG_CONTEXT = 'von_org_context';
const LS_ORG_ROLE = 'von_org_role';

/**
 * Load user's available organisations from backend
 */
export async function loadMyOrganisations() {
    try {
        const ctx = getUserContext();
        const userConceptId = ctx?.user_id;
        const url = userConceptId
            ? `/von/api/organisations/my_organisations?user_concept_id=${encodeURIComponent(userConceptId)}`
            : '/von/api/organisations/my_organisations';

        const response = await getJson(url);
        return response.organisations || [];
    } catch (err) {
        console.error('Error loading organisations:', err);
        return [];
    }
}

/**
 * Get current session context (user, org, role, namespace)
 */
export async function getSessionContext() {
    try {
        return await getJson('/von/api/session/context');
    } catch (err) {
        console.error('Error getting session context:', err);
        return {
            authenticated: false,
            user_id: null,
            organisation_id: null,
            role: null,
            namespace: null
        };
    }
}

/**
 * Switch to a different organisation
 * JVNAUTOSCI-1011: Now stores in sessionStorage for window-scoped contexts,
 * but also writes to localStorage for persistence across restarts/new windows.
 * @param {string} orgConceptId - The organisation concept ID to switch to
 * @param {string} [orgName] - Optional organisation name for event detail
 */
export async function switchOrganisation(orgConceptId, orgName = null) {
    try {
        const response = await postJson('/von/api/session/set_organisation', {
            organisation_concept_id: orgConceptId
        });

        if (response.status === 'updated') {
            const orgData = {
                concept_id: response.organisation_id,
                namespace: response.namespace
            };

            // JVNAUTOSCI-1011: Store in sessionStorage for window-scoped context
            sessionStorage.setItem(SS_ORG_CONTEXT, JSON.stringify(orgData));
            sessionStorage.setItem(SS_ORG_ROLE, response.role);

            // Also store in localStorage for persistence across restarts/new windows
            localStorage.setItem(LS_ORG_CONTEXT, JSON.stringify(orgData));
            localStorage.setItem(LS_ORG_ROLE, response.role);

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

        // Build dropdown HTML
        let html = '<div class="org-selector-wrapper">';
        html += '<label for="orgSelect" class="org-label">Organisation:</label>';
        html += '<select id="orgSelect" class="org-dropdown">';

        // Option for user-only (no org)
        html += '<option value="">Personal (No Org)</option>';

        // Organisation options
        organisations.forEach(org => {
            const selected = context.organisation_id === org.concept_id ? 'selected' : '';
            html += `<option value="${org.concept_id}" ${selected} data-role="${org.role}">`;
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
                        const label = selected ? String(selected.textContent || '').trim() : '';
                        const name = label.replace(/\s*\(.+\)\s*$/, '').trim();
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
                    if (orgId) {
                        // Get the org name from the selected option
                        const selected = e.target.selectedOptions?.[0];
                        const label = selected ? String(selected.textContent || '').trim() : '';
                        const orgDisplayName = label.replace(/\\s*\\(.+\\)\\s*$/, '').trim();
                        const response = await switchOrganisation(orgId, orgDisplayName || null);
                        try {
                            const orgData = {
                                id: null,
                                concept_id: orgId,
                                name: orgDisplayName || null
                            };
                            // JVNAUTOSCI-1011: Use sessionStorage for window-scoped context
                            sessionStorage.setItem(SS_CURRENT_ORG, JSON.stringify(orgData));
                            // Also persist to localStorage for restart/new window scenarios
                            localStorage.setItem('von_current_org', JSON.stringify(orgData));
                        } catch { }
                        try {
                            if (response?.namespace) {
                                sessionStorage.setItem(SS_CURRENT_NAMESPACE, response.namespace);
                                localStorage.setItem('current_user_namespace', response.namespace);
                            }
                        } catch { }
                        try { sessionStorage.removeItem('von_org_switching'); } catch { }
                    } else {
                        // Switch back to personal (no org)
                        const response = await postJson('/von/api/session/set_organisation', {
                            organisation_concept_id: null
                        });
                        // JVNAUTOSCI-1011: Clear both sessionStorage and localStorage
                        sessionStorage.removeItem(SS_ORG_CONTEXT);
                        sessionStorage.removeItem(SS_ORG_ROLE);
                        localStorage.removeItem(LS_ORG_CONTEXT);
                        localStorage.removeItem(LS_ORG_ROLE);
                        try { sessionStorage.removeItem(SS_CURRENT_ORG); } catch { }
                        try { localStorage.removeItem('von_current_org'); } catch { }
                        try {
                            if (response?.namespace) {
                                sessionStorage.setItem(SS_CURRENT_NAMESPACE, response.namespace);
                                localStorage.setItem('current_user_namespace', response.namespace);
                            }
                        } catch { }
                        try { sessionStorage.removeItem('von_org_switching'); } catch { }

                        // Dispatch event
                        const event = new CustomEvent('orgSwitched', {
                            detail: {
                                organisation_id: null,
                                organisation_name: null,
                                role: null,
                                namespace: response.namespace,
                                window_session_id: response.window_session_id
                            }
                        });
                        document.dispatchEvent(event);
                    }

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
        container.innerHTML = '<span class="org-error">Error loading organisations</span>';
    }
}

/**
 * Get stored organisation context (for UI state restoration)
 * JVNAUTOSCI-1011: Now reads from sessionStorage first (window-scoped),
 * falling back to localStorage for backward compatibility
 */
export function getStoredOrgContext() {
    // Try sessionStorage first (window-scoped)
    try {
        const sessionStored = sessionStorage.getItem(SS_ORG_CONTEXT);
        if (sessionStored) {
            return JSON.parse(sessionStored);
        }
    } catch {
        // Fall through to localStorage
    }
    // Fallback to localStorage for backward compatibility
    try {
        const stored = localStorage.getItem(LS_ORG_CONTEXT);
        return stored ? JSON.parse(stored) : null;
    } catch {
        return null;
    }
}

/**
 * Get stored organisation role
 * JVNAUTOSCI-1011: Now reads from sessionStorage first
 */
export function getStoredOrgRole() {
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
