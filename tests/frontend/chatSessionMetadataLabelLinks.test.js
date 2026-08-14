/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#test_user', org_id: '#V#test_org' })),
    getWindowSessionId: jest.fn(() => 'test-window-session-id'),
    WINDOW_SESSION_HEADER: 'X-Von-Window-Session'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    getCurrentUserConceptId: jest.fn(() => '#V#test_user'),
    renderSpanSuggestions: jest.fn()
}));

describe('chat session metadata label links', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        document.body.innerHTML = '<div id="chatSessionMetadata"></div>';
    });

    afterEach(() => {
        jest.restoreAllMocks();
        localStorage.clear();
        delete global.fetch;
    });

    test('Clicking group labels opens the corresponding type concept tab', () => {
        const { __test_only__renderChatSessionMetadataPanel } = require(chatTabModulePath);

        const seen = [];
        const handler = (e) => {
            seen.push(e?.detail);
        };
        document.addEventListener('von:selectConceptById', handler);

        __test_only__renderChatSessionMetadataPanel({
            sessionId: 's1',
            links: {},
            statusText: null,
            statusTone: null,
            disabled: false
        });

        const clickLabel = (labelText) => {
            const el = Array.from(document.querySelectorAll('.chat-session-metadata-label-link'))
                .find((node) => (node.textContent || '').trim() === labelText);
            expect(el).toBeTruthy();
            el.click();
        };

        clickLabel('Programmes');
        clickLabel('Projects');
        clickLabel('Activities');
        clickLabel('Modalities');

        expect(seen).toEqual([
            { conceptId: '#V#programme', createConceptTab: true },
            { conceptId: '#V#project', createConceptTab: true },
            { conceptId: '#V#work_activity', createConceptTab: true },
            { conceptId: '#V#conversation_modality', createConceptTab: true }
        ]);

        document.removeEventListener('von:selectConceptById', handler);
    });

    test('Shows current conversation title above Conversation context', async () => {
        const longName = 'Represent SAIL architecture and long-term integration roadmap with workflow checks';
        document.body.innerHTML = '<div id="chatSessionTabs"></div><div id="chatSessionMetadata"></div><div id="scrollableField"></div>';

        global.fetch = jest.fn(async (url) => {
            if (String(url).startsWith('/von/api/session/context')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        user_id: '#V#test_user',
                        organisation_id: '#V#test_org',
                        namespace: '#V#test_user@test_org'
                    })
                };
            }
            if (String(url).startsWith('/von/history/sessions')) {
                return {
                    ok: true,
                    json: async () => ({
                        authenticated: true,
                        active_session_id: 's-long',
                        sessions: [
                            { session_id: 's-long', session_name: longName }
                        ]
                    })
                };
            }
            if (String(url).startsWith('/von/api/session/chat_session_links')) {
                return {
                    ok: true,
                    json: async () => ({ session_links: {} })
                };
            }
            return { ok: true, json: async () => ({}) };
        });

        const chatTab = require(chatTabModulePath);
        await window.refreshChatSessionTabsForOrgSwitch();

        chatTab.__test_only__renderChatSessionMetadataPanel({
            sessionId: 's-long',
            links: {},
            statusText: null,
            statusTone: null,
            disabled: false
        });

        const metadataEl = document.getElementById('chatSessionMetadata');
        const titleEl = metadataEl.querySelector('.chat-session-current-title');
        expect(titleEl).toBeTruthy();
        expect((titleEl.textContent || '').trim()).toBe(longName);

        const header = metadataEl.querySelector('.chat-session-metadata-header');
        expect(header).toBeTruthy();
        expect(header.previousElementSibling).toBe(titleEl);
    });

    test('Defaults conversation context to furled when no preference is stored', () => {
        const { __test_only__renderChatSessionMetadataPanel } = require(chatTabModulePath);

        expect(localStorage.getItem('von:chatSessionMetadataCollapsed')).toBeNull();
        __test_only__renderChatSessionMetadataPanel({
            sessionId: 's-default-furled',
            links: {},
            statusText: null,
            statusTone: null,
            disabled: false
        });

        const metadataEl = document.getElementById('chatSessionMetadata');
        const toggle = metadataEl.querySelector('.chat-session-metadata-toggle');
        const body = metadataEl.querySelector('.chat-session-metadata-body');

        expect(toggle).toBeTruthy();
        expect(toggle.getAttribute('aria-expanded')).toBe('false');
        expect(toggle.title).toBe('Show conversation context');
        expect(body).toBeTruthy();
        expect(body.classList).toContain('is-collapsed');
    });

    test('Honours a stored expanded conversation-context preference', () => {
        localStorage.setItem('von:chatSessionMetadataCollapsed', 'false');
        const { __test_only__renderChatSessionMetadataPanel } = require(chatTabModulePath);

        __test_only__renderChatSessionMetadataPanel({
            sessionId: 's-stored-expanded',
            links: {},
            statusText: null,
            statusTone: null,
            disabled: false
        });

        const metadataEl = document.getElementById('chatSessionMetadata');
        const toggle = metadataEl.querySelector('.chat-session-metadata-toggle');
        const body = metadataEl.querySelector('.chat-session-metadata-body');

        expect(toggle).toBeTruthy();
        expect(toggle.getAttribute('aria-expanded')).toBe('true');
        expect(toggle.title).toBe('Hide conversation context');
        expect(body).toBeTruthy();
        expect(body.classList).not.toContain('is-collapsed');
    });
});
