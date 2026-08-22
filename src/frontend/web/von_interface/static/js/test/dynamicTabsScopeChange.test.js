import {
    attachAnalysisButtons,
    toggleUserRelation
} from '../dynamicTabs.js';
import {
    deriveScopeControlEdit,
    executeGovernedScopeControlChange
} from '../utils/governedPublicationScope.js';

const USER_ID = '#V#signed_in_user';
const ORGANISATION_ID = '#V#trusted_organisation';
const USER_PREDICATE = '#V#specific_to_user';
const ORGANISATION_PREDICATE = '#V#specific_to_organisation';

function jsonResponse(payload, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: jest.fn().mockResolvedValue(payload)
    };
}

function scopeSnapshot({
    conceptId = '#V#scope_fixture',
    user = null,
    organisation = null,
    fingerprint = 'scope-before'
} = {}) {
    const scopeEdges = {};
    if (user) scopeEdges[USER_PREDICATE] = [user];
    if (organisation) scopeEdges[ORGANISATION_PREDICATE] = [organisation];
    let publicationContext = { kind: 'global', concept_id: null };
    if (user && organisation) {
        publicationContext = {
            kind: 'composite',
            concept_id: null,
            components: [
                { kind: 'user', concept_id: user },
                { kind: 'organisation', concept_id: organisation }
            ]
        };
    }
    else if (user) publicationContext = { kind: 'user', concept_id: user };
    else if (organisation) {
        publicationContext = { kind: 'organisation', concept_id: organisation };
    }
    return {
        concept_id: conceptId,
        publication_context: {
            ...publicationContext,
            source: 'concept_visibility'
        },
        scope_edges: scopeEdges,
        scope_fingerprint: fingerprint
    };
}

function scopeEdgeRows(snapshot) {
    return Object.entries(snapshot.scope_edges).flatMap(([predicate, targets]) =>
        targets.map((target) => ({ predicate, target }))
    );
}

function edgeKey(edge) {
    return `${edge.predicate}\u0000${edge.target}`;
}

function previewPayload(before, after, resolvedScopeEdit) {
    const beforeRows = scopeEdgeRows(before);
    const afterRows = scopeEdgeRows(after);
    const beforeKeys = new Set(beforeRows.map(edgeKey));
    const afterKeys = new Set(afterRows.map(edgeKey));
    return {
        success: true,
        preview: true,
        changed: false,
        from: before.publication_context,
        to: after.publication_context,
        resolved_scope_edit: resolvedScopeEdit,
        destination_scope_edges: after.scope_edges,
        scope_delta: {
            remove: beforeRows.filter((edge) => !afterKeys.has(edgeKey(edge))),
            add: afterRows.filter((edge) => !beforeKeys.has(edgeKey(edge)))
        }
    };
}

function successfulExecution(after) {
    return {
        success: true,
        effect_status: 'succeeded',
        mutation_outcome: 'succeeded',
        canonical_read_back: after
    };
}

async function flushUntil(predicate, attempts = 30) {
    for (let index = 0; index < attempts; index += 1) {
        await Promise.resolve();
        if (predicate()) return;
    }
    throw new Error('Asynchronous scope-control condition was not reached.');
}

describe('governed Concept-tab publication scope controls', () => {
    const originalFetch = global.fetch;

    beforeEach(() => {
        jest.useFakeTimers();
        document.body.innerHTML = '';
        window.localStorage.clear();
    });

    afterEach(() => {
        jest.runOnlyPendingTimers();
        jest.useRealTimers();
        jest.restoreAllMocks();
        global.fetch = originalFetch;
    });

    test.each([
        ['global adds user', scopeSnapshot(), 'user', { kind: 'user', enabled: true }],
        [
            'global adds organisation',
            scopeSnapshot(),
            'organisation',
            { kind: 'organisation', enabled: true }
        ],
        [
            'sole user removes user',
            scopeSnapshot({ user: USER_ID }),
            'user',
            { kind: 'user', enabled: false }
        ],
        [
            'sole user adds organisation without removing user',
            scopeSnapshot({ user: USER_ID }),
            'organisation',
            { kind: 'organisation', enabled: true }
        ],
        [
            'sole organisation removes organisation',
            scopeSnapshot({ organisation: ORGANISATION_ID }),
            'organisation',
            { kind: 'organisation', enabled: false }
        ],
        [
            'sole organisation adds user without removing organisation',
            scopeSnapshot({ organisation: ORGANISATION_ID }),
            'user',
            { kind: 'user', enabled: true }
        ],
        [
            'mixed removes only user',
            scopeSnapshot({ user: USER_ID, organisation: ORGANISATION_ID }),
            'user',
            { kind: 'user', enabled: false }
        ],
        [
            'mixed removes only organisation',
            scopeSnapshot({ user: USER_ID, organisation: ORGANISATION_ID }),
            'organisation',
            { kind: 'organisation', enabled: false }
        ]
    ])('%s', (_name, scopeReadBack, controlKind, expected) => {
        expect(deriveScopeControlEdit(scopeReadBack, controlKind)).toEqual(expected);
    });

    test('refuses ambiguous selected targets and malformed historical edges', () => {
        expect(() => deriveScopeControlEdit({
            scope_edges: {
                [USER_PREDICATE]: ['#V#person_a', '#V#person_b']
            }
        }, 'user')).toThrow('multiple publication-scope targets');
        expect(() => deriveScopeControlEdit({
            scope_edges: {
                [USER_PREDICATE]: ['legacy-user-id']
            }
        }, 'organisation')).toThrow('malformed publication scope');
        expect(() => deriveScopeControlEdit({
            scope_edges: {
                specific_to_user: [USER_ID]
            }
        }, 'user')).toThrow('non-canonical');
    });

    test('organisation add preserves user through an actual control click and ignores late display state', async () => {
        window.localStorage.setItem('von_current_user', '#V#forged_browser_user');
        window.localStorage.setItem('von:organisation', '#V#forged_browser_org');
        const before = scopeSnapshot({
            conceptId: '#V#private_note',
            user: USER_ID,
            fingerprint: 'scope-user-before'
        });
        const after = scopeSnapshot({
            conceptId: '#V#private_note',
            user: USER_ID,
            organisation: ORGANISATION_ID,
            fingerprint: 'scope-mixed-after'
        });
        const preview = previewPayload(before, after, {
            kind: 'organisation',
            enabled: true,
            concept_id: ORGANISATION_ID
        });
        const governedResponses = [
            jsonResponse(before),
            jsonResponse(preview),
            jsonResponse(successfulExecution(after))
        ];
        const events = [];
        let resolveInitialState;
        let initialStateJson;
        const initialStateResponse = new Promise((resolve) => {
            resolveInitialState = () => {
                const response = jsonResponse({
                    raw_doc: {
                        relationships: {
                            [USER_PREDICATE]: [USER_ID]
                        }
                    }
                });
                initialStateJson = response.json;
                resolve(response);
            };
        });
        global.fetch = jest.fn((...args) => {
            if (String(args[0]).startsWith('/vontology/api/vontology/node_content')) {
                return initialStateResponse;
            }
            events.push(args[1]?.method || 'GET');
            return Promise.resolve(governedResponses.shift());
        });
        jest.spyOn(window, 'confirm').mockImplementation((message) => {
            events.push('confirm');
            expect(message).toContain('combined user and organisation scope');
            expect(message).toContain(`${ORGANISATION_PREDICATE} → ${ORGANISATION_ID}`);
            expect(message).not.toContain(`Remove: ${USER_PREDICATE}`);
            return true;
        });

        const header = document.createElement('div');
        document.body.appendChild(header);
        attachAnalysisButtons(header, '#V#private_note', 'individual');
        const orgBtn = header.querySelector('.org-relation-button');
        const userBtn = header.querySelector('.user-relation-button');
        expect(orgBtn.disabled).toBe(false);
        expect(userBtn.disabled).toBe(false);

        // The authoritative read/preview/execute path remains usable while
        // the older raw display read is pending.
        orgBtn.click();
        await flushUntil(() => events.length === 4 && orgBtn.disabled === false);
        expect(orgBtn.getAttribute('aria-pressed')).toBe('true');
        expect(userBtn.getAttribute('aria-pressed')).toBe('true');
        expect(orgBtn.title).toBe('Remove the organisation publication restriction');
        expect(userBtn.title).toBe('Remove the user publication restriction');

        // A late user-only display response must not erase canonical mixed
        // read-back from the completed governed edit.
        resolveInitialState();
        await flushUntil(() => initialStateJson?.mock.calls.length === 1);
        await Promise.resolve();
        expect(orgBtn.getAttribute('aria-pressed')).toBe('true');
        expect(userBtn.getAttribute('aria-pressed')).toBe('true');

        expect(events).toEqual(['GET', 'POST', 'confirm', 'POST']);
        expect(global.fetch).toHaveBeenCalledTimes(4);
        const governedCalls = global.fetch.mock.calls.filter(([url]) =>
            String(url).startsWith('/api/ontology-authority/concepts/')
        );
        expect(governedCalls).toHaveLength(3);
        expect(governedCalls.every(([url]) => (
            url === '/api/ontology-authority/concepts/%23V%23private_note/scope'
        ))).toBe(true);
        const previewBody = JSON.parse(governedCalls[1][1].body);
        const executeBody = JSON.parse(governedCalls[2][1].body);
        expect(previewBody).toMatchObject({
            scope_edit: { kind: 'organisation', enabled: true },
            expected_scope_fingerprint: 'scope-user-before',
            preview: true,
            reason: 'concept_tab_publication_scope_change'
        });
        expect(previewBody.scope_edit).not.toHaveProperty('concept_id');
        expect(previewBody).not.toHaveProperty('destination_kind');
        expect(executeBody).toMatchObject({
            scope_edit: {
                kind: 'organisation',
                enabled: true,
                concept_id: ORGANISATION_ID
            },
            expected_scope_fingerprint: 'scope-user-before',
            preview: false,
            reason: 'concept_tab_publication_scope_change'
        });
        expect(executeBody.request_id).not.toBe(previewBody.request_id);
        governedCalls.forEach(([, options]) => {
            const requestHeaders = options?.headers || {};
            expect(requestHeaders).not.toHaveProperty('X-User-Concept-ID');
            expect(requestHeaders).not.toHaveProperty('X-User-Client-ID');
            expect(requestHeaders).not.toHaveProperty('X-Organisation-Concept-ID');
            expect(requestHeaders['X-Von-Window-Session']).toMatch(/^ws_/);
        });
    });

    test('user add preserves organisation through the dynamic helper path', async () => {
        window.localStorage.setItem('von_current_user', '#V#forged_browser_user');
        window.localStorage.setItem('von:organisation', '#V#forged_browser_org');
        const before = scopeSnapshot({
            organisation: ORGANISATION_ID,
            fingerprint: 'scope-organisation-before'
        });
        const after = scopeSnapshot({
            user: USER_ID,
            organisation: ORGANISATION_ID,
            fingerprint: 'scope-mixed-after'
        });
        global.fetch = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload(before, after, {
                kind: 'user',
                enabled: true,
                concept_id: USER_ID
            })))
            .mockResolvedValueOnce(jsonResponse(successfulExecution(after)));
        jest.spyOn(window, 'confirm').mockReturnValue(true);
        const orgBtn = document.createElement('button');
        const userBtn = document.createElement('button');

        const result = await toggleUserRelation('#V#scope_fixture', userBtn, orgBtn);

        expect(result.success).toBe(true);
        expect(orgBtn.getAttribute('aria-pressed')).toBe('true');
        expect(userBtn.getAttribute('aria-pressed')).toBe('true');
        const previewBody = JSON.parse(global.fetch.mock.calls[1][1].body);
        const executeBody = JSON.parse(global.fetch.mock.calls[2][1].body);
        expect(previewBody.scope_edit).toEqual({ kind: 'user', enabled: true });
        expect(executeBody.scope_edit).toEqual({
            kind: 'user',
            enabled: true,
            concept_id: USER_ID
        });
        expect(scopeEdgeRows(result.canonical_read_back)).toEqual(expect.arrayContaining([
            { predicate: USER_PREDICATE, target: USER_ID },
            { predicate: ORGANISATION_PREDICATE, target: ORGANISATION_ID }
        ]));
    });

    test('confirmation cancellation stops after a valid preview and preserves canonical state', async () => {
        const before = scopeSnapshot({ user: USER_ID, fingerprint: 'scope-user-before' });
        const after = scopeSnapshot({ fingerprint: 'scope-global-after' });
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload(before, after, {
                kind: 'user',
                enabled: false,
                concept_id: USER_ID
            })));

        const result = await executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'user',
            fetchImpl,
            confirmImpl: () => false,
            requestIdFactory: (phase) => `cancel-${phase}`
        });

        expect(result).toMatchObject({ success: false, cancelled: true });
        expect(result.canonical_read_back).toEqual(before);
        expect(fetchImpl).toHaveBeenCalledTimes(2);
    });

    test('authority denial never confirms or executes', async () => {
        const confirmImpl = jest.fn().mockReturnValue(true);
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(scopeSnapshot({ user: USER_ID })))
            .mockResolvedValueOnce(jsonResponse({
                success: false,
                error_code: 'global_ontology_admin_authority_required',
                error: 'Global ontology administrator authority is required.'
            }, 409));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'user',
            fetchImpl,
            confirmImpl,
            requestIdFactory: (phase) => `denied-${phase}`
        })).rejects.toMatchObject({ code: 'global_ontology_admin_authority_required' });
        expect(fetchImpl).toHaveBeenCalledTimes(2);
        expect(confirmImpl).not.toHaveBeenCalled();
    });

    test('preview cannot substitute a different restriction edit', async () => {
        const before = scopeSnapshot({ user: USER_ID });
        const after = scopeSnapshot({ user: USER_ID, organisation: ORGANISATION_ID });
        const substituted = previewPayload(before, after, {
            kind: 'user',
            enabled: true,
            concept_id: USER_ID
        });
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(substituted));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'organisation',
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `substitution-${phase}`
        })).rejects.toMatchObject({ code: 'scope_preview_edit_mismatch' });
        expect(fetchImpl).toHaveBeenCalledTimes(2);
    });

    test('preview cannot drop the complementary restriction or add a second edge', async () => {
        const before = scopeSnapshot({ user: USER_ID });
        const incorrectAfter = scopeSnapshot({ organisation: ORGANISATION_ID });
        const invalidPreview = previewPayload(before, incorrectAfter, {
            kind: 'organisation',
            enabled: true,
            concept_id: ORGANISATION_ID
        });
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(invalidPreview));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'organisation',
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `complement-${phase}`
        })).rejects.toMatchObject({ code: 'scope_preview_edge_set_mismatch' });
        expect(fetchImpl).toHaveBeenCalledTimes(2);
    });

    test('success response is rejected when exact canonical read-back disagrees', async () => {
        const before = scopeSnapshot({
            user: USER_ID,
            organisation: ORGANISATION_ID,
            fingerprint: 'scope-mixed-before'
        });
        const after = scopeSnapshot({
            user: USER_ID,
            fingerprint: 'scope-user-after'
        });
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload(before, after, {
                kind: 'organisation',
                enabled: false,
                concept_id: ORGANISATION_ID
            })))
            .mockResolvedValueOnce(jsonResponse(successfulExecution(before)));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'organisation',
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `readback-mismatch-${phase}`
        })).rejects.toMatchObject({ code: 'scope_change_canonical_read_back_mismatch' });
        expect(fetchImpl).toHaveBeenCalledTimes(3);
    });

    test('stale execute refreshes both controls from canonical read-back without retry', async () => {
        jest.spyOn(console, 'error').mockImplementation(() => {});
        const before = scopeSnapshot({
            conceptId: '#V#stale_scope',
            user: USER_ID,
            organisation: ORGANISATION_ID,
            fingerprint: 'scope-mixed-before'
        });
        const reviewedAfter = scopeSnapshot({
            conceptId: '#V#stale_scope',
            organisation: ORGANISATION_ID,
            fingerprint: 'scope-organisation-after'
        });
        const concurrent = scopeSnapshot({
            conceptId: '#V#stale_scope',
            user: USER_ID,
            fingerprint: 'scope-concurrently-changed'
        });
        global.fetch = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload(before, reviewedAfter, {
                kind: 'user',
                enabled: false,
                concept_id: USER_ID
            })))
            .mockResolvedValueOnce(jsonResponse({
                success: false,
                changed: false,
                error_code: 'scope_precondition_failed',
                error: 'The concept scope changed after the preview was obtained.',
                canonical_read_back: concurrent
            }, 409));
        jest.spyOn(window, 'confirm').mockReturnValue(true);
        const orgBtn = document.createElement('button');
        const userBtn = document.createElement('button');

        const result = await toggleUserRelation('#V#stale_scope', userBtn, orgBtn);

        expect(result.success).toBe(false);
        expect(result.error.code).toBe('scope_precondition_failed');
        expect(global.fetch).toHaveBeenCalledTimes(3);
        expect(orgBtn.getAttribute('aria-pressed')).toBe('false');
        expect(userBtn.getAttribute('aria-pressed')).toBe('true');
        expect(orgBtn.disabled).toBe(false);
        expect(userBtn.disabled).toBe(false);
    });
});
