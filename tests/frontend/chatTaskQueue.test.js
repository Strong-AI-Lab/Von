/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'mock-window-session-id')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    createVontologyCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    getCartoucheAppearanceSettings: jest.fn(() => ({})),
    linkifyVontologyTokensInElement: jest.fn(),
    normalisePotentialConceptAlias: jest.fn((value) => value),
    normalisePotentialConceptId: jest.fn((value) => value),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

function flushMicrotasks() {
    return new Promise((resolve) => setTimeout(resolve, 0));
}

describe('chat task queue', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div class="thinking-card-wrapper" id="thinkingCardWrapper" aria-hidden="true">
                <div class="thinking-card">
                    <div class="thinking-card-header" id="loadingIndicator">
                        <span class="thinking-card-phase loading-indicator-text">Thinking...</span>
                        <span class="thinking-card-meta" id="thinkingCardMeta"></span>
                        <button id="abortButton" aria-hidden="true"></button>
                    </div>
                    <div class="thinking-card-body" id="loadingIndicatorDetail"></div>
                </div>
            </div>
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_resetChatRequestState();
        chatTab.__testOnly_setActiveChatSession('session-1', 'Current');
    });

    afterEach(() => {
        try {
            const chatTab = require(chatTabModulePath);
            if (typeof chatTab.__testOnly_resetChatRequestState === 'function') {
                chatTab.__testOnly_resetChatRequestState();
            }
        } catch (_) {
            // Module may not have been imported in a failed setup.
        }
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('queues while thinking and executes edited queued prompt after current turn', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const generateBodies = [];
        let resolveFirstGenerate = null;

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                const parsed = JSON.parse(options.body || '{}');
                generateBodies.push(parsed);

                if (generateBodies.length === 1) {
                    return new Promise((resolve) => {
                        resolveFirstGenerate = () => resolve({
                            ok: true,
                            json: async () => ({
                                response: 'First response',
                                llm_debug: { model: 'gpt-5.2' }
                            })
                        });
                    });
                }

                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Second response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'First request';
        const firstRequestPromise = sendMessage();

        await flushMicrotasks();
        expect(generateBodies).toHaveLength(1);
        expect(document.getElementById('sendButton').textContent).toBe('Queue Prompt');

        promptInput.value = 'Second draft';
        await sendMessage();

        const queuedEditor = document.querySelector('.chat-task-queue-edit');
        expect(queuedEditor).toBeTruthy();
        queuedEditor.value = 'Second edited';
        queuedEditor.dispatchEvent(new Event('input', { bubbles: true }));

        expect(resolveFirstGenerate).toBeTruthy();
        resolveFirstGenerate();

        await firstRequestPromise;
        await flushMicrotasks();
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[1].prompt).toBe('Second edited');
        expect(document.querySelector('.chat-task-queue-item')).toBeNull();
    }, 15000);

    test('deleting a queued prompt prevents queued execution', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const generateBodies = [];
        let resolveFirstGenerate = null;

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                const parsed = JSON.parse(options.body || '{}');
                generateBodies.push(parsed);

                if (generateBodies.length === 1) {
                    return new Promise((resolve) => {
                        resolveFirstGenerate = () => resolve({
                            ok: true,
                            json: async () => ({
                                response: 'First response',
                                llm_debug: { model: 'gpt-5.2' }
                            })
                        });
                    });
                }

                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Second response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'First request';
        const firstRequestPromise = sendMessage();
        await flushMicrotasks();

        promptInput.value = 'Second queued';
        await sendMessage();

        const deleteButton = document.querySelector('.chat-task-queue-delete');
        expect(deleteButton).toBeTruthy();
        deleteButton.click();

        expect(resolveFirstGenerate).toBeTruthy();
        resolveFirstGenerate();

        await firstRequestPromise;
        await flushMicrotasks();
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(1);
        expect(document.querySelector('.chat-task-queue-item')).toBeNull();
        expect(document.getElementById('chatTaskQueuePanel')?.classList.contains('hidden')).toBe(true);
    }, 15000);

    test('restores interrupted persisted prompts and restarts them explicitly', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const generateBodies = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(body.text || '') })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-1',
                            prompt_raw: 'Recovered task',
                            status: 'in_progress',
                            session_id: 'session-1',
                            session_name: 'Recovered'
                        }]
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-1/requeue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-1',
                            prompt_raw: 'Recovered task',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Recovered'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-1/claim') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-1',
                            prompt_raw: 'Recovered task',
                            status: 'in_progress',
                            session_id: 'session-1',
                            session_name: 'Recovered'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-1/finish') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: { queue_id: 'queue-1', status: 'completed' }
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                const parsed = JSON.parse(options.body || '{}');
                generateBodies.push(parsed);
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Recovered response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();

        expect(document.querySelector('.chat-task-queue-item-label')?.textContent).toBe('Interrupted • Recovered');
        const restartButton = document.querySelector('.chat-task-queue-restart');
        expect(restartButton).toBeTruthy();

        restartButton.click();
        await new Promise((resolve) => setTimeout(resolve, 20));
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].prompt).toBe('Recovered task');
        expect(document.querySelector('.chat-task-queue-item')).toBeNull();
    }, 15000);
});
