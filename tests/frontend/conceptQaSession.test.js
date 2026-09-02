/** @jest-environment jsdom */

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    ensureUniqueWindowSessionId: jest.fn(async () => 'window-qa-test'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session',
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    getCurrentUserConceptId: jest.fn(() => '#V#tester'),
}));

const {
    confirmConceptQaFormalisation,
    describeConceptQaElicitationGap,
    fetchConceptQaProjection,
    normaliseConceptQaProjection,
    normaliseConceptQaSession,
    startConceptQaSession,
    submitConceptQaTurn,
    transitionConceptQaSession,
} = require('../../src/frontend/web/von_interface/static/js/utils/conceptQaSession.js');

function response(payload, status = 200) {
    return {
        ok: status >= 200 && status < 300,
        status,
        json: async () => payload,
    };
}

describe('concept Q&A frontend contract', () => {
    beforeEach(() => {
        global.fetch = jest.fn();
    });

    test('normalises the active canonical session without collapsing separate receipts', () => {
        const projection = normaliseConceptQaProjection({
            sessions: [{
                session_id: 'qa-session-1',
                mode: 'concept_q_and_a',
                origin_kind: 'concept_q_and_a',
                focal_concept_id: '#V#primary_labs',
                lifecycle: 'active',
                conversation_reference: {
                    schema_version: 'conversation_reference.v1',
                    binding_kind: 'explicit_session_id',
                    session_id: 'qa-session-1',
                },
            }],
        });

        expect(projection.active_session).toEqual(expect.objectContaining({
            session_id: 'qa-session-1',
            concept_id: '#V#primary_labs',
            lifecycle: 'active',
            origin_kind: 'concept_q_and_a',
        }));
        expect(projection.active_session.available_actions).toContain('submit_turn');

        const turn = normaliseConceptQaSession({
            session: projection.active_session,
            receipts: {
                exact_input: { status: 'stored', assertion_id: 'ska-1' },
                formalisation: { status: 'candidate' },
                notes: { status: 'failed' },
            },
        });
        expect(turn.receipts).toEqual({
            exact_input: { status: 'stored', assertion_id: 'ska-1' },
            formalisation: { status: 'candidate' },
            notes: { status: 'failed' },
        });
    });

    test('accepts the compatibility legacy-active list field', () => {
        const projection = normaliseConceptQaProjection({
            legacy_active_interactions: [{ interaction_id: 'legacy-1' }],
        });
        expect(projection.legacy_interactions).toEqual([{ interaction_id: 'legacy-1' }]);
    });

    test('describes only a structured missing-relation target as a Q&A gap', () => {
        const session = {
            turns: [{
                role: 'assistant',
                content: 'Who is it a member of?',
                concept_q_and_a: {
                    elicitation_predicate: {
                        predicate_concept_id: '#V#memberOfVonOrg',
                        predicate_label: 'member of',
                        status: 'missing',
                        gap_status: 'asserted_relation_missing',
                        requirement_kind: 'constitutive_relation',
                        priority_class: 'constitutive',
                        priority: 0,
                    },
                },
            }],
        };
        expect(describeConceptQaElicitationGap(session)).toBe(
            'Current constitutive gap: member of — asserted relation missing. '
            + 'This is a prioritised Q&A target, not an assertion or a creation block.'
        );
        expect(describeConceptQaElicitationGap({
            turns: [{
                role: 'assistant',
                concept_q_and_a: {
                    elicitation_predicate: {
                        predicate_concept_id: '#V#homepage',
                        predicate_label: 'homepage',
                        status: 'missing',
                        requirement_kind: 'salient_relation',
                        priority_class: 'salient',
                        priority: 2,
                    },
                },
            }],
        })).toBe(
            'Current suggested relation: homepage (priority 2). '
            + 'This is a salient Q&A target, not a requirement or an asserted relation.'
        );
        expect(describeConceptQaElicitationGap({
            turns: [{ role: 'assistant', content: 'Who is it a member of?' }],
        })).toBe('');
    });

    test('uses the canonical projection, start, turn, finish and cancel routes', async () => {
        global.fetch
            .mockResolvedValueOnce(response({ sessions: [], active_session: null }))
            .mockResolvedValueOnce(response({
                session_id: 'qa-session-2',
                lifecycle: 'active',
                focal_concept_id: '#V#primary_labs',
            }))
            .mockResolvedValueOnce(response({
                session: {
                    session_id: 'qa-session-2',
                    lifecycle: 'active',
                    focal_concept_id: '#V#primary_labs',
                },
                receipts: { exact_input: { status: 'not_admitted' } },
            }))
            .mockResolvedValueOnce(response({
                session_id: 'qa-session-2',
                lifecycle: 'finished',
                focal_concept_id: '#V#primary_labs',
            }))
            .mockResolvedValueOnce(response({
                session_id: 'qa-session-3',
                lifecycle: 'cancelled',
                focal_concept_id: '#V#primary_labs',
            }));

        await fetchConceptQaProjection('#V#primary_labs');
        await startConceptQaSession('#V#primary_labs');
        await submitConceptQaTurn({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-session-2',
            answer: 'Is that represented?',
        });
        await transitionConceptQaSession({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-session-2',
            action: 'finish',
        });
        await transitionConceptQaSession({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-session-3',
            action: 'cancel',
        });

        expect(global.fetch.mock.calls.map(([url]) => url)).toEqual([
            '/api/concepts/%23V%23primary_labs/q_and_a',
            '/api/concepts/%23V%23primary_labs/q_and_a/start',
            '/api/concepts/%23V%23primary_labs/q_and_a/sessions/qa-session-2/turns',
            '/api/concepts/%23V%23primary_labs/q_and_a/sessions/qa-session-2/finish',
            '/api/concepts/%23V%23primary_labs/q_and_a/sessions/qa-session-3/cancel',
        ]);
        const turnBody = JSON.parse(global.fetch.mock.calls[2][1].body);
        expect(turnBody.answer).toBe('Is that represented?');
        expect(turnBody.turn_id).toMatch(/^(qa-|[0-9a-f]{8}-)/i);
    });

    test('prefers a safe server confirmation action and requires canonical read-back', async () => {
        global.fetch.mockResolvedValue(response({
            session: {
                session_id: 'qa-session-4',
                focal_concept_id: '#V#primary_labs',
                lifecycle: 'active',
            },
            receipts: {
                formalisation: {
                    status: 'succeeded',
                    assertion_id: 'tentative-1',
                    read_back: { verified: true },
                },
            },
        }));

        const result = await confirmConceptQaFormalisation({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-session-4',
            assertionId: 'tentative-1',
            confirmation: {
                available: true,
                method: 'POST',
                action: '/api/concepts/%23V%23primary_labs/q_and_a/sessions/qa-session-4/formalisations/tentative-1/confirm',
                expected_receipt_revision: 3,
            },
        });

        expect(global.fetch).toHaveBeenCalledWith(
            '/api/concepts/%23V%23primary_labs/q_and_a/sessions/qa-session-4/formalisations/tentative-1/confirm',
            expect.objectContaining({ method: 'POST' }),
        );
        expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual(expect.objectContaining({
            expected_receipt_revision: 3,
            request_id: expect.stringMatching(/^(qa-|[0-9a-f]{8}-)/i),
        }));
        expect(result.receipts.formalisation.read_back.verified).toBe(true);
    });
});
