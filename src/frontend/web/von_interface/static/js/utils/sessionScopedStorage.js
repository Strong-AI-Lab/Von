/**
 * Session-scoped storage helpers for multi-window organisation/namespace isolation.
 * JVNAUTOSCI-1011: All org/namespace reads use sessionStorage first (per-window),
 * with localStorage fallback (persistent across restarts/new windows).
 *
 * IMPORTANT: Always use these helpers instead of direct localStorage/sessionStorage
 * access for organisation and namespace data to ensure proper window isolation.
 */

// Storage keys
const KEYS = {
    CURRENT_ORG: 'von_current_org',
    ORG_CONTEXT: 'von_org_context',
    NAMESPACE: 'current_user_namespace',
    NAMESPACE_LEGACY: 'von_namespace',
    ORG_SWITCHING: 'von_org_switching',
};

/**
 * Get the current organisation context (session-scoped).
 * Tries sessionStorage first (per-window), then localStorage (shared).
 * @returns {Object|null} Parsed org object with id, concept_id, name, namespace, or null
 */
export function getSessionScopedOrgContext() {
    // Try von_current_org from sessionStorage first
    try {
        const sessionOrg = sessionStorage.getItem(KEYS.CURRENT_ORG);
        if (sessionOrg) return JSON.parse(sessionOrg);
    } catch { /* ignore */ }

    // Fallback to localStorage von_current_org
    try {
        const localOrg = localStorage.getItem(KEYS.CURRENT_ORG);
        if (localOrg) return JSON.parse(localOrg);
    } catch { /* ignore */ }

    // Try von_org_context from sessionStorage
    try {
        const sessionCtx = sessionStorage.getItem(KEYS.ORG_CONTEXT);
        if (sessionCtx) return JSON.parse(sessionCtx);
    } catch { /* ignore */ }

    // Fallback to localStorage von_org_context
    try {
        const localCtx = localStorage.getItem(KEYS.ORG_CONTEXT);
        if (localCtx) return JSON.parse(localCtx);
    } catch { /* ignore */ }

    return null;
}

/**
 * Get the current organisation ID (concept_id or id).
 * @returns {string|null} Organisation concept ID or null
 */
export function getSessionScopedOrgId() {
    const org = getSessionScopedOrgContext();
    return org?.concept_id || org?.id || null;
}

/**
 * Get the current namespace (session-scoped).
 * Tries sessionStorage first (per-window), then localStorage (shared).
 * @returns {string} Namespace string or empty string
 */
export function getSessionScopedNamespace() {
    // Try current_user_namespace from sessionStorage
    try {
        const sessionNs = sessionStorage.getItem(KEYS.NAMESPACE);
        if (sessionNs) return sessionNs.trim();
    } catch { /* ignore */ }

    // Fallback to localStorage current_user_namespace
    try {
        const localNs = localStorage.getItem(KEYS.NAMESPACE);
        if (localNs) return localNs.trim();
    } catch { /* ignore */ }

    // Try von_namespace from sessionStorage (legacy)
    try {
        const sessionLegacy = sessionStorage.getItem(KEYS.NAMESPACE_LEGACY);
        if (sessionLegacy) return sessionLegacy.trim();
    } catch { /* ignore */ }

    // Fallback to localStorage von_namespace (legacy)
    try {
        const localLegacy = localStorage.getItem(KEYS.NAMESPACE_LEGACY);
        if (localLegacy) return localLegacy.trim();
    } catch { /* ignore */ }

    return '';
}

/**
 * Set the current organisation context (writes to both storages).
 * @param {Object|null} orgData - Organisation object or null to clear
 */
export function setSessionScopedOrgContext(orgData) {
    try {
        if (orgData == null) {
            sessionStorage.removeItem(KEYS.CURRENT_ORG);
            localStorage.removeItem(KEYS.CURRENT_ORG);
        } else {
            const json = JSON.stringify(orgData);
            sessionStorage.setItem(KEYS.CURRENT_ORG, json);
            localStorage.setItem(KEYS.CURRENT_ORG, json);
        }
    } catch { /* ignore */ }
}

/**
 * Set the current namespace (writes to both storages).
 * @param {string|null} namespace - Namespace string or null to clear
 */
export function setSessionScopedNamespace(namespace) {
    try {
        if (namespace == null || namespace === '') {
            sessionStorage.removeItem(KEYS.NAMESPACE);
            localStorage.removeItem(KEYS.NAMESPACE);
        } else {
            sessionStorage.setItem(KEYS.NAMESPACE, namespace);
            localStorage.setItem(KEYS.NAMESPACE, namespace);
        }
    } catch { /* ignore */ }
}

/**
 * Check if sessionStorage already has org context (avoid redundant sync).
 * @returns {boolean}
 */
export function hasSessionOrgContext() {
    try {
        return !!(sessionStorage.getItem(KEYS.CURRENT_ORG) || sessionStorage.getItem(KEYS.ORG_CONTEXT));
    } catch {
        return false;
    }
}

/**
 * Check if sessionStorage already has namespace (avoid redundant sync).
 * @returns {boolean}
 */
export function hasSessionNamespace() {
    try {
        return !!(sessionStorage.getItem(KEYS.NAMESPACE));
    } catch {
        return false;
    }
}

/**
 * Sync org context from localStorage to sessionStorage if session is empty.
 * Called on page load to bootstrap window context from persistent storage.
 */
export function syncOrgContextFromLocalStorage() {
    if (hasSessionOrgContext()) return;

    try {
        const lsOrg = localStorage.getItem(KEYS.CURRENT_ORG);
        if (lsOrg) sessionStorage.setItem(KEYS.CURRENT_ORG, lsOrg);
    } catch { /* ignore */ }

    try {
        const lsCtx = localStorage.getItem(KEYS.ORG_CONTEXT);
        if (lsCtx) sessionStorage.setItem(KEYS.ORG_CONTEXT, lsCtx);
    } catch { /* ignore */ }
}

/**
 * Sync namespace from localStorage to sessionStorage if session is empty.
 * Called on page load to bootstrap window context from persistent storage.
 */
export function syncNamespaceFromLocalStorage() {
    if (hasSessionNamespace()) return;

    try {
        const lsNs = localStorage.getItem(KEYS.NAMESPACE);
        if (lsNs) sessionStorage.setItem(KEYS.NAMESPACE, lsNs);
    } catch { /* ignore */ }
}

/**
 * Clear all org/namespace context from both storages (used on logout).
 */
export function clearAllOrgContext() {
    try {
        sessionStorage.removeItem(KEYS.CURRENT_ORG);
        sessionStorage.removeItem(KEYS.ORG_CONTEXT);
        sessionStorage.removeItem(KEYS.NAMESPACE);
        sessionStorage.removeItem(KEYS.ORG_SWITCHING);
        localStorage.removeItem(KEYS.CURRENT_ORG);
        localStorage.removeItem(KEYS.ORG_CONTEXT);
    } catch { /* ignore */ }
}

// Export keys for direct access if needed (e.g., for specific checks)
export { KEYS as STORAGE_KEYS };

