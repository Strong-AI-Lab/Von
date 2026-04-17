/**
 * Session-scoped storage helpers for multi-window organisation/namespace isolation.
 * JVNAUTOSCI-1011: All org/namespace reads use sessionStorage first (per-window),
 * with localStorage fallback (persistent across restarts/new windows).
 *
 * IMPORTANT: Always use these helpers instead of direct localStorage/sessionStorage
 * access for organisation and namespace data to ensure proper window isolation.
 */

import { parseStoredContextValue } from './runtimeIdentityBootstrap.js';

// Storage keys
const KEYS = {
    CURRENT_ORG: 'von_current_org',
    ORG_CONTEXT: 'von_org_context',
    NAMESPACE: 'current_user_namespace',
    NAMESPACE_LEGACY: 'von_namespace',
    ORG_SWITCHING: 'von_org_switching',
};

function readJsonFromStorage(storage, key) {
    try {
        const raw = storage?.getItem(key);
        return parseStoredContextValue(raw);
    } catch {
        return null;
    }
}

function conceptIdToNamespaceSlug(value) {
    const raw = String(value || '').trim();
    if (!raw) return '';
    return raw.startsWith('#V#') ? raw.slice(3) : raw;
}

function readTrimmedStorageValue(storage, key) {
    try {
        const raw = storage?.getItem(key);
        return typeof raw === 'string' ? raw.trim() : '';
    } catch {
        return '';
    }
}

function isOrgScopedNamespace(namespace) {
    return typeof namespace === 'string' && namespace.includes('@');
}

function encodeStorageScopeSegment(value) {
    const raw = typeof value === 'string' ? value.trim() : '';
    if (!raw) return '';
    try {
        return encodeURIComponent(raw);
    } catch {
        return raw.replace(/[^A-Za-z0-9._~-]+/g, '_');
    }
}

export function deriveNamespaceFromStoredContext() {
    const local = typeof localStorage !== 'undefined' ? localStorage : null;
    const session = typeof sessionStorage !== 'undefined' ? sessionStorage : null;
    const storedUser = readJsonFromStorage(local, 'von_current_user');
    const userSlug = conceptIdToNamespaceSlug(storedUser?.concept_id);
    if (!userSlug) return '';

    const storedOrg =
        readJsonFromStorage(session, KEYS.CURRENT_ORG)
        || readJsonFromStorage(local, KEYS.CURRENT_ORG)
        || readJsonFromStorage(session, KEYS.ORG_CONTEXT)
        || readJsonFromStorage(local, KEYS.ORG_CONTEXT);
    const orgSlug = conceptIdToNamespaceSlug(storedOrg?.concept_id);

    return orgSlug ? `#V#${userSlug}@${orgSlug}` : `#V#${userSlug}`;
}

function resolvePreferredNamespaceFromStorage() {
    const local = typeof localStorage !== 'undefined' ? localStorage : null;
    const session = typeof sessionStorage !== 'undefined' ? sessionStorage : null;
    const sessionNamespace = readTrimmedStorageValue(session, KEYS.NAMESPACE);
    const localNamespace = readTrimmedStorageValue(local, KEYS.NAMESPACE);
    const sessionLegacyNamespace = readTrimmedStorageValue(session, KEYS.NAMESPACE_LEGACY);
    const localLegacyNamespace = readTrimmedStorageValue(local, KEYS.NAMESPACE_LEGACY);
    const derivedNamespace = deriveNamespaceFromStoredContext();
    const storedCandidates = [
        sessionNamespace,
        localNamespace,
        sessionLegacyNamespace,
        localLegacyNamespace,
    ].filter(Boolean);
    const firstStoredNamespace = storedCandidates[0] || '';
    const firstOrgScopedStoredNamespace =
        storedCandidates.find(isOrgScopedNamespace) || '';

    // Browser storage can preserve the selected organisation while leaving the
    // primary namespace key stale and user-only after restart. In that state,
    // the user+organisation-derived composite namespace is authoritative.
    if (derivedNamespace) {
        if (!firstStoredNamespace || firstStoredNamespace === derivedNamespace) {
            return derivedNamespace;
        }
        if (isOrgScopedNamespace(derivedNamespace)) {
            return derivedNamespace;
        }
    }

    return firstOrgScopedStoredNamespace || firstStoredNamespace || derivedNamespace;
}

function repairPrimaryNamespaceStorage(namespace) {
    const preferredNamespace = typeof namespace === 'string' ? namespace.trim() : '';
    const local = typeof localStorage !== 'undefined' ? localStorage : null;
    const session = typeof sessionStorage !== 'undefined' ? sessionStorage : null;
    const sessionNamespace = readTrimmedStorageValue(session, KEYS.NAMESPACE);
    const localNamespace = readTrimmedStorageValue(local, KEYS.NAMESPACE);

    if (!preferredNamespace) {
        if (sessionNamespace || localNamespace) {
            setSessionScopedNamespace(null);
        }
        return '';
    }

    if (sessionNamespace !== preferredNamespace || localNamespace !== preferredNamespace) {
        setSessionScopedNamespace(preferredNamespace);
    }

    return preferredNamespace;
}

/**
 * Get the current organisation context (session-scoped).
 * Tries sessionStorage first (per-window), then localStorage (shared).
 * @returns {Object|null} Parsed org object with id, concept_id, name, namespace, or null
 */
export function getSessionScopedOrgContext() {
    // Try von_current_org from sessionStorage first
    try {
        const sessionOrg = sessionStorage.getItem(KEYS.CURRENT_ORG);
        if (sessionOrg) return parseStoredContextValue(sessionOrg);
    } catch { /* ignore */ }

    // Fallback to localStorage von_current_org
    try {
        const localOrg = localStorage.getItem(KEYS.CURRENT_ORG);
        if (localOrg) return parseStoredContextValue(localOrg);
    } catch { /* ignore */ }

    // Try von_org_context from sessionStorage
    try {
        const sessionCtx = sessionStorage.getItem(KEYS.ORG_CONTEXT);
        if (sessionCtx) return parseStoredContextValue(sessionCtx);
    } catch { /* ignore */ }

    // Fallback to localStorage von_org_context
    try {
        const localCtx = localStorage.getItem(KEYS.ORG_CONTEXT);
        if (localCtx) return parseStoredContextValue(localCtx);
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
    return repairPrimaryNamespaceStorage(resolvePreferredNamespaceFromStorage());
}

/**
 * Build a localStorage/sessionStorage key scoped to the effective namespace.
 * Uses the repaired canonical namespace so restart bootstrap and organisation
 * repair logic do not drift apart from caller-specific storage keys.
 * @param {string} prefix - Storage key prefix
 * @param {string|null} namespace - Optional explicit namespace override
 * @returns {string} Namespace-scoped storage key
 */
export function buildNamespaceScopedStorageKey(prefix, namespace = null) {
    const base = typeof prefix === 'string' ? prefix.trim() : '';
    if (!base) return '';
    const effectiveNamespace =
        typeof namespace === 'string' && namespace.trim()
            ? namespace.trim()
            : getSessionScopedNamespace();
    if (!effectiveNamespace) {
        return base;
    }
    return `${base}:${encodeStorageScopeSegment(effectiveNamespace)}`;
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
    repairPrimaryNamespaceStorage(resolvePreferredNamespaceFromStorage());
}

/**
 * Clear all org/namespace context from both storages (used on logout).
 */
export function clearAllOrgContext() {
    try {
        sessionStorage.removeItem(KEYS.CURRENT_ORG);
        sessionStorage.removeItem(KEYS.ORG_CONTEXT);
        sessionStorage.removeItem(KEYS.NAMESPACE);
        sessionStorage.removeItem(KEYS.NAMESPACE_LEGACY);
        sessionStorage.removeItem(KEYS.ORG_SWITCHING);
        localStorage.removeItem(KEYS.CURRENT_ORG);
        localStorage.removeItem(KEYS.ORG_CONTEXT);
        localStorage.removeItem(KEYS.NAMESPACE);
        localStorage.removeItem(KEYS.NAMESPACE_LEGACY);
    } catch { /* ignore */ }
}

// Export keys for direct access if needed (e.g., for specific checks)
export { KEYS as STORAGE_KEYS };

