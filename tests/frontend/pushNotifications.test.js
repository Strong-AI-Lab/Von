/** @jest-environment jsdom */
jest.mock('../../src/frontend/web/von_interface/static/js/apiService.js', () => ({
    getJsonDetailed: jest.fn(), postJson: jest.fn()
}));
jest.mock('../../src/frontend/web/von_interface/static/js/components/conversationCatalogue.js', () => ({ selectMessageConversation: jest.fn() }));
jest.mock('../../src/frontend/web/von_interface/static/js/tabNavigation.js', () => ({ activateTab: jest.fn() }));
const base = '../../src/frontend/web/von_interface/static/js/';
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

beforeEach(() => {
    jest.resetModules();
    document.body.innerHTML = '';
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
    Object.defineProperty(navigator, 'userAgent', { value: 'Chromium Android', configurable: true });
    Object.defineProperty(navigator, 'platform', { value: 'Linux', configurable: true });
    window.matchMedia = jest.fn(() => ({ matches: false }));
    window.PushManager = function () {};
    global.caches = { match: jest.fn(async () => null) };
    window.Notification = { permission: 'default', requestPermission: jest.fn(async () => 'default') };
    const reg = { pushManager: { getSubscription: jest.fn(async () => null), subscribe: jest.fn() } };
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
        register: jest.fn(async () => reg), ready: Promise.resolve(reg), getRegistration: jest.fn(async () => reg)
    } });
    const api = require(base + 'apiService.js');
    api.getJsonDetailed.mockResolvedValue({ data: { configured: true, state: 'disabled', public_key: 'BAAA' }, status: 200 });
});

function controls() {
    document.body.innerHTML = `<section id="pushNotificationSettings">
        <p data-push-status></p><button data-push-enable></button><button data-push-disable></button>
        <button data-push-test></button><button data-push-refresh></button></section>`;
    return require(base + 'pushNotifications.js');
}

test('no permission request on initialisation; a dismissed user gesture leaves notifications off', async () => {
    const module = controls();
    await module.initialiseNotificationControls(); await flush();
    expect(Notification.requestPermission).not.toHaveBeenCalled();
    document.querySelector('[data-push-enable]').click();
    expect(Notification.requestPermission).toHaveBeenCalledTimes(1);
    await flush();
    expect(document.querySelector('[data-push-status]').textContent).toContain('dismissed');
    expect(require(base + 'apiService.js').postJson).not.toHaveBeenCalled();
});

test('denied permission offers browser settings without prompting again', async () => {
    Notification.permission = 'denied';
    const module = controls();
    await module.initialiseNotificationControls(); await flush();
    expect(document.querySelector('[data-push-enable]').disabled).toBe(true);
    expect(document.querySelector('[data-push-status]').textContent).toContain('blocked');
    expect(Notification.requestPermission).not.toHaveBeenCalled();
});

test('ordinary iPhone tab directs installation; installed app uses feature detection', () => {
    Object.defineProperty(navigator, 'userAgent', { value: 'iPhone', configurable: true });
    const module = require(base + 'pushNotifications.js');
    expect(module.notificationSupport()).toContain('Home Screen');
    window.matchMedia = () => ({ matches: true });
    expect(module.notificationSupport()).toBeNull();
    delete window.PushManager;
    expect(module.notificationSupport()).toContain('does not support');
});

test('disable does not claim success or discard browser subscription if server revocation fails', async () => {
    const { postJson } = require(base + 'apiService.js');
    postJson.mockRejectedValue(new Error('offline'));
    const module = require(base + 'pushNotifications.js');
    await expect(module.disableBrowserNotifications()).rejects.toThrow('offline');
    expect(navigator.serviceWorker.getRegistration).not.toHaveBeenCalled();
});

test('lost worker binding is shown as needing reconnection, not falsely enabled', async () => {
    Notification.permission = 'granted';
    const reg = await navigator.serviceWorker.ready;
    reg.pushManager.getSubscription.mockResolvedValue({ toJSON: () => ({}) });
    require(base + 'apiService.js').getJsonDetailed.mockResolvedValue({ data: {
        configured: true, state: 'active', subscription_id: 'id', generation: 'generation'
    } });
    const module = controls();
    await module.initialiseNotificationControls(); await flush();
    expect(document.querySelector('[data-push-status]').textContent).toContain('lost its notification binding');
    expect(document.querySelector('[data-push-test]').disabled).toBe(true);
    expect(document.querySelector('[data-push-disable]').disabled).toBe(false);
});

test('notification navigation opens the access-checked exchange from the API response', async () => {
    const receipt = 'a'.repeat(64);
    window.history.replaceState({}, '', `/?notification=${receipt}`);
    const row = { session_id: 'messages:exact', viewer_id: '#V#alice', participant_ids: ['#V#alice', '#V#codex'] };
    require(base + 'apiService.js').getJsonDetailed.mockResolvedValue({ data: row, status: 200 });
    const module = require(base + 'pushNotifications.js');
    await module.openNotificationMessage();
    expect(require(base + 'components/conversationCatalogue.js').selectMessageConversation).toHaveBeenCalledWith(row);
    window.history.replaceState({}, '', '/');
});
