/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));

const { sendMessage, showLlmDebugPopup } = require(chatTabModulePath);

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

        global.fetch = jest.fn((url) => {
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
                        response_channels: {
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
});
