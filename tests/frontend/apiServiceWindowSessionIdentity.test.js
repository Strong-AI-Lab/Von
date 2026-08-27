/** @jest-environment jsdom */

const apiServicePath = '../../src/frontend/web/von_interface/static/js/apiService.js';

function createBroadcastChannelHarness() {
    const channelsByName = new Map();
    return class FakeBroadcastChannel {
        constructor(name) {
            this.name = name;
            this.listeners = new Set();
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
            queueMicrotask(() => {
                for (const peer of channelsByName.get(this.name) || []) {
                    if (peer === this) continue;
                    for (const listener of peer.listeners) listener({ data });
                }
            });
        }

        close() {
            channelsByName.get(this.name)?.delete(this);
        }
    };
}

describe('apiService window-session request barrier', () => {
    beforeEach(() => {
        jest.resetModules();
        localStorage.clear();
        sessionStorage.clear();
    });

    afterEach(() => {
        delete global.BroadcastChannel;
        delete window.BroadcastChannel;
        delete global.fetch;
    });

    test('does not fetch until a copied tab ID rotates, then sends only the replacement header', async () => {
        const FakeBroadcastChannel = createBroadcastChannelHarness();
        global.BroadcastChannel = FakeBroadcastChannel;
        window.BroadcastChannel = FakeBroadcastChannel;
        sessionStorage.setItem('von_window_session_id', 'ws_copied');

        const incumbent = new FakeBroadcastChannel('von_window_session_coordination_v1');
        incumbent.addEventListener('message', ({ data }) => {
            if (data?.type !== 'probe' || data.window_session_id !== 'ws_copied') return;
            incumbent.postMessage({
                protocol: 'von_window_session_coordination.v1',
                type: 'occupied',
                window_session_id: 'ws_copied',
                source_document_id: 'document-incumbent',
                source_priority: '0:document-incumbent',
                target_document_id: data.source_document_id,
                probe_id: data.probe_id,
            });
        });
        global.fetch = jest.fn(async () => ({
            ok: true,
            json: async () => ({ success: true }),
        }));

        const { getJson } = require(apiServicePath);
        const request = getJson('/actor-scoped');

        expect(global.fetch).not.toHaveBeenCalled();
        await expect(request).resolves.toEqual({ success: true });

        expect(global.fetch).toHaveBeenCalledTimes(1);
        const [, options] = global.fetch.mock.calls[0];
        expect(options.headers['X-Von-Window-Session']).toMatch(/^ws_/);
        expect(options.headers['X-Von-Window-Session']).not.toBe('ws_copied');
        expect(sessionStorage.getItem('von_window_session_id'))
            .toBe(options.headers['X-Von-Window-Session']);

        incumbent.close();
    });
});
