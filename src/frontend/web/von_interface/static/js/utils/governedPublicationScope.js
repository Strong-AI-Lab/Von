import { getWindowSessionId, WINDOW_SESSION_HEADER } from '../apiService.js';

const CHANGE_REASON = 'concept_tab_publication_scope_change';
const USER_PREDICATE = '#V#specific_to_user';
const ORGANISATION_PREDICATE = '#V#specific_to_organisation';
const PREDICATE_ALIASES = Object.freeze({
    specific_to_user: USER_PREDICATE,
    specific_to_org: ORGANISATION_PREDICATE,
    specific_to_organisation: ORGANISATION_PREDICATE,
    '#V#specific_to_org': ORGANISATION_PREDICATE
});

function normaliseKind(value) {
    const kind = String(value || '').trim().toLowerCase().replace('-', '_');
    if (kind === 'user' || kind === 'private_user') return 'user';
    if (kind === 'organisation' || kind === 'organization' || kind === 'org') return 'organisation';
    if (kind === 'global' || kind === 'base' || kind === 'base_publication') return 'global';
    return '';
}

function scopeTargets(scopeReadBack, canonicalPredicate) {
    const edges = scopeReadBack?.scope_edges;
    if (!edges || typeof edges !== 'object') return [];
    const seen = new Set();
    Object.entries(edges).forEach(([predicate, values]) => {
        const canonical = PREDICATE_ALIASES[predicate] || predicate;
        if (canonical !== canonicalPredicate) return;
        (Array.isArray(values) ? values : [values]).forEach((value) => {
            const target = typeof value === 'string' ? value.trim() : '';
            if (target) seen.add(target);
        });
    });
    return [...seen];
}

function scopeError(code, message, payload = null) {
    const error = new Error(message);
    error.code = code;
    error.payload = payload;
    error.canonicalReadBack = payload?.canonical_read_back || null;
    return error;
}

export function publicationScopeFlags(scopeReadBack) {
    return {
        hasOrganisationScope: scopeTargets(scopeReadBack, ORGANISATION_PREDICATE).length > 0,
        hasUserScope: scopeTargets(scopeReadBack, USER_PREDICATE).length > 0
    };
}

export function deriveScopeControlDestination(scopeReadBack, controlKind) {
    const kind = normaliseKind(controlKind);
    if (!['user', 'organisation'].includes(kind)) {
        throw scopeError('invalid_scope_control', 'A user or organisation publication-scope control is required.');
    }
    const userTargets = scopeTargets(scopeReadBack, USER_PREDICATE);
    const organisationTargets = scopeTargets(scopeReadBack, ORGANISATION_PREDICATE);
    const selectedTargets = kind === 'user' ? userTargets : organisationTargets;
    if (selectedTargets.length === 0) {
        // The server resolves the authenticated actor/current organisation;
        // browser-held identity is deliberately absent.
        return { destination_kind: kind };
    }
    if (selectedTargets.length !== 1 || !selectedTargets[0].startsWith('#V#')) {
        throw scopeError(
            'ambiguous_publication_scope_transition',
            'This quick control cannot safely choose a destination for multiple or historical publication contexts. No change was attempted.'
        );
    }

    const remainingKind = kind === 'user' ? 'organisation' : 'user';
    const remainingTargets = kind === 'user' ? organisationTargets : userTargets;
    if (remainingTargets.length === 0) return { destination_kind: 'global' };
    if (remainingTargets.length === 1 && remainingTargets[0].startsWith('#V#')) {
        return {
            destination_kind: remainingKind,
            destination_concept_id: remainingTargets[0]
        };
    }
    throw scopeError(
        'ambiguous_publication_scope_transition',
        'This quick control cannot safely choose a destination for multiple or historical publication contexts. No change was attempted.'
    );
}

export function describePublicationContext(context) {
    const kind = normaliseKind(context?.kind);
    const conceptId = typeof context?.concept_id === 'string' ? context.concept_id.trim() : '';
    if (kind === 'global') return 'global Vontology publication';
    if (kind === 'organisation') return conceptId ? `organisation scope ${conceptId}` : 'the current organisation scope';
    if (kind === 'user') return conceptId ? `user-only scope ${conceptId}` : 'your user-only scope';
    return 'the recorded historical or mixed scope';
}

function describeEdge(edge) {
    return [edge?.predicate, edge?.target]
        .map((value) => String(value || '').trim())
        .filter(Boolean)
        .join(' → ');
}

export function buildScopeChangeReviewMessage(conceptId, preview) {
    const removed = (Array.isArray(preview?.scope_delta?.remove) ? preview.scope_delta.remove : [])
        .map(describeEdge).filter(Boolean);
    const added = (Array.isArray(preview?.scope_delta?.add) ? preview.scope_delta.add : [])
        .map(describeEdge).filter(Boolean);
    const lines = [
        `Change publication scope for ${conceptId}?`,
        '',
        `From: ${describePublicationContext(preview?.from)}`,
        `To: ${describePublicationContext(preview?.to)}`,
        `Remove: ${removed.length ? removed.join(', ') : 'no scope edges'}`,
        `Add: ${added.length ? added.join(', ') : 'no scope edge (global)'}`
    ];
    if (normaliseKind(preview?.to?.kind) === 'global') {
        lines.push('', 'This removes the user/organisation-only publication restriction.');
    }
    return [...lines, '', 'Apply this reviewed change?'].join('\n');
}

function requestId(phase) {
    let token = '';
    try { token = globalThis.crypto?.randomUUID?.() || ''; } catch (_) { /* fallback */ }
    if (!token) token = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `concept-scope-${phase}-${token}`;
}

function headers(json = false) {
    return {
        ...(json ? { 'Content-Type': 'application/json' } : {}),
        [WINDOW_SESSION_HEADER]: getWindowSessionId()
    };
}

async function responseJson(response, fallback, requireSuccess = false) {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || (requireSuccess && payload.success !== true)) {
        throw scopeError(
            payload.error_code || payload.error || `http_${response.status}`,
            payload.message || payload.error || fallback || `HTTP ${response.status}`,
            payload
        );
    }
    return payload;
}

function destinationFromPreview(requested, preview) {
    const kind = normaliseKind(preview?.to?.kind);
    const conceptId = typeof preview?.to?.concept_id === 'string'
        ? preview.to.concept_id.trim()
        : '';
    if (!kind || kind !== normaliseKind(requested.destination_kind)) {
        throw scopeError('scope_preview_destination_mismatch', 'The governed preview did not return the requested publication destination.', preview);
    }
    if (kind !== 'global' && !conceptId.startsWith('#V#')) {
        throw scopeError('scope_preview_destination_required', 'The governed preview did not bind an exact destination concept.', preview);
    }
    if (requested.destination_concept_id && requested.destination_concept_id !== conceptId) {
        throw scopeError('scope_preview_destination_mismatch', 'The governed preview changed the explicit publication destination.', preview);
    }
    return {
        destination_kind: kind,
        ...(kind === 'global' ? {} : { destination_concept_id: conceptId })
    };
}

function readBackMatches(scopeReadBack, destination) {
    const users = scopeTargets(scopeReadBack, USER_PREDICATE);
    const organisations = scopeTargets(scopeReadBack, ORGANISATION_PREDICATE);
    if (destination.destination_kind === 'global') return users.length === 0 && organisations.length === 0;
    if (destination.destination_kind === 'user') {
        return organisations.length === 0 && users.length === 1 && users[0] === destination.destination_concept_id;
    }
    return users.length === 0 && organisations.length === 1 && organisations[0] === destination.destination_concept_id;
}

export async function executeGovernedScopeControlChange({
    conceptId,
    controlKind,
    fetchImpl = globalThis.fetch,
    confirmImpl = globalThis.confirm,
    requestIdFactory = requestId
}) {
    if (!conceptId || typeof fetchImpl !== 'function') {
        throw scopeError('scope_change_request_invalid', 'A concept and HTTP client are required.');
    }
    const endpoint = `/api/ontology-authority/concepts/${encodeURIComponent(conceptId)}/scope`;
    const read = await responseJson(await fetchImpl(endpoint, {
        credentials: 'same-origin',
        headers: headers()
    }), 'Could not read the current publication scope.');
    const fingerprint = String(read.scope_fingerprint || '').trim();
    if (!fingerprint) {
        throw scopeError('scope_fingerprint_required', 'The current publication scope did not include a review fingerprint.', read);
    }

    const requested = deriveScopeControlDestination(read, controlKind);
    const previewRequestId = String(requestIdFactory('preview') || '').trim();
    if (!previewRequestId) throw scopeError('scope_request_id_required', 'A preview request identifier is required.');
    const preview = await responseJson(await fetchImpl(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers(true),
        body: JSON.stringify({
            ...requested,
            expected_scope_fingerprint: fingerprint,
            request_id: previewRequestId,
            preview: true,
            reason: CHANGE_REASON
        })
    }), 'The publication-scope preview was denied.', true);
    const destination = destinationFromPreview(requested, preview);
    if (!(typeof confirmImpl === 'function' && confirmImpl(buildScopeChangeReviewMessage(conceptId, preview)))) {
        return { success: false, cancelled: true, preview, canonical_read_back: read };
    }

    let executeRequestId = String(requestIdFactory('execute') || '').trim();
    if (!executeRequestId) throw scopeError('scope_request_id_required', 'An execution request identifier is required.');
    if (executeRequestId === previewRequestId) executeRequestId += ':execute';
    const result = await responseJson(await fetchImpl(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers(true),
        body: JSON.stringify({
            ...destination,
            expected_scope_fingerprint: fingerprint,
            request_id: executeRequestId,
            preview: false,
            reason: CHANGE_REASON
        })
    }), 'The publication-scope change did not complete.', true);
    const canonicalReadBack = result?.canonical_read_back;
    if (!canonicalReadBack || !readBackMatches(canonicalReadBack, destination)) {
        throw scopeError(
            'scope_change_canonical_read_back_mismatch',
            'Canonical read-back did not prove the reviewed publication-scope change.',
            result
        );
    }
    return { ...result, success: true, preview, destination, canonical_read_back: canonicalReadBack };
}
