/* Push only: do not cache authenticated pages or intercept application fetches. */
const STATE_CACHE = 'von-push-state-v1';
const STATE_URL = new URL('/von/.push-binding', self.location.origin).href;

async function readBinding() {
    const cache = await caches.open(STATE_CACHE);
    const entry = await cache.match(STATE_URL);
    return entry ? entry.json() : null;
}

async function saveBinding(value) {
    const cache = await caches.open(STATE_CACHE);
    await cache.put(STATE_URL, new Response(JSON.stringify(value)));
}

async function disable() {
    await caches.delete(STATE_CACHE);
    for (const notification of await self.registration.getNotifications()) notification.close();
    const subscription = await self.registration.pushManager.getSubscription();
    if (subscription) await subscription.unsubscribe();
}

self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('message', event => {
    // Commands are local lifecycle actions from our own app windows.
    if (!event.source?.url || new URL(event.source.url).origin !== self.location.origin) return;
    if (event.data?.type === 'von-push-disable') {
        event.waitUntil(disable().then(() => event.ports[0]?.postMessage({ ok: true })));
    }
});

self.addEventListener('push', event => {
    event.waitUntil((async () => {
        let payload;
        try { payload = event.data.json(); } catch { return; }
        if (!/^[a-f0-9]{64}$/.test(payload.id || '')) return;
        if (!/^[a-f0-9]{32}$/.test(payload.generation || '')) return;
        if (payload.kind === 'confirmation') {
            // Only a push arriving in the requesting authenticated browser can
            // complete this challenge. It is never returned to the subscriber API.
            const response = await fetch('/api/notifications/confirm', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id: payload.id, challenge: payload.challenge })
            });
            if (!response.ok || (await response.json()).state !== 'active') return;
            await saveBinding({ id: payload.id, generation: payload.generation, seen: [] });
            await self.registration.showNotification('Von notifications enabled', {
                body: 'Direct messages can now notify you on this device.',
                tag: 'von-push-confirmation', data: { url: '/von/' }
            });
            return;
        }
        const binding = await readBinding();
        if (!binding || binding.id !== payload.id || binding.generation !== payload.generation) return;
        // Recheck the live browser actor/device before displaying even generic
        // text. A queued push must not outlive logout or organisation removal.
        const active = await fetch('/api/notifications/validate', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: payload.id, generation: payload.generation })
        });
        if (!active.ok) {
            if (active.status === 401 || active.status === 403) await disable();
            return;
        }
        if ((await active.json()).active !== true) return;
        const receipt = /^[a-f0-9]{64}$/.test(payload.receipt || '') ? payload.receipt : null;
        if (payload.kind !== 'test' && (payload.kind !== 'message' || !receipt)) return;
        if (receipt && binding.seen.includes(receipt)) return;
        await self.registration.showNotification('Von', {
            body: payload.kind === 'test' ? 'This is your test notification.' : 'You have a new message. Open Von to read it.',
            icon: '/static/pwa-icon-192.png',
            tag: receipt ? `von-${receipt}` : 'von-push-test', renotify: false,
            data: { url: receipt ? `/von/?notification=${receipt}` : '/von/' }
        });
        if (receipt) {
            binding.seen = [...binding.seen.slice(-127), receipt];
            await saveBinding(binding);
        }
    })());
});

self.addEventListener('notificationclick', event => {
    event.notification.close();
    // Only the canonical opaque receipt route is allowed, never a payload URL.
    const raw = event.notification.data?.url || '';
    const url = /^\/von\/\?notification=[a-f0-9]{64}$/.test(raw) ? raw : '/von/';
    event.waitUntil(self.clients.openWindow(new URL(url, self.location.origin).href));
});

self.addEventListener('pushsubscriptionchange', event => {
    // No background re-enrolment with an expired login or changed actor. The
    // next foreground visit shows the missing subscription and offers Enable.
    event.waitUntil(disable());
});
