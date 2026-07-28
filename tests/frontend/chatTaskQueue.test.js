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

    test('sends the focused queued prompt immediately without duplicating it', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            __testOnly_setLiveChatRequestForSession,
            sendMessage,
        } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        __testOnly_setLiveChatRequestForSession('busy-session', {
            promptQueueRecordId: 'busy-record',
            promptRaw: 'Busy'
        });

        const fetchCalls = [];
        const generateBodies = [];
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });

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
                if ((options.method || 'GET') === 'POST') {
                    throw new Error('selected queued prompt should not create a duplicate record');
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [
                            {
                                queue_id: 'queue-1',
                                prompt_raw: 'First queued',
                                status: 'queued',
                                session_id: 'session-1',
                                session_name: 'Current'
                            },
                            {
                                queue_id: 'queue-2',
                                prompt_raw: 'Second selected queued',
                                status: 'queued',
                                session_id: 'session-1',
                                session_name: 'Current'
                            }
                        ]
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-2') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-2',
                            prompt_raw: 'Second selected queued',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Current'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-2/claim') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-2',
                            prompt_raw: 'Second selected queued',
                            status: 'in_progress',
                            session_id: 'session-1',
                            session_name: 'Current'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-2/finish') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: { queue_id: 'queue-2', status: 'completed' }
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                const parsed = JSON.parse(options.body || '{}');
                generateBodies.push(parsed);
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Selected queued response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        __testOnly_setLiveChatRequestForSession('busy-session', null);

        const queuedEditors = document.querySelectorAll('.chat-task-queue-edit');
        expect(queuedEditors).toHaveLength(2);
        queuedEditors[1].focus();
        queuedEditors[1].dispatchEvent(new FocusEvent('focusin', { bubbles: true }));

        await sendMessage();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(generateBodies[0].prompt).toBe('Second selected queued');
        expect(generateBodies[0].conversation_session_id).toBe('session-1');
        expect(fetchCalls.some((call) => (
            String(call.url) === '/von/api/chat_prompt_queue'
            && (call.options.method || 'GET') === 'POST'
        ))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/queue-2/claim'))).toBe(true);
    }, 15000);

    test('switches to the queued prompt session before starting an off-session selected prompt', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            __testOnly_setSessionTabsCache,
            sendMessage,
        } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        __testOnly_setSessionTabsCache([
            { session_id: 'session-1', session_name: 'Current' },
            { session_id: 'session-2', session_name: 'Target' }
        ]);

        const fetchCalls = [];
        const generateBodies = [];
        let resolveGenerate = null;
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-off-session',
                            prompt_raw: 'Off-session selected queued',
                            status: 'queued',
                            session_id: 'session-2',
                            session_name: 'Target'
                        }]
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/session/set_chat_session') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        session_id: 'session-2',
                        session_name: 'Target'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history?')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        history: [],
                        segments_returned: 1,
                        total_segments: 1
                    })
                });
            }

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

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-off-session') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-off-session',
                            prompt_raw: 'Off-session selected queued',
                            status: 'queued',
                            session_id: 'session-2',
                            session_name: 'Target'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-off-session/claim') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-off-session',
                            prompt_raw: 'Off-session selected queued',
                            status: 'in_progress',
                            session_id: 'session-2',
                            session_name: 'Target'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-off-session/finish') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: { queue_id: 'queue-off-session', status: 'completed' }
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                const parsed = JSON.parse(options.body || '{}');
                generateBodies.push(parsed);
                return new Promise((resolve) => {
                    resolveGenerate = () => resolve({
                        ok: true,
                        json: async () => ({
                            response: 'Off-session response',
                            llm_debug: { model: 'gpt-5.2' }
                        })
                    });
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        const queuedEditor = document.querySelector('.chat-task-queue-edit');
        expect(queuedEditor).toBeTruthy();
        queuedEditor.focus();
        queuedEditor.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));

        await sendMessage();
        await flushMicrotasks();
        await flushMicrotasks();

        const setSessionIndex = fetchCalls.findIndex((call) => (
            String(call.url) === '/von/api/session/set_chat_session'
        ));
        const claimIndex = fetchCalls.findIndex((call) => (
            String(call.url) === '/von/api/chat_prompt_queue/queue-off-session/claim'
        ));
        const generateIndex = fetchCalls.findIndex((call) => String(call.url).startsWith('/von/generate'));
        expect(setSessionIndex).toBeGreaterThanOrEqual(0);
        expect(claimIndex).toBeGreaterThan(setSessionIndex);
        expect(generateIndex).toBeGreaterThan(setSessionIndex);
        expect(generateBodies[0].conversation_session_id).toBe('session-2');
        expect(document.getElementById('scrollableField')?.textContent).toContain('Off-session selected queued');
        expect(document.getElementById('thinkingCardWrapper')?.getAttribute('aria-hidden')).toBe('false');

        expect(resolveGenerate).toBeTruthy();
        resolveGenerate();
        await flushMicrotasks();
        await flushMicrotasks();
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

    test('keeps off-session running queue records visible while current-session prompts wait', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            __testOnly_setLiveChatRequestForSession,
        } = require(chatTabModulePath);

        __testOnly_setLiveChatRequestForSession('session-2', {
            promptQueueRecordId: 'queue-running',
            promptRaw: 'Background task'
        });

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [
                            {
                                queue_id: 'queue-running',
                                prompt_raw: 'Background task',
                                status: 'in_progress',
                                session_id: 'session-2',
                                session_name: 'Background'
                            },
                            {
                                queue_id: 'queue-waiting',
                                prompt_raw: 'Current task',
                                status: 'queued',
                                session_id: 'session-1',
                                session_name: 'Current'
                            }
                        ]
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();

        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 running, 1 queued');
        const labels = Array.from(document.querySelectorAll('.chat-task-queue-item-label'))
            .map((node) => node.textContent);
        expect(labels).toEqual(['Running • Background', 'Next up • Current']);
        const runningItem = document.querySelector('.chat-task-queue-item-running');
        expect(runningItem?.querySelector('.chat-task-queue-delete')).toBeNull();
        expect(runningItem?.querySelector('.chat-task-queue-dismiss')).toBeNull();
        expect(runningItem?.querySelector('.chat-task-queue-restart')).toBeNull();
    }, 15000);

    test('shows recent failed persisted prompts without rerunning them', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        const fetchCalls = [];
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });
            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [],
                        recent_failed_items: [{
                            queue_id: 'queue-failed',
                            prompt_raw: 'List the most recent 10 gmail messages together with any labels',
                            status: 'failed',
                            session_id: 'session-1',
                            session_name: 'Current',
                            completed_at: '2026-06-22T13:19:21.486Z',
                            last_error: 'The final response did not return from Von.'
                        }]
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        await new Promise((resolve) => setTimeout(resolve, 20));
        await flushMicrotasks();

        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 failed');
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent).toBe('Failed • Current');
        expect(document.querySelector('.chat-task-queue-edit')?.readOnly).toBe(true);
        expect(document.querySelector('.chat-task-queue-failure-message')?.textContent).toBe(
            'The final response did not return from Von.'
        );
        expect(document.querySelector('.chat-task-queue-delete')).toBeNull();
        expect(document.querySelector('.chat-task-queue-dismiss')?.textContent).toBe('Dismiss');
        expect(document.querySelector('.chat-task-queue-restart')).toBeNull();
        expect(fetchCalls.some((call) => String(call.url).startsWith('/von/generate'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/claim'))).toBe(false);
    }, 15000);

    test('keeps a failed row visible until dismissal succeeds without executing it', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        const fetchCalls = [];
        let resolveDismiss = null;
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });
            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [],
                        recent_failed_items: [{
                            queue_id: 'queue-failed',
                            prompt_raw: 'Expired task',
                            status: 'failed',
                            session_id: 'session-1',
                            session_name: 'Current',
                            last_error: 'Prompt queue record expired after being in progress for more than 24 hours.'
                        }]
                    })
                });
            }
            if (
                typeof url === 'string'
                && url === '/von/api/chat_prompt_queue/queue-failed'
                && options.method === 'DELETE'
            ) {
                return new Promise((resolve) => {
                    resolveDismiss = () => resolve({
                        ok: true,
                        json: async () => ({
                            success: true,
                            item: {
                                queue_id: 'queue-failed',
                                status: 'cancelled',
                                last_error: 'Prompt queue record expired after being in progress for more than 24 hours.'
                            }
                        })
                    });
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        const dismissButton = document.querySelector('.chat-task-queue-dismiss');
        expect(dismissButton).toBeTruthy();

        dismissButton.click();
        await flushMicrotasks();

        expect(resolveDismiss).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-item-failed')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-dismiss')?.disabled).toBe(true);

        resolveDismiss();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('.chat-task-queue-item-failed')).toBeNull();
        expect(fetchCalls.filter((call) => call.options.method === 'DELETE')).toHaveLength(1);
        expect(fetchCalls.some((call) => String(call.url).startsWith('/von/generate'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/claim'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/requeue'))).toBe(false);
        expect(fetchCalls.some((call) => call.options.method === 'PATCH')).toBe(false);
        expect(fetchCalls.some((call) => call.options.method === 'POST')).toBe(false);
    }, 15000);

    test('retains a failed row with a useful warning when dismissal fails', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [],
                        recent_failed_items: [{
                            queue_id: 'queue-failed',
                            prompt_raw: 'Expired task',
                            status: 'failed',
                            session_id: 'session-1',
                            session_name: 'Current',
                            last_error: 'Prompt queue record expired after being in progress for more than 24 hours.'
                        }]
                    })
                });
            }
            if (
                typeof url === 'string'
                && url === '/von/api/chat_prompt_queue/queue-failed'
                && options.method === 'DELETE'
            ) {
                return Promise.resolve({
                    ok: false,
                    status: 503,
                    json: async () => ({
                        success: false,
                        error: 'chat prompt queue storage is unavailable',
                        error_code: 'backend_unavailable'
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        document.querySelector('.chat-task-queue-dismiss').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('.chat-task-queue-item-failed')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-failure-message')?.textContent).toContain(
            'expired after being in progress'
        );
        expect(document.querySelector('.chat-task-queue-sync-warning')?.textContent).toBe(
            'Queue storage is temporarily unavailable.'
        );
        expect(document.querySelector('.chat-task-queue-dismiss')?.disabled).toBe(false);
    }, 15000);

    test('resubmits the focused failed persisted prompt through the normal send path', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            sendMessage,
        } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const fetchCalls = [];
        const generateBodies = [];
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });

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
                if ((options.method || 'GET') === 'POST') {
                    const parsed = JSON.parse(options.body || '{}');
                    return Promise.resolve({
                        ok: true,
                        json: async () => ({
                            success: true,
                            item: {
                                queue_id: 'queue-active',
                                prompt_raw: parsed.prompt_raw,
                                status: parsed.status || 'in_progress',
                                session_id: parsed.session_id,
                                session_name: parsed.session_name
                            }
                        })
                    });
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [],
                        recent_failed_items: [{
                            queue_id: 'queue-failed',
                            prompt_raw: 'List the most recent 10 gmail messages together with any labels',
                            status: 'failed',
                            session_id: 'session-1',
                            session_name: 'Current',
                            completed_at: '2026-06-22T13:19:21.486Z',
                            last_error: 'The final response did not return from Von.'
                        }]
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-active/finish') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: { queue_id: 'queue-active', status: 'completed' }
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                const parsed = JSON.parse(options.body || '{}');
                generateBodies.push(parsed);
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Selected failed prompt response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();

        const failedEditor = document.querySelector('.chat-task-queue-edit');
        expect(failedEditor).toBeTruthy();
        failedEditor.focus();
        failedEditor.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
        expect(document.querySelector('.chat-task-queue-item-selected')).toBeTruthy();

        await sendMessage();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].prompt).toBe('List the most recent 10 gmail messages together with any labels');
        expect(generateBodies[0].conversation_session_id).toBe('session-1');
        expect(fetchCalls.some((call) => String(call.url).includes('/queue-failed/claim'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/queue-failed/finish'))).toBe(false);
        expect(document.querySelector('.chat-task-queue-item-failed')).toBeNull();
    }, 15000);

    test('shows precise persisted claim failure reason', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-1',
                            prompt_raw: 'Queued task',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Current'
                        }]
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-1') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-1',
                            prompt_raw: 'Queued task',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Current'
                        }
                    })
                });
            }

            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue/queue-1/claim') {
                return Promise.resolve({
                    ok: false,
                    status: 404,
                    json: async () => ({
                        success: false,
                        error: 'prompt queue record belongs to a different scope',
                        error_code: 'scope_mismatch',
                        details: { current_status: 'queued', scope_match: false }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('.chat-task-queue-sync-warning')?.textContent).toBe(
            'This queued task belongs to a different browser or organisation scope.'
        );
    }, 15000);
});
