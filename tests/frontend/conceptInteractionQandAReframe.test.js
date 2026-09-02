const fs = require('fs');
const path = require('path');

const {
    describeConceptQaRepresentation,
    handleStartInteraction,
    isUnmistakableMissingConceptQaRoute,
    refreshConceptQaSessionCard,
} = require('../../src/frontend/web/von_interface/static/js/conceptTab.js');
const {
    setCurrentlySelectedConceptId,
} = require('../../src/frontend/web/von_interface/static/js/state.js');

const conceptTemplate = fs.readFileSync(
    path.resolve(__dirname, '../../src/frontend/web/von_interface/templates/concept_tab.html'),
    'utf8',
);

describe('concept-improvement Q&A entry point', () => {
    test('keeps Discuss separate and discloses the specialised notes-updating flow', () => {
        document.body.innerHTML = conceptTemplate;

        const discuss = document.getElementById('discussConceptButton');
        const improveByQa = document.getElementById('startInteractionButton');
        const disclosure = document.getElementById('conceptInteractionDisclosure');
        const submitAnswer = document.getElementById('submitAnswerButton');
        const cancelQa = document.getElementById('cancelInteractionButton');
        const finishQa = document.getElementById('endInteractionButton');
        const restart = document.getElementById('resetConceptTabButton');

        expect(discuss.textContent.trim()).toBe('Discuss');
        expect(improveByQa.textContent.trim()).toBe('Improve concept by Q&A');
        expect(improveByQa.title).toContain('recoverable conversation');
        expect(disclosure.textContent).toMatch(/exact claims are stored with provenance/i);
        expect(disclosure.textContent).toMatch(/formalisation and notes updates are reported separately/i);
        expect(submitAnswer.title).toContain('continue the concept Q&A');
        expect(cancelQa.textContent.trim()).toBe('Cancel Q&A');
        expect(cancelQa.title).toContain('concept-improvement Q&A');
        expect(finishQa.textContent.trim()).toBe('Finish Q&A');
        expect(finishQa.title).toContain('retaining its transcript');
        expect(restart.textContent.trim()).toBe('Improve concept by Q&A again');

        const visibleControlCopy = [improveByQa, submitAnswer, cancelQa, finishQa, restart]
            .map((element) => element.textContent.trim())
            .join(' ');
        expect(visibleControlCopy).not.toMatch(/\binteraction\b/i);
    });

    test('distinguishes note persistence failure from absent synthesis', () => {
        const presentation = describeConceptQaRepresentation({
            synthesis: "Primary Labs' main product is the news site theprimary.com.",
            synthesis_status: 'persistence_failed',
            representation: {
                exact_answer: { status: 'stored', assertion_id: 'ska-answer-1' },
                concept_notes: { status: 'persistence_failed' },
            },
        });

        expect(presentation.synthesisText).toContain(
            "Primary Labs' main product is the news site theprimary.com."
        );
        expect(presentation.synthesisText).toContain('notes update failed');
        expect(presentation.statusText).toContain('stored as scoped knowledge');
        expect(presentation.statusText).toContain('No typed relation is implied');
        expect(presentation.statusText).not.toContain('No synthesis generated');
    });

    test('reports successful provenance and note representation', () => {
        const presentation = describeConceptQaRepresentation({
            synthesis: "Primary Labs' main product is the news site theprimary.com.",
            synthesis_status: 'updated',
            representation: {
                exact_answer: { status: 'stored', assertion_id: 'ska-answer-1' },
                concept_notes: { status: 'updated' },
            },
        });

        expect(presentation.statusText).toContain('Exact text claim stored with provenance');
        expect(presentation.statusText).toContain('concept notes updated');
        expect(presentation.statusColor).toBe('green');
    });

    test('uses the legacy path only for an unmistakably absent canonical route', () => {
        expect(isUnmistakableMissingConceptQaRoute({ status: 404, payload: {} })).toBe(true);
        expect(isUnmistakableMissingConceptQaRoute({
            status: 404,
            payload: {
                error: 'Q&A conversation was not found for this concept',
                error_code: 'conversation_not_found',
            },
        })).toBe(false);
        expect(isUnmistakableMissingConceptQaRoute({
            status: 403,
            payload: { error_code: 'actor_context_required' },
        })).toBe(false);
        expect(isUnmistakableMissingConceptQaRoute({ status: 405, payload: {} })).toBe(false);
    });

    test('projects an active Q&A as resume, transcript and canonical-reference actions', async () => {
        document.body.innerHTML = conceptTemplate;
        const originalFetch = global.fetch;
        global.fetch = jest.fn(async () => ({
            ok: true,
            status: 200,
            json: async () => ({
                sessions: [{
                    session_id: 'qa-primary-labs-1',
                    focal_concept_ids: ['#V#primary_labs'],
                    lifecycle: { status: 'active', revision: 1 },
                    mode: 'concept_q_and_a',
                    origin_kind: 'concept_q_and_a',
                    conversation_reference: {
                        schema_version: 'conversation_reference.v1',
                        binding_kind: 'explicit_session_id',
                        session_id: 'qa-primary-labs-1',
                    },
                    turns: [{
                        role: 'assistant',
                        content: 'Which organisation is it a member of?',
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
                }],
            }),
        }));
        try {
            await refreshConceptQaSessionCard('#V#primary_labs');
        } finally {
            global.fetch = originalFetch;
        }

        expect(document.getElementById('conceptQaSessionBadge').textContent).toBe('Active');
        expect(document.getElementById('startInteractionButton').classList).toContain('hidden');
        expect(document.getElementById('resumeConceptQaButton').classList).not.toContain('hidden');
        expect(document.getElementById('openConceptQaTranscriptButton').classList).not.toContain('hidden');
        expect(document.getElementById('copyConceptQaReferenceButton').classList).not.toContain('hidden');
        expect(document.getElementById('conceptQaGapStatus').textContent).toContain(
            'Current constitutive gap: member of — asserted relation missing'
        );
        expect(document.getElementById('conceptQaGapStatus').textContent).toContain(
            'not an assertion or a creation block'
        );
    });

    test('renders a retryable initial-question failure as an existing recoverable Q&A', async () => {
        document.body.innerHTML = conceptTemplate;
        const originalFetch = global.fetch;
        global.fetch = jest.fn(async () => ({
            ok: true,
            status: 200,
            json: async () => ({
                sessions: [{
                    session_id: 'qa-primary-labs-retry',
                    focal_concept_ids: ['#V#primary_labs'],
                    lifecycle: { status: 'active', revision: 0 },
                    mode: 'concept_q_and_a',
                    origin_kind: 'concept_q_and_a',
                    initial_question: {
                        status: 'retryable_failure',
                        retryable: true,
                        attempt_count: 1,
                        failure: {
                            error_code: 'initial_question_generation_failed',
                            message: 'The initial Q&A question could not be generated.',
                        },
                    },
                    conversation_reference: {
                        schema_version: 'conversation_reference.v1',
                        binding_kind: 'explicit_session_id',
                        session_id: 'qa-primary-labs-retry',
                    },
                    turns: [],
                }],
            }),
        }));
        try {
            await refreshConceptQaSessionCard('#V#primary_labs');
        } finally {
            global.fetch = originalFetch;
        }

        expect(document.getElementById('conceptQaSessionBadge').textContent).toBe('Needs retry');
        expect(document.getElementById('conceptQaSessionSummary').textContent)
            .toContain('Retry to continue this same recoverable conversation');
        expect(document.getElementById('startInteractionButton').textContent)
            .toBe('Retry initial question');
        expect(document.getElementById('startInteractionButton').classList)
            .not.toContain('hidden');
        expect(document.getElementById('resumeConceptQaButton').classList).toContain('hidden');
        expect(document.getElementById('openConceptQaTranscriptButton').classList)
            .not.toContain('hidden');
    });

    test('keeps a partial start on the concept card instead of opening an empty transcript', async () => {
        document.body.innerHTML = conceptTemplate;
        setCurrentlySelectedConceptId('#V#primary_labs');
        const originalFetch = global.fetch;
        global.fetch = jest.fn(async (url) => {
            expect(String(url)).toContain('/q_and_a/start');
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    session_id: 'qa-primary-labs-partial-start',
                    focal_concept_ids: ['#V#primary_labs'],
                    lifecycle: { status: 'active', revision: 0 },
                    mode: 'concept_q_and_a',
                    origin_kind: 'concept_q_and_a',
                    initial_question: {
                        status: 'retryable_failure',
                        retryable: true,
                        attempt_count: 1,
                    },
                    turns: [],
                }),
            };
        });
        let started;
        try {
            started = await handleStartInteraction();
        } finally {
            global.fetch = originalFetch;
        }

        expect(started).toBe(false);
        expect(global.fetch).toBe(originalFetch);
        expect(document.getElementById('conceptQaSessionCard').dataset.state).toBe('retryable');
        expect(document.getElementById('startInteractionButton').textContent)
            .toBe('Retry initial question');
        expect(document.getElementById('conceptQaSessionSummary').textContent)
            .toContain('same recoverable conversation');
    });
});
