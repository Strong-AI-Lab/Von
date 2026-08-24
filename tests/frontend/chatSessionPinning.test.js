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
        const recentTimestamp = new Date(Date.now() - 60 * 60 * 1000).toISOString();
        const olderRecentTimestamp = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();

        document.body.innerHTML = `
            <input id="vontologySearchInput" />
            <div id="chatSessionTabs"></div>
            <div id="chatSessionHistoryControls"></div>
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
                                last_message_at: recentTimestamp,
                                created_at: recentTimestamp,
                                message_count: 4,
                            },
                            {
                                session_id: 's2',
                                session_name: 'Priority session',
                                last_message_at: olderRecentTimestamp,
                                created_at: olderRecentTimestamp,
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

        const reordered = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tab[data-session-id]')
        ).map((el) => el.dataset.sessionId);
        expect(reordered[0]).toBe('s2');

        const pinnedTab = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s2"]'
        );
        const pinnedBadge = pinnedTab?.querySelector('.chat-session-tab-group-badge-pinned');
        expect(pinnedBadge?.textContent || '').toContain('Pinned (1)');

        const recentTab = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s1"]'
        );
        const recentBadge = recentTab?.querySelector('.chat-session-tab-group-badge-recent');
        expect(recentBadge?.textContent || '').toBe('Recent');

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

        expect(document.querySelector('#chatSessionTabs .chat-session-tab-group-badge')).toBeNull();
    });

    test('server preferences override stale browser state and UI actions sync back', async () => {
        localStorage.setItem(
            'von:pinnedChatSessionIds:#V#pin_user',
            JSON.stringify(['s1'])
        );
        global.fetch = jest.fn(async (url, options = {}) => {
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
                                last_message_at: new Date().toISOString(),
                                conversation_preference: {
                                    preference_present: true,
                                    hidden: false,
                                    pinned: false,
                                },
                            },
                            {
                                session_id: 's2',
                                session_name: 'Priority session',
                                last_message_at: new Date(Date.now() - 1000).toISOString(),
                                conversation_preference: {
                                    preference_present: true,
                                    hidden: false,
                                    pinned: true,
                                },
                            },
                        ],
                    })
                };
            }
            if (String(url) === '/von/api/session/conversation_preference') {
                return {
                    ok: true,
                    json: async () => ({ status: 'updated' }),
                };
            }
            return { ok: true, json: async () => ({}) };
        });

        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        expect(JSON.parse(
            localStorage.getItem('von:pinnedChatSessionIds:#V#pin_user') || '[]'
        )).toEqual(['s2']);

        const unpinButton = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="s2"] .chat-session-tab-pin-toggle'
        );
        unpinButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
        await Promise.resolve();
        await Promise.resolve();

        const preferenceCall = global.fetch.mock.calls.find(
            ([url]) => String(url) === '/von/api/session/conversation_preference'
        );
        expect(preferenceCall).toBeTruthy();
        expect(JSON.parse(preferenceCall[1].body)).toEqual({
            session_id: 's2',
            action: 'unpin',
        });
    });

    test('keeps compact history controls and reveals recent conversations in tranches', async () => {
        localStorage.setItem('chatHistoryRecentLimit', '2');
        localStorage.setItem('chatHistoryRecentWindowDays', '30');
        const sessions = Array.from({ length: 4 }, (_, index) => ({
            session_id: `recent-${index}`,
            session_name: `Recent ${index}`,
            last_message_at: new Date(Date.now() - index * 60_000).toISOString(),
            created_at: new Date(Date.now() - index * 60_000).toISOString(),
        }));
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
                        sessions,
                        recent_window_days: 30,
                        older_than_window_count: 37,
                    })
                };
            }
            return { ok: true, json: async () => ({}) };
        });

        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        const container = document.getElementById('chatSessionTabs');
        const controls = document.getElementById('chatSessionHistoryControls');
        expect(container.children[0].classList.contains('chat-session-tab-new')).toBe(true);
        expect(container.children[1].dataset.sessionId).toBeTruthy();
        expect(controls.children[0].classList.contains('chat-session-history-indicator')).toBe(true);
        expect(controls.children[0].textContent).toBe('[37 > 30d]');
        expect(controls.children[0].title).toContain('37 conversations are older than 30 days');
        expect(controls.children[1].classList.contains('chat-session-history-toggle')).toBe(true);
        expect(controls.children[1].textContent).toBe('+2');

        controls.children[1].click();

        expect(controls.querySelector('.chat-session-history-toggle')).toBeNull();
        expect(container.querySelectorAll('[data-session-id]')).toHaveLength(4);
        expect(container.children[1].dataset.sessionId).toBeTruthy();
    });

    test('Shift-click reveals every remaining recent conversation at once', async () => {
        localStorage.setItem('chatHistoryRecentLimit', '2');
        localStorage.setItem('chatHistoryRecentWindowDays', '30');
        const sessions = Array.from({ length: 7 }, (_, index) => ({
            session_id: `recent-${index}`,
            session_name: `Recent ${index}`,
            last_message_at: new Date(Date.now() - index * 60_000).toISOString(),
            created_at: new Date(Date.now() - index * 60_000).toISOString(),
        }));
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
                        sessions,
                        recent_window_days: 30,
                        older_than_window_count: 0,
                    })
                };
            }
            return { ok: true, json: async () => ({}) };
        });

        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        const container = document.getElementById('chatSessionTabs');
        const overflowButton = document.querySelector('.chat-session-history-toggle');
        expect(overflowButton.textContent).toBe('+5');
        expect(overflowButton.title).toContain('Shift-click to show all');

        overflowButton.dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }));

        expect(document.querySelector('.chat-session-history-toggle')).toBeNull();
        expect(container.querySelectorAll('[data-session-id]')).toHaveLength(7);
    });
});
