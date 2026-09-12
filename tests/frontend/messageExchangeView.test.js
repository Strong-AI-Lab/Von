jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({ getJson: jest.fn(), postJson: jest.fn(), postJsonDetailed: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/sessionScopedStorage.js', () => ({ getSessionScopedOrgId: () => '#V#lab' }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/conversationCatalogue.js', () => ({ selectMessageConversation: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/utils/selectConceptByIdHandler.js', () => ({ hydrateConceptCartouchesInRoot: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/components/participantProfile.js', () => ({ profileButton: () => global.document.createElement('button'), participantAvatar: () => global.document.createElement('span') }));

const base = '../../src/frontend/web/von_interface/static/js/';
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
function row(other) { return { session_id: `messages:${other}`, source_kind: 'message_exchange', viewer_id: '#V#alice', participant_ids: ['#V#alice', other], other_participant_ids: [other], session_name: other, organisation_concept_id: '#V#lab' }; }
function response(id = 'one') { return { current_user_id: '#V#alice', messages: [{ concept_id: id, created_at: '2026-09-11T12:00:00Z', concept_data: { content_fallback: id }, relationships: { '#V#has_sender': ['#V#bob'] } }] }; }

beforeEach(() => {
    jest.resetModules();
    localStorage.clear(); sessionStorage.clear();
    window.scrollTo = jest.fn();
    document.body.innerHTML = '<div id="conversationWorkspace" class="show-message-exchange"><div id="messagesContainer"></div></div>';
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValue(response()); api.getJson.mockResolvedValue({});
});

test('late opening cannot overwrite the newly selected destination', async () => {
    const panel = require(base + 'components/messagePanel.js');
    const api = require(base + 'apiService.js');
    let finish;
    api.postJson.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const old = panel.openMessageExchange(row('#V#bob'));
    await panel.openMessageExchange(row('#V#carol'));
    finish(response()); await old;
    expect(document.getElementById('conversationTitle').textContent).toBe('#V#carol');
    expect(document.querySelector('.message-reply-destination').textContent).toContain('#V#carol');
});

test('drafts stay with their exact exchange, and group read-only state does not stick to a pair', async () => {
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    document.getElementById('messageInput').value = 'Bob draft';
    const group = row('#V#carol'); group.participant_ids.push('#V#dave'); group.other_participant_ids.push('#V#dave');
    await panel.openMessageExchange(group);
    expect(document.getElementById('messageInput').value).toBe('');
    expect(document.getElementById('sendMessageBtn').disabled).toBe(true);
    await panel.openMessageExchange(row('#V#bob'));
    expect(document.getElementById('messageInput').value).toBe('Bob draft');
    expect(document.getElementById('sendMessageBtn').disabled).toBe(false);
});

test('a new-message receipt opens its canonical exchange and preserves the previous draft', async () => {
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    document.getElementById('messageInput').value = 'Keep Bob draft';
    await panel.showMessageComposer();
    document.getElementById('newMessageRecipient').value = '#V#carol';
    document.getElementById('newMessageContent').value = 'Hello Carol';
    require(base + 'apiService.js').postJsonDetailed.mockResolvedValue({ data: { success: true, conversation: row('#V#carol') } });
    document.getElementById('sendNewMessage').click(); await flush();
    expect(require(base + 'components/conversationCatalogue.js').selectMessageConversation).toHaveBeenCalledWith(row('#V#carol'));
    expect(document.getElementById('messageInput').value).toBe('Keep Bob draft');
});

test('a refresh reconciles deleted content instead of retaining it forever', async () => {
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    expect(document.querySelector('[data-contribution-id="one"]')).not.toBeNull();
    require(base + 'apiService.js').postJson.mockResolvedValue({ current_user_id: '#V#alice', messages: [] });
    await panel.refreshOpenMessageExchange();
    expect(document.querySelector('[data-contribution-id="one"]')).toBeNull();
});


test('cancelling a new message restores the ordinary conversation pane', async () => {
    document.getElementById('conversationWorkspace').classList.remove('show-message-exchange');
    const panel = require(base + 'components/messagePanel.js');
    await panel.showMessageComposer();
    expect(document.getElementById('conversationWorkspace').classList.contains('show-message-exchange')).toBe(true);
    document.getElementById('cancelNewMessage').click();
    expect(document.getElementById('conversationWorkspace').classList.contains('show-message-exchange')).toBe(false);
});

function unreadResponse() {
    const result = response();
    result.messages[0].relationships['#V#has_recipient'] = ['#V#alice'];
    result.messages[0].concept_data.read_by = [];
    return result;
}
function mockReadObserver() {
    const observers = [];
    global.IntersectionObserver = jest.fn(function (callback) {
        this.callback = callback;
        this.observe = jest.fn(); this.unobserve = jest.fn(); this.disconnect = jest.fn();
        observers.push(this);
    });
    return observers;
}
function visibleEntry() {
    document.getElementById('messageViewContent').getClientRects = () => [{}];
    return { target: document.querySelector('[data-contribution-id]'), isIntersecting: true, intersectionRatio: 0.5 };
}
afterEach(() => { delete global.IntersectionObserver; });

test('only visible incoming unread messages are marked, with confirmed labels and a catalogue refresh', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(unreadResponse());
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    expect(document.querySelector('.message-unread-label').textContent).toBe('Unread');
    expect(api.postJson).toHaveBeenCalledTimes(1);
    const entry = visibleEntry();
    observers[0].callback([{ ...entry, isIntersecting: false, intersectionRatio: 0 }]);
    expect(api.postJson).toHaveBeenCalledTimes(1);
    api.postJson.mockResolvedValueOnce({ success: true, updated_count: 1 });
    const refresh = jest.fn(); document.addEventListener('von:conversation-contribution', refresh, { once: true });
    observers[0].callback([entry]); await flush();
    expect(api.postJson).toHaveBeenLastCalledWith('/api/messages/read/bulk', { message_ids: ['one'] });
    expect(document.querySelector('.message-unread-label')).toBeNull();
    expect(refresh).toHaveBeenCalledTimes(1);
});

test('a failed visible read stays labelled and same-content refresh re-arms the observer', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(unreadResponse());
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    api.postJson.mockRejectedValueOnce(new Error('offline'));
    observers[0].callback([visibleEntry()]); await flush();
    expect(document.querySelector('.message-unread-label')).not.toBeNull();
    expect(document.querySelector('.message-read-status').textContent).toContain('Retry');
    api.postJson.mockResolvedValueOnce(unreadResponse());
    await panel.refreshOpenMessageExchange();
    expect(observers).toHaveLength(2);
    expect(observers[1].observe).toHaveBeenCalledTimes(1);
});

test('partial update receipts do not optimistically clear unread contributions', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(unreadResponse());
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    api.postJson.mockResolvedValueOnce({ success: true, updated_count: 0 }).mockResolvedValueOnce(unreadResponse());
    observers[0].callback([visibleEntry()]); await flush();
    expect(document.querySelector('.message-unread-label')).not.toBeNull();
    expect(document.querySelector('.message-read-status')).not.toBeNull();
    expect(observers).toHaveLength(1); // No immediate retry loop on scope failure.
});

test('queued observers from a previous selection and hidden documents cannot mark messages', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValue(unreadResponse());
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    const entry = visibleEntry();
    Object.defineProperty(document, 'hidden', { configurable: true, value: true });
    observers[0].callback([entry]);
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    await panel.openMessageExchange(row('#V#carol'));
    observers[0].callback([visibleEntry()]);
    expect(api.postJson.mock.calls.filter(([url]) => url === '/api/messages/read/bulk')).toHaveLength(0);
});
