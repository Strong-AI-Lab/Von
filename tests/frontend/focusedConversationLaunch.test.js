/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#test_user', org_id: '#V#test_org' })),
    getWindowSessionId: jest.fn(() => 'test-window-session-id'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#test_user'),
    renderSpanSuggestions: jest.fn()
}));

describe('focused conversation launch', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        document.body.innerHTML = `
            <div id="chatSessionFocusChips" class="is-hidden"></div>
            <div id="chatSessionMetadata"></div>
            <textarea id="promptInput"></textarea>
            <div id="scrollableField"></div>
            <div id="loadingIndicator"></div>
            <button id="sendButton"></button>
        `;
        window.scrollTo = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        delete global.fetch;
    });

    test('renders focal chips which navigate back to their concepts', () => {
        const chatTab = require(chatTabModulePath);
        const seen = [];
        const handler = (event) => seen.push(event.detail);
        document.addEventListener('von:selectConceptById', handler);

        chatTab.__testOnly_renderChatSessionFocusChips([{
            concept_id: '#V#jozef_stefan_institute',
            display_name: 'Jožef Stefan Institute'
        }]);

        const chips = document.querySelector('#chatSessionFocusChips');
        const chip = chips.querySelector('.chat-session-focus-chip');
        expect(chips.classList.contains('is-hidden')).toBe(false);
        expect(chips.textContent).toContain('Discussing');
        expect(chip.textContent).toBe('Jožef Stefan Institute');

        chip.click();
        expect(seen).toEqual([{
            conceptId: '#V#jozef_stefan_institute',
            createConceptTab: true,
            promoteExistingTab: true,
            modifierKeys: { shiftKey: true }
        }]);
        document.removeEventListener('von:selectConceptById', handler);
    });

    test('removes cached focus and its chips when the server revokes access', async () => {
        global.fetch = jest.fn(async () => ({
            ok: false,
            status: 403,
            json: async () => ({ error: 'Conversation not available.' })
        }));
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_setActiveChatSession('s-revoked', 'Shared discussion');
        chatTab.__testOnly_acceptChatSessionFocus('s-revoked', [{
            concept_id: '#V#private_project',
            display_name: 'Private project'
        }]);

        expect(document.querySelector('.chat-session-focus-chip')?.textContent).toBe('Private project');
        await chatTab.__testOnly_loadChatSessionFocus('s-revoked', { force: true });

        expect(document.querySelector('#chatSessionFocusChips').classList.contains('is-hidden')).toBe(true);
        // Selecting the same session must not resurrect the denied focus from cache.
        chatTab.__testOnly_setActiveChatSession('s-revoked', 'Shared discussion');
        expect(document.querySelector('.chat-session-focus-chip')).toBeNull();
    });

    test('revalidates a cached focus after selecting a conversation', async () => {
        const focusRequests = [];
        global.fetch = jest.fn(async (url) => {
            const requestUrl = String(url);
            if (requestUrl.includes('chat_session_focus')) {
                focusRequests.push(requestUrl);
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({
                        focal_concepts: [{
                            concept_id: '#V#shared_project',
                            display_name: 'Server-authorised project'
                        }]
                    })
                };
            }
            if (requestUrl === '/von/api/session/set_chat_session') {
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({ session_id: 's-shared', session_name: 'Shared discussion' })
                };
            }
            if (requestUrl.startsWith('/von/history?')) {
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({ history: [], segments_returned: 1, total_segments: 1 })
                };
            }
            return { ok: true, status: 200, json: async () => ({}) };
        });
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_acceptChatSessionFocus('s-shared', [{
            concept_id: '#V#stale_project',
            display_name: 'Stale cached project'
        }]);

        const switchPromise = chatTab.switchToChatSession('s-shared');
        // A local cache is not shown while the chosen conversation is being reauthorised.
        expect(document.querySelector('.chat-session-focus-chip')).toBeNull();
        await switchPromise;
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(focusRequests).toHaveLength(1);
        expect(document.querySelector('.chat-session-focus-chip')?.textContent).toBe('Server-authorised project');
    });

    test('offers Continue and explicit user-first or Von-first new discussion choices', async () => {
        global.fetch = jest.fn(async (url) => {
            expect(String(url)).toContain('focal_concept_id=%23V%23task_2675');
            return {
                ok: true,
                json: async () => ({
                    recent_sessions: [{ session_id: 's-existing', session_name: 'Earlier discussion' }]
                })
            };
        });
        const chatTab = require(chatTabModulePath);
        const launcher = await chatTab.__testOnly_openFocusedConversationLauncher({
            conceptId: '#V#task_2675',
            conceptName: 'Focused conversations'
        });

        expect(launcher).toBeTruthy();
        expect(launcher.getAttribute('role')).toBe('dialog');
        expect(Array.from(launcher.querySelectorAll('button')).map((button) => button.textContent)).toEqual([
            'Continue: Earlier discussion',
            'New conversation — I’ll start',
            'New conversation — Von starts',
            'Cancel'
        ]);
        expect(global.fetch).toHaveBeenCalledTimes(1);
    });

    test('sends an assistant opening without a synthetic user turn', async () => {
        const requests = [];
        global.fetch = jest.fn(async (url, options = {}) => {
            if (String(url) === '/von/generate') {
                requests.push(JSON.parse(options.body));
                return { ok: true, json: async () => ({ response: 'How can I help with this task?' }) };
            }
            return { ok: true, json: async () => ({}) };
        });
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_setActiveChatSession('s-opening', 'Opening discussion');

        await chatTab.__testOnly_sendChatPrompt({
            sessionId: 's-opening',
            promptOverride: '',
            turnKind: 'assistant_opening',
            initiationId: 'opening-test',
            allowEmptyPrompt: true,
            skipPromptQueueRecord: true,
        });

        expect(requests).toHaveLength(1);
        expect(requests[0]).toMatchObject({
            prompt: '',
            turn_kind: 'assistant_opening',
            initiation_id: 'opening-test',
            conversation_session_id: 's-opening',
        });
        expect(document.querySelector('.user-turn')).toBeNull();
        expect(document.querySelector('.message-container')?.textContent).toContain('How can I help with this task?');
    });

    test('does not orphan a Von-first conversation while another response is live', async () => {
        global.fetch = jest.fn();
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_setLiveChatRequest('s-live', { sessionId: 's-live' });

        await expect(chatTab.__testOnly_beginFocusedConversation({
            focalConceptIds: ['#V#task_2675'],
            mode: 'assistant_opening'
        })).rejects.toThrow('Wait for the current response');

        expect(global.fetch).not.toHaveBeenCalled();
        chatTab.__testOnly_setLiveChatRequest('s-live', null);
    });

    test('retries a failed assistant opening with the same durable initiation id', async () => {
        const generateRequests = [];
        global.fetch = jest.fn(async (url, options = {}) => {
            if (String(url) !== '/von/generate') {
                return { ok: true, json: async () => ({}) };
            }
            generateRequests.push(JSON.parse(options.body));
            if (generateRequests.length === 1) {
                return {
                    ok: false,
                    json: async () => ({ error: 'temporary opening failure' })
                };
            }
            return {
                ok: true,
                json: async () => ({ response: 'A useful opening about the task.' })
            };
        });
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_setActiveChatSession('s-retry-opening', 'Opening discussion');

        await chatTab.__testOnly_sendChatPrompt({
            sessionId: 's-retry-opening',
            sessionName: 'Opening discussion',
            promptOverride: '',
            turnKind: 'assistant_opening',
            initiationId: 'opening-retry-stable',
            allowEmptyPrompt: true,
            skipPromptQueueRecord: true,
        });

        const retryButton = document.querySelector('.assistant-opening-retry-button');
        expect(retryButton?.textContent).toBe('Retry Von opening');
        retryButton.click();
        await new Promise((resolve) => setTimeout(resolve, 0));
        await new Promise((resolve) => setTimeout(resolve, 0));

        expect(generateRequests).toHaveLength(2);
        expect(generateRequests.map((request) => request.initiation_id)).toEqual([
            'opening-retry-stable',
            'opening-retry-stable'
        ]);
        expect(generateRequests.every((request) => request.prompt === '')).toBe(true);
        expect(document.querySelector('.user-turn')).toBeNull();
        expect(document.body.textContent).toContain('A useful opening about the task.');
    });
});
