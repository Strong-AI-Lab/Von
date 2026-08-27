/** @jest-environment jsdom */

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
    __testOnly_refreshWorkflowStatusSnapshot,
    __testOnly_resetChatRequestState,
    __testOnly_resetWorkflowStatusState,
    __testOnly_getThinkingProgressPollFetchTimeoutMs,
    __testOnly_setActiveChatSession,
    __testOnly_setLiveChatRequestForSession,
    formatChatTimestamp,
    sendMessage
} = require(chatTabModulePath);

describe('formatChatTimestamp', () => {
    const createDate = (daysAgo, hours = 12, minutes = 0) => {
        const date = new Date();
        date.setDate(date.getDate() - daysAgo);
        date.setHours(hours, minutes, 0, 0);
        return date;
    };

    test('returns formatted time for today', () => {
        const today = new Date();
        const isoString = today.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).toMatch(/^Today /);
    });

    test('returns formatted time for yesterday', () => {
        const yesterday = createDate(1);
        const isoString = yesterday.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).toMatch(/^Yesterday /);
    });

    test('returns day and time for within a week', () => {
        const threeDaysAgo = createDate(3);
        const isoString = threeDaysAgo.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).not.toContain('Today');
        expect(result).not.toContain('Yesterday');
        const dayName = threeDaysAgo.toLocaleString([], { weekday: 'short' });
        expect(result).toContain(dayName);
    });

    test('returns date and time for older dates', () => {
        const tenDaysAgo = createDate(10);
        const isoString = tenDaysAgo.toISOString();
        const result = formatChatTimestamp(isoString);
        expect(result).not.toContain('Today');
        expect(result).not.toContain('Yesterday');
        const month = tenDaysAgo.toLocaleString([], { month: 'short' });
        expect(result).toContain(month);
    });
});

describe('chat abort behaviour', () => {
    beforeEach(() => {
        __testOnly_resetChatRequestState();
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
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('abort restores prompt text and re-enables sending', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        __testOnly_setActiveChatSession('session-abort-test', 'Abort Test');

        const promptInput = document.getElementById('promptInput');
        promptInput.value = "since we'\n";
        promptInput.focus();
        try {
            promptInput.setSelectionRange(promptInput.value.length, promptInput.value.length);
        } catch (_) {
            // jsdom best-effort
        }

        const fetchCalls = [];
        let resolveQueueCreate = null;

        global.fetch = jest.fn((url, options = {}) => {
            fetchCalls.push({ url, options });
            if (url === '/von/api/chat_prompt_queue' && options.method === 'POST') {
                return new Promise((resolve) => {
                    resolveQueueCreate = () => resolve({
                        ok: true,
                        status: 201,
                        json: async () => ({
                            success: true,
                            item: {
                                queue_id: 'queue-aborted-before-dispatch',
                                prompt_raw: "since we'\n",
                                session_id: 'session-abort-test',
                                status: 'queued'
                            }
                        })
                    });
                });
            }
            if (
                url === '/von/api/chat_prompt_queue/queue-aborted-before-dispatch'
                && options.method === 'DELETE'
            ) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                throw new Error('aborted pre-dispatch prompt must not generate');
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const sendPromise = sendMessage();

        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('false');

        document.getElementById('abortButton').click();
        expect(resolveQueueCreate).not.toBeNull();
        resolveQueueCreate();

        await new Promise((r) => setTimeout(r, 0));

        expect(fetchCalls.some(({ url }) => url === '/von/generate')).toBe(false);
        expect(fetchCalls.some(({ url, options }) => (
            url === '/von/api/chat_prompt_queue/queue-aborted-before-dispatch'
            && options.method === 'DELETE'
        ))).toBe(true);
        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('true');
        expect(promptInput.value).toBe("since we'\n");

        await expect(sendPromise).resolves.toBeUndefined();
    });

    test('non-json generate failure shows controlled server response error', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        __testOnly_resetChatRequestState();
        __testOnly_setActiveChatSession('session-non-json', 'Non JSON');

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
                return Promise.resolve({
                    ok: false,
                    json: async () => {
                        throw new SyntaxError('Unexpected token <');
                    }
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'Trigger non-json failure';

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(document.getElementById('scrollableField').textContent).toContain(
            'Server returned a non-JSON error response.'
        );
        expect(document.getElementById('scrollableField').textContent).not.toContain(
            'Unexpected token <'
        );
    });
});

describe('thinking progress polling', () => {
    test('uses a non-trivial fetch timeout for live progress payloads', () => {
        expect(__testOnly_getThinkingProgressPollFetchTimeoutMs()).toBe(30_000);
    });

    test('omits browser credentials from live progress polling requests', async () => {
        const {
            fetchWithTimeout,
            getUserContext
        } = require('../../src/frontend/web/von_interface/static/js/apiService.js');

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

        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });
        fetchWithTimeout.mockResolvedValue({
            ok: false,
            status: 202,
            json: async () => ({ status: 'pending' })
        });
        __testOnly_setActiveChatSession('session-progress-credentials', 'Progress Credentials');

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'Who am I in this conversation?';

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                return new Promise((resolve, reject) => {
                    options.signal?.addEventListener('abort', () => {
                        const err = new Error('aborted');
                        err.name = 'AbortError';
                        reject(err);
                    });
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const sendPromise = sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));
        await new Promise((resolve) => setTimeout(resolve, 0));

        const progressCall = fetchWithTimeout.mock.calls.find(
            ([url]) => typeof url === 'string' && url.startsWith('/von/progress/')
        );
        expect(progressCall).toBeDefined();
        expect(progressCall[1]?.credentials).toBe('same-origin');
        expect(progressCall[1]?.headers?.['X-User-Concept-ID']).toBeUndefined();

        document.getElementById('abortButton').click();
        await expect(sendPromise).resolves.toBeUndefined();
    });
});

describe('workflow monitor snapshot refreshes', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="workflowStatusPanel"></div>
            <div id="workflowStatusBody"></div>
            <button id="sendButton"></button>
        `;
        __testOnly_resetWorkflowStatusState();
        __testOnly_resetChatRequestState();
        __testOnly_setActiveChatSession('session-workflow-status-test', 'Workflow Status Test');
        jest.clearAllMocks();
    });

    afterEach(() => {
        __testOnly_resetWorkflowStatusState();
        __testOnly_resetChatRequestState();
        jest.restoreAllMocks();
    });

    test('defers silent workflow status snapshot fetches while a live chat request is active', async () => {
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => ({ items: [] })
        });

        __testOnly_setLiveChatRequestForSession('session-workflow-status-test', {
            requestId: 'request-1'
        });

        await __testOnly_refreshWorkflowStatusSnapshot({ silent: true });

        const workflowStatusCalls = fetchWithTimeout.mock.calls.filter(
            ([url]) => typeof url === 'string' && url.startsWith('/api/workflows/instances?')
        );
        expect(workflowStatusCalls).toHaveLength(0);
    });

    test('allows explicit workflow status refreshes even while a live chat request is active', async () => {
        const { fetchWithTimeout } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        fetchWithTimeout.mockResolvedValue({
            ok: true,
            json: async () => ({ items: [] })
        });

        __testOnly_setLiveChatRequestForSession('session-workflow-status-test', {
            requestId: 'request-2'
        });

        await __testOnly_refreshWorkflowStatusSnapshot({ silent: false });

        const workflowStatusCalls = fetchWithTimeout.mock.calls.filter(
            ([url]) => typeof url === 'string' && url.startsWith('/api/workflows/instances?')
        );
        expect(workflowStatusCalls).toHaveLength(1);
        expect(workflowStatusCalls[0][0]).toMatch(/^\/api\/workflows\/instances\?/);
    });
});
