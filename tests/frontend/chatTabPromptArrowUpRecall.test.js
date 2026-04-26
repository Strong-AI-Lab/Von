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
    __testOnly_resetAllArrowUpRecallBuffers
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
});
