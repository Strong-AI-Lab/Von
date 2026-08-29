/** @jest-environment jsdom */

const chatTabModulePath = '../../src/frontend/web/von_interface/static/js/chatTab.js';

jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    annotateTurn: jest.fn(),
    fetchWithTimeout: jest.fn(),
    getJsonDetailed: jest.fn(),
    getUserContext: jest.fn(() => ({ user_id: '#V#michael_witbrock', org_id: '#V#test_org' })),
    postJson: jest.fn(async () => ({ status: 'updated', namespace: '#V#michael_witbrock@test_org' })),
    getWindowSessionId: jest.fn(() => 'test-window-session-id'),
    WINDOW_SESSION_HEADER: 'X-Window-Session-ID'
}));

jest.mock('../../src/frontend/web/von_interface/static/js/domUtils.js', () => ({
    elements: {},
    renderSpanSuggestions: jest.fn(),
    getCurrentUserConceptId: jest.fn(() => null)
}));

function jsonResponse(data, { ok = true, status = 200 } = {}) {
    return { ok, status, json: async () => data };
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((resolvePromise, rejectPromise) => {
        resolve = resolvePromise;
        reject = rejectPromise;
    });
    return { promise, resolve, reject };
}

async function flushPromises() {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('org switch conversation isolation', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
        document.body.innerHTML = `
            <div id="chatSessionTabs"></div>
            <div id="chatSessionMetadata"></div>
            <div id="chatSessionFocusChips"></div>
            <div id="scrollableField"></div>
            <div id="incomingInvitesList"></div>
            <span id="incomingInvitesBadge" class="hidden"></span>
            <button id="incomingInvitesBtn"></button>
            <div id="incomingInvitesStatus"></div>
        `;
    });

    afterEach(() => {
        jest.restoreAllMocks();
        delete global.fetch;
    });

    test('refresh hook triggers chat session reload', async () => {
        global.fetch = jest.fn(async (url) => {
            if (String(url).startsWith('/von/api/session/context')) {
                return jsonResponse({
                    authenticated: true,
                    user_id: '#V#michael_witbrock',
                    organisation_id: '#V#test_org',
                    namespace: '#V#michael_witbrock@test_org'
                });
            }
            if (String(url).startsWith('/von/history/sessions')) {
                return jsonResponse({
                    authenticated: true,
                    sessions: [{ session_id: 's1', session_name: 'Session 1' }]
                });
            }
            return jsonResponse({});
        });
        require(chatTabModulePath);

        expect(typeof window.refreshChatSessionTabsForOrgSwitch).toBe('function');
        await window.refreshChatSessionTabsForOrgSwitch();

        expect(global.fetch).toHaveBeenCalledWith(
            '/von/history/sessions?limit=200&summary=light&agent_visibility=exclude&keep_newest_agent_created=true&recent_window_days=30',
            expect.objectContaining({ signal: expect.any(AbortSignal) })
        );
    });

    test('does not render a history response from the organisation being left', async () => {
        const historyResponse = deferred();
        global.fetch = jest.fn((url) => {
            if (String(url).startsWith('/von/api/session/context')) {
                return Promise.resolve(jsonResponse({
                    user_id: '#V#michael_witbrock',
                    organisation_id: '#V#test_org'
                }));
            }
            if (String(url).startsWith('/von/history?')) {
                return historyResponse.promise;
            }
            return Promise.resolve(jsonResponse({}));
        });
        const chatTab = require(chatTabModulePath);
        chatTab.__testOnly_setActiveChatSession('old-session', 'Old conversation');

        const historyLoad = chatTab.__testOnly_loadChatHistory();
        await flushPromises();
        chatTab.__testOnly_startOrganisationSwitchForChatTab({ switch_id: 'switch-new-org' });
        historyResponse.resolve(jsonResponse({
            history: [
                { role: 'user', content: 'Hi' },
                { role: 'assistant', content: 'Yunli-owned response' }
            ],
            segments_returned: 1,
            total_segments: 1
        }));

        await expect(historyLoad).resolves.toBe(false);
        expect(document.getElementById('scrollableField').textContent).toBe('Loading conversations...');
        expect(document.getElementById('scrollableField').textContent).not.toContain('Yunli');
        expect(chatTab.__testOnly_getConversationTranscriptTurnsSnapshot()).toEqual([]);
    });

    test('does not adopt a conversation created just before switch start', async () => {
        const createResponse = deferred();
        global.fetch = jest.fn((url) => {
            if (String(url).startsWith('/von/api/session/create_chat_session')) {
                return createResponse.promise;
            }
            return Promise.resolve(jsonResponse({}));
        });
        const chatTab = require(chatTabModulePath);

        const creation = chatTab.__testOnly_createChatSession('Old organisation draft');
        await flushPromises();
        chatTab.__testOnly_startOrganisationSwitchForChatTab({ switch_id: 'switch-new-org' });
        createResponse.resolve(jsonResponse({
            session_id: 'old-created-session',
            session_name: 'Old organisation draft',
            history: [{ role: 'user', content: 'Hi' }]
        }));

        await expect(creation).rejects.toMatchObject({
            organisationSwitchSuperseded: true
        });
        expect(document.querySelector('[data-session-id="old-created-session"]')).toBeNull();
        expect(document.getElementById('scrollableField').textContent).toBe('Loading conversations...');
    });

    test('rejects a late old-org session list and loads only the accepted org list', async () => {
        const oldSessionsResponse = deferred();
        let sessionRequestCount = 0;
        global.fetch = jest.fn((url) => {
            if (String(url).startsWith('/von/api/session/context')) {
                return Promise.resolve(jsonResponse({
                    user_id: '#V#michael_witbrock',
                    organisation_id: '#V#test_org'
                }));
            }
            if (String(url).startsWith('/von/history/sessions')) {
                sessionRequestCount += 1;
                if (sessionRequestCount === 1) return oldSessionsResponse.promise;
                return Promise.resolve(jsonResponse({
                    authenticated: true,
                    sessions: [{ session_id: 'new-session', session_name: 'SAIL conversation' }]
                }));
            }
            return Promise.resolve(jsonResponse({ history: [], session_links: {} }));
        });
        const chatTab = require(chatTabModulePath);

        const staleRefresh = chatTab.__testOnly_refreshChatSessionTabs();
        await flushPromises();
        chatTab.__testOnly_startOrganisationSwitchForChatTab({ switch_id: 'switch-sail' });
        oldSessionsResponse.resolve(jsonResponse({
            authenticated: true,
            sessions: [{ session_id: 'old-session', session_name: 'Yunli conversation' }]
        }));
        await staleRefresh;

        expect(document.body.textContent).not.toContain('Yunli conversation');
        await chatTab.__testOnly_handleOrgSwitchForChatTab({ switch_id: 'switch-sail' });

        expect(document.body.textContent).toContain('SAIL conversation');
        expect(document.body.textContent).not.toContain('Yunli conversation');
    });

    test('does not merge accepted invitations returned after switch start', async () => {
        const pendingInvitesResponse = deferred();
        const acceptedInvitesResponse = deferred();
        global.fetch = jest.fn((url) => {
            const requestUrl = String(url);
            if (requestUrl.includes('/invites?status=pending')) return pendingInvitesResponse.promise;
            if (requestUrl.includes('/invites?status=accepted')) return acceptedInvitesResponse.promise;
            if (requestUrl.startsWith('/von/api/session/context')) {
                return Promise.resolve(jsonResponse({
                    user_id: '#V#michael_witbrock',
                    organisation_id: '#V#test_org'
                }));
            }
            if (requestUrl.startsWith('/von/history/sessions')) {
                return Promise.resolve(jsonResponse({
                    authenticated: true,
                    sessions: [{ session_id: 'new-session', session_name: 'SAIL conversation' }]
                }));
            }
            return Promise.resolve(jsonResponse({ history: [], session_links: {} }));
        });
        const chatTab = require(chatTabModulePath);

        const inviteLoad = chatTab.__testOnly_loadIncomingInvites({ silent: true });
        await flushPromises();
        chatTab.__testOnly_startOrganisationSwitchForChatTab({ switch_id: 'switch-sail' });
        pendingInvitesResponse.resolve(jsonResponse({ invites: [], total_count: 0 }));
        acceptedInvitesResponse.resolve(jsonResponse({
            invites: [{
                invite_id: 'old-invite',
                session_id: 'old-shared-session',
                inviter_user_id: '#V#yunli'
            }]
        }));
        await inviteLoad;
        await chatTab.__testOnly_handleOrgSwitchForChatTab({ switch_id: 'switch-sail' });

        expect(document.body.textContent).toContain('SAIL conversation');
        expect(document.querySelector('[data-session-id="old-shared-session"]')).toBeNull();
    });

    test('ignores an older completion during rapid switching and recovers after latest failure', async () => {
        let sessionRequestCount = 0;
        global.fetch = jest.fn((url) => {
            const requestUrl = String(url);
            if (requestUrl.startsWith('/von/api/session/context')) {
                return Promise.resolve(jsonResponse({
                    user_id: '#V#michael_witbrock',
                    organisation_id: '#V#test_org'
                }));
            }
            if (requestUrl.startsWith('/von/history/sessions')) {
                sessionRequestCount += 1;
                return Promise.resolve(jsonResponse({
                    authenticated: true,
                    sessions: [{ session_id: 'recovered-session', session_name: 'Recovered conversation' }]
                }));
            }
            return Promise.resolve(jsonResponse({ history: [], session_links: {} }));
        });
        const chatTab = require(chatTabModulePath);

        chatTab.__testOnly_startOrganisationSwitchForChatTab({ switch_id: 'switch-one' });
        chatTab.__testOnly_startOrganisationSwitchForChatTab({ switch_id: 'switch-two' });
        await expect(chatTab.__testOnly_handleOrgSwitchForChatTab({ switch_id: 'switch-one' }))
            .resolves.toBe(false);
        expect(sessionRequestCount).toBe(0);
        expect(chatTab.__testOnly_getChatOrganisationTransitionState().pendingSwitchId).toBe('switch-two');

        await expect(chatTab.__testOnly_handleOrgSwitchFailureForChatTab({ switch_id: 'switch-two' }))
            .resolves.toBe(true);
        expect(sessionRequestCount).toBe(1);
        expect(document.body.textContent).toContain('Recovered conversation');
        expect(chatTab.__testOnly_getChatOrganisationTransitionState().pendingSwitchId).toBeNull();
    });
});
