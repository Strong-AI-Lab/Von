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

test('scrolling away exposes the shared latest control without waiting for new messages or writing read state', async () => {
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    const content = document.getElementById('messageViewContent');
    Object.defineProperties(content, { scrollHeight: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 200 } });
    content.scrollTop = 20;
    content.dispatchEvent(new Event('scroll'));
    const latest = document.querySelector('.chat-scroll-to-end-btn');
    expect(latest.getAttribute('aria-label')).toBe('Scroll to latest message');
    expect(latest.tabIndex).toBe(0);
    latest.click();
    expect(content.scrollTop).toBe(1000);
    expect(latest.tabIndex).toBe(-1);
    expect(document.activeElement.dataset.contributionId).toBe('one');
    expect(require(base + 'apiService.js').postJson).toHaveBeenCalledTimes(1);
});

test('refresh and pagination preserve scroll position and unread navigation without bulk acknowledgement', async () => {
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce({ ...unreadResponse(), before: 'older' });
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    const content = document.getElementById('messageViewContent');
    Object.defineProperties(content, { scrollHeight: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 200 } });
    content.scrollTop = 25;
    const updated = unreadResponse();
    updated.messages.push({ ...response('two').messages[0], created_at: '2026-09-12T12:00:00Z' });
    api.postJson.mockResolvedValueOnce(updated);
    await panel.refreshOpenMessageExchange();
    expect(content.scrollTop).toBe(25);
    const older = response('old'); older.messages[0].created_at = '2026-09-10T12:00:00Z';
    api.postJson.mockResolvedValueOnce(older);
    [...content.querySelectorAll('button')].find(b => b.textContent === 'Load earlier messages').click();
    await flush();
    expect(content.scrollTop).toBe(25);
    expect(content.querySelectorAll('[data-contribution-id]')).toHaveLength(3);
    const first = content.querySelector('.is-unread');
    first.scrollIntoView = jest.fn();
    document.querySelector('.message-jump-unread').click();
    expect(first.scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
    expect(document.activeElement).toBe(first);
    expect(api.postJson.mock.calls.some(([url]) => url.includes('/read/'))).toBe(false);
});

test('the visible-read observer excludes the fixed composer overlap', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValue(unreadResponse());
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    document.getElementById('messageViewContent').getBoundingClientRect = () => ({ top: 100, bottom: 800, height: 700 });
    document.getElementById('messageComposeArea').getBoundingClientRect = () => ({ top: 650, bottom: 800, height: 150 });
    await panel.refreshOpenMessageExchange();
    expect(observers).toHaveLength(2);
    expect(global.IntersectionObserver.mock.calls[1][1].rootMargin).toBe('0px 0px -150px 0px');
});

function page(ids, before = null, unread = [], firstDay = 1) {
    return { current_user_id: '#V#alice', before, messages: ids.map((id, index) => ({
        ...response(id).messages[0], created_at: `2026-09-${String(index + firstDay).padStart(2, '0')}T12:00:00Z`,
        relationships: { '#V#has_sender': ['#V#bob'], '#V#has_recipient': ['#V#alice'] },
        concept_data: { content_fallback: id, read_by: unread.includes(id) ? [] : ['#V#alice'] }
    })) };
}

test('unloaded unread count exposes navigation that pages past loaded unread messages to the first unread', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['latest'], 'page2', [], 15))
        .mockResolvedValueOnce(page(['middle'], 'page3', [], 12))
        .mockResolvedValueOnce(page(['old-unread', 'recent-unread'], 'page4', ['old-unread', 'recent-unread'], 9))
        .mockResolvedValueOnce(page(['first-unread'], null, ['first-unread']));
    await require(base + 'components/messagePanel.js').openMessageExchange({ ...row('#V#bob'), shared_unread_count: 2 });
    const jump = document.querySelector('.message-jump-unread');
    expect(jump.hidden).toBe(false);
    expect(jump.textContent).toBe('Jump to first unread');
    jump.click(); await flush();
    expect(document.activeElement.dataset.contributionId).toBe('first-unread');
    expect(api.postJson.mock.calls.map(([, body]) => body.before)).toEqual([null, 'page2', 'page3', 'page4']);
    expect(document.querySelectorAll('.is-unread')).toHaveLength(3);
    expect(jump.disabled).toBe(false);
});

test('already loaded unread does not skip an older unread separated by a read page', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    const latest = page(['recent-unread'], 'read-gap', ['recent-unread'], 15);
    api.postJson.mockResolvedValueOnce(latest)
        .mockResolvedValueOnce(page(['read-gap'], 'oldest', [], 10))
        .mockResolvedValueOnce(page(['first-unread'], null, ['first-unread']));
    // A stale low count must not cause an early stop.
    await require(base + 'components/messagePanel.js').openMessageExchange({ ...row('#V#bob'), shared_unread_count: 1 });
    document.querySelector('.message-jump-unread').click(); await flush();
    expect(document.activeElement.dataset.contributionId).toBe('first-unread');
    expect(api.postJson.mock.calls.map(([, body]) => body.before)).toEqual([null, 'read-gap', 'oldest']);
    expect(observers[0].disconnect).toHaveBeenCalled();
    expect(observers).toHaveLength(2); // Re-arm only after the final jump.
    expect(document.querySelectorAll('.is-unread')).toHaveLength(2);
});

test('a fully loaded conversation focuses its first unread without a request', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['read', 'first', 'read-gap', 'last'], null, ['first', 'last']));
    await require(base + 'components/messagePanel.js').openMessageExchange(row('#V#bob'));
    document.querySelector('.message-jump-unread').click();
    expect(document.activeElement.dataset.contributionId).toBe('first');
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledWith({ block: 'start' });
    expect(api.postJson).toHaveBeenCalledTimes(1);
});

test('failed unread pagination retains the action for retry', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['latest'], 'older')).mockRejectedValueOnce(new Error('offline'));
    await require(base + 'components/messagePanel.js').openMessageExchange({ ...row('#V#bob'), shared_unread_count: 1 });
    const jump = document.querySelector('.message-jump-unread');
    jump.click(); await flush();
    expect(document.querySelector('.message-unread-navigation-status').textContent).toContain('Try jumping again');
    expect(jump.disabled).toBe(false);
    api.postJson.mockResolvedValueOnce(page(['unread'], null, ['unread']));
    jump.click(); await flush();
    expect(document.activeElement.dataset.contributionId).toBe('unread');
});

test('switching conversation cancels a pending unread jump without moving focus', async () => {
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['latest'], 'older'));
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange({ ...row('#V#bob'), shared_unread_count: 1 });
    let finish;
    api.postJson.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    document.querySelector('.message-jump-unread').click();
    api.postJson.mockResolvedValueOnce(page(['carol']));
    await panel.openMessageExchange(row('#V#carol'));
    finish(page(['bob-unread'], null, ['bob-unread'])); await flush();
    expect(document.querySelector('[data-contribution-id="bob-unread"]')).toBeNull();
    expect(document.querySelector('[data-contribution-id="carol"]')).not.toBeNull();
    expect(document.activeElement.dataset.contributionId).toBeUndefined();
});

test('exhausting earlier pages reports stale unread counts and hides the action', async () => {
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['latest'], 'older')).mockResolvedValueOnce(page(['old']));
    await require(base + 'components/messagePanel.js').openMessageExchange({ ...row('#V#bob'), shared_unread_count: 1 });
    document.querySelector('.message-jump-unread').click(); await flush();
    expect(document.querySelector('.message-unread-navigation-status').textContent).toContain('No unread messages remain');
    expect(document.querySelector('.message-jump-unread').hidden).toBe(true);
});

test('default loading does not navigate to unread or to the bottom', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const api = require(base + 'apiService.js');
    let finish;
    api.postJson.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const opening = require(base + 'components/messagePanel.js').openMessageExchange(row('#V#bob'));
    const content = document.getElementById('messageViewContent');
    Object.defineProperty(content, 'scrollHeight', { configurable: true, value: 1000 });
    content.scrollTop = 30;
    finish(unreadResponse());
    await opening;
    expect(content.scrollTop).toBe(30);
    expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
});

test('background unread loading preserves even a near-bottom reader and scrolling during the request', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const panel = require(base + 'components/messagePanel.js');
    await panel.openMessageExchange(row('#V#bob'));
    const content = document.getElementById('messageViewContent');
    Object.defineProperties(content, {
        scrollHeight: { configurable: true, value: 1000 },
        clientHeight: { configurable: true, value: 200 }
    });
    content.scrollTop = 790;
    let finish;
    require(base + 'apiService.js').postJson.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const refresh = panel.refreshOpenMessageExchange();
    expect(content.querySelector('[data-contribution-id="one"]')).not.toBeNull();
    expect(content.scrollTop).toBe(790);
    content.scrollTop = 450;
    const updated = unreadResponse();
    updated.messages.push({ ...response('two').messages[0], created_at: '2026-09-12T12:00:00Z' });
    finish(updated);
    await refresh;
    expect(content.scrollTop).toBe(450);
    expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
});

test('deferred unread navigation preserves the visible contribution across intermediate pages before jumping', async () => {
    HTMLElement.prototype.scrollIntoView = jest.fn();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['latest'], 'page2'));
    await require(base + 'components/messagePanel.js').openMessageExchange({ ...row('#V#bob'), shared_unread_count: 1 });
    const content = document.getElementById('messageViewContent');
    content.scrollTop = 25;
    const geometry = jest.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
        if (this.dataset.contributionId === 'latest') {
            const top = 100 + (content.querySelector('[data-contribution-id="middle"]') ? 200 : 0) - content.scrollTop;
            return { top, bottom: top + 100 };
        }
        return { top: 0, bottom: 0 };
    });
    let finishMiddle, finishUnread;
    api.postJson.mockImplementationOnce(() => new Promise(resolve => { finishMiddle = resolve; }))
        .mockImplementationOnce(() => new Promise(resolve => { finishUnread = resolve; }));
    const jump = document.querySelector('.message-jump-unread');
    jump.click();
    expect(jump.textContent).toBe('Loading unread messages…');
    expect(content.scrollTop).toBe(25);
    content.scrollTop = 40;
    finishMiddle(page(['middle'], 'page3')); await flush();
    expect(content.scrollTop).toBe(240);
    expect(content.querySelector('[data-contribution-id="latest"]').getBoundingClientRect().top).toBe(60);
    expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
    finishUnread(page(['unread'], null, ['unread'])); await flush();
    expect(HTMLElement.prototype.scrollIntoView).toHaveBeenCalledTimes(1);
    expect(document.activeElement.dataset.contributionId).toBe('unread');
    geometry.mockRestore();
});

test('confirmed reads hide navigation at zero even with older history and ignore stale catalogue replies', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['first', 'last'], 'older', ['first', 'last']));
    const panel = require(base + 'components/messagePanel.js');
    const exchange = { ...row('#V#bob'), shared_unread_count: 2 };
    await panel.openMessageExchange(exchange);
    const staleRefresh = panel.captureMessageExchangeUnreadRefresh();
    const jump = document.querySelector('.message-jump-unread');
    const entry = visibleEntry();
    api.postJson.mockResolvedValue({ success: true, updated_count: 1 });
    observers[0].callback([entry]); await flush();
    expect(jump.hidden).toBe(false);
    observers[0].callback([{ ...entry, target: document.querySelector('[data-contribution-id="last"]') }]); await flush();
    expect(document.querySelector('.is-unread')).toBeNull();
    expect(jump.hidden).toBe(true);
    staleRefresh([exchange]);
    expect(jump.hidden).toBe(true);
    api.postJson.mockResolvedValueOnce(page(['carol'], 'older', ['carol']));
    await panel.openMessageExchange({ ...row('#V#carol'), shared_unread_count: 1 });
    expect(jump.hidden).toBe(false);
    api.postJson.mockResolvedValueOnce(page(['first', 'last'], 'older'));
    await panel.openMessageExchange({ ...exchange, shared_unread_count: 0 });
    expect(jump.hidden).toBe(true);
});

test('catalogue read-back reconciles unread elsewhere without treating omitted rows as zero', async () => {
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValue(page(['latest'], 'older'));
    const panel = require(base + 'components/messagePanel.js');
    const exchange = { ...row('#V#bob'), shared_unread_count: 1 };
    await panel.openMessageExchange(exchange);
    const reconcile = panel.captureMessageExchangeUnreadRefresh();
    const jump = document.querySelector('.message-jump-unread');
    reconcile([]);
    expect(jump.hidden).toBe(false);
    reconcile([{ ...exchange, shared_unread_count: 0 }]);
    expect(jump.hidden).toBe(true);
    await panel.openMessageExchange({ ...row('#V#carol'), shared_unread_count: 1 });
    reconcile([{ ...exchange, shared_unread_count: 0 }]);
    expect(jump.hidden).toBe(false);
});

test('a catalogue count during an in-flight read is not decremented twice', async () => {
    const observers = mockReadObserver();
    const api = require(base + 'apiService.js');
    api.postJson.mockResolvedValueOnce(page(['last-loaded'], 'older', ['last-loaded']));
    const panel = require(base + 'components/messagePanel.js');
    const exchange = { ...row('#V#bob'), shared_unread_count: 2 };
    await panel.openMessageExchange(exchange);
    const refreshBeforeRead = panel.captureMessageExchangeUnreadRefresh();
    let finish;
    api.postJson.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    observers[0].callback([visibleEntry()]);
    const refreshDuringRead = panel.captureMessageExchangeUnreadRefresh();
    refreshBeforeRead([{ ...exchange, shared_unread_count: 1 }]);
    finish({ success: true, updated_count: 1 }); await flush();
    refreshDuringRead([{ ...exchange, shared_unread_count: 1 }]);
    expect(document.querySelector('.is-unread')).toBeNull();
    expect(document.querySelector('.message-jump-unread').hidden).toBe(false);
});
