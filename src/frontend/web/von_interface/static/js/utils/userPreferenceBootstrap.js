import { getJson } from '../apiService.js';
import {
    getSessionScopedOrgContext,
    hasSessionOrgContext,
    setSessionScopedOrgContext,
} from './sessionScopedStorage.js';

const PREFERRED_LANGUAGE_KEY = 'von_preferred_language';

export async function fetchUserPreferences(userConceptId, {
    getJsonImpl = getJson,
} = {}) {
    const conceptId = typeof userConceptId === 'string' ? userConceptId.trim() : '';
    if (!conceptId) return null;

    try {
        return await getJsonImpl(`/api/settings/user_prefs/${encodeURIComponent(conceptId)}`);
    } catch {
        return null;
    }
}

function buildStoredOrgContext(organisationConceptId, existingOrgContext = null) {
    const conceptId = typeof organisationConceptId === 'string'
        ? organisationConceptId.trim()
        : '';
    if (!conceptId) return null;

    const preserveExisting = existingOrgContext?.concept_id === conceptId;
    return {
        id: preserveExisting ? (existingOrgContext?.id || null) : null,
        concept_id: conceptId,
        name: preserveExisting ? (existingOrgContext?.name || null) : null,
    };
}

export async function hydrateStoredSelectionsFromUserPreferences(userConceptId, {
    getJsonImpl = getJson,
    getStoredOrgContext = getSessionScopedOrgContext,
    hasStoredOrgSelection = hasSessionOrgContext,
    setStoredOrgContext = setSessionScopedOrgContext,
    localStorageImpl = typeof localStorage !== 'undefined' ? localStorage : null,
} = {}) {
    const prefs = await fetchUserPreferences(userConceptId, { getJsonImpl });
    if (!prefs || typeof prefs !== 'object') return null;

    const existingOrgContext = typeof getStoredOrgContext === 'function'
        ? getStoredOrgContext()
        : null;

    const hasExplicitOrgSelection = Boolean(existingOrgContext?.concept_id)
        || (
            typeof hasStoredOrgSelection === 'function'
            && hasStoredOrgSelection()
        );

    if (!hasExplicitOrgSelection && prefs.organisation_concept_id) {
        const orgContext = buildStoredOrgContext(
            prefs.organisation_concept_id,
            existingOrgContext,
        );
        if (orgContext && typeof setStoredOrgContext === 'function') {
            setStoredOrgContext(orgContext);
        }
    }

    try {
        if (
            localStorageImpl
            && prefs.preferred_language
            && !localStorageImpl.getItem(PREFERRED_LANGUAGE_KEY)
        ) {
            localStorageImpl.setItem(PREFERRED_LANGUAGE_KEY, prefs.preferred_language);
        }
    } catch {
        // Ignore storage write failures during bootstrap.
    }

    return prefs;
}
