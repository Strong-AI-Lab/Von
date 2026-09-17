jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), postJson: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/sessionScopedStorage.js', () => ({ getSessionScopedOrgId: jest.fn(() => '#V#lab') }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/messagePanel.js', () => ({
    openMessageExchange: jest.fn(), refreshOpenMessageExchange: jest.fn(), showMessageComposer: jest.fn(), resetMessagePanelContext: jest.fn(), captureMessageExchangeUnreadRefresh: jest.fn(() => jest.fn())
}));

const api = require('../../src/frontend/web/von_interface/static/js/apiService.js');
const scope = require('../../src/frontend/web/von_interface/static/js/utils/sessionScopedStorage.js');
const catalogue = require('../../src/frontend/web/von_interface/static/js/components/conversationCatalogue.js');
const { selectConversationHistorySessions } = require('../../src/frontend/web/von_interface/static/js/utils/conversationHistoryPreferences.js');

function exchange(id, date, unread = 0) {
    return { session_id: id, source_kind: 'message_exchange', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#bob'], other_participant_ids: ['#V#bob'], session_name: 'Bob', last_message_at: date, shared_unread_count: unread };
}

beforeEach(() => {
    catalogue.resetConversationCatalogue();
    jest.clearAllMocks();
    scope.getSessionScopedOrgId.mockReturnValue('#V#lab');
    document.body.innerHTML = '<div id="conversationWorkspace"><textarea id="draft">Unsaved research question</textarea></div>';
    api.postJson.mockResolvedValue({ profiles: [{ concept_id: '#V#bob', display_name: 'Bob' }] });
});

test('an incoming message promotes its existing row without changing selection or a chat draft', async () => {
    api.getJson.mockResolvedValue({ conversations: [exchange('messages:bob', '2026-09-10T12:00:00Z')] });
    await catalogue.refreshMessageCatalogue();
    api.getJson.mockResolvedValue({ conversations: [exchange('messages:bob', '2026-09-11T12:00:00Z', 1)] });
    await catalogue.refreshMessageCatalogue();
    const rows = selectConversationHistorySessions({ sessions: [
        { session_id: 'chat', last_message_at: '2026-09-11T11:00:00Z' }, ...catalogue.directConversationRows()
    ], nowMs: Date.parse('2026-09-11T13:00:00Z') }).sessionsToRender;
    expect(rows.map(row => row.session_id)).toEqual(['messages:bob', 'chat']);
    expect(catalogue.activeMessageConversationId()).toBeNull();
    expect(document.getElementById('draft').value).toBe('Unsaved research question');
});

test('a background normal answer promotes the same conversation ahead of a message exchange', () => {
    const normal = { session_id: 'research', last_message_at: '2026-09-11T12:01:00Z' };
    const rows = selectConversationHistorySessions({ sessions: [exchange('messages:bob', '2026-09-11T12:00:00Z'), normal], accessTimestampBySessionId: { 'messages:bob': '2026-09-11T13:00:00Z' }, nowMs: Date.parse('2026-09-11T13:00:00Z') }).sessionsToRender;
    expect(rows[0]).toBe(normal);
});

test('late organisation responses cannot repopulate the catalogue', async () => {
    let finish;
    api.getJson.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    const pending = catalogue.refreshMessageCatalogue();
    scope.getSessionScopedOrgId.mockReturnValue('#V#other');
    catalogue.resetConversationCatalogue();
    finish({ conversations: [exchange('messages:private', '2026-09-11T12:00:00Z')] });
    await pending;
    expect(catalogue.directConversationRows()).toEqual([]);
});

test('profile lookup failure leaves the conversation available', async () => {
    api.getJson.mockResolvedValue({ conversations: [exchange('messages:bob', '2026-09-11T12:00:00Z')] });
    api.postJson.mockRejectedValue(new Error('Profile service offline'));
    await catalogue.refreshMessageCatalogue();
    expect(catalogue.directConversationRows()).toHaveLength(1);
});

test('unread exchanges survive the recency cutoff and equal timestamps have stable order', () => {
    const old = exchange('messages:old', '2020-01-01T00:00:00Z', 1);
    const rows = selectConversationHistorySessions({ sessions: [
        { session_id: 'b', last_message_at: '2026-09-11T12:00:00Z' }, old,
        { session_id: 'a', last_message_at: '2026-09-11T12:00:00Z' }
    ], nowMs: Date.parse('2026-09-11T13:00:00Z') }).sessionsToRender;
    expect(rows.map(row => row.session_id)).toEqual(['a', 'b', 'messages:old']);
});

async function mountSearch() {
    document.body.insertAdjacentHTML('beforeend', '<div class="conversation-tray-header"></div>');
    api.getJson.mockResolvedValue({ conversations: [] });
    catalogue.initialiseConversationCatalogue({ render: jest.fn() });
    await Promise.resolve(); await Promise.resolve();
    return document.querySelector('input[type="search"]');
}

function enterQuery(input, value) {
    input.value = value;
    input.dispatchEvent(new Event('input'));
}

describe('catalogue content search', () => {
    beforeEach(() => jest.useFakeTimers());
    afterEach(() => { jest.clearAllTimers(); jest.useRealTimers(); });

    test('title and participant matches precede semantic-only results, including old unloaded chats', async () => {
        const input = await mountSearch();
        enterQuery(input, 'cooling buildings');
        api.getJson.mockResolvedValue({ success: true, coverage_complete: true, results: [
            { session_id: 'topic', session_name: 'Design', last_message_at: '2020-01-01', match: { score: 900, fields: ['semantic_content'], snippet: 'Passive ventilation reduces indoor temperatures.' } },
            { session_id: 'title', session_name: 'Cooling buildings', match: { score: 1, fields: ['display_name', 'semantic_content'] } }
        ] });
        await catalogue.searchCatalogueContent();
        const result = catalogue.filterCatalogueRows([
            { session_id: 'title', session_name: 'Cooling buildings' },
            { session_id: 'person', participant_ids: ['#V#cooling_buildings_team'], session_name: 'Team' },
            { session_id: 'irrelevant', session_name: 'Shopping' }
        ]);
        expect(result.map(row => row.session_id)).toEqual(['title', 'person', 'topic']);
        expect(result[2].match.snippet).toContain('ventilation');
        expect(api.getJson.mock.calls.at(-1)[0]).toContain('match_mode=hybrid');
        enterQuery(input, '');
        expect(catalogue.filterCatalogueRows([])).toEqual([]);
    });

    test('typing is debounced and an obsolete response cannot enter the new query', async () => {
        const input = await mountSearch();
        api.getJson.mockClear();
        enterQuery(input, 'first');
        jest.advanceTimersByTime(200);
        expect(api.getJson).not.toHaveBeenCalled();
        let finish;
        api.getJson.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
        jest.advanceTimersByTime(100);
        expect(api.getJson).toHaveBeenCalledTimes(1);
        enterQuery(input, 'second');
        finish({ success: true, results: [{ session_id: 'obsolete', session_name: 'first' }] });
        await Promise.resolve(); await Promise.resolve();
        expect(catalogue.filterCatalogueRows([])).toEqual([]);
    });

    test('semantic failure retains participant matching and exposes retry', async () => {
        const input = await mountSearch();
        enterQuery(input, 'bob');
        api.getJson.mockRejectedValue(new Error('Offline'));
        await catalogue.searchCatalogueContent();
        expect(catalogue.filterCatalogueRows([exchange('bob', '2026-09-13')])).toHaveLength(1);
        const controls = document.createElement('div');
        catalogue.mountCatalogueControls(controls);
        expect(controls.textContent).toContain('Retry search');
    });
});


test('imports require explicit display and the toggle reaches list and content search', async () => {
    jest.useFakeTimers();
    const input = await mountSearch();
    const native = { session_id: 'native', session_name: 'Research' };
    const imported = { session_id: 'imported', session_name: 'Research', origin_kind: 'external_conversation_import' };
    const button = [...document.querySelectorAll('button')].find(b => b.textContent === 'Show imported');
    expect(button.getAttribute('aria-pressed')).toBe('false');
    expect(catalogue.filterCatalogueRows([imported, native])).toEqual([native]);
    enterQuery(input, 'Research');
    api.getJson.mockResolvedValue({ success: true, conversations: [native, imported], results: [native, imported] });
    button.click();
    await Promise.resolve(); await Promise.resolve();
    expect(button.getAttribute('aria-pressed')).toBe('true');
    expect(input.value).toBe('Research');
    expect(catalogue.filterCatalogueRows([imported, native])).toHaveLength(2);
    expect(api.getJson.mock.calls.some(([url]) => url.includes('conversation-catalogue') && url.includes('include_imported=true'))).toBe(true);
    expect(api.getJson.mock.calls.some(([url]) => url.includes('conversation_search') && url.includes('include_imported=true'))).toBe(true);
    button.click();
    expect(catalogue.filterCatalogueRows([imported, native])).toEqual([native]);
    jest.clearAllTimers(); jest.useRealTimers();
});
