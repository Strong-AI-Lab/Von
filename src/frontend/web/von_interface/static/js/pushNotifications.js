import { getJsonDetailed, postJson } from './apiService.js';

const API = '/api/notifications';

export function notificationSupport() {
    if (!window.isSecureContext) return 'Notifications require a secure HTTPS connection.';
    const isAppleMobile = /iPad|iPhone|iPod/.test(navigator.userAgent)
        || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    if (isAppleMobile && !window.matchMedia('(display-mode: standalone)').matches && !navigator.standalone) {
        return 'On iPhone or iPad, add Von to the Home Screen, then open that app to enable notifications.';
    }
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
        return 'This browser does not support Von push notifications. Try a current supported browser.';
    }
    return null;
}

async function registration() {
    await navigator.serviceWorker.register('/von/service-worker.js', { scope: '/von/' });
    return navigator.serviceWorker.ready;
}

export async function disableBrowserNotifications() {
    // Server revocation first; report failures instead of presenting a false opt-out.
    await postJson(`${API}/disable`, {});
    await clearLocalNotifications();
}

export async function clearLocalNotifications() {
    if (!('serviceWorker' in navigator)) return;
    const reg = await navigator.serviceWorker.getRegistration('/von/');
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
    for (const notification of await reg.getNotifications()) notification.close();
    reg.active?.postMessage({ type: 'von-push-disable' });
}

export async function initialiseNotificationControls() {
    const root = document.getElementById('pushNotificationSettings');
    if (!root || root.dataset.initialised) return;
    root.dataset.initialised = 'true';
    const status = root.querySelector('[data-push-status]');
    const enable = root.querySelector('[data-push-enable]');
    const disable = root.querySelector('[data-push-disable]');
    const test = root.querySelector('[data-push-test]');
    const refresh = root.querySelector('[data-push-refresh]');
    let server = null;
    let reg = null;
    let busy = false;

    async function update() {
        enable.disabled = disable.disabled = test.disabled = true;
        const unsupported = notificationSupport();
        if (unsupported) { status.textContent = unsupported; return; }
        try {
            ({ data: server } = await getJsonDetailed(`${API}/status`));
            if (!server.configured) {
                status.textContent = 'Notifications are not configured on this server yet.';
                return;
            }
            reg = await registration();
            const sub = await reg.pushManager.getSubscription();
            const permission = Notification.permission;
            const cached = await caches.match('/von/.push-binding', { cacheName: 'von-push-state-v1' });
            const binding = cached ? await cached.json() : null;
            const needsReconnect = server.state === 'active' && !!sub
                && (binding?.id !== server.subscription_id || binding?.generation !== server.generation);
            const active = permission === 'granted' && !!sub && server.state === 'active' && !needsReconnect;
            status.textContent = permission === 'denied'
                ? 'Notifications are blocked in browser or system settings. Change that permission there, then refresh.'
                : needsReconnect ? 'This device lost its notification binding. Disable on this device, then Enable again to reconnect.'
                : active ? 'Notifications enabled for direct messages in this organisation context on this device. Lock-screen text stays private.'
                    : server.state === 'pending' ? 'Waiting for the confirmation push. Refresh status after it arrives; if it fails, retry Enable after one minute.'
                        : 'Notifications are off. Enable them for direct messages in the current organisation context.';
            enable.disabled = active || needsReconnect || permission === 'denied';
            disable.disabled = !sub && server.state === 'disabled';
            test.disabled = !active;
            if (permission !== 'granted' && server.state === 'active') {
                await disableBrowserNotifications();
            }
            if (active) {
                // Renew an existing, confirmed opt-in while this actor is present.
                // Permission is never requested as part of refresh.
                await postJson(`${API}/subscribe`, { subscription: sub.toJSON() });
            }
        } catch (error) {
            status.textContent = `Notification status unavailable. ${error.message}`;
        }
    }

    async function action(fn) {
        if (busy) return;
        busy = true;
        enable.disabled = disable.disabled = test.disabled = refresh.disabled = true;
        try { await fn(); } catch (error) { status.textContent = error.message; }
        finally { busy = false; refresh.disabled = false; }
    }

    enable.addEventListener('click', () => {
        // Invoke before any await: iOS requires direct user activation.
        const permission = Notification.requestPermission();
        void action(async () => {
            const result = await permission;
            if (result !== 'granted') {
                await update();
                if (result === 'default') status.textContent = 'Permission was dismissed. Notifications remain off; you can enable them later.';
                return;
            }
            let sub = await reg.pushManager.getSubscription();
            if (!sub) {
                const padded = server.public_key.replace(/-/g, '+').replace(/_/g, '/');
                const key = Uint8Array.from(atob(padded), character => character.charCodeAt(0));
                sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
            }
            await postJson(`${API}/subscribe`, { subscription: sub.toJSON() });
            await update();
        });
    });
    disable.addEventListener('click', () => void action(async () => {
        await disableBrowserNotifications(); await update();
    }));
    test.addEventListener('click', () => void action(async () => {
        const result = await postJson(`${API}/test`, { id: server.subscription_id });
        await update();
        status.textContent = result.provider_result === 'accepted'
            ? 'The push provider accepted the test. Check your device for the notification; acceptance does not prove display.'
            : 'The test was not accepted. Refresh status and try enabling notifications again.';
    }));
    refresh.addEventListener('click', () => void update());
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && !busy) void update();
    });
    await update();
}

export async function openNotificationMessage() {
    const receipt = new URL(window.location.href).searchParams.get('notification');
    if (!receipt || !/^[a-f0-9]{64}$/.test(receipt)) return;
    try {
        const { data: row } = await getJsonDetailed(`${API}/open/${receipt}`);
        const { selectMessageConversation } = await import('./components/conversationCatalogue.js');
        const { activateTab } = await import('./tabNavigation.js');
        activateTab('chatTab');
        await selectMessageConversation(row);
    } catch (error) {
        const message = document.createElement('p');
        message.setAttribute('role', 'status');
        message.textContent = `This notification cannot be opened in the current account. ${error.message}`;
        document.getElementById('vonAuthenticatedApp')?.prepend(message);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => void initialiseNotificationControls());
} else {
    void initialiseNotificationControls();
}
