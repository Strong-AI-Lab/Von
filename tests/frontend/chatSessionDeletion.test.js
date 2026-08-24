/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'test-window-session'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#delete_user'),
    renderSpanSuggestions: jest.fn()
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/textDecorator.js', () => ({
    applyCartoucheAppearance: jest.fn(),
    cartouchifyElementText: jest.fn(),
    createVontologyAliasCartouche: jest.fn(),
    createVontologyCartouche: jest.fn(),
    findPotentialConceptAliasMatches: jest.fn(() => []),
    getCartoucheAppearanceSettings: jest.fn(() => ({
        useShortestName: false,
        showName: true,
        showId: true,
        showKind: true,
        kindAsBackground: false
    })),
    linkifyVontologyTokensInElement: jest.fn(),
    normalisePotentialConceptAlias: jest.fn(() => ''),
    normalisePotentialConceptId: jest.fn(() => ''),
    replaceTextNodeWithVontologyAliasCartouches: jest.fn(() => [])
}));

jest.mock('../../src/frontend/web/von_interface/static/js/utils/toast.js', () => ({
    showToast: jest.fn()
}));

describe('chat session Trash', () => {
    let originalMatchMedia;

    beforeEach(() => {
        jest.resetModules();
        originalMatchMedia = window.matchMedia;
        window.matchMedia = jest.fn(() => ({ matches: false }));
        document.body.innerHTML = `
            <div id="chatTab">
                <div id="conversationWorkspace" data-tabs-layout="horizontal"
                    data-effective-tabs-layout="horizontal">
                    <div id="chatSessionTabs"></div>
                    <div id="chatSessionCount"></div>
                    <div id="chatSessionMetadata"></div>
                    <div id="scrollableField"></div>
                </div>
            </div>
        `;
        localStorage.clear();
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        window.matchMedia = originalMatchMedia;
    });

    function seedSessions(chatTab) {
        const timestamp = new Date().toISOString();
        chatTab.__testOnly_setSessionTabsCache([
            {
                session_id: 'delete-me',
                session_name: 'Delete me',
                namespace: '#V#delete_user@org',
                last_message_at: timestamp,
                message_count: 0
            },
            {
                session_id: 'keep-me',
                session_name: 'Keep me',
                namespace: '#V#delete_user@org',
                last_message_at: timestamp,
                message_count: 2
            }
        ]);
    }

    test('uses the mounted route and removes the tab only after verified success', async () => {
        global.fetch.mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({
                status: 'trashed',
                session_id: 'delete-me',
                changed: true,
                recoverable: true,
                trashed: true,
                canonical_read_back: { session_id: 'delete-me', trashed: true }
            })
        });
        const chatTab = require(chatTabModulePath);
        const { showToast } = require(
            '../../src/frontend/web/von_interface/static/js/utils/toast.js'
        );
        seedSessions(chatTab);

        const deleted = await chatTab.__testOnly_deleteConversation('delete-me');

        expect(deleted).toBe(true);
        expect(global.fetch).toHaveBeenCalledWith(
            '/von/api/session/delete_chat_session',
            expect.objectContaining({
                method: 'POST',
                body: JSON.stringify({ session_id: 'delete-me' })
            })
        );
        expect(global.fetch.mock.calls[0][1].headers).toEqual(expect.objectContaining({
            'Content-Type': 'application/json',
            'X-Von-Window-Session': 'test-window-session',
            'X-User-Concept-ID': '#V#delete_user'
        }));
        expect(
            chatTab.__testOnly_getSessionTabsCache().map((session) => session.session_id)
        ).toEqual(['keep-me']);
        expect(showToast).toHaveBeenCalledWith('Conversation moved to Trash', 'success');
    });

    test('retains the tab and reports the HTTP error when the response is not JSON', async () => {
        global.fetch.mockResolvedValue({
            ok: false,
            status: 404,
            json: async () => {
                throw new SyntaxError('Unexpected token <');
            }
        });
        const chatTab = require(chatTabModulePath);
        const { showToast } = require(
            '../../src/frontend/web/von_interface/static/js/utils/toast.js'
        );
        seedSessions(chatTab);

        const deleted = await chatTab.__testOnly_deleteConversation('delete-me');

        expect(deleted).toBe(false);
        expect(
            chatTab.__testOnly_getSessionTabsCache().map((session) => session.session_id)
        ).toEqual(['delete-me', 'keep-me']);
        expect(showToast).toHaveBeenCalledWith(
            'Failed to move conversation to Trash (HTTP 404)',
            'error'
        );
    });

    test('retains the tab when a 200 response lacks a Trash read-back', async () => {
        global.fetch.mockResolvedValue({
            ok: true,
            status: 200,
            json: async () => ({ status: 'trashed', session_id: 'delete-me' })
        });
        const chatTab = require(chatTabModulePath);
        const { showToast } = require(
            '../../src/frontend/web/von_interface/static/js/utils/toast.js'
        );
        seedSessions(chatTab);

        const deleted = await chatTab.__testOnly_deleteConversation('delete-me');

        expect(deleted).toBe(false);
        expect(
            chatTab.__testOnly_getSessionTabsCache().map((session) => session.session_id)
        ).toEqual(['delete-me', 'keep-me']);
        expect(showToast).toHaveBeenCalledWith(
            'Moving the conversation to Trash could not be verified',
            'error'
        );
    });
});
