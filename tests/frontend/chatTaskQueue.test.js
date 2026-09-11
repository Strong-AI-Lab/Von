/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/voiceConversation.js', () => ({
    createVoiceConversation: jest.fn(() => ({ end: jest.fn(), dispose: jest.fn(), isActive: () => false, reply: jest.fn() }))
}));

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
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
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockReset();
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

    test('voice utterances use canonical server enqueue with frozen speech correlation and a stable submission ID', async () => {
        const chat = require(chatTabModulePath);
        document.body.insertAdjacentHTML('beforeend', '<button id="resetButton"></button><button id="voiceConversationButton"></button><p id="voiceConversationStatus"></p>');
        const calls = [];
        global.fetch = jest.fn(async (url, options = {}) => {
            calls.push({ url, options });
            const payload = JSON.parse(options.body || '{}');
            const item = { ...payload, queue_id: 'voice-queue', status: 'queued' };
            return { ok: true, status: 200, json: async () => ({ success: true, item, items: [], history: [] }) };
        });
        require('../../src/frontend/web/von_interface/static/js/apiService.js').fetchWithTimeout
            .mockImplementation((...args) => global.fetch(...args));
        chat.initializeChatTab();
        const { createVoiceConversation } = require('../../src/frontend/web/von_interface/static/js/voiceConversation.js');
        const options = createVoiceConversation.mock.calls.at(-1)[0];
        await options.onSubmit('Discuss Vontology', { attemptId: 'speech-attempt', itemId: 'item-one', submissionId: 'speech-attempt-item-one' });
        const enqueue = calls.find(c => c.url === '/von/api/chat_prompt_queue' && c.options.method === 'POST');
        const body = JSON.parse(enqueue.options.body);
        expect(body).toMatchObject({ prompt_raw: 'Discuss Vontology', dispatch_mode: 'server', enqueue_submission_id: 'speech-attempt-item-one',
            execution_envelope: { client_context: { speech_attempt_ids: ['speech-attempt'], speech_item_id: 'item-one' } } });
        expect(calls.some(c => String(c.url).startsWith('/von/generate'))).toBe(false);
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

    test('persists a same-session prompt while a turn is active and leaves dispatch to the backend', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const generateBodies = [];
        const queueBodies = [];
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

            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueBodies.push(body);
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({
                        success: true,
                        item: {
                            ...body,
                            queue_id: `queue-${queueBodies.length}`,
                            status: 'queued'
                        }
                    })
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
        expect(queuedEditor.readOnly).toBe(true);
        queuedEditor.value = 'Second edited';
        queuedEditor.dispatchEvent(new Event('input', { bubbles: true }));

        expect(resolveFirstGenerate).toBeTruthy();
        resolveFirstGenerate();

        await firstRequestPromise;
        await flushMicrotasks();
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(1);
        expect(queueBodies.map((body) => body.prompt_raw)).toEqual([
            'First request',
            'Second draft'
        ]);
        expect(queueBodies[0]).toMatchObject({
            client_request_id: expect.any(String),
            attempt_id: expect.any(String)
        });
        expect(queueBodies[0]).not.toHaveProperty('enqueue_submission_id');
        expect(queueBodies[0]).not.toHaveProperty('dispatch_mode');
        expect(queueBodies[1]).toMatchObject({
            enqueue_submission_id: expect.any(String),
            dispatch_mode: 'server',
            execution_envelope_version: 1,
            execution_envelope: {
                language: 'en-NZ',
                presenter_mode: true,
                skip_buttonify: false,
                thinking_card_mode: expect.any(String)
            }
        });
        expect(queueBodies[1]).not.toHaveProperty('client_request_id');
        expect(queueBodies[1]).not.toHaveProperty('attempt_id');
        expect(queuedEditor.value).toBe('Second draft');
        expect(document.querySelector('.chat-task-queue-item')).toBeTruthy();
        expect(global.fetch.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(false);
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/claim'))).toBe(false);
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/finish'))).toBe(false);
    }, 15000);

    test('guards one enqueue save per session while preserving a newly typed draft', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_setLiveChatRequestForSession,
            sendMessage,
        } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ'
        });
        __testOnly_setLiveChatRequestForSession('session-1', {
            promptQueueRecordId: 'queue-active',
            promptRaw: 'Active turn'
        });

        const queueBodies = [];
        let resolveFirstQueuePost = null;
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueBodies.push(body);
                const response = {
                    ok: true,
                    status: 201,
                    json: async () => ({
                        success: true,
                        item: {
                            ...body,
                            queue_id: `queue-${queueBodies.length}`,
                            status: 'queued'
                        }
                    })
                };
                if (queueBodies.length === 1) {
                    return new Promise((resolve) => {
                        resolveFirstQueuePost = () => resolve(response);
                    });
                }
                return Promise.resolve(response);
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        const promptInput = document.getElementById('promptInput');
        const sendButton = document.getElementById('sendButton');
        promptInput.value = 'First queued prompt';
        const firstQueuePromise = sendMessage();

        expect(queueBodies).toHaveLength(1);
        expect(promptInput.value).toBe('');
        expect(sendButton.disabled).toBe(true);
        expect(sendButton.textContent).toBe('Queueing…');

        promptInput.value = 'A new draft typed during persistence';
        await sendMessage();
        expect(queueBodies).toHaveLength(1);
        expect(promptInput.value).toBe('A new draft typed during persistence');

        expect(resolveFirstQueuePost).toBeTruthy();
        resolveFirstQueuePost();
        await firstQueuePromise;

        expect(promptInput.value).toBe('A new draft typed during persistence');
        expect(queueBodies[0].enqueue_submission_id).toEqual(expect.any(String));

        await sendMessage();
        expect(queueBodies).toHaveLength(2);
        expect(queueBodies[1].prompt_raw).toBe('A new draft typed during persistence');
        expect(queueBodies[1].enqueue_submission_id).toEqual(expect.any(String));
        expect(queueBodies[1].enqueue_submission_id)
            .not.toBe(queueBodies[0].enqueue_submission_id);
    }, 15000);

    test('marks an ambiguous save as unconfirmed and retries the same immutable submission', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_setLiveChatRequestForSession,
            sendMessage,
        } = require(chatTabModulePath);
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ'
        });
        __testOnly_setLiveChatRequestForSession('session-1', {
            promptQueueRecordId: 'queue-active',
            promptRaw: 'Active turn'
        });

        const queueBodies = [];
        let resolveFailedPost = null;
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueBodies.push(body);
                if (queueBodies.length === 1) {
                    return new Promise((resolve) => {
                        resolveFailedPost = () => resolve({
                            ok: false,
                            status: 503,
                            json: async () => ({
                                success: false,
                                error_code: 'backend_unavailable'
                            })
                        });
                    });
                }
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({
                        success: true,
                        item: {
                            ...body,
                            queue_id: 'queue-after-retry',
                            status: 'queued'
                        }
                    })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'Prompt whose queue save fails';
        const queuePromise = sendMessage();
        promptInput.value = 'New draft preserved during failure';
        expect(resolveFailedPost).toBeTruthy();
        resolveFailedPost();
        await queuePromise;

        expect(promptInput.value).toBe('New draft preserved during failure');
        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 queue unconfirmed');
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent)
            .toBe('Queue unconfirmed • Current');
        expect(document.querySelector('.chat-task-queue-sync-warning')?.textContent)
            .toBe('Queue storage is temporarily unavailable.');
        expect(document.querySelector('.chat-task-queue-edit')?.readOnly).toBe(true);

        document.querySelector('.chat-task-queue-enqueue-retry').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(queueBodies).toHaveLength(2);
        expect(queueBodies[1]).toEqual(queueBodies[0]);
        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 queued');
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent)
            .toBe('Next up • Current');
        expect(global.fetch.mock.calls.some(([url]) => String(url).startsWith('/von/generate'))).toBe(false);
        expect(global.fetch.mock.calls.some(([, options]) => options?.method === 'PATCH')).toBe(false);
    }, 15000);

    test.each([
        [409, 'conversation_turn_active', false],
        [429, 'turn_capacity_reached', false],
        [409, 'window_context_unavailable', true],
    ])('leaves retryable HTTP %i %s admission queued for the server dispatcher', async (status, errorCode, expectsWindowRepair) => {
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
        const queueBodies = [];
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
                queueBodies.push(body);
                const queueRecord = {
                    ...body,
                    queue_id: body.dispatch_mode === 'server'
                        ? 'queue-server-retry'
                        : 'queue-direct-retry',
                    status: 'queued'
                };
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({ success: true, item: queueRecord })
                });
            }

            if (url === '/von/generate') {
                const body = JSON.parse(options.body || '{}');
                generateBodies.push(body);
                lifecycleEvents.push(`generate:${generateBodies.length}`);
                return Promise.resolve({
                    ok: false,
                    status,
                    headers: { get: (name) => name === 'Retry-After' ? '0' : null },
                    json: async () => ({
                        error: errorCode,
                        detail: 'Retry this queued turn.',
                        retryable: true,
                        ...(errorCode === 'window_context_unavailable' ? {} : {
                            prompt_queue_id: 'queue-direct-retry'
                        }),
                        retry_after_seconds: 0
                    })
                });
            }

            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        document.getElementById('promptInput').value = 'Retry this exact turn';
        await expect(sendMessage()).resolves.toBeUndefined();

        await flushMicrotasks();

        expect(generateBodies).toHaveLength(1);
        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 queued');
        expect(fetchCalls.filter((call) => (
            call.url === '/von/api/chat_prompt_queue'
            && (call.options.method || 'GET') === 'POST'
        ))).toHaveLength(2);
        expect(queueBodies[0]).toMatchObject({
            client_request_id: generateBodies[0].client_request_id,
            attempt_id: generateBodies[0].attempt_id
        });
        expect(queueBodies[0]).not.toHaveProperty('dispatch_mode');
        expect(queueBodies[0]).not.toHaveProperty('enqueue_submission_id');
        expect(queueBodies[1]).toMatchObject({
            dispatch_mode: 'server',
            enqueue_submission_id: expect.any(String),
            execution_envelope_version: 1,
            execution_envelope: expect.objectContaining({
                language: 'en-NZ',
                presenter_mode: true,
                skip_buttonify: false,
                thinking_card_mode: expect.any(String)
            })
        });
        expect(queueBodies[1]).not.toHaveProperty('client_request_id');
        expect(queueBodies[1]).not.toHaveProperty('attempt_id');
        expect(fetchCalls.some((call) => (
            call.url === '/von/api/chat_prompt_queue/queue-direct-retry'
            && call.options.method === 'DELETE'
        ))).toBe(true);
        expect(fetchCalls.some((call) => /\/(claim|requeue|finish)$/.test(String(call.url)))).toBe(false);
        if (expectsWindowRepair) {
            expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
                organisation_concept_id: 'org'
            });
        } else {
            expect(postJson).not.toHaveBeenCalled();
        }
    }, 15000);

    test('reuses the immutable server enqueue identity after window-context repair', async () => {
        const { getUserContext, postJson } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_setLiveChatRequestForSession,
            sendMessage,
        } = require(chatTabModulePath);
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org-a',
            language: 'en-NZ'
        });
        postJson.mockReset();
        postJson.mockResolvedValue({ success: true });

        const events = [];
        const enqueueSubmissionIds = [];
        const queueBodies = [];
        let queuePostCount = 0;
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                const body = JSON.parse(options.body || '{}');
                queueBodies.push(body);
                queuePostCount += 1;
                enqueueSubmissionIds.push(body.enqueue_submission_id);
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
                return Promise.resolve({
                    ok: true,
                    status: 201,
                    json: async () => ({
                        success: true,
                        item: {
                            queue_id: 'queue-after-window-repair',
                            prompt_raw: body.prompt_raw,
                            session_id: body.session_id,
                            enqueue_submission_id: body.enqueue_submission_id,
                            dispatch_mode: body.dispatch_mode,
                            execution_envelope_version: body.execution_envelope_version,
                            status: 'queued'
                        }
                    })
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

        __testOnly_setLiveChatRequestForSession('session-1', {
            promptQueueRecordId: 'queue-active',
            promptRaw: 'Active turn'
        });
        document.getElementById('promptInput').value = 'Repair before persistence';
        await sendMessage();

        expect(queuePostCount).toBe(2);
        expect(postJson).toHaveBeenCalledWith('/von/api/session/set_organisation', {
            organisation_concept_id: 'org-a'
        });
        expect(enqueueSubmissionIds[0]).toEqual(expect.any(String));
        expect(enqueueSubmissionIds[1]).toBe(enqueueSubmissionIds[0]);
        expect(queueBodies[0]).toEqual(queueBodies[1]);
        expect(queueBodies[0]).toMatchObject({
            dispatch_mode: 'server',
            execution_envelope_version: 1,
            execution_envelope: expect.objectContaining({
                language: 'en-NZ',
                presenter_mode: true,
                thinking_card_mode: expect.any(String)
            })
        });
        expect(queueBodies[0]).not.toHaveProperty('client_request_id');
        expect(queueBodies[0]).not.toHaveProperty('attempt_id');
        expect(events).toEqual(['queue-post-1', 'repair', 'queue-post-2']);
    }, 15000);

    test('does not execute a selected server-dispatch row in the browser', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            sendMessage,
        } = require(chatTabModulePath);
        const originalAlert = window.alert;
        window.alert = jest.fn();
        const fetchCalls = [];
        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-server-owned',
                            prompt_raw: 'Already owned by the server dispatcher',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Current',
                            enqueue_submission_id: 'enqueue-server-owned',
                            dispatch_mode: 'server',
                            execution_envelope_version: 1
                        }]
                    })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
        });

        try {
            await __testOnly_refreshChatPromptQueueFromServer();
            const editor = document.querySelector('.chat-task-queue-edit');
            expect(editor).toBeTruthy();
            expect(editor.readOnly).toBe(true);
            editor.focus();
            editor.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));

            await sendMessage();

            expect(window.alert).toHaveBeenCalledWith('Please enter a prompt.');
            expect(fetchCalls.some(({ url }) => String(url).startsWith('/von/generate'))).toBe(false);
            expect(fetchCalls.some(({ url }) => String(url).endsWith('/claim'))).toBe(false);
            expect(fetchCalls.some(({ url }) => String(url).endsWith('/finish'))).toBe(false);
            expect(fetchCalls.some(({ options }) => options?.method === 'PATCH')).toBe(false);
        } finally {
            window.alert = originalAlert;
        }
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

    test('does not dispatch a focused later legacy row ahead of the same-session head', async () => {
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

        expect(generateBodies).toHaveLength(0);
        expect(document.querySelectorAll('.chat-task-queue-item')).toHaveLength(2);
        expect(fetchCalls.some((call) => (
            String(call.url) === '/von/api/chat_prompt_queue'
            && (call.options.method || 'GET') === 'POST'
        ))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).endsWith('/claim'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).endsWith('/finish'))).toBe(false);
    }, 15000);

    test('projects queue rows only into their conversation while retaining actor-wide tab activity', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            __testOnly_setSessionTabsCache,
        } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        document.body.insertAdjacentHTML('afterbegin', '<div id="chatSessionTabs"></div>');
        __testOnly_setSessionTabsCache([
            { session_id: 'session-1', session_name: 'Current' },
            { session_id: 'session-2', session_name: 'Target' }
        ]);

        const fetchCalls = [];
        const generateBodies = [];
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
                generateBodies.push(JSON.parse(options.body || '{}'));
            }

            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();

        const setSessionIndex = fetchCalls.findIndex((call) => (
            String(call.url) === '/von/api/session/set_chat_session'
        ));
        const generateIndex = fetchCalls.findIndex((call) => String(call.url).startsWith('/von/generate'));
        expect(setSessionIndex).toBe(-1);
        expect(fetchCalls.some((call) => String(call.url).endsWith('/claim'))).toBe(false);
        expect(generateIndex).toBe(-1);
        expect(generateBodies).toHaveLength(0);
        expect(document.querySelector('.chat-task-queue-edit')).toBeNull();
        expect(document.getElementById('chatTaskQueuePanel')?.classList.contains('hidden')).toBe(true);
        expect(document.querySelector(
            '#chatSessionTabs [data-session-id="session-2"] .chat-session-tab-activity'
        )?.textContent).toBe('Queued');
    }, 15000);

    test('requeues interrupted persisted prompts without browser-side dispatch', async () => {
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

        expect(JSON.parse(global.fetch.mock.calls.find(([url]) => String(url).endsWith('/requeue'))[1].body).execution_envelope).toBeTruthy();
        expect(generateBodies).toHaveLength(0);
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent)
            .toBe('Ready to resume • Recovered');
        expect(document.querySelector('.chat-task-queue-item')).toBeTruthy();
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/claim'))).toBe(false);
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/finish'))).toBe(false);
    }, 15000);

    test('keeps off-session queue activity on its tab, not in the active composer panel', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        document.body.insertAdjacentHTML('afterbegin', '<div id="chatSessionTabs"></div>');
        const { __testOnly_setSessionTabsCache } = require(chatTabModulePath);
        __testOnly_setSessionTabsCache([
            { session_id: 'session-1', session_name: 'Current' },
            { session_id: 'session-2', session_name: 'Background' }
        ]);
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
                                session_name: 'Background',
                                enqueue_submission_id: 'enqueue-running',
                                dispatch_mode: 'server',
                                execution_envelope_version: 1
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

        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 queued');
        const labels = Array.from(document.querySelectorAll('.chat-task-queue-item-label'))
            .map((node) => node.textContent);
        expect(labels).toEqual(['Ready to resume • Current']);
        expect(document.querySelector('.chat-task-queue-item-running')).toBeNull();
        expect(document.querySelector(
            '#chatSessionTabs [data-session-id="session-2"] .chat-session-tab-activity'
        )?.textContent).toBe('Running');
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

    test('polls server-owned queue work and refreshes active history after its row disappears', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);
        const fetchCalls = [];
        let queueGetCount = 0;

        jest.useFakeTimers();
        try {
            global.fetch = jest.fn((url, options = {}) => {
                fetchCalls.push({ url, options });
                if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                    queueGetCount += 1;
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({
                            success: true,
                            items: queueGetCount === 1 ? [{
                                queue_id: 'queue-polled',
                                prompt_raw: 'Server-dispatched prompt',
                                status: 'queued',
                                session_id: 'session-1',
                                session_name: 'Current',
                                enqueue_submission_id: 'enqueue-polled',
                                dispatch_mode: 'server',
                                execution_envelope_version: 1
                            }] : []
                        })
                    });
                }
                if (typeof url === 'string' && url.startsWith('/von/history?')) {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({
                            history: [],
                            segments_returned: 1,
                            total_segments: 1
                        })
                    });
                }
                return Promise.resolve({ ok: true, status: 200, json: async () => ({ success: true }) });
            });

            await __testOnly_refreshChatPromptQueueFromServer();
            expect(queueGetCount).toBe(1);
            expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 queued');

            await jest.advanceTimersByTimeAsync(2000);

            expect(queueGetCount).toBe(2);
            expect(document.querySelector('.chat-task-queue-item')).toBeNull();
            expect(fetchCalls.some(({ url }) => String(url).startsWith('/von/history?'))).toBe(true);
            expect(fetchCalls.some(({ url }) => String(url).startsWith('/von/generate'))).toBe(false);
            expect(fetchCalls.some(({ url }) => /\/(claim|finish)$/.test(String(url)))).toBe(false);

            await jest.advanceTimersByTimeAsync(4000);
            expect(queueGetCount).toBe(2);
        } finally {
            jest.useRealTimers();
        }
    }, 15000);

    test('a cross-tab queue signal refreshes canonical state without dispatching in the browser', async () => {
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
        await flushMicrotasks();
        await flushMicrotasks();

        expect(generateBodies).toHaveLength(0);
        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 queued');
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/claim'))).toBe(false);
        expect(global.fetch.mock.calls.some(([url]) => String(url).endsWith('/finish'))).toBe(false);
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
        expect(document.querySelector('.chat-task-queue-failed-retry')?.textContent).toBe('Retry');
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

    test('retries a failed row once through the linked server endpoint', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        const fetchCalls = [];
        let resolveRetry = null;
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
            if (
                typeof url === 'string'
                && url === '/von/api/chat_prompt_queue/queue-failed/retry'
                && options.method === 'POST'
            ) {
                return new Promise((resolve) => {
                    resolveRetry = () => resolve({
                        ok: true,
                        status: 201,
                        json: async () => ({
                            success: true,
                            item: {
                                queue_id: 'queue-retry',
                                retry_source_queue_id: 'queue-failed',
                                prompt_raw: 'List the most recent 10 gmail messages together with any labels',
                                status: 'queued',
                                dispatch_mode: 'server',
                                dispatch_ready: true,
                                session_id: 'session-1',
                                session_name: 'Current'
                            }
                        })
                    });
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({ success: true }) });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        const retryButton = document.querySelector('.chat-task-queue-failed-retry');
        expect(retryButton).toBeTruthy();

        retryButton.click();
        retryButton.click();
        await flushMicrotasks();

        expect(resolveRetry).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-item-failed')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-failed-retry')?.disabled).toBe(true);
        expect(fetchCalls.filter((call) => (
            call.url === '/von/api/chat_prompt_queue/queue-failed/retry'
        ))).toHaveLength(1);

        resolveRetry();
        await flushMicrotasks();
        await flushMicrotasks();

        const retryCall = fetchCalls.find((call) => (
            call.url === '/von/api/chat_prompt_queue/queue-failed/retry'
        ));
        expect(JSON.parse(retryCall.options.body)).toEqual({
            retry_request_id: expect.any(String)
        });
        expect(document.querySelector('.chat-task-queue-item-failed')).toBeNull();
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent)
            .toBe('Next up • Current');
        expect(fetchCalls.some((call) => String(call.url).startsWith('/von/generate'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/claim'))).toBe(false);
        expect(fetchCalls.some((call) => String(call.url).includes('/finish'))).toBe(false);
    }, 15000);

    test('adopts an in-progress linked retry into the Thinking card and delivers its result', async () => {
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const {
            __testOnly_getLiveChatRequestForSession,
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);
        const fetchCalls = [];

        global.fetch = jest.fn((url, options = {}) => {
                fetchCalls.push({ url, options });
                if (url === '/von/api/chat_prompt_queue') {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({
                            success: true,
                            items: [{
                                queue_id: 'queue-retry',
                                retry_source_queue_id: 'queue-failed',
                                prompt_raw: 'Retry me with visible progress',
                                status: 'in_progress',
                                dispatch_mode: 'server',
                                session_id: 'session-1',
                                session_name: 'Current',
                                enqueue_submission_id: 'enqueue-retry',
                                client_request_id: 'request-retry',
                                attempt_id: 'attempt-retry',
                                claimed_at: '2026-09-01T19:42:00Z'
                            }]
                        })
                    });
                }
                if (url === '/von/progress/request-retry') {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({
                            request_id: 'request-retry',
                            status: 'working',
                            phase: 'adaptive_research',
                            phase_label: 'Researching',
                            result_summary: 'Retry is running on the server.'
                        })
                    });
                }
                if (url === '/von/api/task/status/request-retry') {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ status: 'completed' })
                    });
                }
                if (url === '/von/api/task/result/request-retry') {
                    return Promise.resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({
                            status: 'completed',
                            result: {
                                response: 'The retried task completed visibly.',
                                llm_debug: { model: 'test-model' }
                            }
                        })
                    });
                }
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
                        json: async () => ({ history_length: 2, authenticated: true })
                    });
                }
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true })
                });
        });
        fetchWithTimeout.mockImplementation((url, options = {}) => global.fetch(url, options));

        await __testOnly_refreshChatPromptQueueFromServer();
        await Promise.resolve();
        await Promise.resolve();

        const observedRequest = __testOnly_getLiveChatRequestForSession('session-1');
        expect(observedRequest).toMatchObject({
            aborted: false,
            clientRequestId: 'request-retry',
            observingServerDispatch: true,
            sessionKey: 'session-1'
        });
        expect(observedRequest?.foregroundDeliveryCompleted).not.toBe(true);
        expect(observedRequest?.foregroundTaskResultPoll).toBeTruthy();

        expect(document.getElementById('thinkingCardWrapper')?.getAttribute('aria-hidden'))
            .toBe('false');
        expect(document.getElementById('abortButton')?.getAttribute('aria-hidden'))
            .toBe('false');
        expect(document.querySelector('.chat-task-queue-item')).toBeNull();
        expect(document.getElementById('scrollableField')?.textContent)
            .not.toContain('Retry me with visible progress');
        expect(fetchCalls.some(({ url }) => String(url).startsWith('/von/generate')))
            .toBe(false);

        await new Promise((resolve) => setTimeout(resolve, 350));
        await Promise.resolve();
        await Promise.resolve();

        expect(observedRequest?.aborted).toBe(false);
        expect(fetchCalls.map(({ url }) => url))
            .toContain('/von/api/task/status/request-retry');
        expect(fetchCalls.filter(({ url }) => url === '/von/api/task/result/request-retry'))
            .toHaveLength(1);
        expect(document.querySelector('.chat-message-text')?.dataset.originalText)
            .toBe('The retried task completed visibly.');
        expect(fetchCalls.some(({ url }) => String(url).startsWith('/von/generate')))
            .toBe(false);
    }, 15000);

    test('Stop cancels the exact server-owned retry without submitting another generation', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
            __testOnly_getLiveChatRequestForSession,
        } = require(chatTabModulePath);
        const fetchCalls = [];

        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });
            if (url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-retry-stop',
                            retry_source_queue_id: 'queue-failed-stop',
                            prompt_raw: 'Retry and then stop me',
                            status: 'in_progress',
                            dispatch_mode: 'server',
                            session_id: 'session-1',
                            session_name: 'Current',
                            enqueue_submission_id: 'enqueue-retry-stop',
                            client_request_id: 'request-retry-stop',
                            attempt_id: 'attempt-retry-stop'
                        }]
                    })
                });
            }
            if (url === '/von/progress/request-retry-stop') {
                return Promise.resolve({
                    ok: true,
                    status: 202,
                    json: async () => ({
                        request_id: 'request-retry-stop',
                        status: 'working',
                        result_summary: 'Still running.'
                    })
                });
            }
            if (
                url === '/von/api/task/cancel/request-retry-stop'
                && options.method === 'POST'
            ) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, status: 'cancelled' })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => ({ success: true })
            });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        await Promise.resolve();
        const observedRequest = __testOnly_getLiveChatRequestForSession('session-1');
        expect(document.getElementById('thinkingCardWrapper')?.getAttribute('aria-hidden'))
            .toBe('false');

        document.getElementById('abortButton').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(fetchCalls.filter(({ url }) => (
            url === '/von/api/task/cancel/request-retry-stop'
        ))).toHaveLength(1);
        expect(fetchCalls.some(({ url }) => String(url).startsWith('/von/generate')))
            .toBe(false);
        expect(document.getElementById('thinkingCardWrapper')?.getAttribute('aria-hidden'))
            .toBe('false');
        expect(observedRequest?.aborted).toBe(false);
        expect(observedRequest?.backgroundCancellationRequested).toBe(true);
        expect(observedRequest?.latestProgress).toMatchObject({
            status: 'cancelling',
            phase_label: 'Cancelling'
        });
        expect(document.getElementById('abortButton')?.disabled).toBe(true);
        expect(document.getElementById('abortButton')?.getAttribute('aria-label'))
            .toBe('Cancellation requested');
        expect(document.getElementById('promptInput')?.value).toBe('');
    }, 15000);

    test('retains failure evidence when linked retry is not accepted', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue') {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        success: true,
                        items: [],
                        recent_failed_items: [{
                            queue_id: 'queue-failed',
                            prompt_raw: 'Retry me safely',
                            status: 'failed',
                            session_id: 'session-1',
                            session_name: 'Current',
                            last_error: 'model_error'
                        }]
                    })
                });
            }
            if (
                url === '/von/api/chat_prompt_queue/queue-failed/retry'
                && options.method === 'POST'
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
        document.querySelector('.chat-task-queue-failed-retry').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('.chat-task-queue-item-failed')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-failure-message')?.textContent)
            .toBe('model_error');
        expect(document.querySelector('.chat-task-queue-sync-warning')?.textContent)
            .toBe('Queue storage is temporarily unavailable.');
        expect(document.querySelector('.chat-task-queue-failed-retry')?.disabled)
            .toBe(false);
    }, 15000);

    test('shows a precise persisted update failure for an editable legacy row', async () => {
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
        const editor = document.querySelector('.chat-task-queue-edit');
        editor.value = 'Edited queued task';
        editor.dispatchEvent(new Event('input', { bubbles: true }));
        await new Promise((resolve) => setTimeout(resolve, 350));
        await flushMicrotasks();

        expect(document.querySelector('.chat-task-queue-sync-warning')?.textContent).toBe(
            'This queued task belongs to a different browser or organisation scope.'
        );
    }, 15000);

    test('retains a canonical queued row and surfaces the error when DELETE fails', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-delete-retained',
                            prompt_raw: 'Keep me until cancellation is acknowledged',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Current'
                        }]
                    })
                });
            }
            if (url === '/von/api/chat_prompt_queue/queue-delete-retained') {
                return Promise.resolve({
                    ok: false,
                    status: 503,
                    json: async () => ({
                        success: false,
                        error: 'queue backend unavailable',
                        error_code: 'backend_unavailable'
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => ({ success: true })
            });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        const deleteButton = document.querySelector('.chat-task-queue-delete');
        expect(deleteButton).toBeTruthy();
        deleteButton.click();

        expect(document.querySelector('[data-queue-id="queue-delete-retained"]')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-delete').disabled).toBe(true);

        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('[data-queue-id="queue-delete-retained"]')).toBeTruthy();
        expect(document.querySelector('.chat-task-queue-delete').disabled).toBe(false);
        expect(document.querySelector('.chat-task-queue-sync-warning')?.textContent).toBe(
            'Queue storage is temporarily unavailable.'
        );
    }, 15000);

    test('treats a DELETE not-found response as canonical absence', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        items: [{
                            queue_id: 'queue-already-absent',
                            prompt_raw: 'Already cancelled elsewhere',
                            status: 'queued',
                            session_id: 'session-1',
                            session_name: 'Current'
                        }]
                    })
                });
            }
            if (url === '/von/api/chat_prompt_queue/queue-already-absent') {
                return Promise.resolve({
                    ok: false,
                    status: 404,
                    json: async () => ({
                        success: false,
                        error: 'cancellable prompt was not found',
                        error_code: 'not_found'
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => ({ success: true })
            });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        document.querySelector('.chat-task-queue-delete').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('[data-queue-id="queue-already-absent"]')).toBeNull();
        expect(document.getElementById('chatTaskQueuePanel')?.classList.contains('hidden')).toBe(true);
    }, 15000);

    test('renders one terminal row when active and failed projections overlap', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);

        global.fetch = jest.fn(() => Promise.resolve({
            ok: true,
            status: 200,
            json: async () => ({
                success: true,
                items: [{
                    queue_id: 'queue-transition-race',
                    prompt_raw: 'Transitioning row',
                    status: 'queued',
                    session_id: 'session-1',
                    session_name: 'Current'
                }],
                recent_failed_items: [{
                    queue_id: 'queue-transition-race',
                    prompt_raw: 'Transitioning row',
                    status: 'failed',
                    session_id: 'session-1',
                    session_name: 'Current',
                    last_error: 'Canonical terminal failure'
                }]
            })
        }));

        await __testOnly_refreshChatPromptQueueFromServer();

        expect(document.querySelectorAll('.chat-task-queue-item')).toHaveLength(1);
        expect(document.querySelector('.chat-task-queue-item-label')?.textContent)
            .toBe('Failed • Current');
        expect(document.getElementById('chatTaskQueueCount')?.textContent).toBe('1 failed');
    }, 15000);

    test('cancels an unresolved replacement before its legacy source', async () => {
        const {
            __testOnly_refreshChatPromptQueueFromServer,
        } = require(chatTabModulePath);
        const deleteOrder = [];
        let queueReads = 0;
        const replacement = {
            queue_id: 'queue-handoff-replacement',
            prompt_raw: 'Cancel this unresolved handoff',
            status: 'queued',
            session_id: 'session-1',
            session_name: 'Current',
            dispatch_mode: 'server',
            dispatch_ready: false,
            enqueue_submission_id: 'submission-handoff-cancel',
            handoff_source_queue_id: 'queue-handoff-source'
        };
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                queueReads += 1;
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        items: queueReads <= 2 ? [replacement] : [],
                        recent_failed_items: []
                    })
                });
            }
            if (options.method === 'DELETE') {
                deleteOrder.push(String(url));
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, item: { status: 'cancelled' } })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => ({ success: true })
            });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        document.querySelector('.chat-task-queue-handoff-cancel').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(deleteOrder).toEqual([
            '/von/api/chat_prompt_queue/queue-handoff-replacement',
            '/von/api/chat_prompt_queue/queue-handoff-source'
        ]);
        expect(document.querySelector('.chat-task-queue-item')).toBeNull();
    }, 15000);

    test('retains a manually sent legacy row until generate acknowledges it', async () => {
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

        let resolveGenerate = null;
        const generateBodies = [];
        const queued = {
            queue_id: 'queue-manual-legacy',
            prompt_raw: 'Manually dispatch this legacy prompt',
            status: 'queued',
            session_id: 'session-1',
            session_name: 'Current'
        };
        global.fetch = jest.fn((url, options = {}) => {
            if (url === '/von/api/chat_prompt_queue' && (options.method || 'GET') === 'GET') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, items: [queued] })
                });
            }
            if (url === '/von/api/chat_prompt_queue/queue-manual-legacy' && options.method === 'PATCH') {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, item: queued })
                });
            }
            if (url === '/von/generate') {
                generateBodies.push(JSON.parse(options.body || '{}'));
                return new Promise((resolve) => {
                    resolveGenerate = () => resolve({
                        ok: true,
                        status: 200,
                        json: async () => ({ response: 'Acknowledged response' })
                    });
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ html: '' })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => ({ success: true })
            });
        });

        await __testOnly_refreshChatPromptQueueFromServer();
        const editor = document.querySelector('.chat-task-queue-edit');
        editor.focus();
        editor.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
        await sendMessage();
        await flushMicrotasks();

        expect(resolveGenerate).toBeTruthy();
        expect(generateBodies).toHaveLength(1);
        expect(generateBodies[0].prompt_queue_id).toBe('queue-manual-legacy');
        expect(document.querySelector('[data-queue-id="queue-manual-legacy"]')).toBeTruthy();

        resolveGenerate();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(document.querySelector('[data-queue-id="queue-manual-legacy"]')).toBeNull();
    }, 15000);

    test('retains the exact foreground row until an admission-deferred server enqueue is acknowledged', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        const { sendMessage } = require(chatTabModulePath);

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const events = [];
        const serverQueueBodies = [];
        const generateBodies = [];
        let serverQueuePostCount = 0;
        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ html: '' })
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
                if (body.dispatch_mode !== 'server') {
                    events.push('direct-post');
                    return Promise.resolve({
                        ok: true,
                        status: 201,
                        json: async () => ({
                            success: true,
                            item: {
                                ...body,
                                queue_id: 'queue-direct-handoff',
                                status: 'queued'
                            }
                        })
                    });
                }
                serverQueuePostCount += 1;
                serverQueueBodies.push(body);
                events.push(`server-post-${serverQueuePostCount}`);
                if (serverQueuePostCount === 1) {
                    return Promise.reject(new TypeError('response lost after enqueue'));
                }
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({
                        success: true,
                        idempotent_replay: true,
                        item: {
                            ...body,
                            queue_id: 'queue-server-handoff',
                            status: 'queued'
                        }
                    })
                });
            }
            if (url === '/von/generate') {
                events.push('generate');
                generateBodies.push(JSON.parse(options.body || '{}'));
                return Promise.resolve({
                    ok: false,
                    status: 429,
                    headers: { get: () => '0' },
                    json: async () => ({
                        error: 'turn_capacity_reached',
                        detail: 'Try this turn later.',
                        retryable: true,
                        prompt_queue_id: 'queue-direct-handoff'
                    })
                });
            }
            if (url === '/von/api/chat_prompt_queue/queue-direct-handoff') {
                events.push('delete-source');
                return Promise.resolve({
                    ok: false,
                    status: 503,
                    json: async () => ({
                        success: false,
                        error: 'queue backend unavailable',
                        error_code: 'backend_unavailable'
                    })
                });
            }
            return Promise.resolve({
                ok: true,
                status: 200,
                json: async () => ({ success: true })
            });
        });

        document.getElementById('promptInput').value = 'Preserve this exact prompt';
        await sendMessage();
        await flushMicrotasks();

        expect(events).toEqual(['direct-post', 'generate', 'server-post-1']);
        expect(events).not.toContain('delete-source');
        expect(document.querySelectorAll('.chat-task-queue-item-label'))
            .toHaveLength(2);
        expect(Array.from(document.querySelectorAll('.chat-task-queue-item-label'))
            .map((element) => element.textContent)).toEqual(expect.arrayContaining([
            'Queue unconfirmed • Current',
            'Handoff pending • Current'
        ]));
        expect(document.querySelectorAll('.chat-task-queue-delete')).toHaveLength(0);

        document.querySelector('.chat-task-queue-enqueue-retry').click();
        await flushMicrotasks();
        await flushMicrotasks();

        expect(serverQueueBodies).toHaveLength(2);
        expect(serverQueueBodies[1].enqueue_submission_id)
            .toBe(serverQueueBodies[0].enqueue_submission_id);
        expect(events).toEqual([
            'direct-post',
            'generate',
            'server-post-1',
            'server-post-2',
            'delete-source'
        ]);
        expect(generateBodies).toHaveLength(1);
        expect(Array.from(document.querySelectorAll('.chat-task-queue-item-label'))
            .map((element) => element.textContent)).toEqual(expect.arrayContaining([
            'Next up • Current',
            'Superseded • Current'
        ]));
        const supersededItem = Array.from(document.querySelectorAll('.chat-task-queue-item'))
            .find((item) => item.querySelector('.chat-task-queue-item-label')?.textContent
                === 'Superseded • Current');
        expect(supersededItem).toBeTruthy();
        expect(supersededItem.querySelector('.chat-task-queue-edit').readOnly).toBe(true);
        expect(supersededItem.querySelector('.chat-task-queue-sync-warning').textContent).toBe(
            'Queue storage is temporarily unavailable.'
        );
        expect(supersededItem.querySelector('.chat-task-queue-delete').textContent)
            .toBe('Retry cleanup');
    }, 15000);
});
