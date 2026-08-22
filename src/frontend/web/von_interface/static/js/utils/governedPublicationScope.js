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
const PREDICATE_BY_KIND = Object.freeze({
    user: USER_PREDICATE,
    organisation: ORGANISATION_PREDICATE
});

function normaliseKind(value) {
    const kind = String(value || '').trim().toLowerCase().replace('-', '_');
    if (kind === 'user' || kind === 'private_user') return 'user';
    if (kind === 'organisation' || kind === 'organization' || kind === 'org') return 'organisation';
    if (kind === 'global' || kind === 'base' || kind === 'base_publication') return 'global';
    if (kind === 'mixed' || kind === 'composite') return 'mixed';
    if (kind === 'historical') return 'historical';
    return '';
}

function canonicalPredicate(predicate) {
    const cleaned = typeof predicate === 'string' ? predicate.trim() : '';
    const canonical = PREDICATE_ALIASES[cleaned] || cleaned;
    return Object.values(PREDICATE_BY_KIND).includes(canonical) ? canonical : '';
}

function scopeTargets(scopeReadBack, canonicalScopePredicate) {
    const edges = scopeReadBack?.scope_edges;
    if (!edges || typeof edges !== 'object') return [];
    const seen = new Set();
    Object.entries(edges).forEach(([predicate, values]) => {
        if (canonicalPredicate(predicate) !== canonicalScopePredicate) return;
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

function edgeKey(predicate, target) {
    return `${predicate}\u0000${target}`;
}

function exactScopeEdges(value, {
    code = 'invalid_publication_scope_edges',
    message = 'The publication scope did not contain exact canonical edges.',
    requireCanonicalPredicates = false
} = {}) {
    const rawEdges = value?.scope_edges ?? value;
    if (!rawEdges || typeof rawEdges !== 'object' || Array.isArray(rawEdges)) {
        throw scopeError(code, message, value);
    }
    const result = new Map();
    Object.entries(rawEdges).forEach(([rawPredicate, rawTargets]) => {
        const predicate = canonicalPredicate(rawPredicate);
        if (!predicate || (requireCanonicalPredicates && rawPredicate !== predicate)) {
            throw scopeError(code, message, value);
        }
        const targets = Array.isArray(rawTargets) ? rawTargets : [rawTargets];
        targets.forEach((rawTarget) => {
            const target = typeof rawTarget === 'string' ? rawTarget.trim() : '';
            if (!target.startsWith('#V#')) {
                throw scopeError(code, message, value);
            }
            const key = edgeKey(predicate, target);
            if (result.has(key) && requireCanonicalPredicates) {
                throw scopeError(code, message, value);
            }
            result.set(key, { predicate, target });
        });
    });
    return result;
}

function edgeSetEquals(left, right) {
    if (left.size !== right.size) return false;
    return [...left.keys()].every((key) => right.has(key));
}

function edgeSetDifference(left, right) {
    return new Map([...left].filter(([key]) => !right.has(key)));
}

function edgeDescriptors(value, field) {
    const rows = value?.scope_delta?.[field];
    if (!Array.isArray(rows)) {
        throw scopeError(
            'scope_preview_delta_invalid',
            'The governed preview did not return an exact publication-scope delta.',
            value
        );
    }
    const result = new Map();
    rows.forEach((row) => {
        const predicate = canonicalPredicate(row?.predicate);
        const target = typeof row?.target === 'string' ? row.target.trim() : '';
        if (!predicate || row?.predicate !== predicate || !target.startsWith('#V#')) {
            throw scopeError(
                'scope_preview_delta_invalid',
                'The governed preview did not return an exact canonical publication-scope edge.',
                value
            );
        }
        const key = edgeKey(predicate, target);
        if (result.has(key)) {
            throw scopeError(
                'scope_preview_delta_invalid',
                'The governed preview returned a duplicate publication-scope edge.',
                value
            );
        }
        result.set(key, { predicate, target });
    });
    return result;
}

function contextForEdges(edges) {
    const users = [...edges.values()]
        .filter((edge) => edge.predicate === USER_PREDICATE)
        .map((edge) => edge.target);
    const organisations = [...edges.values()]
        .filter((edge) => edge.predicate === ORGANISATION_PREDICATE)
        .map((edge) => edge.target);
    if (users.length === 0 && organisations.length === 0) {
        return { kind: 'global', concept_id: null };
    }
    if (users.length === 1 && organisations.length === 0) {
        return { kind: 'user', concept_id: users[0] };
    }
    if (users.length === 0 && organisations.length === 1) {
        return { kind: 'organisation', concept_id: organisations[0] };
    }
    return { kind: 'mixed', concept_id: null };
}

function contextMatches(actual, expected) {
    const actualKind = normaliseKind(actual?.kind);
    const actualConceptId = typeof actual?.concept_id === 'string'
        ? actual.concept_id.trim()
        : null;
    return actualKind === expected.kind && (
        expected.concept_id === null
            ? !actualConceptId
            : actualConceptId === expected.concept_id
    );
}

export function publicationScopeFlags(scopeReadBack) {
    return {
        hasOrganisationScope: scopeTargets(scopeReadBack, ORGANISATION_PREDICATE).length > 0,
        hasUserScope: scopeTargets(scopeReadBack, USER_PREDICATE).length > 0
    };
}

export function deriveScopeControlEdit(scopeReadBack, controlKind) {
    const kind = normaliseKind(controlKind);
    if (!['user', 'organisation'].includes(kind)) {
        throw scopeError(
            'invalid_scope_control',
            'A user or organisation publication-scope control is required.'
        );
    }
    const edges = exactScopeEdges(scopeReadBack, {
        code: 'ambiguous_publication_scope_edit',
        message: 'This quick control cannot safely edit non-canonical or malformed publication scope. No change was attempted.',
        requireCanonicalPredicates: true
    });
    const predicate = PREDICATE_BY_KIND[kind];
    const selected = [...edges.values()].filter((edge) => edge.predicate === predicate);
    if (selected.length > 1) {
        throw scopeError(
            'ambiguous_publication_scope_edit',
            'This quick control cannot safely choose among multiple publication-scope targets. No change was attempted.'
        );
    }
    // An omitted target is deliberate. Preview resolves an add from trusted
    // actor/organisation context and resolves a removal from the sole exact
    // edge. Execute receives the exact target bound by that preview.
    return { kind, enabled: selected.length === 0 };
}

export function describePublicationContext(context) {
    const kind = normaliseKind(context?.kind);
    const conceptId = typeof context?.concept_id === 'string' ? context.concept_id.trim() : '';
    if (kind === 'global') return 'global Vontology publication';
    if (kind === 'organisation') {
        return conceptId ? `organisation scope ${conceptId}` : 'the current organisation scope';
    }
    if (kind === 'user') return conceptId ? `user-only scope ${conceptId}` : 'your user-only scope';
    if (kind === 'mixed') return 'combined user and organisation scope';
    return 'the recorded historical scope';
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
        `Change publication restrictions for ${conceptId}?`,
        '',
        `From: ${describePublicationContext(preview?.from)}`,
        `To: ${describePublicationContext(preview?.to)}`,
        `Remove: ${removed.length ? removed.join(', ') : 'no scope edge'}`,
        `Add: ${added.length ? added.join(', ') : 'no scope edge'}`
    ];
    if (normaliseKind(preview?.to?.kind) === 'global') {
        lines.push('', 'This removes the final user/organisation publication restriction.');
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

function validatedPreview(read, requested, preview) {
    const resolved = preview?.resolved_scope_edit;
    const kind = normaliseKind(resolved?.kind);
    const enabled = resolved?.enabled;
    const conceptId = typeof resolved?.concept_id === 'string'
        ? resolved.concept_id.trim()
        : '';
    if (
        kind !== requested.kind ||
        typeof enabled !== 'boolean' ||
        enabled !== requested.enabled ||
        !conceptId.startsWith('#V#')
    ) {
        throw scopeError(
            'scope_preview_edit_mismatch',
            'The governed preview did not bind the requested publication restriction edit.',
            preview
        );
    }

    const before = exactScopeEdges(read, {
        code: 'scope_preview_source_invalid',
        message: 'The reviewed publication scope no longer has exact canonical scope edges.',
        requireCanonicalPredicates: true
    });
    const predicate = PREDICATE_BY_KIND[kind];
    const selected = [...before.values()].filter((edge) => edge.predicate === predicate);
    if (
        (enabled && selected.length !== 0) ||
        (!enabled && (selected.length !== 1 || selected[0].target !== conceptId))
    ) {
        throw scopeError(
            'scope_preview_edit_mismatch',
            'The governed preview changed a different publication restriction.',
            preview
        );
    }

    const expected = new Map(before);
    const selectedKey = edgeKey(predicate, conceptId);
    if (enabled) expected.set(selectedKey, { predicate, target: conceptId });
    else expected.delete(selectedKey);

    const destination = exactScopeEdges(preview?.destination_scope_edges, {
        code: 'scope_preview_destination_edges_invalid',
        message: 'The governed preview did not return exact canonical destination scope edges.',
        requireCanonicalPredicates: true
    });
    const expectedAdded = edgeSetDifference(expected, before);
    const expectedRemoved = edgeSetDifference(before, expected);
    const previewAdded = edgeDescriptors(preview, 'add');
    const previewRemoved = edgeDescriptors(preview, 'remove');
    if (
        expectedAdded.size + expectedRemoved.size !== 1 ||
        !edgeSetEquals(destination, expected) ||
        !edgeSetEquals(previewAdded, expectedAdded) ||
        !edgeSetEquals(previewRemoved, expectedRemoved)
    ) {
        throw scopeError(
            'scope_preview_edge_set_mismatch',
            'The governed preview did not preserve every complementary publication restriction.',
            preview
        );
    }

    const fromContext = contextForEdges(before);
    const toContext = contextForEdges(expected);
    if (!contextMatches(preview?.from, fromContext) || !contextMatches(preview?.to, toContext)) {
        throw scopeError(
            'scope_preview_context_mismatch',
            'The governed preview context did not match its exact publication-scope edges.',
            preview
        );
    }
    return {
        resolvedScopeEdit: { kind, enabled, concept_id: conceptId },
        destinationEdges: destination
    };
}

function readBackMatches(scopeReadBack, destinationEdges) {
    try {
        return edgeSetEquals(exactScopeEdges(scopeReadBack, {
            requireCanonicalPredicates: true
        }), destinationEdges);
    } catch (_) {
        return false;
    }
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
        throw scopeError(
            'scope_fingerprint_required',
            'The current publication scope did not include a review fingerprint.',
            read
        );
    }

    const requestedScopeEdit = deriveScopeControlEdit(read, controlKind);
    const previewRequestId = String(requestIdFactory('preview') || '').trim();
    if (!previewRequestId) {
        throw scopeError('scope_request_id_required', 'A preview request identifier is required.');
    }
    const preview = await responseJson(await fetchImpl(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers(true),
        body: JSON.stringify({
            scope_edit: requestedScopeEdit,
            expected_scope_fingerprint: fingerprint,
            request_id: previewRequestId,
            preview: true,
            reason: CHANGE_REASON
        })
    }), 'The publication-scope preview was denied.', true);
    const validated = validatedPreview(read, requestedScopeEdit, preview);
    if (!(typeof confirmImpl === 'function' && confirmImpl(buildScopeChangeReviewMessage(conceptId, preview)))) {
        return { success: false, cancelled: true, preview, canonical_read_back: read };
    }

    let executeRequestId = String(requestIdFactory('execute') || '').trim();
    if (!executeRequestId) {
        throw scopeError('scope_request_id_required', 'An execution request identifier is required.');
    }
    if (executeRequestId === previewRequestId) executeRequestId += ':execute';
    const result = await responseJson(await fetchImpl(endpoint, {
        method: 'POST',
        credentials: 'same-origin',
        headers: headers(true),
        body: JSON.stringify({
            scope_edit: validated.resolvedScopeEdit,
            expected_scope_fingerprint: fingerprint,
            request_id: executeRequestId,
            preview: false,
            reason: CHANGE_REASON
        })
    }), 'The publication-scope change did not complete.', true);
    const canonicalReadBack = result?.canonical_read_back;
    if (!canonicalReadBack || !readBackMatches(canonicalReadBack, validated.destinationEdges)) {
        throw scopeError(
            'scope_change_canonical_read_back_mismatch',
            'Canonical read-back did not prove the reviewed publication-scope change.',
            result
        );
    }
    return {
        ...result,
        success: true,
        preview,
        scope_edit: validated.resolvedScopeEdit,
        destination_scope_edges: preview.destination_scope_edges,
        canonical_read_back: canonicalReadBack
    };
}
