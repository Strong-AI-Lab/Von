import {
    __testOnly_hydrateChatConceptCartouches,
    __testOnly_resetChatConceptMetaCaches,
    formatChatTimestamp,
    sendMessage
} from '../chatTab';

// Mock dependencies to avoid import errors
jest.mock('../apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));
jest.mock('../domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));
jest.mock('../utils/textDecorator.js', () => ({
    annotateElementText: jest.fn(),
    cartouchifyElementText: jest.fn(),
    cartouchifyVontologyTokensInElement: jest.fn()
}));

describe('formatChatTimestamp', () => {
    // Helper to create a date relative to now
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
        // Check for day name (Mon, Tue, etc.)
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
            <div id="loadingIndicator" aria-hidden="true"></div>
            <button id="sendButton"></button>
            <button id="abortButton" style="display:none" aria-hidden="true"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('abort restores prompt text and re-enables sending', async () => {
        const { getUserContext } = require('../apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = "since we'\n";
        promptInput.focus();
        try {
            promptInput.setSelectionRange(promptInput.value.length, promptInput.value.length);
        } catch (_) {
            // jsdom best-effort
        }

        let generateSignal = null;

        global.fetch = jest.fn((url, options = {}) => {
            if (typeof url === 'string' && url.startsWith('/von/history/length')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ history_length: 0, authenticated: true })
                });
            }

            if (typeof url === 'string' && url.startsWith('/von/generate')) {
                generateSignal = options.signal;
                return new Promise((resolve, reject) => {
                    if (generateSignal) {
                        generateSignal.addEventListener('abort', () => {
                            const err = new Error('aborted');
                            err.name = 'AbortError';
                            reject(err);
                        });
                    }
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const sendPromise = sendMessage();

        expect(document.getElementById('sendButton').disabled).toBe(true);
        expect(document.getElementById('abortButton').style.display).toBe('inline-flex');

        document.getElementById('abortButton').click();

        // Let abort propagate through promise chain
        await new Promise((r) => setTimeout(r, 0));

        expect(generateSignal).not.toBeNull();
        expect(generateSignal.aborted).toBe(true);
        expect(document.getElementById('sendButton').disabled).toBe(false);
        expect(document.getElementById('abortButton').style.display).toBe('none');
        expect(promptInput.value).toBe("since we'\n");

        // Ensure the sendMessage promise resolves without throwing
        await expect(sendPromise).resolves.toBeUndefined();
    });
});

describe('chat cartouche hydration retries', () => {
    beforeEach(() => {
        jest.useFakeTimers();
        __testOnly_resetChatConceptMetaCaches();
        document.body.innerHTML = `
            <div id="root">
                <button class="vontology-cartouche" data-full-concept-id="#V#literary_work">
                    <span class="vontology-cartouche-name">#V#literary_work</span>
                    <span class="vontology-cartouche-id">#V#literary_work</span>
                    <span class="vontology-cartouche-kind type">Type</span>
                </button>
            </div>
        `;
    });

    afterEach(() => {
        jest.useRealTimers();
        jest.restoreAllMocks();
        delete global.fetch;
    });

    async function flushMicrotasks() {
        await Promise.resolve();
        await Promise.resolve();
    }

    test('auto-rehydrates after initial provisional lookup', async () => {
        const fullId = '#V#literary_work';
        let nodeContentCalls = 0;

        global.fetch = jest.fn((url) => {
            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/node_content')) {
                nodeContentCalls += 1;
                if (nodeContentCalls === 1) {
                    return Promise.resolve({ ok: false, status: 404 });
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ display_name: 'Literary Work', kind: 'type' })
                });
            }

            if (typeof url === 'string' && url.startsWith('/vontology/api/vontology/search')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ results: [{ id: fullId, kind: 'type' }] })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        const root = document.getElementById('root');
        __testOnly_hydrateChatConceptCartouches(root);
        await flushMicrotasks();

        const nameEl = document.querySelector('.vontology-cartouche-name');
        expect(nameEl.textContent).toBe(fullId);

        jest.advanceTimersByTime(300);
        await flushMicrotasks();

        expect(nameEl.textContent).toBe('Literary Work');
    });
});
