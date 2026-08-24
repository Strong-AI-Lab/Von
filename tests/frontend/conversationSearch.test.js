/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(),
    getWindowSessionId: jest.fn(() => 'window-1'),
    postJson: jest.fn(),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#alice'),
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

describe('conversation search UI', () => {
    beforeEach(() => {
        jest.resetModules();
        window.matchMedia = jest.fn(() => ({ matches: false }));
        document.body.innerHTML = `
            <input id="conversationSearchInput" />
            <button id="conversationSearchButton"></button>
            <button id="conversationTrashButton" aria-pressed="false"></button>
            <div id="conversationSearchPanel" hidden>
                <div id="conversationSearchStatus"></div>
                <div id="conversationSearchResults"></div>
                <button id="conversationSearchMore" hidden></button>
            </div>
            <div id="chatSessionTabs"></div>
            <div id="chatSessionCount"></div>
            <div id="chatSessionMetadata"></div>
            <div id="scrollableField"></div>
        `;
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('renders named, dated, bounded results and passes the cursor for more', async () => {
        global.fetch
            .mockResolvedValueOnce({
                ok: true,
                json: async () => ({
                    success: true,
                    results: [{
                        session_id: 's1',
                        session_name: 'Detector calibration',
                        last_message_at: '2026-08-23T10:00:00Z',
                        match: { snippet: '...old exact detector phrase...' }
                    }],
                    next_cursor: 'opaque-cursor',
                    index_coverage: { complete_for_accessible_window: false }
                })
            })
            .mockResolvedValueOnce({
                ok: true,
                json: async () => ({
                    success: true,
                    results: [{
                        session_id: 's2',
                        session_name: 'Collider planning',
                        created_at: '2026-08-20T09:00:00Z',
                        match: { snippet: 'semantic topic match' }
                    }],
                    next_cursor: null,
                    index_coverage: { complete_for_accessible_window: true }
                })
            });
        const chatTab = require(chatTabModulePath);
        document.getElementById('conversationSearchInput').value = 'detector';

        await chatTab.__testOnly_performConversationSearch();
        await chatTab.__testOnly_performConversationSearch({ append: true });

        expect(global.fetch.mock.calls[0][0]).toContain('q=detector');
        expect(global.fetch.mock.calls[1][0]).toContain('cursor=opaque-cursor');
        expect(document.getElementById('conversationSearchResults').textContent).toContain('Detector calibration');
        expect(document.getElementById('conversationSearchResults').textContent).toContain('Collider planning');
        expect(document.getElementById('conversationSearchResults').textContent).toContain('old exact detector phrase');
        expect(chatTab.__testOnly_getConversationSearchState().results).toHaveLength(2);
        expect(document.getElementById('conversationSearchMore').hidden).toBe(true);
    });

    test('Trash view restores through the recoverable lifecycle route', async () => {
        global.fetch
            .mockResolvedValueOnce({
                ok: true,
                json: async () => ({
                    success: true,
                    results: [{
                        session_id: 'trashed-1',
                        session_name: 'Recover me',
                        trashed: true,
                        match: {}
                    }],
                    next_cursor: null,
                    index_coverage: { complete_for_accessible_window: true }
                })
            })
            .mockResolvedValueOnce({
                ok: true,
                json: async () => ({
                    status: 'restored',
                    session_id: 'trashed-1',
                    trashed: false,
                    recoverable: true
                })
            });
        const chatTab = require(chatTabModulePath);

        await chatTab.__testOnly_performConversationSearch({ trashedOnly: true });
        await chatTab.__testOnly_restoreConversation('trashed-1');

        expect(global.fetch.mock.calls[0][0]).toContain('q=*');
        expect(global.fetch.mock.calls[0][0]).toContain('trashed_only=true');
        expect(global.fetch.mock.calls[1][0]).toBe('/von/api/session/delete_chat_session');
        expect(global.fetch.mock.calls[1][1].body).toBe(JSON.stringify({
            session_id: 'trashed-1',
            action: 'restore'
        }));
        expect(chatTab.__testOnly_getConversationSearchState().results).toEqual([]);
    });
});
