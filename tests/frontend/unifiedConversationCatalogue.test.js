jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), postJson: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/sessionScopedStorage.js', () => ({ getSessionScopedOrgId: jest.fn(() => '#V#lab') }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/messagePanel.js', () => ({
    openMessageExchange: jest.fn(), refreshOpenMessageExchange: jest.fn(), showMessageComposer: jest.fn(), resetMessagePanelContext: jest.fn()
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
