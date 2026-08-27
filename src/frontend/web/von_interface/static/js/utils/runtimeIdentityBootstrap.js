function trimString(value) {
    return typeof value === 'string' ? value.trim() : '';
}

function normaliseDisplayName(primary, fallback = null) {
    const preferred = trimString(primary);
    if (preferred) return preferred;
    const backup = trimString(fallback);
    return backup || null;
}

export function parseStoredContextValue(rawValue) {
    if (rawValue == null) {
        return null;
    }

    if (typeof rawValue === 'object') {
        return rawValue;
    }

    const raw = trimString(rawValue);
    if (!raw) {
        return null;
    }

    if (raw.startsWith('{') || raw.startsWith('[')) {
        try {
            return JSON.parse(raw);
        } catch {
            return null;
        }
    }

    if (raw.startsWith('#V#')) {
        return { concept_id: raw };
    }

    return null;
}

function conceptIdToNamespaceSlug(value) {
    const conceptId = trimString(value);
    if (!conceptId) return '';
    return conceptId.startsWith('#V#') ? conceptId.slice(3) : conceptId;
}

export function buildNamespaceFromConcepts(userConceptId, organisationConceptId = null) {
    const userSlug = conceptIdToNamespaceSlug(userConceptId);
    if (!userSlug) return '';
    const orgSlug = conceptIdToNamespaceSlug(organisationConceptId);
    return orgSlug ? `#V#${userSlug}@${orgSlug}` : `#V#${userSlug}`;
}

export function resolveBrowserBootstrapUserContext({
    settings = null,
    authStatus = null,
    storedUser = null,
} = {}) {
    const conceptId = trimString(
        settings?.current_user_person_concept_id
        || authStatus?.user_concept_id
        || storedUser?.concept_id
    );
    const id = trimString(settings?.current_user_person_id || storedUser?.id) || null;
    const name = normaliseDisplayName(
        settings?.current_user_person_name,
        authStatus?.name || storedUser?.name
    );

    if (!conceptId && !id) {
        return null;
    }

    return {
        id,
        concept_id: conceptId || null,
        name,
    };
}

export function resolveBrowserBootstrapOrganisationContext({
    settings = null,
    sessionContext = null,
    storedOrganisation = null,
    explicitPersonal = false,
} = {}) {
    if (explicitPersonal) {
        return null;
    }
    const conceptId = trimString(
        settings?.current_organisation_concept_id
        || sessionContext?.organisation_id
        || storedOrganisation?.concept_id
    );
    const id = trimString(settings?.current_organisation_id || storedOrganisation?.id) || null;
    const name = normaliseDisplayName(
        settings?.current_organisation_name,
        storedOrganisation?.name
    );

    if (!conceptId && !id) {
        return null;
    }

    return {
        id,
        concept_id: conceptId || null,
        name,
    };
}

export function resolveBrowserBootstrapNamespace({
    settings = null,
    sessionContext = null,
    userContext = null,
    organisationContext = null,
    explicitPersonal = false,
} = {}) {
    const sessionNamespace = trimString(sessionContext?.namespace);
    if (sessionNamespace && (!explicitPersonal || !sessionNamespace.includes('@'))) {
        return sessionNamespace;
    }

    return buildNamespaceFromConcepts(
        settings?.current_user_person_concept_id || userContext?.concept_id || null,
        explicitPersonal
            ? null
            : (settings?.current_organisation_concept_id || organisationContext?.concept_id || null)
    );
}
