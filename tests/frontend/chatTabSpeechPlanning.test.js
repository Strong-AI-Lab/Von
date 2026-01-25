/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'mock-window-session-id')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));

const { sendMessage, showLlmDebugPopup, __test_only__rehydrateHistory, __testOnly_resetChatTtsState } = require(chatTabModulePath);

describe('chat speech planning (presenter channels)', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="scrollableField"></div>
            <div id="loadingIndicator" aria-hidden="true"></div>
            <button id="sendButton"></button>
            <button id="abortButton" aria-hidden="true"></button>
            <textarea id="promptInput"></textarea>
            <input type="checkbox" id="annotationToggle" />
            <input type="checkbox" id="ttsToggle" />

            <div id="chatLlmDebugPopup" class="hidden" aria-hidden="true" data-current-debug-data=""></div>
            <button id="closeChatLlmDebug"></button>
            <button id="copyChatLlmDebugJson"></button>
            <div id="chatLlmDebugMeta"></div>
            <pre id="chatLlmDebugMessages"></pre>
            <pre id="chatLlmDebugResponse"></pre>
            <div id="chatLlmDebugToolsSection" class="hidden"></div>
            <pre id="chatLlmDebugTools"></pre>
            <div id="chatLlmDebugAuxSection" class="hidden"></div>
            <pre id="chatLlmDebugAux"></pre>
        `;

        // Enable TTS in the environment.
        global.SpeechSynthesisUtterance = function (text) {
            this.text = text;
            this.lang = '';
            this.rate = 1;
            this.pitch = 1;
            this.volume = 1;
        };

        global.speechSynthesis = {
            speak: jest.fn(),
            cancel: jest.fn(),
            getVoices: jest.fn(() => [])
        };

        // Ensure module-level TTS state does not leak between tests.
        __testOnly_resetChatTtsState();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
        delete global.SpeechSynthesisUtterance;
        delete global.speechSynthesis;
    });

    test('Speak uses spoken channel and debug popup includes speech_planning', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
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
                    ok: true,
                    json: async () => ({
                        response: 'SCREEN TEXT',
                        presenter_channels: {
                            screen: 'SCREEN TEXT',
                            spoken: 'SPOKEN TEXT',
                            format: 'tagged_blocks_v1'
                        },
                        llm_debug: { model: 'gpt-5.2', response: 'SCREEN TEXT', messages: [] }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();

        // speech.js intentionally delays speak() briefly after cancel() to avoid
        // first-words clipping in some browsers.
        await new Promise((resolve) => setTimeout(resolve, 150));

        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('SPOKEN TEXT');

        const messageContainers = Array.from(document.querySelectorAll('.message-container'));
        const assistantContainer = messageContainers.find((el) => {
            const turnId = el?.dataset?.turnId;
            return typeof turnId === 'string' && turnId.startsWith('a-');
        });
        const assistantTurnId = assistantContainer?.dataset?.turnId;
        expect(assistantTurnId).toBeTruthy();

        showLlmDebugPopup(assistantTurnId);
        const popup = document.getElementById('chatLlmDebugPopup');
        const jsonText = popup.dataset.currentDebugData;
        expect(jsonText).toContain('speech_planning');
        expect(jsonText).toContain('tts_source');
        expect(jsonText).toContain('spoken');
    });

    test('Shift+click Speak uses screen channel (accessibility)', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
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
                    ok: true,
                    json: async () => ({
                        response: 'Here is **bold**, and `code`.',
                        presenter_channels: {
                            screen: 'Here is **bold**, and `code`.',
                            spoken: 'SPOKEN TEXT',
                            format: 'tagged_blocks_v1'
                        },
                        llm_debug: { model: 'gpt-5.2', response: 'SCREEN TEXT', messages: [] }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        await new Promise((resolve) => setTimeout(resolve, 150));

        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('Here is bold, and code.');
    });

    test('Default Speak derives narration when spoken missing (never raw markdown)', async () => {
        const { getUserContext } = require('../../src/frontend/web/von_interface/static/js/apiService.js');
        getUserContext.mockReturnValue({
            user_id: 'user',
            org_id: 'org',
            language: 'en-NZ',
            gmail_profile: null
        });

        const promptInput = document.getElementById('promptInput');
        promptInput.value = 'test prompt';

        global.fetch = jest.fn((url, options) => {
            if (typeof url === 'string' && url.startsWith('/von/api/render_markdown')) {
                let text = '';
                try {
                    text = JSON.parse(options?.body ?? '{}')?.text ?? '';
                } catch (_) {
                    text = '';
                }
                return Promise.resolve({
                    ok: true,
                    json: async () => ({ html: String(text) })
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
                    ok: true,
                    json: async () => ({
                        response: 'Here is **bold**, and `code`.',
                        presenter_channels: {
                            screen: 'Here is **bold**, and `code`.',
                            spoken: null,
                            format: 'tagged_blocks_v1'
                        },
                        llm_debug: { model: 'gpt-5.2', response: 'SCREEN TEXT', messages: [] }
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        await sendMessage();

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 150));
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance1 = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance1.text).toBe('Here is bold, and code.');

        // Stop the current speech (same turnId), then speak again with Shift held.
        speakButton.click();

        speakButton.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        await new Promise((resolve) => setTimeout(resolve, 150));
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance2 = global.speechSynthesis.speak.mock.calls[1][0];
        expect(utterance2.text).toBe('Here is bold, and code.');
    });

    test('History Speak backfills spoken talk track on-demand', async () => {
        // Arrange: history contains an assistant turn with no presenter channels.
        const scrollableField = document.getElementById('scrollableField');
        expect(scrollableField).toBeTruthy();

        global.fetch = jest.fn((url, init) => {
            if (typeof url === 'string' && url.startsWith('/von/history/backfill_spoken')) {
                return Promise.resolve({
                    ok: true,
                    json: async () => ({
                        status: 'ok',
                        presenter_channels: {
                            screen: 'SCREEN TEXT',
                            spoken: 'SPOKEN FROM BACKFILL',
                            format: 'narration_fallback_v1'
                        },
                        updated: true
                    })
                });
            }

            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        __test_only__rehydrateHistory(scrollableField, [
            { role: 'user', content: 'Hi', timestamp: '2026-01-02T00:00:00Z' },
            {
                role: 'assistant',
                content: 'SCREEN TEXT',
                timestamp: '2026-01-02T00:00:01Z',
                history_location: { session_id: 'sess-1', history_index: 1 }
            }
        ]);

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        // Act
        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 0));

        await new Promise((resolve) => setTimeout(resolve, 200));

        // Assert
        expect(global.fetch).toHaveBeenCalled();
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance.text).toBe('SPOKEN FROM BACKFILL');
    });

    test('History Speak falls back to on-screen text when backfill fails (and cools down)', async () => {
        const scrollableField = document.getElementById('scrollableField');
        expect(scrollableField).toBeTruthy();

        const warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => { });
        let backfillCalls = 0;
        global.fetch = jest.fn((url, init) => {
            if (typeof url === 'string' && url.startsWith('/von/history/backfill_spoken')) {
                backfillCalls += 1;
                return Promise.resolve({
                    ok: false,
                    status: 404,
                    json: async () => ({ error: 'History entry not found' })
                });
            }
            return Promise.resolve({ ok: true, json: async () => ({}) });
        });

        __test_only__rehydrateHistory(scrollableField, [
            { role: 'user', content: 'Hi', timestamp: '2026-01-02T00:00:00Z' },
            {
                role: 'assistant',
                content: 'SCREEN TEXT',
                timestamp: '2026-01-02T00:00:01Z',
                history_location: { session_id: 'sess-1', history_index: 1 }
            }
        ]);

        const speakButton = document.querySelector('.chat-tts-button');
        expect(speakButton).toBeTruthy();

        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 200));
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance1 = global.speechSynthesis.speak.mock.calls[0][0];
        expect(utterance1.text).toBe('SCREEN TEXT');

        const notice1 = document.querySelector('.chat-tts-notice');
        expect(notice1).toBeTruthy();
        expect(String(notice1.textContent || '')).toContain('Talk track unavailable');

        expect(backfillCalls).toBe(1);
        expect(warnSpy).toHaveBeenCalled();

        // Second click stops the current speech (toggle behaviour).
        speakButton.click();

        // The click handler is async (history backfill/cool-down). Give it a tick to
        // restore the button state before clicking again.
        await new Promise((resolve) => setTimeout(resolve, 0));

        // Third click within cooldown should speak again without retrying backfill.
        speakButton.click();
        await new Promise((resolve) => setTimeout(resolve, 200));
        expect(global.speechSynthesis.speak).toHaveBeenCalled();
        const utterance2 = global.speechSynthesis.speak.mock.calls[1][0];
        expect(utterance2.text).toBe('SCREEN TEXT');
        expect(backfillCalls).toBe(1);
    });
});
