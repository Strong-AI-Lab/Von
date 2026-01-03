import {
    __testOnly_convertQuotedInstructionBlockquotesToButtons,
    __testOnly_convertReplyOptionsListsToButtons,
    __testOnly_deriveLlmDebugWarnings,
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

describe('chat insert prompt button behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
        `;
    });

    test('click inserts and submits by default', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = '<blockquote><p><strong>"Do the thing"</strong></p></blockquote>';
        __testOnly_convertQuotedInstructionBlockquotesToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(String(promptInput.value)).toContain('Do the thing');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });

    test('shift-click inserts without submitting', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');

        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = '<blockquote><p><strong>"Do the thing"</strong></p></blockquote>';
        __testOnly_convertQuotedInstructionBlockquotesToButtons(root);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        expect(String(promptInput.value)).toContain('Do the thing');
        expect(sendButton.click).toHaveBeenCalledTimes(0);
    });
});

describe('chat reply options button behaviour', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <button id="sendButton"></button>
            <textarea id="promptInput"></textarea>
        `;
    });

    test('list options become buttons that insert and submit by default', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <p>Please reply with one of:</p>
            <ul>
                <li><strong>“Yes, scan all emails from the last 24 hours and update the to-do list.”</strong></li>
                <li><strong>“Scan them, but show me the tasks before adding.”</strong></li>
            </ul>
        `;

        __testOnly_convertReplyOptionsListsToButtons(root);

        const list = root.querySelector('ul');
        expect(list).not.toBeNull();

        const items = Array.from(list.querySelectorAll(':scope > li'));
        expect(items.length).toBe(2);

        const buttons = Array.from(root.querySelectorAll('.chat-insert-prompt-button'));
        expect(buttons.length).toBe(2);

        buttons[0].dispatchEvent(new MouseEvent('click', { bubbles: true }));

        expect(String(promptInput.value)).toContain('Yes, scan all emails from the last 24 hours and update the to-do list.');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });

    test('shift-click inserts without submitting', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <p>Please reply with one of:</p>
            <ul>
                <li>"Scan them, but show me the tasks before adding."</li>
            </ul>
        `;

        __testOnly_convertReplyOptionsListsToButtons(root);

        const list = root.querySelector('ul');
        expect(list).not.toBeNull();
        expect(list.querySelectorAll(':scope > li').length).toBe(1);

        const btn = root.querySelector('.chat-insert-prompt-button');
        expect(btn).not.toBeNull();

        btn.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        expect(String(promptInput.value)).toContain('Scan them, but show me the tasks before adding.');
        expect(sendButton.click).toHaveBeenCalledTimes(0);
    });

    test('supports "tell me how you want to proceed" marker and splits multi-option items', () => {
        const sendButton = document.getElementById('sendButton');
        const promptInput = document.getElementById('promptInput');
        sendButton.click = jest.fn();

        const root = document.createElement('div');
        root.innerHTML = `
            <p>Tell me how you want to proceed:</p>
            <ul>
                <li><strong>“Add both new tasks to today’s to-do list.”</strong></li>
                <li><strong>“Add only #2.” / “Add only #3.”</strong></li>
                <li><strong>“Add them as future / backlog items.”</strong></li>
            </ul>
        `;

        __testOnly_convertReplyOptionsListsToButtons(root);

        const list = root.querySelector('ul');
        expect(list).not.toBeNull();
        expect(list.querySelectorAll(':scope > li').length).toBe(4);

        const buttons = Array.from(root.querySelectorAll('.chat-insert-prompt-button'));
        expect(buttons.map((b) => b.textContent)).toEqual([
            "Add both new tasks to today’s to-do list.",
            'Add only #2.',
            'Add only #3.',
            'Add them as future / backlog items.'
        ]);

        buttons[2].dispatchEvent(new MouseEvent('click', { bubbles: true }));
        expect(String(promptInput.value)).toContain('Add only #3.');
        expect(sendButton.click).toHaveBeenCalledTimes(1);
    });
});

describe('LLM debug warnings (presenter channel health)', () => {
    test('flags missing spoken channel when screen exists', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            presenter_channels: {
                screen: 'On-screen content',
                spoken: null,
                format: 'tagged_blocks_v1'
            }
        });

        expect(warnings).toContain(
            'Presenter output missing spoken channel; text-to-speech will fall back to screen text.'
        );
    });

    test('flags missing screen channel when spoken exists', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            presenter_channels: {
                screen: null,
                spoken: 'Talk track',
                format: 'tagged_blocks_v1'
            }
        });

        expect(warnings).toContain(
            'Presenter output missing screen channel; display will fall back to spoken text.'
        );
    });

    test('flags failed spoken backfill attempt', () => {
        const warnings = __testOnly_deriveLlmDebugWarnings({
            presenter_channels: {
                screen: 'On-screen content',
                spoken: '',
                format: 'narration_fallback_v1'
            },
            spoken_backfill_second_pass_attempted: true,
            spoken_backfill_second_pass_reason: 'missing_spoken'
        });

        expect(warnings).toContain(
            'Spoken backfill attempted but spoken channel is still missing (missing_spoken).'
        );
    });
});
