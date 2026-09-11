/** @jest-environment jsdom */

function jsonResponse(data, { ok = true, status = 200 } = {}) {
    return { ok, status, json: async () => data };
}

function conversationPayload({ results = [], nextCursor = null, accessibleCount = null, complete = true } = {}) {
    return {
        success: true,
        results,
        next_cursor: nextCursor,
        index_coverage: {
            accessible_window_count: accessibleCount ?? results.length,
            complete_for_accessible_window: complete,
            candidate_window_complete: complete,
            limitations: complete ? [] : ['accessible_conversation_window_incomplete']
        }
    };
}

function installSearchDom({ recovery = false } = {}) {
    document.body.dataset.activeTab = 'chatTab';
    document.body.innerHTML = `
        <div id="chatTab" class="active"></div>
        <input id="vontologySearchInput" />
        <div id="vontologySearchResults"></div>
        ${recovery ? `
            <button id="conversationRecoveryButton" hidden aria-pressed="false"></button>
            <span id="conversationRecoveryCount" hidden></span>
        ` : ''}
    `;
}

function loadSearchModule() {
    const vontology = require('../../src/frontend/web/von_interface/static/js/vontology.js');
    const { elements } = require('../../src/frontend/web/von_interface/static/js/domUtils.js');
    elements.vontologySearchInput = document.getElementById('vontologySearchInput');
    elements.vontologySearchResults = document.getElementById('vontologySearchResults');
    return vontology;
}

describe('unified global conversation and concept search', () => {
    beforeEach(() => {
        jest.resetModules();
        installSearchDom();
        localStorage.setItem('von_current_user', JSON.stringify({ concept_id: '#V#alice' }));
        sessionStorage.setItem('von_window_session_id', 'window-1');
        global.fetch = jest.fn();
    });

    afterEach(() => {
        jest.restoreAllMocks();
    });

    test('shows conversations first in the Conversations workspace, with concepts still available', async () => {
        global.fetch.mockImplementation((url) => {
            if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(jsonResponse({ conversations: [], has_more: false }));
            if (String(url).includes('/vontology/api/vontology/search')) {
                return Promise.resolve(jsonResponse({
                    results: [{ id: '#V#detector', name: 'Detector', kind: 'type' }]
                }));
            }
            if (String(url).includes('/conversation_search')) {
                return Promise.resolve(jsonResponse(conversationPayload({
                    results: [{
                        session_id: 's1',
                        display_name: 'Detector calibration',
                        last_message_at: '2026-08-23T10:00:00Z',
                        match: { snippet: '...old exact detector phrase...' }
                    }]
                })));
            }
            throw new Error(`Unexpected URL: ${url}`);
        });
        const vontology = loadSearchModule();

        await vontology.performVontologySearch('detector');

        const headings = [...document.querySelectorAll('.unified-search-group-heading')]
            .map(node => node.textContent);
        expect(headings).toEqual(['Conversations', 'Concepts']);
        expect(document.getElementById('vontologySearchResults').textContent).toContain('Detector calibration');
        expect(document.getElementById('vontologySearchResults').textContent).toContain('old exact detector phrase');
        expect(document.getElementById('vontologySearchResults').textContent).toContain('Detector');
        expect(vontology.__test_getUnifiedSearchState().items.map(item => item.searchType))
            .toEqual(['conversation', 'concept']);
        const conversationUrl = global.fetch.mock.calls
            .map(call => String(call[0]))
            .find(url => url.includes('/conversation_search'));
        expect(conversationUrl).toContain('match_mode=lexical');
    });

    test('one failed provider does not suppress results from the other', async () => {
        global.fetch.mockImplementation((url) => {
            if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(jsonResponse({ conversations: [], has_more: false }));
            if (String(url).includes('/vontology/api/vontology/search')) {
                return Promise.resolve(jsonResponse({}, { ok: false, status: 503 }));
            }
            return Promise.resolve(jsonResponse(conversationPayload({
                results: [{ session_id: 's1', session_name: 'Usable conversation', match: {} }]
            })));
        });
        const vontology = loadSearchModule();

        await vontology.performVontologySearch('usable');

        const text = document.getElementById('vontologySearchResults').textContent;
        expect(text).toContain('Usable conversation');
        expect(text).toContain('Concept search is temporarily unavailable.');
    });

    test('renders the fast provider while the other provider is still pending', async () => {
        let resolveConversation;
        global.fetch.mockImplementation((url) => {
            if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(jsonResponse({ conversations: [], has_more: false }));
            if (String(url).includes('/vontology/api/vontology/search')) {
                return Promise.resolve(jsonResponse({
                    results: [{ id: '#V#fast', name: 'Fast concept', kind: 'type' }]
                }));
            }
            return new Promise(resolve => { resolveConversation = resolve; });
        });
        const vontology = loadSearchModule();

        const pending = vontology.performVontologySearch('fast');
        await new Promise(resolve => setTimeout(resolve, 0));

        const interimText = document.getElementById('vontologySearchResults').textContent;
        expect(interimText).toContain('Fast concept');
        expect(interimText).toContain('Searching conversations');

        resolveConversation(jsonResponse(conversationPayload()));
        await pending;
    });

    test('passes the opaque cursor and appends more conversations', async () => {
        let conversationCall = 0;
        global.fetch.mockImplementation((url) => {
            if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(jsonResponse({ conversations: [], has_more: false }));
            const raw = String(url);
            if (raw.includes('/vontology/api/vontology/search')) {
                return Promise.resolve(jsonResponse({ results: [] }));
            }
            conversationCall += 1;
            if (conversationCall === 1) {
                return Promise.resolve(jsonResponse(conversationPayload({
                    results: [{ session_id: 's1', session_name: 'First result', match: {} }],
                    nextCursor: 'opaque-cursor'
                })));
            }
            expect(raw).toContain('cursor=opaque-cursor');
            return Promise.resolve(jsonResponse(conversationPayload({
                results: [{ session_id: 's2', session_name: 'Second result', match: {} }]
            })));
        });
        const vontology = loadSearchModule();
        await vontology.performVontologySearch('result');

        await vontology.__test_loadMoreConversationSearchResults();

        expect(vontology.__test_getUnifiedSearchState().conversations.items.map(row => row.session_id))
            .toEqual(['s1', 's2']);
        expect(document.getElementById('vontologySearchResults').textContent).toContain('Second result');
    });

    test('clearing the native search input immediately invalidates a late response', async () => {
        let resolveConversation;
        global.fetch.mockImplementation((url) => {
            if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(jsonResponse({ conversations: [], has_more: false }));
            if (String(url).includes('/vontology/api/vontology/search')) {
                return Promise.resolve(jsonResponse({ results: [] }));
            }
            return new Promise(resolve => { resolveConversation = resolve; });
        });
        const vontology = loadSearchModule();
        vontology.setupVontologySearchUI();
        const input = document.getElementById('vontologySearchInput');
        input.value = 'late';
        const pending = vontology.performVontologySearch('late');

        await new Promise(resolve => setTimeout(resolve, 0));
        input.value = '';
        input.dispatchEvent(new Event('search', { bubbles: true }));
        resolveConversation(jsonResponse(conversationPayload({
            results: [{ session_id: 'late', session_name: 'Late result', match: {} }]
        })));
        await pending;

        expect(vontology.__test_getUnifiedSearchState().items).toEqual([]);
        expect(document.getElementById('vontologySearchResults').textContent).toBe('');
        expect(document.getElementById('vontologySearchResults').classList.contains('open')).toBe(false);
    });

    test('selecting a conversation dispatches the canonical open event', async () => {
        global.fetch.mockImplementation((url) => {
            if (String(url).includes('/api/messages/catalogue')) return Promise.resolve(jsonResponse({ conversations: [], has_more: false }));
            if (String(url).includes('/vontology/api/vontology/search')) {
                return Promise.resolve(jsonResponse({ results: [] }));
            }
            return Promise.resolve(jsonResponse(conversationPayload({
                results: [{ session_id: 's1', session_name: 'Open me', match: {} }]
            })));
        });
        const vontology = loadSearchModule();
        const selected = [];
        document.addEventListener('von:open-conversation-search-result', event => selected.push(event.detail));
        await vontology.performVontologySearch('open');

        document.querySelector('.unified-search-conversation-main').click();

        expect(selected).toHaveLength(1);
        expect(selected[0].conversation).toMatchObject({ session_id: 's1', name: 'Open me' });
        expect(document.getElementById('vontologySearchResults').classList.contains('open')).toBe(false);
    });

    test('recovery count hides only after verified empty read-back', async () => {
        installSearchDom({ recovery: true });
        let restored = false;
        global.fetch.mockImplementation((url, options = {}) => {
            const raw = String(url);
            if (raw === '/von/api/session/delete_chat_session') {
                restored = true;
                return Promise.resolve(jsonResponse({
                    status: 'restored', session_id: 'trash-1', trashed: false, recoverable: true
                }));
            }
            if (raw.includes('/conversation_search')) {
                const results = restored
                    ? []
                    : [{ session_id: 'trash-1', session_name: 'Recover me', trashed: true, match: {} }];
                return Promise.resolve(jsonResponse(conversationPayload({
                    results,
                    accessibleCount: restored ? 0 : 1,
                    complete: true
                })));
            }
            throw new Error(`Unexpected URL: ${url} ${options.method || 'GET'}`);
        });
        const vontology = loadSearchModule();

        await vontology.__test_refreshConversationRecoveryCount();
        expect(document.getElementById('conversationRecoveryButton').hidden).toBe(false);
        expect(document.getElementById('conversationRecoveryCount').textContent).toBe('1');

        await vontology.__test_openRemovedConversations();
        expect(document.getElementById('vontologySearchResults').textContent).toContain('Removed conversations');
        document.querySelector('.unified-search-restore-button').click();
        await new Promise(resolve => setTimeout(resolve, 0));
        await new Promise(resolve => setTimeout(resolve, 0));

        expect(restored).toBe(true);
        expect(vontology.__test_getUnifiedSearchState().recoveryCountConfirmedZero).toBe(true);
        expect(document.getElementById('conversationRecoveryButton').hidden).toBe(true);
    });
});
