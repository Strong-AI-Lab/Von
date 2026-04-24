/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#agent_filter_user', org_id: '#V#test_org' })),
    postJson: jest.fn(async () => ({ status: 'updated', namespace: '#V#agent_filter_user@test_org' })),
    getWindowSessionId: jest.fn(() => 'test-window-session-id'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#agent_filter_user'),
    renderSpanSuggestions: jest.fn()
}));

describe('chat session agent-created filtering', () => {
    beforeEach(() => {
        const now = Date.now();
        const humanTimestamp = new Date(now - 60 * 60 * 1000).toISOString();
        const newestAgentTimestamp = new Date(now - 2 * 60 * 60 * 1000).toISOString();
        const olderAgentTimestamp = new Date(now - 3 * 60 * 60 * 1000).toISOString();

        document.body.innerHTML = `
            <div id="chatSessionTabs"></div>
            <div id="chatSessionMetadata"></div>
            <div id="scrollableField"></div>
        `;

        localStorage.clear();

        global.fetch = jest.fn(async (url) => {
            const urlText = String(url);
            if (String(url).startsWith('/von/api/session/context')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        user_id: '#V#agent_filter_user',
                        organisation_id: '#V#test_org',
                        namespace: '#V#agent_filter_user@test_org',
                    })
                };
            }
            if (urlText.startsWith('/von/history/sessions')) {
                const showAgentCreated = urlText.includes('agent_visibility=include');
                const sessions = [
                    {
                        session_id: 'human-1',
                        session_name: 'Human conversation',
                        namespace: '#V#agent_filter_user@test_org',
                        last_message_at: humanTimestamp,
                        created_at: humanTimestamp,
                        message_count: 4,
                    },
                    {
                        session_id: 'agent-new',
                        session_name: 'Newest test conversation',
                        namespace: '#V#agent_filter_user@test_org',
                        last_message_at: newestAgentTimestamp,
                        created_at: newestAgentTimestamp,
                        message_count: 2,
                        origin_kind: 'browser_test_fixture',
                        is_agent_created: true,
                        test_artifact_kind: 'browser_test_fixture_chat_session',
                    },
                    ...(showAgentCreated ? [
                        {
                            session_id: 'agent-old',
                            session_name: 'Older test conversation',
                            namespace: '#V#agent_filter_user@test_org',
                            last_message_at: olderAgentTimestamp,
                            created_at: olderAgentTimestamp,
                            message_count: 2,
                            origin_kind: 'benchmark_harness',
                            is_agent_created: true,
                            test_artifact_kind: 'kb_clone_benchmark_chat_session',
                        },
                    ] : []),
                ];
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        active_session_id: null,
                        sessions,
                        agent_visibility_applied: true,
                        agent_visibility: showAgentCreated ? 'include' : 'exclude',
                        agent_created_session_total: 2,
                        hidden_agent_created_session_count: showAgentCreated ? 0 : 1,
                        newest_visible_agent_created_session_id: 'agent-new',
                        total_after_agent_visibility: showAgentCreated ? 3 : 2,
                        hidden_by_limit_count: 0,
                        raw_session_count: 3,
                        limit: 200,
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

    test('hides older agent-created conversations by default and toggles them from the new-chat menu', async () => {
        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        const initialIds = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tab[data-session-id]')
        ).map((el) => el.dataset.sessionId);
        expect(initialIds).toContain('human-1');
        expect(initialIds).toContain('agent-new');
        expect(initialIds).not.toContain('agent-old');

        const newestAgentTab = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="agent-new"]'
        );
        expect(newestAgentTab?.classList.contains('is-agent-created')).toBe(true);
        expect(newestAgentTab?.querySelector('.chat-session-agent-marker')?.textContent).toBe('von');
        expect(global.fetch).toHaveBeenCalledWith(
            expect.stringContaining('limit=200'),
            expect.any(Object)
        );
        expect(global.fetch).toHaveBeenCalledWith(
            expect.stringContaining('agent_visibility=exclude'),
            expect.any(Object)
        );

        const newChatButton = document.querySelector(
            '#chatSessionTabs .chat-session-tab-new'
        );
        newChatButton.dispatchEvent(new MouseEvent('contextmenu', {
            bubbles: true,
            clientX: 10,
            clientY: 10
        }));

        const menuButtons = Array.from(document.querySelectorAll('.chat-session-menu button'));
        const showTestsButton = menuButtons.find((button) => (
            button.textContent || ''
        ).startsWith('Show test conversations'));
        expect(showTestsButton).toBeTruthy();
        showTestsButton.click();

        const storageKey = 'von:showAgentCreatedChatSessions:#V#agent_filter_user';
        expect(localStorage.getItem(storageKey)).toBe('true');
        await new Promise((resolve) => setTimeout(resolve, 0));

        const toggledIds = Array.from(
            document.querySelectorAll('#chatSessionTabs .chat-session-tab[data-session-id]')
        ).map((el) => el.dataset.sessionId);
        expect(toggledIds).toContain('human-1');
        expect(toggledIds).toContain('agent-new');
        expect(toggledIds).toContain('agent-old');
        expect(global.fetch).toHaveBeenCalledWith(
            expect.stringContaining('agent_visibility=include'),
            expect.any(Object)
        );
    });

    test('copies a structured conversation reference from the tab context menu', async () => {
        const writeText = jest.fn().mockResolvedValue(undefined);
        Object.assign(navigator, {
            clipboard: { writeText }
        });

        require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        const humanTab = document.querySelector(
            '#chatSessionTabs .chat-session-tab[data-session-id="human-1"]'
        );
        humanTab.dispatchEvent(new MouseEvent('contextmenu', {
            bubbles: true,
            clientX: 20,
            clientY: 20
        }));

        const menuButtons = Array.from(document.querySelectorAll('.chat-session-menu button'));
        const copyButton = menuButtons.find((button) => (
            button.textContent || ''
        ) === 'Copy conversation reference');
        expect(copyButton).toBeTruthy();
        copyButton.click();
        await Promise.resolve();

        expect(writeText).toHaveBeenCalledTimes(1);
        const payload = JSON.parse(writeText.mock.calls[0][0]);
        expect(payload).toEqual(expect.objectContaining({
            kind: 'von_conversation_ref',
            conversation_ref: expect.objectContaining({
                session_id: 'human-1',
                user_concept_id: '#V#agent_filter_user',
                namespace: '#V#agent_filter_user@test_org',
                include_legacy: false,
            }),
            chat_history_lookup: expect.objectContaining({
                user_id: '#V#agent_filter_user',
                session_id: 'human-1',
                namespace: '#V#agent_filter_user@test_org',
            }),
        }));
    });
});
