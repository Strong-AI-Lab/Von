import {
    createWindowSessionIdentityCoordinator,
    WINDOW_SESSION_KEY,
} from '../utils/windowSessionIdentity.js';

class MemoryStorage {
    constructor(seed = {}) {
        this.values = new Map(Object.entries(seed));
    }

    getItem(key) {
        return this.values.has(key) ? this.values.get(key) : null;
    }

    setItem(key, value) {
        this.values.set(key, String(value));
    }
}

function createBroadcastChannelHarness({ delayForMessage = () => 0 } = {}) {
    const channelsByName = new Map();

    return class FakeBroadcastChannel {
        constructor(name) {
            this.name = name;
            this.listeners = new Set();
            this.closed = false;
            const channels = channelsByName.get(name) || new Set();
            channels.add(this);
            channelsByName.set(name, channels);
        }

        addEventListener(type, listener) {
            if (type === 'message') this.listeners.add(listener);
        }

        removeEventListener(type, listener) {
            if (type === 'message') this.listeners.delete(listener);
        }

        postMessage(data) {
            const delayMs = delayForMessage(data);
            const deliver = () => {
                for (const peer of channelsByName.get(this.name) || []) {
                    if (peer === this || peer.closed) continue;
                    for (const listener of peer.listeners) {
                        listener({ data });
                    }
                }
            };
            if (delayMs > 0) {
                setTimeout(deliver, delayMs);
            } else {
                queueMicrotask(deliver);
            }
        }

        close() {
            this.closed = true;
            channelsByName.get(this.name)?.delete(this);
        }
    };
}

function createDocumentIdFactory(prefix) {
    let counter = 0;
    return () => `${prefix}-${counter += 1}`;
}

function createCoordinator({
    BroadcastChannelImpl,
    storage,
    priority,
    replacementId,
    collisionProbeMs = 5,
    eventTarget = null,
    isTopLevelDocument = true,
}) {
    return createWindowSessionIdentityCoordinator({
        BroadcastChannelImpl,
        storage,
        documentPriority: priority,
        createDocumentInstanceId: createDocumentIdFactory(`document-${priority}`),
        createWindowSessionId: jest.fn(() => replacementId),
        collisionProbeMs,
        eventTarget,
        isTopLevelDocument,
    });
}

describe('window session identity coordination', () => {
    test('rotates only a copied claimant while the established owner retains its ID', async () => {
        const BroadcastChannelImpl = createBroadcastChannelHarness();
        const ownerStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_copied' });
        const owner = createCoordinator({
            BroadcastChannelImpl,
            storage: ownerStorage,
            priority: '001',
            replacementId: 'ws_owner_unused',
        });
        await expect(owner.ensureUniqueWindowSessionId()).resolves.toBe('ws_copied');

        const claimantStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_copied' });
        const claimant = createCoordinator({
            BroadcastChannelImpl,
            storage: claimantStorage,
            priority: '002',
            replacementId: 'ws_claimant_replacement',
        });

        await expect(claimant.ensureUniqueWindowSessionId()).resolves.toBe('ws_claimant_replacement');
        expect(ownerStorage.getItem(WINDOW_SESSION_KEY)).toBe('ws_copied');
        expect(claimantStorage.getItem(WINDOW_SESSION_KEY)).toBe('ws_claimant_replacement');

        owner.close();
        claimant.close();
    });

    test('uses probe priority so simultaneous claimants do not both rotate', async () => {
        const BroadcastChannelImpl = createBroadcastChannelHarness();
        const earlierStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_shared' });
        const laterStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_shared' });
        const earlier = createCoordinator({
            BroadcastChannelImpl,
            storage: earlierStorage,
            priority: '001',
            replacementId: 'ws_earlier_unused',
        });
        const later = createCoordinator({
            BroadcastChannelImpl,
            storage: laterStorage,
            priority: '002',
            replacementId: 'ws_later_replacement',
        });

        await expect(Promise.all([
            earlier.ensureUniqueWindowSessionId(),
            later.ensureUniqueWindowSessionId(),
        ])).resolves.toEqual(['ws_shared', 'ws_later_replacement']);
        expect(earlierStorage.getItem(WINDOW_SESSION_KEY)).toBe('ws_shared');
        expect(laterStorage.getItem(WINDOW_SESSION_KEY)).toBe('ws_later_replacement');

        earlier.close();
        later.close();
    });

    test('preserves a reload ID when the retiring document has released its channel', async () => {
        const BroadcastChannelImpl = createBroadcastChannelHarness();
        const storage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_reload' });
        const beforeReload = createCoordinator({
            BroadcastChannelImpl,
            storage,
            priority: '001',
            replacementId: 'ws_before_reload_unused',
        });
        await beforeReload.ensureUniqueWindowSessionId();
        beforeReload.close();

        const afterReload = createCoordinator({
            BroadcastChannelImpl,
            storage,
            priority: '002',
            replacementId: 'ws_after_reload_unused',
        });

        await expect(afterReload.ensureUniqueWindowSessionId()).resolves.toBe('ws_reload');
        expect(storage.getItem(WINDOW_SESSION_KEY)).toBe('ws_reload');
        afterReload.close();
    });

    test('does not let a same-origin iframe claim its top-level tab ID', async () => {
        let constructedChannels = 0;
        const BaseChannel = createBroadcastChannelHarness();
        class CountingChannel extends BaseChannel {
            constructor(name) {
                super(name);
                constructedChannels += 1;
            }
        }
        const storage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_parent' });
        const iframe = createCoordinator({
            BroadcastChannelImpl: CountingChannel,
            storage,
            priority: '002',
            replacementId: 'ws_iframe_must_not_use',
            isTopLevelDocument: false,
        });

        await expect(iframe.ensureUniqueWindowSessionId()).resolves.toBe('ws_parent');
        expect(constructedChannels).toBe(0);
        expect(storage.getItem(WINDOW_SESSION_KEY)).toBe('ws_parent');
    });

    test('rotates and notifies after a delayed incumbent response', async () => {
        const BroadcastChannelImpl = createBroadcastChannelHarness({
            delayForMessage: (message) => message?.type === 'occupied' ? 20 : 0,
        });
        const ownerStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_delayed' });
        const owner = createCoordinator({
            BroadcastChannelImpl,
            storage: ownerStorage,
            priority: '001',
            replacementId: 'ws_owner_unused',
        });
        await owner.ensureUniqueWindowSessionId();

        const eventTarget = {
            CustomEvent,
            addEventListener: jest.fn(),
            dispatchEvent: jest.fn(),
        };
        const claimantStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_delayed' });
        const claimant = createCoordinator({
            BroadcastChannelImpl,
            storage: claimantStorage,
            priority: '002',
            replacementId: 'ws_late_replacement',
            eventTarget,
        });

        await expect(claimant.ensureUniqueWindowSessionId()).resolves.toBe('ws_delayed');
        await new Promise((resolve) => setTimeout(resolve, 60));

        expect(claimantStorage.getItem(WINDOW_SESSION_KEY)).toBe('ws_late_replacement');
        expect(eventTarget.dispatchEvent).toHaveBeenCalledWith(expect.objectContaining({
            type: 'von:windowSessionIdentityChanged',
        }));

        owner.close();
        claimant.close();
    });

    test('cancels a hidden-page probe and notifies when BFCache restore must rotate', async () => {
        const BroadcastChannelImpl = createBroadcastChannelHarness();
        const eventListeners = new Map();
        const eventTarget = {
            CustomEvent,
            addEventListener: jest.fn((type, listener) => {
                eventListeners.set(type, listener);
            }),
            dispatchEvent: jest.fn(),
        };
        const restoredStorage = new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_bfcache' });
        const restored = createCoordinator({
            BroadcastChannelImpl,
            storage: restoredStorage,
            priority: '002',
            replacementId: 'ws_bfcache_replacement',
            collisionProbeMs: 20,
            eventTarget,
        });

        const hiddenProbe = restored.ensureUniqueWindowSessionId();
        eventListeners.get('pagehide')?.({});
        await expect(hiddenProbe).resolves.toBe('ws_bfcache');

        const owner = createCoordinator({
            BroadcastChannelImpl,
            storage: new MemoryStorage({ [WINDOW_SESSION_KEY]: 'ws_bfcache' }),
            priority: '001',
            replacementId: 'ws_owner_unused',
            collisionProbeMs: 20,
        });
        await owner.ensureUniqueWindowSessionId();

        eventListeners.get('pageshow')?.({ persisted: true });
        await new Promise((resolve) => setTimeout(resolve, 70));

        expect(restoredStorage.getItem(WINDOW_SESSION_KEY)).toBe('ws_bfcache_replacement');
        expect(eventTarget.dispatchEvent).toHaveBeenCalledWith(expect.objectContaining({
            type: 'von:windowSessionIdentityChanged',
            detail: {
                previous_window_session_id: 'ws_bfcache',
                window_session_id: 'ws_bfcache_replacement',
            },
        }));

        owner.close();
        restored.close();
    });
});
