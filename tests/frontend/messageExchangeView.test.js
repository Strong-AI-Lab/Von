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
