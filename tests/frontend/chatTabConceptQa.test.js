/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';
const qaModulePath = '../../src/frontend/web/von_interface/static/js/utils/conceptQaSession.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#qa_tester', org_id: '#V#lab' })),
    getWindowSessionId: jest.fn(() => 'qa-window'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session',
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#qa_tester'),
    renderSpanSuggestions: jest.fn(),
}));

jest.mock(qaModulePath, () => {
    const actual = jest.requireActual(qaModulePath);
    return {
        ...actual,
        transitionConceptQaSession: jest.fn(),
        submitConceptQaTurn: jest.fn(),
        confirmConceptQaFormalisation: jest.fn(),
    };
});

function fixture() {
    window.scrollTo = jest.fn();
    document.body.innerHTML = `
        <div id="chatTab">
            <div id="conversationWorkspace">
                <div id="chatSessionTabs"></div>
                <div id="chatSessionMetadata"></div>
                <div id="conceptQaConversationControls" class="hidden"></div>
                <div id="scrollableField"></div>
                <textarea id="promptInput" placeholder="Talk with Von here..."></textarea>
                <button id="sendButton">Send Prompt</button>
            </div>
        </div>
    `;
}

function activeSession(overrides = {}) {
    return {
        session_id: 'qa-chat-1',
        chat_session_id: 'qa-chat-1',
        session_name: 'Primary Labs Q&A',
        focal_concept_id: '#V#primary_labs',
        lifecycle: 'active',
        mode: 'concept_q_and_a',
        origin_kind: 'concept_q_and_a',
        conversation_reference: {
            schema_version: 'conversation_reference.v1',
            binding_kind: 'explicit_session_id',
            session_id: 'qa-chat-1',
            user_concept_id: '#V#qa_tester',
        },
        ...overrides,
    };
}

describe('chat workspace concept Q&A mode', () => {
    beforeEach(() => {
        fixture();
        jest.useFakeTimers();
        global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({}) }));
        const qa = require(qaModulePath);
        qa.transitionConceptQaSession.mockReset();
        qa.submitConceptQaTurn.mockReset();
        qa.confirmConceptQaFormalisation.mockReset();
    });

    afterEach(() => {
        const chat = require(chatTabModulePath);
        chat.__testOnly_resetConceptQaState();
        chat.__testOnly_resetChatRequestState();
        jest.clearAllTimers();
        jest.useRealTimers();
        jest.resetModules();
    });

    test('renders Q&A mode and waits for canonical finish read-back', async () => {
        const qa = require(qaModulePath);
        let resolveFinish;
        qa.transitionConceptQaSession.mockReturnValue(new Promise((resolve) => {
            resolveFinish = resolve;
        }));
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession({
            turns: [{
                role: 'assistant',
                content: 'Which organisation is it a member of?',
                concept_q_and_a: {
                    elicitation_predicate: {
                        predicate_concept_id: '#V#memberOf',
                        predicate_label: 'member of',
                        status: 'missing',
                        gap_status: 'asserted_relation_missing',
                        requirement_kind: 'constitutive_relation',
                        priority_class: 'constitutive',
                        priority: 0,
                    },
                },
            }],
        }));

        expect(document.getElementById('conceptQaConversationControls').textContent)
            .toContain('Concept Q&A');
        expect(document.getElementById('conceptQaConversationControls').textContent)
            .toContain('not an assertion or a creation block');
        expect(document.getElementById('sendButton').textContent).toBe('Submit answer');

        const pending = chat.__testOnly_transitionActiveConceptQa('finish');
        await Promise.resolve();
        expect(qa.transitionConceptQaSession).toHaveBeenCalledWith({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-chat-1',
            action: 'finish',
        });
        expect(chat.__testOnly_getConceptQaSession().lifecycle).toBe('active');
        expect(document.getElementById('conceptQaConversationControls').textContent)
            .toContain('Saving…');

        resolveFinish(activeSession({ lifecycle: 'finished' }));
        await pending;
        expect(chat.__testOnly_getConceptQaSession().lifecycle).toBe('finished');
        expect(document.getElementById('conceptQaConversationControls').textContent)
            .toContain('transcript and prior knowledge are retained');
        expect(document.getElementById('sendButton').disabled).toBe(true);

        chat.__testOnly_updateExternalConversationUi();
        expect(document.getElementById('promptInput').disabled).toBe(true);
    });

    test('rehydrates stable turn IDs, LLM access and separate receipts in the normal transcript', () => {
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession());
        chat.__test_only__rehydrateHistory(document.getElementById('scrollableField'), [
            {
                role: 'assistant',
                turn_id: 'qa-turn-stable-7',
                content: 'Would you like to confirm the tentative relation?',
                timestamp: '2026-09-02T12:00:00Z',
                history_location: { session_id: 'qa-chat-1', history_index: 7 },
                receipts: {
                    exact_input: { status: 'not_admitted' },
                    formalisation: {
                        status: 'tentative',
                        assertion_id: 'tentative-7',
                        predicate_id: '#V#memberOf',
                        confirmation: {
                            available: true,
                            method: 'POST',
                            action: '/api/concepts/%23V%23primary_labs/q_and_a/sessions/qa-chat-1/formalisations/tentative-7/confirm',
                        },
                    },
                    notes: { status: 'no_update' },
                },
            },
        ]);

        const turn = document.querySelector('[data-turn-id="qa-turn-stable-7"]');
        expect(turn).not.toBeNull();
        expect(turn.querySelector('.llm-debug-button')).not.toBeNull();
        expect(turn.textContent).toContain('Transcript only');
        expect(turn.textContent).toContain('Tentative typed relation recorded');
        expect(turn.textContent).toContain('Confirm relation');
    });

    test('does not offer or dispatch confirmation from a cancelled Q&A receipt', async () => {
        const qa = require(qaModulePath);
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession({ lifecycle: 'cancelled' }));
        chat.__test_only__rehydrateHistory(document.getElementById('scrollableField'), [
            {
                role: 'assistant',
                turn_id: 'qa-turn-terminal-confirmation',
                content: 'The tentative relation remains recorded.',
                receipts: {
                    formalisation: {
                        status: 'tentative',
                        assertion_id: 'tentative-terminal-7',
                        confirmation: {
                            available: true,
                            available_after_cancel: false,
                        },
                    },
                },
            },
        ]);

        const button = document.querySelector('.concept-qa-receipt-confirm');
        expect(button.disabled).toBe(true);
        expect(button.textContent).toBe('Unavailable — Q&A cancelled');
        button.click();
        await Promise.resolve();
        expect(qa.confirmConceptQaFormalisation).not.toHaveBeenCalled();

        await expect(chat.__testOnly_confirmActiveConceptQaFormalisation({
            status: 'tentative',
            assertion_id: 'tentative-terminal-7',
            confirmation: {
                available: true,
                available_after_cancel: false,
            },
        })).resolves.toBe(false);
        expect(qa.confirmConceptQaFormalisation).not.toHaveBeenCalled();
        expect(document.getElementById('conceptQaConversationControls').textContent)
            .toContain('receipt can no longer confirm');
    });

    test('allows post-finish confirmation only when the receipt explicitly supports it', async () => {
        const qa = require(qaModulePath);
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession({ lifecycle: 'finished' }));

        const receipt = {
            status: 'tentative',
            assertion_id: 'tentative-finished-7',
            confirmation: {
                available: true,
                available_after_finish: true,
            },
        };
        const historyTurn = (formalisation) => ({
            role: 'assistant',
            turn_id: 'qa-turn-finished-confirmation',
            content: 'The tentative relation remains recorded.',
            receipts: { formalisation },
        });

        chat.__test_only__rehydrateHistory(
            document.getElementById('scrollableField'),
            [historyTurn({
                ...receipt,
                confirmation: { available: true },
            })],
        );
        expect(document.querySelector('.concept-qa-receipt-confirm').disabled).toBe(true);
        expect(document.querySelector('.concept-qa-receipt-confirm').textContent)
            .toBe('Unavailable — Q&A finished');

        chat.__test_only__rehydrateHistory(
            document.getElementById('scrollableField'),
            [historyTurn(receipt)],
        );
        expect(document.querySelector('.concept-qa-receipt-confirm').disabled).toBe(false);
        expect(document.querySelector('.concept-qa-receipt-confirm').textContent)
            .toBe('Confirm relation');

        const canonicalReadBack = { epistemic_status: 'asserted' };
        qa.confirmConceptQaFormalisation.mockResolvedValue({
            session: activeSession({
                lifecycle: 'finished',
                turns: [{
                    role: 'assistant',
                    turn_id: 'qa-turn-confirmed-after-finish',
                    content: 'Confirmed and read back.',
                }],
            }),
            receipts: {
                formalisation: {
                    status: 'asserted',
                    canonical_read_back: canonicalReadBack,
                    confirmation: { available: false },
                },
            },
            payload: {
                success: true,
                confirmation: { canonical_read_back: canonicalReadBack },
            },
        });

        await expect(chat.__testOnly_confirmActiveConceptQaFormalisation(receipt))
            .resolves.toBe(true);
        expect(qa.confirmConceptQaFormalisation).toHaveBeenCalledWith({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-chat-1',
            assertionId: 'tentative-finished-7',
            confirmation: receipt.confirmation,
        });
    });

    test('routes the composer through the Q&A turn endpoint and refreshes concept knowledge', async () => {
        const qa = require(qaModulePath);
        const updated = activeSession({
            turns: [
                {
                    role: 'user',
                    turn_id: 'qa-saved-user-1',
                    content: 'Primary Labs is a research company.',
                    receipts: {
                        exact_input: { status: 'stored' },
                        formalisation: { status: 'deferred' },
                        notes: { status: 'not_updated' },
                    },
                },
                {
                    role: 'assistant',
                    turn_id: 'qa-saved-assistant-1',
                    content: 'What field does it work in?',
                    history_location: { session_id: 'qa-chat-1', history_index: 1 },
                },
            ],
        });
        qa.submitConceptQaTurn.mockResolvedValue({
            session: updated,
            receipts: {
                exact_input: { status: 'stored' },
                formalisation: { status: 'deferred' },
                notes: { status: 'not_updated' },
            },
        });
        const knowledgeEvents = [];
        document.addEventListener('von:conceptKnowledgeChanged', (event) => {
            knowledgeEvents.push(event.detail);
        }, { once: true });
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession());
        document.getElementById('promptInput').value = 'Primary Labs is a research company.';

        await chat.__testOnly_sendChatPrompt();

        expect(qa.submitConceptQaTurn).toHaveBeenCalledWith({
            conceptId: '#V#primary_labs',
            sessionId: 'qa-chat-1',
            answer: 'Primary Labs is a research company.',
        });
        expect(document.getElementById('promptInput').value).toBe('');
        expect(document.querySelector('[data-turn-id="qa-saved-user-1"]').textContent)
            .toContain('Exact text claim stored');
        expect(knowledgeEvents).toHaveLength(1);
        expect(knowledgeEvents[0].conceptId).toBe('#V#primary_labs');
    });

    test('keeps denied membership tentative and shows the canonical handoff and retry path', async () => {
        const qa = require(qaModulePath);
        qa.confirmConceptQaFormalisation.mockRejectedValue(Object.assign(
            new Error('organisation_membership_confirmation_denied'),
            {
                status: 403,
                payload: {
                    ...activeSession(),
                    success: false,
                    confirmation: {
                        error_code: 'organisation_membership_confirmation_denied',
                        actionable_denial: {
                            who_can_add_membership: (
                                'an organisation admin or owner with the represented '
                                + 'MANAGE_MEMBERS permission'
                            ),
                            required_permissions: ['MANAGE_MEMBERS'],
                            mechanism: 'canonical_organisation_membership_handoff',
                            membership_management_path: (
                                'canonical organisation membership management'
                            ),
                            same_q_and_a_action_available_to_other_actor: false,
                            original_actor_next_step: (
                                'After an authorised actor adds the membership, the '
                                + 'original Q&A actor can retry this Confirm action; it '
                                + 'will reconcile the membership and promote the '
                                + 'source-linked tentative assertion.'
                            ),
                            tentative_assertion_preserved: true,
                        },
                    },
                },
            },
        ));
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession());

        await expect(chat.__testOnly_confirmActiveConceptQaFormalisation({
            status: 'tentative',
            assertion_id: 'ska_tentative_1',
            confirmation: { available: true },
        })).resolves.toBe(false);

        const controls = document.getElementById('conceptQaConversationControls').textContent;
        expect(controls).toContain('The tentative relation was retained');
        expect(controls).toContain(
            'Membership handoff: an organisation admin or owner with the represented '
            + 'MANAGE_MEMBERS permission must add the membership through canonical '
            + 'organisation membership management.'
        );
        expect(controls).toContain(
            'The authorised membership manager cannot use this actor-scoped Q&A Confirm '
            + 'action on behalf of the original actor.'
        );
        expect(controls).toContain(
            'Next step for the original Q&A actor: After an authorised actor adds the '
            + 'membership, the original Q&A actor can retry this Confirm action; it will '
            + 'reconcile the membership and promote the source-linked tentative assertion.'
        );
        expect(controls).toContain('MANAGE_MEMBERS');
    });

    test('retains generic confirmer guidance for non-membership denials', async () => {
        const qa = require(qaModulePath);
        qa.confirmConceptQaFormalisation.mockRejectedValue(Object.assign(
            new Error('formalisation_confirmation_actor_required'),
            {
                status: 403,
                payload: {
                    ...activeSession(),
                    success: false,
                    confirmation: {
                        error_code: 'formalisation_confirmation_actor_required',
                        actionable_denial: {
                            who_can_confirm: 'the original asserting actor',
                            tentative_assertion_preserved: true,
                        },
                    },
                },
            },
        ));
        const chat = require(chatTabModulePath);
        chat.__testOnly_setConceptQaSession(activeSession());

        await expect(chat.__testOnly_confirmActiveConceptQaFormalisation({
            status: 'tentative',
            assertion_id: 'ska_generic_tentative_1',
            confirmation: { available: true },
        })).resolves.toBe(false);

        expect(document.getElementById('conceptQaConversationControls').textContent)
            .toContain('Confirmation requires the original asserting actor.');
    });
});
