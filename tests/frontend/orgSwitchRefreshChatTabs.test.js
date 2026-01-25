/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#michael_witbrock', org_id: '#V#test_org' })),
    postJson: jest.fn(async () => ({ status: 'updated', namespace: '#V#michael_witbrock@test_org' })),
    getWindowSessionId: jest.fn(() => 'test-window-session-id')
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn()
}));

describe('org switch chat session refresh', () => {
    beforeEach(() => {
        document.body.innerHTML = '<div id="chatSessionTabs"></div><div id="chatSessionMetadata"></div>';
        global.fetch = jest.fn(async (url) => {
            if (String(url).startsWith('/von/api/session/context')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        user_id: '#V#michael_witbrock',
                        organisation_id: '#V#test_org',
                        namespace: '#V#michael_witbrock@test_org'
                    })
                };
            }
            if (String(url).startsWith('/von/history/sessions')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        sessions: [{ session_id: 's1', session_name: 'Session 1' }]
                    })
                };
            }
            return { ok: true, json: async () => ({}) };
        });
    });

    afterEach(() => {
        jest.resetModules();
        jest.restoreAllMocks();
    });

    test('refresh hook triggers chat session reload', async () => {
        require(chatTabModulePath);

        expect(typeof window.refreshChatSessionTabsForOrgSwitch).toBe('function');

        // handleOrgSwitchForChatTab is now async - await the returned promise
        await window.refreshChatSessionTabsForOrgSwitch();

        expect(global.fetch).toHaveBeenCalledWith(
            '/von/history/sessions?limit=50&summary=light',
            expect.any(Object)
        );
    });
});
