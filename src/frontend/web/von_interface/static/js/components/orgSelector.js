/**
 * Organisation Selector Component
 *
 * Manages organisation context switching and role display for Phase 2.
 * Integrates with session management to switch RAG namespace on org change.
 */

import { getJson, getUserContext, postJson } from '../apiService.js';

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
 */
export async function switchOrganisation(orgConceptId) {
    try {
        const response = await postJson('/von/api/session/set_organisation', {
            organisation_concept_id: orgConceptId
        });

        if (response.status === 'updated') {
            // Store in localStorage for persistence (keep concept_id form for UI)
            localStorage.setItem(LS_ORG_CONTEXT, JSON.stringify({
                concept_id: response.organisation_id,
                namespace: response.namespace
            }));
            localStorage.setItem(LS_ORG_ROLE, response.role);

            // Dispatch event so other components can react to org change
            const event = new CustomEvent('orgSwitched', {
                detail: {
                    organisation_id: response.organisation_id,
                    role: response.role,
                    namespace: response.namespace
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
                        localStorage.setItem('von_org_switching', JSON.stringify({
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
                        const response = await switchOrganisation(orgId);
                        try {
                            const selected = e.target.selectedOptions?.[0];
                            const label = selected ? String(selected.textContent || '').trim() : '';
                            const name = label.replace(/\s*\(.+\)\s*$/, '').trim();
                            localStorage.setItem('von_current_org', JSON.stringify({
                                id: null,
                                concept_id: orgId,
                                name: name || null
                            }));
                        } catch { }
                        try {
                            if (response?.namespace) {
                                localStorage.setItem('current_user_namespace', response.namespace);
                            }
                        } catch { }
                        try { localStorage.removeItem('von_org_switching'); } catch { }
                    } else {
                        // Switch back to personal (no org)
                        const response = await postJson('/von/api/session/set_organisation', {
                            organisation_concept_id: null
                        });
                        // Clear localStorage
                        localStorage.removeItem(LS_ORG_CONTEXT);
                        localStorage.removeItem(LS_ORG_ROLE);
                        try { localStorage.removeItem('von_current_org'); } catch { }
                        try {
                            if (response?.namespace) {
                                localStorage.setItem('current_user_namespace', response.namespace);
                            }
                        } catch { }
                        try { localStorage.removeItem('von_org_switching'); } catch { }

                        // Dispatch event
                        const event = new CustomEvent('orgSwitched', {
                            detail: {
                                organisation_id: null,
                                role: null,
                                namespace: response.namespace
                            }
                        });
                        document.dispatchEvent(event);
                    }

                    // Refresh the selector to show updated state
                    await renderOrgSelector(containerId);
                } catch (err) {
                    console.error('Error switching organisation:', err);
                    alert('Failed to switch organisation: ' + err.message);
                    try { localStorage.removeItem('von_org_switching'); } catch { }
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
 */
export function getStoredOrgContext() {
    try {
        const stored = localStorage.getItem(LS_ORG_CONTEXT);
        return stored ? JSON.parse(stored) : null;
    } catch {
        return null;
    }
}

/**
 * Get stored organisation role
 */
export function getStoredOrgRole() {
    return localStorage.getItem(LS_ORG_ROLE);
}

/**
 * Clear stored organisation context (used on logout)
 */
export function clearStoredOrgContext() {
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
