/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'mock-window-session-id'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
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

    test('sends the stored Gemini choice as a bare model with its exact provider', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        localStorage.setItem('von:localModelPreference', JSON.stringify({
            schemaVersion: 'localModelPreference.v1',
            activeSource: 'gemini',
            premiumProvider: 'gemini',
            openaiModel: null,
            geminiModel: 'gemini-3.7-flash',
            geminiModelParameters: { reasoning_effort: 'high' },
            ollamaSelection: null
        }));
        const originalScrollTo = window.scrollTo;
        window.scrollTo = jest.fn();

        let generateBody = null;
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
                generateBody = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        response: 'Gemini response',
                        llm_debug: { provider: 'gemini', model: 'gemini-3.7-flash' }
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        try {
            document.getElementById('promptInput').value = 'Use Gemini';
            await sendMessage();
            expect(generateBody).toMatchObject({
                prompt: 'Use Gemini',
                model: 'gemini-3.7-flash',
                model_provider: 'gemini',
                model_parameters: { reasoning_effort: 'high' }
            });
            expect(generateBody.model).not.toContain('gemini:');
        } finally {
            localStorage.removeItem('von:localModelPreference');
            window.scrollTo = originalScrollTo;
        }
    });

    test('keeps same-session prompts FIFO while the first turn is active', async () => {
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

    test.each([
        [409, 'conversation_turn_active', false],
        [429, 'turn_capacity_reached', false],
        [409, 'window_context_unavailable', true],
    ])('retries retryable HTTP %i %s admission without client-owned transitions', async (status, errorCode, expectsWindowRepair) => {
        const { getUserContext, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const lifecycleEvents = [];
        postJson.mockReset();
        postJson.mockImplementation(async (url, body) => {
            lifecycleEvents.push(`repair:${url}`);
            return { success: true, body };
        });

        const fetchCalls = [];
        const generateBodies = [];
        let queueRecord = null;
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });

            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ html: String(body.text || '') })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueRecord = {
                    queue_id: 'queue-retry-contract',
                    prompt_raw: body.prompt_raw,
                    session_id: body.session_id,
                    session_name: body.session_name,
                    client_request_id: body.client_request_id,
                    attempt_id: body.attempt_id,
                    status: 'queued'
                };
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({ success: true, item: queueRecord })
                });
            }

            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, items: queueRecord ? [queueRecord] : [] })
                });
            }

            if (url === '/von/api/chat_prompt_queue/queue-retry-contract' && options.method === 'PATCH') {
                const body = JSON.parse(options.body || '{}');
                queueRecord = { ...queueRecord, ...body, status: 'queued' };
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, item: queueRecord })
                });
            }

            if (url === '/von/generate') {
                const body = JSON.parse(options.body || '{}');
                generateBodies.push(body);
                lifecycleEvents.push(`generate:${generateBodies.length}`);
                if (generateBodies.length === 1) {
                    return Promise.resolve({
                        ok: false,
                        status,
                        headers: { get: (name) => name === 'Retry-After' ? '0' : null },
                        json: async () => ({
                            error: errorCode,
                            detail: 'Retry this queued turn.',
                            retryable: true,
                            ...(errorCode === 'window_context_unavailable' ? {} : {
                                prompt_queue_id: 'queue-retry-contract'
                            }),
                            retry_after_seconds: 0
                        })
                    });
                }
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        response: 'Retried response',
                        llm_debug: { model: 'gpt-5.2' }
                    })
                });
            }

            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        document.getElementById('promptInput').value = 'Retry this exact turn';
        await expect(sendMessage()).resolves.toBeUndefined();

        for (let attempt = 0; attempt < 80 && generateBodies.length < 2; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 10));
        }
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(2);
        expect(generateBodies[1]).toMatchObject({
            prompt: 'Retry this exact turn',
            prompt_queue_id: 'queue-retry-contract',
            client_request_id: generateBodies[0].client_request_id,
            attempt_id: generateBodies[0].attempt_id
        });
        expect(fetchCalls.filter((call) => (
            call.url === '/von/api/chat_prompt_queue'
            && (call.options.method || 'GET') === 'POST'
        ))).toHaveLength(1);
        expect(fetchCalls.some((call) => /\/(claim|requeue|finish)$/.test(String(call.url)))).toBe(false);
        if (expectsWindowRepair) {
            expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
                organisation_concept_id: 'org'
            });
            expect(lifecycleEvents.indexOf('repair:/von/api/session/set_organisation'))
                .toBeLessThan(lifecycleEvents.indexOf('generate:2'));
        } else {
            expect(postJson).not.toHaveBeenCalled();
        }
    }, 15000);

    test('repairs an expired window context before retrying the initial queue write', async () => {
        const { getUserContext, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org-a',
            language: 'en-NZ'
        });
        postJson.mockReset();
        postJson.mockResolvedValue({ success: true });

        const events = [];
        let queuePostCount = 0;
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                queuePostCount += 1;
                events.push(`queue-post-${queuePostCount}`);
                if (queuePostCount === 1) {
                    return Promise.resolve({
                        ok: false,
                        status: 409,
                        json: async () => ({
                            success: false,
                            error_code: 'window_context_unavailable'
                        })
                    });
                }
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-after-window-repair',
                            prompt_raw: body.prompt_raw,
                            session_id: body.session_id,
                            client_request_id: body.client_request_id,
                            attempt_id: body.attempt_id,
                            status: 'queued'
                        }
                    })
                });
            }
            if (url === '/von/generate') {
                events.push('generate');
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ response: 'Repaired response' })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({ ok: true, json: async () => ({ html: 'Repaired response' }) });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({ ok: true, json: async () => ({ history_length: 0 }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });
        postJson.mockImplementation(async () => {
            events.push('repair');
            return { success: true };
        });

        document.getElementById('promptInput').value = 'Repair before persistence';
        await sendMessage();

        expect(queuePostCount).toBe(2);
        expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: 'org-a'
        });
        expect(events).toEqual(['queue-post-1', 'repair', 'queue-post-2', 'generate']);
    }, 15000);

    test('preserves a canonical current-server owner after deferred admission without retrying locally', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ'
        });

        let queueRecord = null;
        const generateBodies = [];
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueRecord = {
                    queue_id: 'queue-owned-by-current-server',
                    prompt_raw: body.prompt_raw,
                    session_id: body.session_id,
                    session_name: body.session_name,
                    client_request_id: body.client_request_id,
                    attempt_id: body.attempt_id,
                    status: 'queued'
                };
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({ success: true, item: queueRecord })
                });
            }
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                queueRecord = {
                    ...queueRecord,
                    status: 'in_progress',
                    server_instance_id: 'server-current'
                };
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        turn_admission: { server_instance_id: 'server-current' },
                        items: [queueRecord]
                    })
                });
            }
            if (url === '/von/generate') {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: false,
                    status: 409,
                    headers: { get: () => '0' },
                    json: async () => ({
                        error: 'conversation_turn_active',
                        retryable: true,
                        prompt_queue_id: 'queue-owned-by-current-server',
                        retry_after_seconds: 0
                    })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({ ok: true, json: async () => ({ history_length: 0 }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        document.getElementById('promptInput').value = 'Already owned elsewhere';
        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 400));

        expect(generateBodies).toHaveLength(1);
        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 running');
        expect(document.querySelector('.chat-task-queue-item-running')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-restart')).toBeNull();
        expect(document.querySelector('.chat-task-queue-delete')).toBeNull();
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

    test('does not let a focused later prompt bypass the same-session FIFO head', async () => {
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

        expect(generateBodies[0].prompt).toBe('First queued');
        expect(generateBodies[0].conversation_session_id).toBe('session-1');
        expect(fetchCalls.some((call) => (
            String(call.url) === '/von/api/chat_prompt_queue'
            && (call.options.method || 'GET') === 'POST'
        ))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).endsWith('/claim'))).toBe(false);
        expect(generateBodies[0].prompt_queue_id).toBe('queue-1');
    }, 15000);

    test('starts an off-session selected prompt without switching the visible conversation', async () => {
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
        const generateIndex = fetchCalls.findIndex((call) => String(call.url).startsWith('/von/generate'));
        expect(setSessionIndex).toBe(-1);
        expect(fetchCalls.some((call) => String(call.url).endsWith('/claim'))).toBe(false);
        expect(generateIndex).toBeGreaterThanOrEqual(0);
        expect(generateBodies[0].conversation_session_id).toBe('session-2');
        expect(generateBodies[0].prompt_queue_id).toBe('queue-off-session');
        expect(document.getElementById('scrollableField')?.textContent)
            .not.toContain('Off-session selected queued');
        expect(document.getElementById('thinkingCardWrapper')?.getAttribute('aria-hidden')).toBe('true');

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

    test('treats a current-server turn from another tab as running until canonical refresh removes it', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        let includeRunningRecord = true;
        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        turn_admission: { server_instance_id: 'server-current' },
                        items: includeRunningRecord ? [{
                            queue_id: 'queue-other-tab',
                            prompt_raw: 'Running in another tab',
                            status: 'in_progress',
                            session_id: 'session-1',
                            session_name: 'Current',
                            server_instance_id: 'server-current',
                            lease_acquired_at: '2026-08-27T12:00:00Z'
                        }] : []
                    })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();

        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 running');
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent).toBe('Running • Current');
        expect(document.querySelector('.chat-task-queue-item-running')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-restart')).toBeNull();
        expect(document.querySelector('.chat-task-queue-delete')).toBeNull();
        expect(document.getElementById('sendButton').textContent).toBe('Queue Prompt');

        includeRunningRecord = false;
        await __testOnly_refreshChatPromptQueueFromServer();

        expect(document.querySelector('.chat-task-queue-item')).toBeNull();
        expect(document.getElementById('sendButton').textContent).toBe('Send Prompt');
    }, 15000);

    test('a cross-tab queue signal refreshes canonical state and unblocks the next same-session turn', async () => {
        const {
            __testOnly_handleChatPromptQueueStorageEvent,
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        let ownerStillRunning = true;
        const generateBodies = [];
        const queuedRecord = {
            queue_id: 'queue-next-after-other-tab',
            prompt_raw: 'Run after the other tab finishes',
            status: 'queued',
            session_id: 'session-1',
            session_name: 'Current',
            client_request_id: 'request-next',
            attempt_id: 'attempt-next'
        };
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        turn_admission: { server_instance_id: 'server-current' },
                        items: [
                            ...(ownerStillRunning ? [{
                                queue_id: 'queue-owner-other-tab',
                                prompt_raw: 'Owned in another tab',
                                status: 'in_progress',
                                session_id: 'session-1',
                                session_name: 'Current',
                                server_instance_id: 'server-current'
                            }] : []),
                            queuedRecord
                        ]
                    })
                });
            }
            if (
                url === '/von/api/chat_prompt_queue/queue-next-after-other-tab'
                && options.method === 'PATCH'
            ) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, item: queuedRecord })
                });
            }
            if (url === '/von/generate') {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ response: 'Next turn complete' })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({ ok: true, json: async () => ({ html: 'Next turn complete' }) });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({ ok: true, json: async () => ({ history_length: 0 }) });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        await flushMicrotasks();
        expect(generateBodies).toHaveLength(0);
        expect(document.getElementById('chatTaskQueueCount')?.textContent)
            .toBe('1 running, 1 queued');

        ownerStillRunning = false;
        __testOnly_handleChatPromptQueueStorageEvent({
            key: 'von:chatPromptQueueChanged:v1'
        });
        for (let attempt = 0; attempt < 30 && generateBodies.length === 0; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 10));
        }

        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0]).toMatchObject({
            prompt: 'Run after the other tab finishes',
            prompt_queue_id: 'queue-next-after-other-tab',
            client_request_id: 'request-next',
            attempt_id: 'attempt-next',
            conversation_session_id: 'session-1'
        });
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/claim'))).toBe(false);
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

    test('shows a precise persisted update failure reason before server admission', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn((url, _options = {}) => {
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
