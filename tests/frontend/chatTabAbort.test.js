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

    test('stop requests server cancellation and keeps Thinking visible until terminal status', async () => {
        const {
            fetchWithTimeout,
            getUserContext
        } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
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

        fetchWithTimeout.mockReset();
        let cancellationRequested = false;
        let cancellationProgressObserved = false;
        let resolveCancelledStatus = null;
        fetchWithTimeout.mockImplementation((url) => {
            if (typeof url === 'string' && url.startsWith('/von/progress/')) {
                return Promise.resolve({
                    ok: false,
                    status: 202,
                    json: async () => ({ status: 'pending' })
                });
            }
            if (typeof url === 'string' && url.startsWith('/von/api/task/status/')) {
                if (cancellationRequested) {
                    if (!cancellationProgressObserved) {
                        cancellationProgressObserved = true;
                        return Promise.resolve({
                            ok: true,
                            status: 200,
                            json: async () => ({
                                status: 'running',
                                progress: {
                                    status: 'cancellation_requested',
                                    result_summary: 'Stop requested.'
                                }
                            })
                        });
                    }
                    return new Promise((resolve) => {
                        resolveCancelledStatus = () => resolve({
                            ok: true,
                            status: 200,
                            json: async () => ({
                                status: 'cancelled',
                                progress: {
                                    status: 'cancelled',
                                    result_summary: 'Turn stopped.'
                                }
                            })
                        });
                    });
                }
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ status: 'running' })
                });
            }
            return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
        });

        let generateSignal = null;
        let submittedGenerateBody = null;
        const cancellationUrls = [];
        const cancellationCredentials = [];

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateSignal = options.signal;
                submittedGenerateBody = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    status: 202,
                    json: async () => ({
                        background: true,
                        task_id: submittedGenerateBody.client_request_id,
                        status: 'running'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/task/cancel/')) {
                cancellationRequested = true;
                cancellationUrls.push(url);
                cancellationCredentials.push(options.credentials);
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true, message: 'Cancellation requested' })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await expect(sendMessage()).resolves.toBeUndefined();

        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('false');
        expect(submittedGenerateBody.background).toBe(true);

        document.getElementById('abortButton').click();

        await new Promise((r) => setTimeout(r, 0));

        expect(generateSignal).not.toBeNull();
        expect(generateSignal.aborted).toBe(false);
        expect(cancellationUrls).toHaveLength(1);
        expect(cancellationUrls[0]).toBe(
            `/von/api/task/cancel/${encodeURIComponent(submittedGenerateBody.client_request_id)}`
        );
        expect(cancellationCredentials).toEqual(['same-origin']);
        expect(document.querySelector('.loading-indicator-text').textContent)
            .toBe('Stopping…');
        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('abortButton').disabled).toBe(true);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('false');
        expect(promptInput.value).toBe('');

        for (let attempt = 0; attempt < 20 && !cancellationProgressObserved; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 10));
        }
        expect(cancellationProgressObserved).toBe(true);
        expect(generateSignal.aborted).toBe(false);
        expect(document.querySelector('.loading-indicator-text').textContent)
            .toBe('Stopping…');
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('false');
        expect(document.getElementById('abortButton').disabled).toBe(true);

        for (let attempt = 0; attempt < 80 && !resolveCancelledStatus; attempt += 1) {
            await new Promise((resolve) => setTimeout(resolve, 20));
        }
        expect(resolveCancelledStatus).not.toBeNull();
        resolveCancelledStatus();
        for (let attempt = 0; attempt < 10; attempt += 1) {
            if (
                generateSignal.aborted
                && document.getElementById('abortButton').getAttribute('aria-hidden') === 'true'
            ) {
                break;
            }
            await new Promise((resolve) => setTimeout(resolve, 0));
        }

        expect(generateSignal.aborted).toBe(true);
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('true');
        expect(promptInput.value).toBe("since we'\n");
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
        fetchWithTimeout.mockReset();
        let cancellationRequested = false;
        fetchWithTimeout.mockImplementation((url) => {
            if (typeof url === 'string' && url.startsWith('/von/api/task/status/')) {
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ status: cancellationRequested ? 'cancelled' : 'running' })
                });
            }
            return Promise.resolve({
                ok: false,
                status: 202,
                json: async () => ({ status: 'pending' })
            });
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
                const body = JSON.parse(options.body || '{}');
                return Promise.resolve({
                    ok: true,
                    status: 202,
                    json: async () => ({
                        background: true,
                        task_id: body.client_request_id,
                        status: 'running'
                    })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/api/task/cancel/')) {
                cancellationRequested = true;
                return Promise.resolve({
                    ok: true,
                    status: 200,
                    json: async () => ({ success: true })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();
        await new Promise((resolve) => setTimeout(resolve, 0));
        await new Promise((resolve) => setTimeout(resolve, 0));

        const progressCall = fetchWithTimeout.mock.calls.find(
            ([url]) => typeof url === 'string' && url.startsWith('/von/progress/')
        );
        expect(progressCall).toBeDefined();
        expect(progressCall[1]?.credentials).toBe('omit');

        document.getElementById('abortButton').click();
        for (let attempt = 0; attempt < 10; attempt += 1) {
            if (document.getElementById('abortButton').getAttribute('aria-hidden') === 'true') {
                break;
            }
            await new Promise((resolve) => setTimeout(resolve, 0));
        }
        expect(document.getElementById('abortButton').getAttribute('aria-hidden')).toBe('true');
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
