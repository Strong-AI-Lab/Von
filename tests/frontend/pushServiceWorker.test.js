/** @jest-environment node */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

function worker() {
    const handlers = {};
    const data = new Map();
    const cache = { match: async key => data.get(key), put: async (key, value) => data.set(key, value) };
    const shown = [];
    const opened = [];
    const self = {
        location: { origin: 'https://von.example' },
        addEventListener: (type, handler) => { handlers[type] = handler; },
        registration: {
            showNotification: async (title, options) => shown.push({ title, ...options }),
            getNotifications: async () => [], pushManager: { getSubscription: async () => null }
        },
        clients: { openWindow: async url => opened.push(url) }
    };
    const fetch = jest.fn(async url => ({ ok: true, json: async () =>
        url.endsWith('/confirm') ? { state: 'active' } : { active: true } }));
    // A fresh Response is returned for every cache read, like real CacheStorage.
    cache.match = async key => data.get(key)?.clone();
    const context = vm.createContext({ self, fetch, URL, Response,
        caches: { open: async () => cache, delete: async () => data.clear() } });
    vm.runInContext(fs.readFileSync(path.join(__dirname,
        '../../src/frontend/web/von_interface/static/service-worker.js'), 'utf8'), context);
    async function event(type, props) {
        let pending;
        handlers[type]({ ...props, waitUntil: value => { pending = value; } });
        await pending;
    }
    const push = payload => event('push', { data: { json: () => ({ generation: 'c'.repeat(32), ...payload }) } });
    return { push, event, shown, opened, fetch };
}

const id = 'a'.repeat(64), receipt = 'b'.repeat(64);

test('real worker handler uses private text, deduplicates receipt and confines click navigation', async () => {
    const w = worker();
    await w.push({ kind: 'confirmation', id, challenge: 'opaque' });
    await w.push({ kind: 'message', id, receipt, title: 'PRIVATE', url: 'https://evil.test' });
    await w.push({ kind: 'message', id, receipt });
    expect(w.shown).toHaveLength(2);
    expect(w.shown[1].body).toBe('You have a new message. Open Von to read it.');
    await w.event('notificationclick', { notification: { ...w.shown[1], close() {} } });
    expect(w.opened).toEqual([`https://von.example/von/?notification=${receipt}`]);
    await w.event('notificationclick', { notification: { data: { url: 'https://evil.test' }, close() {} } });
    expect(w.opened[1]).toBe('https://von.example/von/');
});

test('revoked account/device and login redirects cannot display queued alerts', async () => {
    const w = worker();
    await w.push({ kind: 'confirmation', id, challenge: 'opaque' });
    w.fetch.mockResolvedValue({ ok: false, status: 403 });
    await w.push({ kind: 'message', id, receipt });
    expect(w.shown).toHaveLength(1);
    w.fetch.mockResolvedValue({ ok: true, json: async () => ({}) });
    await w.push({ kind: 'confirmation', id, challenge: 'opaque' });
    await w.push({ kind: 'message', id, receipt });
    expect(w.shown).toHaveLength(1);
});

test('disable clears the binding and stops subsequently queued alerts', async () => {
    const w = worker();
    await w.push({ kind: 'confirmation', id, challenge: 'opaque' });
    await w.event('message', { source: { url: 'https://von.example/von/' },
        data: { type: 'von-push-disable' }, ports: [] });
    await w.push({ kind: 'message', id, receipt });
    expect(w.shown).toHaveLength(1);
});

test('queued push from a previous account enrolment cannot match a reused endpoint', async () => {
    const w = worker();
    await w.push({ kind: 'confirmation', id, challenge: 'opaque', generation: 'd'.repeat(32) });
    await w.push({ kind: 'message', id, receipt, generation: 'c'.repeat(32) });
    expect(w.shown).toHaveLength(1);
});
