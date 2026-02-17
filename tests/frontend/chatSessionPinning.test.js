/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#pin_user', org_id: '#V#test_org' })),
    postJson: jest.fn(async () => ({ status: 'updated', namespace: '#V#pin_user@test_org' })),
    getWindowSessionId: jest.fn(() => 'test-window-session-id'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#pin_user'),
    renderSpanSuggestions: jest.fn()
}));

describe('chat session pinning', () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="chatSessionTabs"></div>
            <div id="chatSessionMetadata"></div>
            <div id="scrollableField"></div>
        `;

        localStorage.clear();

        global.fetch = jest.fn(async (url) => {
            if (String(url).startsWith('/von/api/session/context')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        user_id: '#V#pin_user',
                        organisation_id: '#V#test_org',
                        namespace: '#V#pin_user@test_org',
                    })
                };
            }
            if (String(url).startsWith('/von/history/sessions')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        active_session_id: null,
                        sessions: [
                            {
                                session_id: 's1',
                                session_name: 'Recent session',
                                last_message_at: '2026-02-17T09:00:00Z',
                                created_at: '2026-02-17T09:00:00Z',
                                message_count: 4,
                            },
                            {
                                session_id: 's2',
                                session_name: 'Priority session',
                                last_message_at: '2026-02-10T09:00:00Z',
                                created_at: '2026-02-10T09:00:00Z',
                                message_count: 2,
                            },
                        ],
                    })
                };
            }
            return { ok: true, json: async () => ({}) };
        });
    });

    afterEach(() => {
        jest.resetModules();
        jest.restoreAllMocks();
        localStorage.clear();
    });

    test('pins a conversation and keeps it in the pinned top group', async () => {
        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        const initialOrder = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tab[data-session-id]')
        ).map((el) => el.dataset.sessionId);
        expect(initialOrder[0]).toBe('s1');

        const pinButton = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s2"] .chat-session-tab-pin-toggle'
        );
        expect(pinButton).toBeTruthy();
        pinButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        const storageKey = 'von:pinnedChatSessionIds:#V#pin_user';
        const storedPins = JSON.parse(localStorage.getItem(storageKey) || '[]');
        expect(storedPins).toContain('s2');

        const groupLabels = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tabs-group-label')
        ).map((el) => el.textContent || '');
        expect(groupLabels.some((label) => label.includes('Pinned'))).toBe(true);

        const reordered = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tab[data-session-id]')
        ).map((el) => el.dataset.sessionId);
        expect(reordered[0]).toBe('s2');

        const pinnedButton = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s2"] .chat-session-tab-pin-toggle'
        );
        expect(pinnedButton.getAttribute('aria-pressed')).toBe('true');
    });

    test('unpins a conversation and clears persisted pin state', async () => {
        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        const pinButton = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s2"] .chat-session-tab-pin-toggle'
        );
        pinButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        const unpinButton = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s2"] .chat-session-tab-pin-toggle'
        );
        unpinButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));

        const storageKey = 'von:pinnedChatSessionIds:#V#pin_user';
        const storedPins = JSON.parse(localStorage.getItem(storageKey) || '[]');
        expect(storedPins).toEqual([]);

        const groupLabels = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tabs-group-label')
        ).map((el) => el.textContent || '');
        expect(groupLabels.some((label) => label.includes('Pinned'))).toBe(false);
    });
});
