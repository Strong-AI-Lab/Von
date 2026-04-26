/** @jest-environment jsdom */

// JVNAUTOSCI-2128: ArrowUp on an empty Talk-with-Von composer recalls the
// most recent user-submitted prompt for the active conversation.

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'mock-window-session-id'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    normalisePotentialConceptAlias: jest.fn((value) => value),
    normalisePotentialConceptId: jest.fn((value) => value),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

const {
    __testOnly_setActiveChatSession,
    __testOnly_rememberLastSubmittedUserPrompt,
    __testOnly_handlePromptInputArrowUpRecall,
    __testOnly_clearLastSubmittedUserPromptForActiveSession,
    __testOnly_resetAllArrowUpRecallBuffers,
    __testOnly_rehydrateLastSubmittedUserPromptFromHistory
} = require(chatTabModulePath);

function buildPromptInput(initialValue = '') {
    document.body.innerHTML = '<textarea id="promptInput"></textarea>';
    const promptInput = document.getElementById('promptInput');
    promptInput.value = initialValue;
    return promptInput;
}

function makeArrowUpEvent(promptInput, overrides = {}) {
    const event = {
        key: 'ArrowUp',
        shiftKey: false,
        ctrlKey: false,
        altKey: false,
        metaKey: false,
        currentTarget: promptInput,
        preventDefault: jest.fn(),
        ...overrides
    };
    return event;
}

describe('JVNAUTOSCI-2128: ArrowUp recall in Talk-with-Von composer', () => {
    beforeEach(() => {
        __testOnly_resetAllArrowUpRecallBuffers();
        __testOnly_setActiveChatSession('session-A', 'Session A');
    });

    test('ArrowUp on empty composer with prior submission recalls last prompt with cursor at end', () => {
        __testOnly_rememberLastSubmittedUserPrompt('hello world');
        const promptInput = buildPromptInput('');

        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        expect(event.preventDefault).toHaveBeenCalledTimes(1);
        expect(promptInput.value).toBe('hello world');
        expect(promptInput.selectionStart).toBe(promptInput.value.length);
        expect(promptInput.selectionEnd).toBe(promptInput.value.length);
    });

    test('ArrowUp on non-empty composer is a no-op (does not overwrite draft)', () => {
        __testOnly_rememberLastSubmittedUserPrompt('previous');
        const promptInput = buildPromptInput('a draft in progress');

        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(promptInput.value).toBe('a draft in progress');
    });

    test('ArrowUp before any submission is a no-op', () => {
        const promptInput = buildPromptInput('');

        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(promptInput.value).toBe('');
    });

    test('Modifier-held ArrowUp is a no-op', () => {
        __testOnly_rememberLastSubmittedUserPrompt('prior');
        const promptInput = buildPromptInput('');

        for (const modifier of ['shiftKey', 'ctrlKey', 'altKey', 'metaKey']) {
            const event = makeArrowUpEvent(promptInput, { [modifier]: true });
            __testOnly_handlePromptInputArrowUpRecall(event);
            expect(event.preventDefault).not.toHaveBeenCalled();
            expect(promptInput.value).toBe('');
        }
    });

    test('Recall is per conversation: switching session hides the other session\'s buffer', () => {
        __testOnly_setActiveChatSession('session-A', 'Session A');
        __testOnly_rememberLastSubmittedUserPrompt('only-in-A');

        __testOnly_setActiveChatSession('session-B', 'Session B');
        const promptInput = buildPromptInput('');
        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        // session-B has no recall buffer of its own.
        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(promptInput.value).toBe('');

        // Returning to session-A still recalls its buffered prompt.
        __testOnly_setActiveChatSession('session-A', 'Session A');
        const promptInputA = buildPromptInput('');
        const eventA = makeArrowUpEvent(promptInputA);
        __testOnly_handlePromptInputArrowUpRecall(eventA);
        expect(eventA.preventDefault).toHaveBeenCalledTimes(1);
        expect(promptInputA.value).toBe('only-in-A');
    });

    test('Clearing the active session\'s buffer disables recall for that session', () => {
        __testOnly_rememberLastSubmittedUserPrompt('to-be-forgotten');
        __testOnly_clearLastSubmittedUserPromptForActiveSession();

        const promptInput = buildPromptInput('');
        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(promptInput.value).toBe('');
    });

    // JVNAUTOSCI-2129: rehydrate the recall buffer from chat history so
    // ArrowUp works after a page reload / Von restart.
    test('Rehydration from history seeds buffer with the most recent user message', () => {
        const history = [
            { role: 'user', content: 'first user message' },
            { role: 'assistant', content: 'an assistant reply' },
            { role: 'user', content: 'most recent user message' },
            { role: 'assistant', content: 'final assistant reply' }
        ];
        __testOnly_rehydrateLastSubmittedUserPromptFromHistory(history);

        const promptInput = buildPromptInput('');
        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        expect(event.preventDefault).toHaveBeenCalledTimes(1);
        expect(promptInput.value).toBe('most recent user message');
        expect(promptInput.selectionStart).toBe(promptInput.value.length);
    });

    test('Rehydration with empty/non-user history leaves buffer untouched', () => {
        __testOnly_rehydrateLastSubmittedUserPromptFromHistory([]);
        __testOnly_rehydrateLastSubmittedUserPromptFromHistory([
            { role: 'assistant', content: 'only assistant content' }
        ]);
        __testOnly_rehydrateLastSubmittedUserPromptFromHistory(null);

        const promptInput = buildPromptInput('');
        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);

        expect(event.preventDefault).not.toHaveBeenCalled();
        expect(promptInput.value).toBe('');
    });

    test('Rehydration is per conversation', () => {
        __testOnly_setActiveChatSession('session-A', 'Session A');
        __testOnly_rehydrateLastSubmittedUserPromptFromHistory([
            { role: 'user', content: 'A-prompt' }
        ]);

        __testOnly_setActiveChatSession('session-B', 'Session B');
        const inputB = buildPromptInput('');
        const eventB = makeArrowUpEvent(inputB);
        __testOnly_handlePromptInputArrowUpRecall(eventB);
        expect(eventB.preventDefault).not.toHaveBeenCalled();
        expect(inputB.value).toBe('');

        __testOnly_setActiveChatSession('session-A', 'Session A');
        const inputA = buildPromptInput('');
        const eventA = makeArrowUpEvent(inputA);
        __testOnly_handlePromptInputArrowUpRecall(eventA);
        expect(eventA.preventDefault).toHaveBeenCalledTimes(1);
        expect(inputA.value).toBe('A-prompt');
    });

    test('Rehydration ignores empty-content user messages', () => {
        __testOnly_rehydrateLastSubmittedUserPromptFromHistory([
            { role: 'user', content: 'real prompt' },
            { role: 'assistant', content: 'reply' },
            { role: 'user', content: '' },
            { role: 'user', content: null }
        ]);

        const promptInput = buildPromptInput('');
        const event = makeArrowUpEvent(promptInput);
        __testOnly_handlePromptInputArrowUpRecall(event);
        expect(event.preventDefault).toHaveBeenCalledTimes(1);
        expect(promptInput.value).toBe('real prompt');
    });
});
