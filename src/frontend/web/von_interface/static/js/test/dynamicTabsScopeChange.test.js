import {
    attachAnalysisButtons,
    toggleUserRelation
} from '../dynamicTabs.js';
import {
    deriveScopeControlDestination,
    executeGovernedScopeControlChange
} from '../utils/governedPublicationScope.js';

function jsonResponse(payload, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: jest.fn().mockResolvedValue(payload)
    };
}

function globalScope(conceptId = '#V#scope_fixture') {
    return {
        concept_id: conceptId,
        publication_context: { kind: 'global', concept_id: null, source: 'concept_visibility' },
        scope_edges: {},
        scope_fingerprint: 'scope-global-before'
    };
}

function userScope(conceptId = '#V#scope_fixture') {
    return {
        concept_id: conceptId,
        publication_context: {
            kind: 'user',
            concept_id: '#V#signed_in_user',
            source: 'concept_visibility'
        },
        scope_edges: {
            '#V#specific_to_user': ['#V#signed_in_user']
        },
        scope_fingerprint: 'scope-user-before'
    };
}

function previewPayload({ from, to, remove = [], add = [] }) {
    return {
        success: true,
        preview: true,
        changed: false,
        from,
        to,
        scope_delta: { remove, add }
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
        [
            'user-only removal publishes globally',
            { scope_edges: { '#V#specific_to_user': ['#V#person'] } },
            'user',
            { destination_kind: 'global' }
        ],
        [
            'organisation-only removal publishes globally',
            { scope_edges: { specific_to_org: ['#V#org'] } },
            'organisation',
            { destination_kind: 'global' }
        ],
        [
            'global to user relies on the authenticated actor',
            { scope_edges: {} },
            'user',
            { destination_kind: 'user' }
        ],
        [
            'global to organisation relies on trusted current context',
            { scope_edges: {} },
            'organisation',
            { destination_kind: 'organisation' }
        ],
        [
            'mixed removal preserves the one exact organisation',
            {
                scope_edges: {
                    specific_to_user: ['#V#person'],
                    '#V#specific_to_org': ['#V#org']
                }
            },
            'user',
            { destination_kind: 'organisation', destination_concept_id: '#V#org' }
        ],
        [
            'mixed removal preserves the one exact user',
            {
                scope_edges: {
                    '#V#specific_to_user': ['#V#person'],
                    specific_to_organisation: ['#V#org']
                }
            },
            'organisation',
            { destination_kind: 'user', destination_concept_id: '#V#person' }
        ]
    ])('%s', (_name, scopeReadBack, controlKind, expected) => {
        expect(deriveScopeControlDestination(scopeReadBack, controlKind)).toEqual(expected);
    });

    test('refuses to guess a remaining destination in ambiguous mixed scope', () => {
        expect(() => deriveScopeControlDestination({
            scope_edges: {
                '#V#specific_to_user': ['#V#person'],
                '#V#specific_to_organisation': ['#V#org_a', '#V#org_b']
            }
        }, 'user')).toThrow('multiple or historical publication contexts');
        expect(() => deriveScopeControlDestination({
            scope_edges: {
                '#V#specific_to_user': ['#V#person_a', '#V#person_b']
            }
        }, 'user')).toThrow('No change was attempted');
    });

    test('user-only button follows GET, preview, confirmation and execute before changing state', async () => {
        const events = [];
        const before = userScope('#V#private_note');
        const preview = previewPayload({
            from: before.publication_context,
            to: { kind: 'global', concept_id: null, source: 'scope_change_request' },
            remove: [{ predicate: '#V#specific_to_user', target: '#V#signed_in_user' }]
        });
        const after = {
            ...globalScope('#V#private_note'),
            scope_fingerprint: 'scope-global-after'
        };
        const governedResponses = [
            jsonResponse(before),
            jsonResponse(preview),
            jsonResponse({
                success: true,
                effect_status: 'succeeded',
                mutation_outcome: 'succeeded',
                canonical_read_back: after
            })
        ];
        let resolveInitialState;
        let initialStateJson;
        const initialStateResponse = new Promise((resolve) => {
            resolveInitialState = () => {
                const response = jsonResponse({
                    raw_doc: {
                        relationships: {
                            '#V#specific_to_user': ['#V#signed_in_user']
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
            expect(message).toContain('global Vontology publication');
            expect(message).toContain('removes the user/organisation-only publication restriction');
            return true;
        });

        const header = document.createElement('div');
        document.body.appendChild(header);
        attachAnalysisButtons(header, '#V#private_note', 'individual');
        const orgBtn = header.querySelector('.org-relation-button');
        const userBtn = header.querySelector('.user-relation-button');
        expect(orgBtn.disabled).toBe(false);
        expect(userBtn.disabled).toBe(false);

        // The click remains executable while the display read is pending; its
        // fresh governed GET, rather than DOM state, decides the transition.
        userBtn.click();
        await flushUntil(() => events.length === 4 && userBtn.disabled === false);
        expect(userBtn.getAttribute('aria-pressed')).toBe('false');

        // The older user-only response arrives after canonical global
        // read-back and must not overwrite either scope control.
        resolveInitialState();
        await flushUntil(() => initialStateJson?.mock.calls.length === 1);
        await Promise.resolve();

        expect(events).toEqual(['GET', 'POST', 'confirm', 'POST']);
        expect(global.fetch).toHaveBeenCalledTimes(4);
        const governedCalls = global.fetch.mock.calls.filter(([url]) =>
            String(url).startsWith('/api/ontology-authority/concepts/')
        );
        const urls = governedCalls.map(([url]) => url);
        expect(urls.every((url) => url === '/api/ontology-authority/concepts/%23V%23private_note/scope')).toBe(true);
        expect(urls.some((url) => url.includes('/concept/user-relation'))).toBe(false);
        const previewBody = JSON.parse(governedCalls[1][1].body);
        const executeBody = JSON.parse(governedCalls[2][1].body);
        expect(previewBody).toMatchObject({
            destination_kind: 'global',
            expected_scope_fingerprint: 'scope-user-before',
            preview: true,
            reason: 'concept_tab_publication_scope_change'
        });
        expect(executeBody).toMatchObject({
            destination_kind: 'global',
            expected_scope_fingerprint: 'scope-user-before',
            preview: false,
            reason: 'concept_tab_publication_scope_change'
        });
        expect(previewBody.request_id).toBeTruthy();
        expect(executeBody.request_id).toBeTruthy();
        expect(executeBody.request_id).not.toBe(previewBody.request_id);
        expect(userBtn.getAttribute('aria-pressed')).toBe('false');
        expect(orgBtn.getAttribute('aria-pressed')).toBe('false');
        expect(userBtn.disabled).toBe(false);
        expect(orgBtn.disabled).toBe(false);
    });

    test.each([
        [
            'user',
            '#V#signed_in_user',
            '#V#specific_to_user'
        ],
        [
            'organisation',
            '#V#trusted_organisation',
            '#V#specific_to_organisation'
        ]
    ])('trusted %s default is resolved by preview and bound into execute', async (
        destinationKind,
        trustedDestination,
        predicate
    ) => {
        window.localStorage.setItem('von_current_user', '#V#forged_browser_user');
        window.localStorage.setItem('von:organisation', '#V#forged_browser_org');
        const before = globalScope();
        const to = {
            kind: destinationKind,
            concept_id: trustedDestination,
            source: 'scope_change_request'
        };
        const after = {
            concept_id: '#V#scope_fixture',
            publication_context: to,
            scope_edges: { [predicate]: [trustedDestination] },
            scope_fingerprint: 'scope-after'
        };
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload({
                from: before.publication_context,
                to,
                add: [{ predicate, target: trustedDestination }]
            })))
            .mockResolvedValueOnce(jsonResponse({
                success: true,
                effect_status: 'succeeded',
                canonical_read_back: after
            }));

        const result = await executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: destinationKind,
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `request-${phase}`
        });

        expect(result.success).toBe(true);
        const previewRequest = JSON.parse(fetchImpl.mock.calls[1][1].body);
        const executeRequest = JSON.parse(fetchImpl.mock.calls[2][1].body);
        expect(previewRequest.destination_kind).toBe(destinationKind);
        expect(previewRequest).not.toHaveProperty('destination_concept_id');
        expect(previewRequest).not.toHaveProperty('user_concept_id');
        expect(previewRequest).not.toHaveProperty('organisation_concept_id');
        expect(executeRequest.destination_concept_id).toBe(trustedDestination);
        const requestHeaders = fetchImpl.mock.calls.map(([, options]) => options?.headers || {});
        requestHeaders.forEach((headers) => {
            expect(headers).not.toHaveProperty('X-User-Concept-ID');
            expect(headers).not.toHaveProperty('X-User-Client-ID');
            expect(headers).not.toHaveProperty('X-Organisation-Concept-ID');
            expect(headers['X-Von-Window-Session']).toMatch(/^ws_/);
        });
        expect(requestHeaders[1]['Content-Type']).toBe('application/json');
        expect(requestHeaders[2]['Content-Type']).toBe('application/json');
        expect(fetchImpl.mock.calls.some(([url]) => String(url).includes('/concept/user-relation'))).toBe(false);
        expect(fetchImpl.mock.calls.some(([url]) => String(url).includes('/concept/organization-relation'))).toBe(false);
    });

    test('confirmation cancellation stops after preview and leaves canonical state unchanged', async () => {
        const before = userScope();
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload({
                from: before.publication_context,
                to: { kind: 'global', concept_id: null },
                remove: [{ predicate: '#V#specific_to_user', target: '#V#signed_in_user' }]
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

    test('preview denial never executes', async () => {
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(userScope()))
            .mockResolvedValueOnce(jsonResponse({
                success: false,
                error_code: 'global_ontology_admin_authority_required',
                error: 'Global ontology administrator authority is required.'
            }, 409));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'user',
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `denied-${phase}`
        })).rejects.toMatchObject({ code: 'global_ontology_admin_authority_required' });
        expect(fetchImpl).toHaveBeenCalledTimes(2);
    });

    test('preview cannot substitute a different destination', async () => {
        const before = globalScope();
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload({
                from: before.publication_context,
                to: { kind: 'user', concept_id: '#V#signed_in_user' },
                add: [{ predicate: '#V#specific_to_user', target: '#V#signed_in_user' }]
            })));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'organisation',
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `mismatch-${phase}`
        })).rejects.toMatchObject({ code: 'scope_preview_destination_mismatch' });
        expect(fetchImpl).toHaveBeenCalledTimes(2);
    });

    test('success response is rejected when canonical read-back disagrees', async () => {
        const before = userScope();
        const fetchImpl = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload({
                from: before.publication_context,
                to: { kind: 'global', concept_id: null },
                remove: [{ predicate: '#V#specific_to_user', target: '#V#signed_in_user' }]
            })))
            .mockResolvedValueOnce(jsonResponse({
                success: true,
                effect_status: 'succeeded',
                canonical_read_back: before
            }));

        await expect(executeGovernedScopeControlChange({
            conceptId: '#V#scope_fixture',
            controlKind: 'user',
            fetchImpl,
            confirmImpl: () => true,
            requestIdFactory: (phase) => `readback-mismatch-${phase}`
        })).rejects.toMatchObject({ code: 'scope_change_canonical_read_back_mismatch' });
        expect(fetchImpl).toHaveBeenCalledTimes(3);
    });

    test('stale execute refreshes both controls from canonical read-back without retry', async () => {
        jest.spyOn(console, 'error').mockImplementation(() => {});
        const before = userScope('#V#stale_scope');
        const concurrent = {
            concept_id: '#V#stale_scope',
            publication_context: {
                kind: 'organisation',
                concept_id: '#V#concurrent_org'
            },
            scope_edges: {
                '#V#specific_to_organisation': ['#V#concurrent_org']
            },
            scope_fingerprint: 'scope-concurrently-changed'
        };
        global.fetch = jest
            .fn()
            .mockResolvedValueOnce(jsonResponse(before))
            .mockResolvedValueOnce(jsonResponse(previewPayload({
                from: before.publication_context,
                to: { kind: 'global', concept_id: null },
                remove: [{ predicate: '#V#specific_to_user', target: '#V#signed_in_user' }]
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
        expect(orgBtn.getAttribute('aria-pressed')).toBe('true');
        expect(userBtn.getAttribute('aria-pressed')).toBe('false');
        expect(orgBtn.disabled).toBe(false);
        expect(userBtn.disabled).toBe(false);
    });
});
